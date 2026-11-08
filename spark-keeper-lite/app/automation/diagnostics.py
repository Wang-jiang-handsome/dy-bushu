"""账号页面结构诊断：抖音改版时一键导出真实 DOM 供排障。

用途
----
抖音随时可能改版，选择器一旦失效，线上表现只是「找不到好友」「发送失败」，
排查成本极高。``diagnose_account`` 把**每条候选选择器**在真实页面上跑一遍，
导出命中数/可见数/样本文本 + 截图 + JSON + HTML，用户把报告发出来就能定位。

``main.py diagnose --account-id N`` 已经按这个签名调用::

    result = diagnose_account(account_id, out_dir=None)
    result["out_dir"]      # 输出目录
    result["selectors"]    # {组名: {"total": n, "visible": n, ...}}

设计要点
--------
* **demo_mode 也要能跑**：不启浏览器，生成一份结构完整的「演示报告」（全部
  零命中 + 说明），这样 CLI 与测试在任何环境都能验证导出逻辑。
* 报告里**绝不含 cookies / storage_path**（报告可能被用户贴到公开渠道）。
* HTML 报告自包含（内联 CSS、内联 base64 截图），单文件即可查看，不依赖网络。
"""

from __future__ import annotations

import base64
import html
import json
import time
from pathlib import Path
from typing import Any

from .. import db
from ..common import logging as sklog
from ..common import timeutil
from . import cdp as cdp_mod
from . import douyin, selectors, session_store


def default_out_dir(account_id: int, settings: Any) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    root = Path(getattr(settings, "data_dir", ".")) / "diagnostics"
    path = root / f"account_{int(account_id)}_{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_account(account_id: int) -> dict | None:
    return db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (int(account_id),))


def account_summary(account: dict | None) -> dict:
    """报告里的账号信息（**不含 storage_path / cookies**）。"""
    account = account or {}
    return {
        "id": account.get("id"),
        "nickname": account.get("nickname") or "",
        "status": account.get("status") or "",
        "enabled": account.get("enabled", 1),
        "login_type": account.get("login_type") or "",
        "last_check_at": account.get("last_check_at") or "",
        "last_error": str(account.get("last_error") or "")[:200],
    }


def selector_report(cdp: Any) -> dict:
    """逐条跑选择器，返回 ``{组名: [ {selector,total,visible,sample,rect,error}, ... ]}``。"""
    result = cdp.call(selectors.query_group_js(), selectors.build_query_payload())
    if not isinstance(result, dict):
        return {}
    groups = result.get("groups") or {}
    return {str(name): list(rows or []) for name, rows in groups.items()}


def summarize(report: dict) -> dict:
    """把逐条结果汇总成 ``{组名: {total, visible, best_selector, sample, confidence}}``。"""
    snapshot = selectors.selector_snapshot()
    out: dict[str, dict] = {}
    for name, rows in (report or {}).items():
        total = sum(int(row.get("total") or 0) for row in rows)
        visible = sum(int(row.get("visible") or 0) for row in rows)
        best = ""
        sample = ""
        for row in rows:
            if int(row.get("visible") or 0) > 0:
                best = str(row.get("selector") or "")
                sample = str(row.get("sample") or "")
                break
        meta = snapshot.get(name, {})
        out[name] = {
            "label": meta.get("label", name),
            "kind": meta.get("kind", "css"),
            "confidence": meta.get("confidence", ""),
            "source": meta.get("source", ""),
            "note": meta.get("note", ""),
            "total": total,
            "visible": visible,
            "best_selector": best,
            "sample": sample,
            "rows": list(rows),
        }
    return out


def render_html(report: dict, *, account: dict | None = None, screenshot: str = "",
                extra: dict | None = None) -> str:
    """生成自包含的 HTML 报告（内联样式 + 内联截图，单文件可看）。"""
    summary = summarize(report)
    extra = extra or {}
    rows: list[str] = []
    for name, info in sorted(summary.items(), key=lambda item: -int(item[1]["visible"])):
        hit = "✅" if info["visible"] else "❌"
        rows.append(
            "<details{open}><summary>{hit} <b>{name}</b> — {label} "
            "（可见 {visible} / 命中 {total}，置信度 {confidence}）</summary>"
            "<p class='meta'>来源：{source}<br>说明：{note}<br>"
            "最佳选择器：<code>{best}</code><br>样本：<code>{sample}</code></p>"
            "<table><tr><th>选择器</th><th>命中</th><th>可见</th><th>样本/错误</th></tr>{body}"
            "</table></details>".format(
                open=" open" if info["visible"] else "", hit=hit, name=html.escape(name),
                label=html.escape(str(info["label"])),
                visible=info["visible"], total=info["total"],
                confidence=html.escape(str(info["confidence"])),
                source=html.escape(str(info["source"])), note=html.escape(str(info["note"])),
                best=html.escape(info["best_selector"] or "—"),
                sample=html.escape(info["sample"] or "—"),
                body="".join(
                    "<tr><td><code>{sel}</code></td><td>{total}</td><td>{visible}</td>"
                    "<td>{note}</td></tr>".format(
                        sel=html.escape(str(row.get("selector") or "")),
                        total=int(row.get("total") or 0),
                        visible=int(row.get("visible") or 0),
                        note=html.escape(str(row.get("error") or row.get("sample") or "")),
                    )
                    for row in info["rows"]
                ),
            ))
    summary_lines = "".join(
        f"<li>{'✅' if item['visible'] else '❌'} {html.escape(name)}："
        f"可见 {item['visible']} / 命中 {item['total']}</li>"
        for name, item in sorted(summary.items(), key=lambda i: -int(i[1]["visible"]))
    )
    shot = (f"<h2>页面截图</h2><img src='data:image/png;base64,{screenshot}' alt='page'>"
            if screenshot else "<p>（没有截图：可能是 demo_mode 或截图失败）</p>")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>抖音页面结构诊断 - 账号 {html.escape(str((account or {}).get('id', '')))}</title>
<style>
 body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; margin: 24px; color: #222; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 24px; }}
 .meta {{ color: #555; font-size: 13px; }}
 table {{ border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-size: 13px; }}
 th, td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; vertical-align: top; }}
 th {{ background: #f5f5f5; }} code {{ background: #f7f7f7; padding: 1px 4px; }}
 details {{ border: 1px solid #eee; border-radius: 6px; padding: 8px 12px; margin-bottom: 8px; }}
 img {{ max-width: 100%; border: 1px solid #ddd; }}
 .kv {{ font-size: 13px; }} .kv td:first-child {{ width: 160px; color: #666; }}
</style></head><body>
<h1>抖音页面结构诊断报告</h1>
<p class="meta">生成时间：{timeutil.utc_now_str()} UTC ·
URL：{html.escape(str(extra.get('url') or '—'))} ·
标题：{html.escape(str(extra.get('title') or '—'))} ·
模式：{html.escape(str(extra.get('mode') or 'real'))}</p>
<h2>账号</h2>
<table class="kv">
 <tr><td>账号 ID</td><td>{html.escape(str((account or {}).get('id', '')))}</td></tr>
 <tr><td>昵称</td><td>{html.escape(str((account or {}).get('nickname') or ''))}</td></tr>
 <tr><td>状态</td><td>{html.escape(str((account or {}).get('status') or ''))}</td></tr>
 <tr><td>登录态文件</td><td>{'存在' if (account or {}).get('has_session_file') else '不存在'}</td></tr>
</table>
<h2>概览</h2><ul>{summary_lines}</ul>
{shot}
<h2>逐条选择器结果</h2>
{''.join(rows)}
<p class="meta">报告不含 cookies 与登录态文件路径；选择器表来自
<code>app/automation/selectors.py</code>，与代码始终一致。</p>
</body></html>"""


def diagnose_account(account_id: int, out_dir: str | Path | None = None) -> dict:
    """跑一遍诊断并导出 JSON/HTML/截图（CLI ``main.py diagnose`` 的入口）。

    返回 ``{"ok", "out_dir", "files", "selectors", "summary", "message"}``；
    ``selectors`` 的形状与 CLI 打印的一致（``{name: {"total","visible",...}}``）。
    """
    from ..config import get_settings
    settings = get_settings()
    account = load_account(account_id)
    if not account:
        return {"ok": False, "out_dir": "", "files": [], "selectors": {}, "summary": {},
                "message": f"账号 {account_id} 不存在"}

    folder = Path(out_dir).expanduser() if out_dir else default_out_dir(account_id, settings)
    folder.mkdir(parents=True, exist_ok=True)
    account_info = {**account_summary(account),
                    "has_session_file": session_store.session_info(account, settings)["exists"]}

    if douyin.is_demo(settings):
        # 演示模式：不启浏览器，导出一份「结构完整但全零命中」的报告，
        # 让 CLI/测试在任何环境都能验证导出链路。
        report = _empty_report()
        extra = {"url": "(demo_mode 未打开页面)", "title": "(demo_mode)", "mode": "demo"}
        payload = summarize(report)
        files = _write_outputs(folder, account_id, report, payload, account_info, extra, "")
        sklog.info(f"诊断完成（demo_mode）：account={account_id} out={folder}",
                   scope="admin", source="automation.diagnostics")
        return {"ok": True, "out_dir": str(folder), "files": files, "selectors": payload,
                "summary": payload, "message": "演示模式：未启动浏览器，仅导出选择器表结构"}

    if not session_store.is_session_valid(account, settings):
        # 没有有效登录态**也照样诊断**：用户来跑诊断，最常见的原因恰恰是
        # 「扫码登录看不到二维码 / 扫了没反应」——这时候最需要的正是登录页的真实 DOM。
        # 旧实现直接 return 且 CLI 不打印原因，用户只看到一个空目录，等于没有诊断。
        return _diagnose_login_page(folder, account_id, account_info, account, settings)

    screenshot = ""
    report: dict = {}
    extra: dict = {"mode": "real", "url": "", "title": ""}
    try:
        with session_store.AccountBrowser(account, settings, kind="diagnose", profile_suffix="diag") as browser:
            cdp = browser.cdp
            state = douyin.read_login_state(cdp)
            extra["logged_in"] = bool(state.get("logged_in"))
            extra["login_reason"] = state.get("reason", "")
            try:
                opened = douyin.open_messages(cdp)
                extra["messages_ok"] = bool(opened.get("ok"))
                extra["messages_reason"] = opened.get("reason", "")
                extra["url"] = cdp.current_url()
                extra["title"] = cdp.title()
                report = selector_report(cdp)
            except cdp_mod.CDPError as exc:
                # 页面无响应（容器的 CPU 限流 / 渲染进程假死）时也要留下证据：
                # 先尽力截图 + 页面快照，再把异常抛给外层统一返回。
                extra["error"] = str(exc)[:400]
                try:
                    screenshot = cdp.capture_screenshot(format="png")
                except Exception:              # noqa: BLE001
                    screenshot = ""
                try:
                    extra["snapshot"] = douyin.page_snapshot(cdp)
                except Exception:              # noqa: BLE001
                    extra["snapshot"] = {}
                raise
            try:
                if not screenshot:
                    screenshot = cdp.capture_screenshot(format="png")
            except cdp_mod.CDPError:
                screenshot = ""
        payload = summarize(report)
        files = _write_outputs(folder, account_id, report, payload, account_info, extra, screenshot)
        message = "诊断完成" if any(item["visible"] for item in payload.values()) \
            else "所有选择器都没有可见命中：页面结构可能已改版，请把报告发出来校准"
        sklog.info(f"诊断完成：account={account_id} out={folder} "
                   f"命中组={[k for k, v in payload.items() if v['visible']]}",
                   scope="admin", source="automation.diagnostics")
        return {"ok": True, "out_dir": str(folder), "files": files, "selectors": payload,
                "summary": payload, "message": message}
    except session_store.BrowserBusy as exc:
        message = f"浏览器并发已满，请稍后重试：{exc}"
        files = _write_failure(folder, account_id, account_info, message, extra)
        return {"ok": False, "out_dir": str(folder), "files": files, "selectors": {},
                "summary": {}, "message": message}
    except session_store.BrowserUnavailable as exc:
        message = f"浏览器不可用：{exc}"
        files = _write_failure(folder, account_id, account_info, message, extra)
        return {"ok": False, "out_dir": str(folder), "files": files, "selectors": {},
                "summary": {}, "message": message}
    except cdp_mod.CDPError as exc:
        message = f"页面操作失败：{exc}"
        files = _write_failure(folder, account_id, account_info, message, extra, screenshot)
        return {"ok": False, "out_dir": str(folder), "files": files, "selectors": {},
                "summary": {}, "message": message}


def _write_failure(folder: Path, account_id: int, account_info: dict, message: str,
                   extra: dict | None = None, screenshot: str = "") -> list[str]:
    """把「诊断失败」也落盘成一份报告。

    真实事故：用户在服务器上跑 `main.py diagnose`，目录**完全是空的** ——
    因为失败分支只 return 不写文件。而失败时恰恰最需要现场（报错 + 截图）。
    """
    payload_extra = {"mode": "failure", "error": message}
    payload_extra.update(extra or {})
    try:
        files = _write_outputs(folder, account_id, _empty_report(), {}, account_info,
                               payload_extra, screenshot)
        return files
    except Exception:                          # noqa: BLE001 - 再失败也不能盖住真实错误
        return []


def _diagnose_login_page(folder: Path, account_id: int, account_info: dict,
                         account: dict, settings: Any) -> dict:
    """没有有效登录态时，诊断**登录页**并把二维码相关选择器的命中情况导出。

    这是「扫码没反应」时最有价值的一份报告：能直接看出
    * 登录按钮 / 登录弹层是否存在（``login_qrcode_container``）
    * 真二维码图片是否渲染出来（``login_qrcode_image``）
    * 是否误判成已登录（会话 cookie）
    """
    extra: dict = {"mode": "login", "url": "", "title": ""}
    try:
        with session_store.AccountBrowser(account, settings, kind="diagnose", profile_suffix="diag",
                                          restore=False) as browser:
            cdp = browser.cdp
            douyin.open_home(cdp)
            time.sleep(3.0)
            opened = douyin.open_login_panel(cdp, timeout=20.0)
            # 等**真二维码**渲染出来（弹层刚开时是抖音 logo 占位 SVG，实测 3s 左右替换）。
            # 旧实现只固定 sleep 2.5s，慢机上截到的是占位图，报告看起来像「没有二维码」，
            # 反而误导排障。这里轮询到真图出现，最多等 20 秒，并把结论写进报告。
            qr_visible = False
            waited = 0.0
            qr_deadline = time.monotonic() + 20.0
            while time.monotonic() < qr_deadline:
                if selectors.find_first(cdp, "login_qrcode_image", timeout=3.0):
                    qr_visible = True
                    break
                time.sleep(0.6)
            waited = round(20.0 - max(0.0, qr_deadline - time.monotonic()), 1)
            extra.update({
                "login_panel_ok": bool(opened.get("ok")),
                "login_panel_via": str(opened.get("via") or ""),
                "has_login_cookie": bool(douyin.has_login_cookie(cdp)),
                "qrcode_visible": qr_visible,
                "qrcode_wait_seconds": waited,
                "url": cdp.current_url(),
                "title": cdp.title(),
            })
            report = selector_report(cdp)
            try:
                screenshot = cdp.capture_screenshot(format="png")
            except cdp_mod.CDPError:
                screenshot = ""
        payload = summarize(report)
        files = _write_outputs(folder, account_id, report, payload, account_info, extra,
                               screenshot)
        visible_groups = [name for name, item in payload.items() if item.get("visible")]
        if qr_visible:
            message = (f"登录页诊断完成：二维码已渲染（等待约 {waited}s）。"
                       f"若扫码后仍无反应，请把 report.json 发出来校准。")
        else:
            message = ("登录页诊断完成：**20 秒内二维码没有渲染出来**"
                       "（弹层打开成功，但二维码图片一直没出现）。"
                       "这是抖音网页端的问题，建议改用「手动导入 Cookie」。")
        sklog.info(f"诊断完成（登录页）：account={account_id} out={folder} "
                   f"弹层={extra['login_panel_via']} 二维码={qr_visible}({waited}s) "
                   f"命中组={visible_groups[:10]}",
                   scope="admin", source="automation.diagnostics")
        return {"ok": True, "out_dir": str(folder), "files": files, "selectors": payload,
                "summary": payload, "message": message}
    except session_store.BrowserBusy as exc:
        return {"ok": False, "out_dir": str(folder), "files": [], "selectors": {}, "summary": {},
                "message": f"浏览器并发已满，请稍后重试：{exc}"}
    except session_store.BrowserUnavailable as exc:
        return {"ok": False, "out_dir": str(folder), "files": [], "selectors": {}, "summary": {},
                "message": f"浏览器不可用：{exc}"}
    except cdp_mod.CDPError as exc:
        return {"ok": False, "out_dir": str(folder), "files": [], "selectors": {}, "summary": {},
                "message": f"页面操作失败：{exc}"}


def _empty_report() -> dict:
    """演示/无页面时的空报告（结构必须与真实报告一致，否则前端解析会崩）。"""
    payload = selectors.build_query_payload()
    return {item["name"]: [{"selector": sel, "total": 0, "visible": 0, "sample": "",
                            "rect": None, "error": ""}
                           for sel in item["selectors"]]
            for item in payload["groups"]}


def _write_outputs(folder: Path, account_id: int, report: dict, summary: dict,
                   account_info: dict, extra: dict, screenshot: str) -> list[str]:
    """写 JSON + HTML（+ 截图），返回文件名列表。"""
    files: list[str] = []
    payload = {
        "account": account_info,
        "generated_at": timeutil.utc_now_str(),
        "extra": extra,
        "selectors": summary,
        "raw": report,
        "note": "本报告不含 cookies 与登录态文件路径；" 
                "若某组 visible=0，说明该元素当前页面上不存在或选择器已失效。",
    }
    json_path = folder / "report.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    files.append(json_path.name)
    if screenshot:
        (folder / "page.png").write_bytes(base64.b64decode(screenshot))
        files.append("page.png")
    html_path = folder / "report.html"
    html_path.write_text(render_html(report, account=account_info, screenshot=screenshot,
                                     extra=extra), encoding="utf-8")
    files.append(html_path.name)
    return files
