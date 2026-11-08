#!/usr/bin/env python
"""契约一致性检查：``docs/API_CONTRACT.md`` ⇄ 运行时路由表。

为什么需要它
------------
前后端并行开发时最容易出现的故障是「文档写了、代码没写」和「代码写了、
文档没写」。前者让前端接了个 404，后者让文档失去可信度。本工具把这两个
方向都变成可复现的检查：

    文档有代码无  → 前端会踩空，**必须修**
    代码有文档无  → 文档失真，**必须补**
    方法不一致    → 同名路径少实现了方法（默认只警告，``--strict`` 才失败）

数据来源
--------
* 文档侧：解析 markdown 表格里的「方法 + `/路径`」两列（纯文本解析，不联网）。
* 代码侧：真的在进程内起一个 ``SparkServer``（随机端口），
  请求 ``GET /api/v1/_routes`` 拿实际注册表——比读 ``all_routes()`` 更接近
  真实运行状态（能顺带暴露「模块导入失败导致路由少一半」的幽灵故障）。

用法
----
    python tools/check_contract.py              # 人类可读报告
    python tools/check_contract.py --json       # 机器可读（selftest 汇总用）
    python tools/check_contract.py --strict     # 方法差异也视为失败
    python tools/check_contract.py --profile lite   # lite 版：/pay/* 缺失属正常

退出码：0 一致 · 1 有差异 · 2 运行时不可用（代码导入/启动失败）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent


def _detect_layout(start: Path) -> tuple[Path, Path]:
    """返回 ``(项目根, 后端目录)``，兼容开发树（root/backend/app）与交付树（root/app）。"""
    for root in (start, start.parent, start.parent.parent):
        if (root / "app" / "config.py").is_file():
            return root, root
        if (root / "backend" / "app" / "config.py").is_file():
            return root, root / "backend"
    return start.parent, start.parent / "backend"


PROJECT_ROOT, BACKEND_DIR = _detect_layout(TOOLS_DIR)
DOC_PATH = PROJECT_ROOT / "docs" / "API_CONTRACT.md"
# 系统 TEMP 在沙箱里不可写，临时数据统一放 backend/data/tmp/（运行产物，已 gitignore）
TMP_ROOT = BACKEND_DIR / "data" / "tmp"

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_METHOD_ROW_RE = re.compile(r"^(?:" + "|".join(METHODS) + r")(?:\s*/\s*(?:" + "|".join(METHODS) + r"))*$")
_SECTION_RE = re.compile(r"^##\s+(\d+)\.")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PARAM_RE = re.compile(r"<[^>]+>")
_OPTIONAL_SUFFIX_RE = re.compile(r"\[([^\]]+)\]$")

# 契约原文里「以运行时的 /api/v1/_routes 为准」的元接口：它们属于基础设施，
# 不属于业务接口清单，因此允许不出现在文档的表格里（但报告里仍会列出来，不藏）。
KNOWN_EXTRA: dict[str, str] = {
    "/api/v1/_routes": "契约自检依赖的元接口（契约 §0 明确以它为准）",
    "/api/v1/version": "元接口：版本信息（前端启动探测）",
    "/api/v1/ping": "元接口：无数据库连通性探测",
    "/api/v1/runtime": "元接口：管理员运行指标",
}
# lite 版（不含支付模块）按设计缺失的路径前缀
LITE_ABSENT_PREFIXES = ("/api/v1/pay/",)


class RuntimeUnavailable(RuntimeError):
    """运行时代码无法加载/启动——这不是契约差异，是代码本身坏了。"""


# --------------------------------------------------------------------------
# 文档侧解析
# --------------------------------------------------------------------------
@dataclass
class DocRoutes:
    """从 API_CONTRACT.md 抽出的接口集合。"""

    methods: dict[str, set[str]] = field(default_factory=dict)   # 规范化路径 → 方法集合
    where: dict[str, str] = field(default_factory=dict)          # 路径 → 出处（章节/行号）
    prefixes: dict[str, str] = field(default_factory=dict)       # 前缀 → 出处（`/install/*` 这类写法）
    skipped: list[str] = field(default_factory=list)             # 明确跳过的非 API 路径

    def pairs(self) -> set[tuple[str, str]]:
        return {(method, path) for path, methods in self.methods.items() for method in methods}


def normalize_path(raw: str) -> str:
    """统一路径写法：去空白/尾斜杠，把 ``<user_id>`` 归一成 ``<param>``。

    参数名在文档与代码里不必一致（``<id>`` vs ``<level_id>``），
    但「哪个位置有个参数」必须一致，所以只归一不丢弃。
    """
    path = (raw or "").strip().strip("`").strip()
    path = path.split("?")[0].split("#")[0].strip()
    if not path.startswith("/"):
        path = "/" + path
    path = _PARAM_RE.sub("<param>", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def _expand_optional_suffix(path: str) -> list[str]:
    """展开文档里的简写 ``/admin/agent-levels[/<id>]`` → 两个路径。"""
    match = _OPTIONAL_SUFFIX_RE.search(path)
    if not match:
        return [normalize_path(path)]
    base = path[: match.start()].rstrip("/")
    suffix = match.group(1)
    return [normalize_path(base), normalize_path(base + (suffix if suffix.startswith("/") else "/" + suffix))]


def _is_prefix_pattern(path: str) -> bool:
    """``/api/v1/install/*`` 这类写法是「前缀说明」，不是具体接口。

    文档里既有接口清单表（具体路径），也有模块归属表（用 ``/*`` 概括整个模块）。
    对后者做精确匹配必然假阳性，正确语义是「该前缀下至少有一个真实路由」。
    """
    return path.endswith("*")


def _prefix_of(path: str) -> str:
    """``/api/v1/install/*`` → ``/api/v1/install``；``/user/notifications*`` → ``/api/v1/user/notifications``。"""
    return normalize_path(path[:-1].rstrip("/") or "/")


def _matches_prefix(candidate: str, prefix: str) -> bool:
    """前缀语义：等于前缀本身，或在前缀的下一级路径里（避免 installer 误匹配 install）。"""
    if prefix == "/":
        return candidate.startswith("/")
    return candidate == prefix or candidate.startswith(prefix + "/")


def _cell_path(cells: list[str]) -> str:
    for cell in cells:
        for token in _BACKTICK_RE.findall(cell):
            token = token.strip()
            if token.startswith("/"):
                return token
    return ""


def parse_contract(doc_path: Path = DOC_PATH) -> DocRoutes:
    """解析文档表格，得到「路径 → 允许的方法集合」。

    只认表格行（``|`` 开头），代码块里的示例路径不会被误当成接口清单。
    章节 1~7 的表格省略了 ``/api/v1`` 前缀（契约 §0 规定前缀），这里补回；
    章节 8 是静态资源与前端路由，非 API 路径直接跳过。
    """
    doc = DocRoutes()
    section = 0
    for lineno, raw_line in enumerate(doc_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        head = _SECTION_RE.match(line)
        if head:
            section = int(head.group(1))
            continue
        if not line.startswith("|") or section < 1:
            continue                                   # §0 是全局约定表，不是接口清单
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or set(cells[0]) <= {"-", ":", " "}:
            continue                                   # 表格分隔行
        if cells[0] in ("方法", "Method", "路径", "Path"):
            continue                                   # 表头
        raw_path = _cell_path(cells)
        if not raw_path:
            continue
        if _is_prefix_pattern(raw_path):
            # 前缀说明行（模块归属表）：只要求「该前缀下存在真实路由」。
            # 静态资源前缀（/static/*、/assets/*）不是 API，跳过。
            bare = raw_path.rstrip("*")
            if bare.startswith(("/static", "/assets")):
                doc.skipped.append(f"{lineno}: {raw_path}（静态资源前缀）")
                continue
            full_prefix = raw_path if raw_path.startswith("/api/v1") else "/api/v1" + raw_path
            doc.prefixes.setdefault(_prefix_of(full_prefix), f"L{lineno}")
            continue
        has_method = bool(_METHOD_ROW_RE.match(cells[0].replace(" ", "")))
        if not has_method:
            # 章节 8 那种「只有路径」的表格行：不是标准接口清单，跳过非 API 路径
            if not raw_path.startswith("/api/v1"):
                doc.skipped.append(f"{lineno}: {raw_path}")
                continue
            doc_paths = _expand_optional_suffix(raw_path)
            for path in doc_paths:
                doc.methods.setdefault(path, set())
                doc.where.setdefault(path, f"L{lineno}")
            continue
        methods = [m for m in cells[0].replace(" ", "").split("/") if m in METHODS]
        for path in _expand_optional_suffix(raw_path):
            full = path if path.startswith("/api/v1") else normalize_path("/api/v1" + path)
            doc.methods.setdefault(full, set()).update(methods)
            doc.where.setdefault(full, f"L{lineno}")
    return doc


# --------------------------------------------------------------------------
# 代码侧采集（真的起一次服务）
# --------------------------------------------------------------------------
# 运行期采集到的旁注（pro 模块挂载情况等），build_report 会带进报告
RUNTIME_NOTES: dict[str, str] = {}
# 运行时上报的「同 method+path 被不同文件重复注册」记录（router.route_overrides()）
RUNTIME_OVERRIDES: dict[str, Any] = {"seen": False, "items": []}


def mount_pro_payment() -> str:
    """把 ``pro/app/payment`` 按**交付布局**挂成 ``app.payment``。

    开发树里 pro 版支付模块在 ``spark-shared/pro/app/payment``，而运行时要求它
    位于 ``<后端>/app/payment``（交付目录就是这么合并的）。不挂载的话，
    开发树里 ``/api/v1/pay/*`` 永远是 404，契约检查会误报「文档有代码无」。

    返回空串=无需挂载/已就绪，否则返回说明或抛 RuntimeUnavailable（模块存在但坏了，
    这种「悄悄少一半路由」的情况必须炸出来，不能当成 lite 版放过）。
    """
    merged = BACKEND_DIR / "app" / "payment"
    source = PROJECT_ROOT / "pro" / "app" / "payment"
    if merged.is_dir():
        return ""                     # 已经是交付布局（或 dev 树里已合并）
    if not source.is_dir():
        return ""                     # 真的没有 pro 模块（lite 版），交给 load_api_modules 跳过
    import importlib.util                                  # noqa: PLC0415

    name = "app.payment"
    try:
        spec = importlib.util.spec_from_file_location(
            name, source / "__init__.py", submodule_search_locations=[str(source)])
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {source} 构造 import spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except Exception as exc:      # noqa: BLE001 - 模块存在却加载失败 = 真故障
        sys.modules.pop(name, None)
        raise RuntimeUnavailable(
            f"pro 支付模块存在但加载失败（{source}）：{type(exc).__name__}: {exc}") from exc
    return f"按交付布局挂载 {source.relative_to(PROJECT_ROOT).as_posix()} → app.payment（等价于交付目录的合并结果）"


def collect_runtime_routes(data_dir: Path | None = None, timeout: float = 20.0) -> list[dict]:
    """在进程内起一个真实 HTTP 服务，读 ``GET /api/v1/_routes``。

    每次都用全新的临时数据目录，避免污染开发库；结束后关服务、删目录。
    """
    tmp_dir = data_dir or (TMP_ROOT / f"contract_{os.getpid()}")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))

    httpd = None
    thread = None
    try:
        from app import bootstrap, db                       # noqa: PLC0415 - 延迟导入，便于报「代码坏了」
        from app.config import load_settings, set_settings  # noqa: PLC0415
        from app.http_server import SparkServer             # noqa: PLC0415

        cfg = load_settings(
            None,
            overrides={
                "host": "127.0.0.1",
                "port": 0,                     # 0 = 让系统分配空闲端口
                "data_dir": tmp_dir,
                "db_path": tmp_dir / "spark.db",
                "secret": "contract-check-secret-0123456789abcdef",
                "rate_limit_per_minute": 0,    # 关限流，避免自检被自己限流
                "scheduler_enabled": False,
                "demo_mode": True,
            },
        )
        set_settings(cfg)
        RUNTIME_NOTES.clear()
        note = mount_pro_payment()
        if note:
            RUNTIME_NOTES["pro_mount"] = note
        bootstrap.init_app(cfg, quiet=True)
        httpd = SparkServer(cfg, None)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        url = f"http://127.0.0.1:{httpd.server_port}/api/v1/_routes"
        with urllib.request.urlopen(url, timeout=timeout) as resp:      # noqa: S310 - 本机回环
            payload = json.loads(resp.read().decode("utf-8"))
        data = payload.get("data") or {}
        routes = data.get("routes") or []
        if not routes:
            raise RuntimeUnavailable("GET /api/v1/_routes 返回空路由表——API 模块可能全部导入失败")
        # 优先用 router 自己上报的重复注册记录（带 文件:行号，比静态扫描准）
        if "overrides" in data:
            RUNTIME_OVERRIDES["seen"] = True
            RUNTIME_OVERRIDES["items"] = data.get("overrides") or []
            RUNTIME_NOTES["route_overrides"] = (
                f"运行时重复注册上报：{int(data.get('override_count') or 0)} 处"
                f"（路由共 {len(routes)} 条，来源 /api/v1/_routes）")
        else:
            RUNTIME_NOTES["route_overrides"] = "该构建的 /api/v1/_routes 未提供 overrides 字段，改用静态扫描"
        return routes
    except RuntimeUnavailable:
        raise
    except Exception as exc:      # noqa: BLE001 - 任何异常都意味着「运行时代码不可用」
        raise RuntimeUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=3)
        try:                       # Windows 上 file handle 释放有延迟，重试几次
            from app import db as _db                        # noqa: PLC0415
            _db.close_conn()
        except Exception:          # noqa: BLE001 - 清理阶段不该掩盖真正的失败
            pass
        for _ in range(5):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if not tmp_dir.exists():
                break
            time.sleep(0.1)


def runtime_methods(routes: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for entry in routes:
        path = normalize_path(str(entry.get("path") or ""))
        method = str(entry.get("method") or "").upper()
        if not path or not method:
            continue
        out.setdefault(path, set()).add(method)
    return out


# --------------------------------------------------------------------------
# 重复注册检测：router.py 的 route() 遇到同 (method, path) 会**静默覆盖**，
# 两个不同模块注册同一个接口时，最终生效的是「最后 import 的那个」——
# 响应体形状会随 import 顺序变化，这类故障在运行时极难发现，所以要静态查出来。
# --------------------------------------------------------------------------
_ROUTE_DECL_RE = re.compile(r"@route\(\s*[\"']([A-Za-z]+)[\"']\s*,\s*[\"']([^\"']+)[\"']")
_SCAN_ROOTS = tuple(
    p for p in (BACKEND_DIR / "app", PROJECT_ROOT / "pro" / "app") if p.exists()
)


def scan_duplicate_routes() -> list[dict]:
    """扫描所有 ``@route(...)`` 声明，返回「跨文件重复注册」的清单。"""
    seen: dict[tuple[str, str], list[str]] = {}
    for root in _SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            try:
                rel = path.relative_to(PROJECT_ROOT).as_posix()
            except ValueError:
                rel = path.as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                for match in _ROUTE_DECL_RE.finditer(line):
                    key = (match.group(1).upper(), normalize_path(match.group(2)))
                    seen.setdefault(key, []).append(f"{rel}:{lineno}")
    duplicates: list[dict] = []
    for (method, path), places in sorted(seen.items()):
        files = {place.split(":")[0] for place in places}
        if len(files) > 1:
            duplicates.append({"method": method, "path": path, "places": places})
    return duplicates


# --------------------------------------------------------------------------
# 比对
# --------------------------------------------------------------------------
def compare(doc: DocRoutes, code: dict[str, set[str]], profile: str = "pro",
            duplicates: list[dict] | None = None) -> dict:
    """返回差异报告。``profile=lite`` 时放行按设计缺失的支付路由。"""
    lite_absent = set()
    if profile == "lite":
        lite_absent = {p for p in doc.methods if p.startswith(LITE_ABSENT_PREFIXES)}
        lite_absent |= {p for p in code if p.startswith(LITE_ABSENT_PREFIXES)}

    missing: list[dict] = []
    for path, methods in sorted(doc.methods.items()):
        if path in lite_absent:
            continue
        if path not in code:
            missing.append({"path": path, "methods": sorted(methods) or ["<未标注方法>"],
                            "doc": doc.where.get(path, "")})
    undocumented: list[dict] = []
    allowlisted: list[dict] = []
    for path, methods in sorted(code.items()):
        if path in doc.methods or path in lite_absent or not methods:
            continue
        item = {"path": path, "methods": sorted(methods), "reason": KNOWN_EXTRA.get(path, "")}
        (allowlisted if path in KNOWN_EXTRA else undocumented).append(item)

    method_gaps: list[dict] = []
    extra_methods: list[dict] = []
    for path in sorted(set(doc.methods) & set(code)):
        if path in lite_absent:
            continue
        want, have = doc.methods[path], code[path]
        if not want:
            continue                                   # 文档只写了路径没写方法
        if want - have:
            method_gaps.append({"path": path, "missing": sorted(want - have),
                                "code_has": sorted(have), "doc": doc.where.get(path, "")})
        if have - want:
            extra_methods.append({"path": path, "extra": sorted(have - want), "doc_has": sorted(want)})

    # 前缀说明行：只要该前缀下有真实路由就算满足（并回报匹配到几条，便于人工核对）
    prefix_report: list[dict] = []
    prefix_missing: list[dict] = []
    for prefix, where in sorted(doc.prefixes.items()):
        if profile == "lite" and prefix.startswith(tuple(p.rstrip("/") for p in LITE_ABSENT_PREFIXES)):
            continue
        matched = sorted(p for p in code if _matches_prefix(p, prefix))
        item = {"prefix": prefix, "doc": where, "matched": len(matched),
                "sample": matched[:4]}
        (prefix_report if matched else prefix_missing).append(item)

    return {
        "profile": profile,
        "doc_path": str(DOC_PATH),
        "doc_total": len(doc.methods),
        "runtime_total": len(code),
        "runtime_entries": sum(len(m) for m in code.values()),
        "missing_in_code": missing,
        "undocumented_in_code": undocumented,
        "allowlisted_in_code": allowlisted,
        "method_missing_in_code": method_gaps,
        "method_extra_in_code": extra_methods,
        "duplicate_routes": list(duplicates or []),
        "prefix_patterns": prefix_report,
        "prefix_missing": prefix_missing,
        "runtime_notes": [f"{key}: {value}" for key, value in RUNTIME_NOTES.items()],
        "doc_skipped_non_api": doc.skipped,
    }


def render(report: dict, *, verbose: bool = True) -> str:
    """人类可读报告。差异明细一定打印出来（这是工具的交付价值）。"""
    lines: list[str] = []
    add = lines.append
    add("=" * 74)
    add(f"  契约一致性检查  profile={report['profile']}")
    add(f"  文档：{report['doc_path']}   接口 {report['doc_total']} 个")
    entries = report.get("runtime_entries", 0)
    add(f"  运行时路由：{report['runtime_total']} 个路径"
        f"（{entries} 条 method+path）" if entries else f"  运行时路由：{report['runtime_total']} 个")
    add("=" * 74)

    def _dump(title: str, items: list[dict], kind: str) -> None:
        add(f"\n[{title}] {len(items)} 处")
        if not items:
            add("  （无）")
            return
        for item in items:
            methods = "/".join(item.get("methods") or item.get("missing") or item.get("extra") or [])
            extra = ""
            if kind == "method_missing":
                extra = f"   代码实际支持：{'/'.join(item['code_has'])}"
            elif kind == "method_extra":
                extra = f"   文档声明：{'/'.join(item['doc_has'])}"
            elif item.get("doc"):
                extra = f"   文档位置：{item['doc']}"
            add(f"  - {methods:<22} {item['path']}{extra}")

    _dump("文档有代码无（前端会 404，必须实现）", report["missing_in_code"], "missing")
    _dump("代码有文档无（文档失真，必须补文档）", report["undocumented_in_code"], "extra")
    add(f"\n[跨文件重复注册同一接口] {len(report.get('duplicate_routes') or [])} 处")
    duplicates = report.get("duplicate_routes") or []
    if not duplicates:
        add("  （无）")
    else:
        add("  说明：router.route() 遇到同 (method,path) 会静默覆盖，最后 import 的模块生效，")
        add("        响应体形状会随 import 顺序漂移——必须删掉其中一个注册点。")
        for item in duplicates:
            add(f"  - {item['method']:<8} {item['path']}")
            for place in item["places"]:
                add(f"        注册于 {place}")
    if report["allowlisted_in_code"]:
        add(f"\n[已白名单的元接口] {len(report['allowlisted_in_code'])} 处（不影响结论）")
        for item in report["allowlisted_in_code"]:
            add(f"  - {'/'.join(item['methods']):<22} {item['path']}  ← {item['reason']}")
    _dump("方法差异：文档声明了但代码没实现", report["method_missing_in_code"], "method_missing")
    _dump("方法差异：代码多出来的方法", report["method_extra_in_code"], "method_extra")

    add(f"\n[文档前缀说明（`/*` 写法，只要求该前缀下有真实路由）] "
        f"{len(report.get('prefix_patterns') or [])} 处")
    for item in report.get("prefix_patterns") or []:
        sample = "、".join(item["sample"])
        add(f"  - {item['prefix']:<34} 命中 {item['matched']} 条  例：{sample}")
    missing_prefixes = report.get("prefix_missing") or []
    add(f"\n[前缀下没有任何路由] {len(missing_prefixes)} 处")
    if not missing_prefixes:
        add("  （无）")
    for item in missing_prefixes:
        add(f"  - {item['prefix']}   文档位置：{item['doc']}")

    for note in report.get("runtime_notes") or []:
        add(f"\n[运行时说明] {note}")

    if verbose and report["doc_skipped_non_api"]:
        add(f"\n[文档中按设计跳过的非 API 路径] {len(report['doc_skipped_non_api'])} 处")
        for item in report["doc_skipped_non_api"]:
            add(f"  - {item}")

    failed = bool(report["missing_in_code"] or report["undocumented_in_code"]
                  or report.get("duplicate_routes") or report.get("prefix_missing"))
    soft = bool(report["method_missing_in_code"] or report["method_extra_in_code"])
    add("\n" + "-" * 74)
    if not failed and not soft:
        add("  结论：一致 ✅")
    elif not failed:
        add("  结论：路径完全一致；存在方法差异（默认只警告，加 --strict 视为失败）⚠️")
    else:
        add("  结论：不一致 ❌  请同步 docs/API_CONTRACT.md 与代码")
    add("-" * 74)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def runtime_duplicates() -> list[dict] | None:
    """把运行时上报的 overrides 转成报告结构；没上报则返回 None（由调用方退回静态扫描）。"""
    if not RUNTIME_OVERRIDES["seen"]:
        return None
    out: list[dict] = []
    for item in RUNTIME_OVERRIDES["items"]:
        places = [str(p) for p in (item.get("overwritten"), item.get("overwritten_by")) if p]
        out.append({
            "method": str(item.get("method") or "").upper(),
            "path": normalize_path(str(item.get("path") or "")),
            "places": places or ["(router 未提供来源)"],
        })
    return out


def auto_profile() -> str:
    """自动判断这份代码是 pro 还是 lite（有支付模块就是 pro）。

    直接 `python tools/check_contract.py` 时，lite 交付包会因为「契约里的 /pay/*
    按设计不存在」而报错——那会把设计差异误报成缺陷，所以默认档位交给代码自己判断。
    """
    has_payment = ((BACKEND_DIR / "app" / "payment").is_dir()
                   or (PROJECT_ROOT / "pro" / "app" / "payment").is_dir())
    return "pro" if has_payment else "lite"


def build_report(profile: str = "pro", doc_path: Path = DOC_PATH) -> dict:
    doc = parse_contract(doc_path)
    code = runtime_methods(collect_runtime_routes())
    duplicates = runtime_duplicates()          # 运行时优先（router 自带 文件:行号）
    if duplicates is None:
        duplicates = scan_duplicate_routes()   # 旧构建兜底：静态扫描 @route 装饰器
        RUNTIME_NOTES["duplicate_source"] = f"静态扫描（{len(duplicates)} 处）"
    else:
        RUNTIME_NOTES["duplicate_source"] = f"运行时 overrides（{len(duplicates)} 处）"
    return compare(doc, code, profile=profile, duplicates=duplicates)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API 契约一致性检查（文档 ⇄ 运行时路由表）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（供 selftest 汇总）")
    parser.add_argument("--strict", action="store_true", help="方法差异也视为失败")
    parser.add_argument("--profile", choices=("auto", "pro", "lite"), default="auto",
                        help="auto=按是否有支付模块自动判断（默认）；lite 放行 /pay/* 路由缺失")
    parser.add_argument("--quiet", action="store_true", help="只输出结论行")
    args = parser.parse_args(argv)
    profile = auto_profile() if args.profile == "auto" else args.profile

    try:
        report = build_report(profile=profile)
    except RuntimeUnavailable as exc:
        if args.json:
            print(json.dumps({"ok": False, "runtime_error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print("=" * 74)
            print("  契约检查无法完成：运行时代码不可用 ❌")
            print(f"  原因：{exc}")
            print("  说明：这通常意味着某个 app/api/* 模块 import 失败，"
                  "路由表会因此少一大半，必须先修代码。")
            print("=" * 74)
        return 2

    if args.json:
        report["ok"] = not (report["missing_in_code"] or report["undocumented_in_code"]
                            or report["duplicate_routes"] or report["prefix_missing"])
        report["ok_strict"] = report["ok"] and not (
            report["method_missing_in_code"] or report["method_extra_in_code"]
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report, verbose=not args.quiet))

    if (report["missing_in_code"] or report["undocumented_in_code"]
            or report["duplicate_routes"] or report["prefix_missing"]):
        return 1
    if args.strict and (report["method_missing_in_code"] or report["method_extra_in_code"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
