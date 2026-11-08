"""抖音网页版弹性选择器表 + 页面查询 JS。

为什么是「一组选择器」而不是一条
--------------------------------
抖音对登录页/私信页做 A/B 灰度，同一个功能在不同账号上至少有 4 种 DOM 结构
（class 名不同、有没有 ``data-e2e`` 不同、输入框末尾节点是 ``<div>`` 还是
``<span>`` 也不同）。「一条选择器通吃」必然会在某次改版后全挂，所以每组
给一个**优先级降级列表**，从最可靠（``data-e2e`` 测试属性）一路降级到最宽泛
（``contenteditable`` 兜底），取第一个**真实可见**的匹配。

为什么所有查询都走 JS 而不是 CDP 的 DOM 域
-------------------------------------------
CDP 的 ``DOM.querySelector`` 只认 CSS 选择器、拿不到可见性与 rect，而且改版
后我们最需要的信息恰好是「这个选择器现在选中了几个节点、可见几个、长什么样」。
所以统一用 ``Runtime.evaluate`` 执行下面这些 JS，一个 RPC 就能拿到计数+可见数+
样本文本+坐标。

置信度与来源标注（每个组都写了）
--------------------------------
* 高：``data-e2e`` 系列属性（前作 ``douyin-spark-keeper/src/selectors.py`` 实测过，
  且这类属性是字节内部测试钩子，改版时最稳）。
* 中：class 片段匹配（``[class*="xxx"]``），来自前作实测 + 公开踩坑文章。
* 低：纯结构兜底（``canvas`` / ``div[contenteditable="true"]``），改版后可能选到
  无关节点，**必须靠可见性 + 坐标 + 诊断报告确认**。

⚠️ 注意：这里的选择器全部要能在 ``document.querySelectorAll`` 里跑，
所以**不能**用 Playwright 的 ``:has-text()`` / ``>>`` 语法。凡是要按文字匹配的
场景，走本模块的 ``TEXT_GROUPS``（JS 扫描可见节点文本），不要塞进 CSS 组。

真实页面结构未经本机验证（沙箱起不了浏览器）：真机首次运行请跑
``python main.py diagnose --account-id N``，用导出的报告校准本表。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

# --------------------------------------------------------------------------
# 可见性助手（所有 JS 都复用它，避免「取到隐藏副本」这个最经典的坑）
# --------------------------------------------------------------------------
#: 抖音页面会同时挂隐藏副本（吸顶栏、预渲染节点、懒加载占位），它们的 rect 全是 0。
#: 直接取第一个匹配会**恒中隐藏节点**，然后报「找不到元素」——所以每次查询都必须过滤。
VISIBLE_JS = (
    "const isVisible = (el) => {"
    "  if (!el || !el.getBoundingClientRect) return false;"
    "  const r = el.getBoundingClientRect();"
    "  if (r.width <= 1 || r.height <= 1) return false;"
    "  const st = window.getComputedStyle(el);"
    "  if (!st) return false;"
    "  if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') return false;"
    "  return true;"
    "};"
)

#: 拿到元素在**页面坐标系**里的位置（截图 clip 需要页面坐标，不是视口坐标）
RECT_JS = (
    "const rectOf = (el) => {"
    "  const r = el.getBoundingClientRect();"
    "  return { x: r.left + window.scrollX, y: r.top + window.scrollY,"
    "           width: r.width, height: r.height,"
    "           cx: r.left + window.scrollX + r.width / 2,"
    "           cy: r.top + window.scrollY + r.height / 2 };"
    "};"
)

#: 样本文本：压缩空白并截断，避免把整页 HTML 打进日志
SAMPLE_JS = (
    "const sampleOf = (el) => String((el.innerText || el.textContent || el.value || '') || '')"
    "  .replace(/\\s+/g, ' ').trim().slice(0, 80);"
)


def _js(body: str) -> str:
    """把助手函数拼到一个箭头函数体前面（用拼接而不是 f-string，避免 JS 花括号冲突）。"""
    return "(payload) => {\n" + VISIBLE_JS + RECT_JS + SAMPLE_JS + "\n" + body + "\n}"


#: JS 里的 ``readEditor``：Draft.js 的内容在 ``[data-contents]`` 子节点里，
#: ``innerText`` 最稳；``\\u00a0``（不间断空格）要归一化，否则「输入框已清空」判定会误判。
READ_EDITOR_SNIPPET = (
    "const readEditor = (el) => String((el.innerText || el.textContent || '') || '')"
    "  .replace(/\\u00a0/g, ' ').trim();"
)


def _js_editor(body: str) -> str:
    """需要读写输入框的 JS：额外带上 ``readEditor``。"""
    return ("(payload) => {\n" + VISIBLE_JS + RECT_JS + SAMPLE_JS + READ_EDITOR_SNIPPET
            + "\n" + body + "\n}")


# --------------------------------------------------------------------------
# 选择器组定义
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SelectorGroup:
    name: str
    label: str
    selectors: tuple[str, ...]
    confidence: str = "中"
    source: str = ""
    prefer: str = "first"      # first = 取第一个可见；last = 取最后一个可见（Draft.js 输入框）
    note: str = ""


def _g(name: str, label: str, selectors: Sequence[str], confidence: str, source: str,
       prefer: str = "first", note: str = "") -> SelectorGroup:
    return SelectorGroup(name=name, label=label, selectors=tuple(selectors),
                         confidence=confidence, source=source, prefer=prefer, note=note)


_SRC_KEEPER = "前作 douyin-spark-keeper/src/selectors.py（Playwright 实测）"
_SRC_PUBLIC = "公开踩坑文章（腾讯云《抖音私信自动化回复-多版本 DOM 适配》等）"
_SRC_SELF = "本项目推断（未实测）"

#: CSS 组：全部可用 ``querySelectorAll`` 执行
GROUPS: dict[str, SelectorGroup] = {
    # ---------------- 登录页 ----------------
    "login_qrcode_container": _g(
        "login_qrcode_container", "登录页二维码容器",
        (
            # ---- 实测选择器（2026-10，当前抖音 PC 版）----
            # 真实登录弹层是一堆哈希类名，只有少数稳定 id。实测弹层结构：
            #   article#douyin_login_comp_flat_panel
            #     div#douyin_login_landing_flat_container
            #       div#douyin_login_comp_scan_code
            #         div#animate_qrcode_container   ← 二维码容器（180×180）
            "#animate_qrcode_container",
            "#douyin_login_comp_scan_code",
            "#douyin_login_landing_flat_container",
            "article#douyin_login_comp_flat_panel",
            # ---- 旧版兜底（其它版本/灰度）----
            '[data-e2e="login-panel"]',
            "#login-panel-new",
            'div[class*="login-panel"]',
            'div[class*="loginPanel"]',
            'div[class*="qrcode-container"]',
            'div[class*="qrcodeContainer"]',
            'div[class*="qr-code"]',
            'div[class*="QrCode"]',
        ),
        "高", "本项目实机实测（2026-10 抖音 PC 版）",
        note="容器用于截图定位与判断「二维码是否已被替换成头像」；"
             "旧选择器全部命中 0，是「扫码没反应」的根因之一",
    ),
    "login_qrcode_image": _g(
        "login_qrcode_image", "二维码图片节点",
        (
            # ---- 实测选择器：真二维码是弹层里的一张 data:image PNG ----
            # 弹层刚打开时这里是「抖音 logo 占位 SVG」，**约 3 秒后**才换成二维码图；
            # 只认 data:image 的 img，避免把占位图/整块面板截给用户。
            '#animate_qrcode_container img[src^="data:image"]',
            '#douyin_login_comp_scan_code img[src^="data:image"]',
            '#douyin_login_landing_flat_container img[src^="data:image"]',
            # ---- 旧版兜底 ----
            'img[alt*="二维码"]',
            'div[class*="qrcode"] img',
            'div[class*="qr-code"] img',
            'div[class*="login"] img[src^="data:image"]',
            'canvas[class*="qrcode"]',
        ),
        "高", "本项目实机实测（2026-10 抖音 PC 版）",
        note="只匹配真二维码图片；注意：**不要**再放裸 canvas 兜底 —— "
             "页面上的广告 canvas（300×150）会被误当成二维码截给用户",
    ),
    "login_sms_entry": _g(
        "login_sms_entry", "短信登录入口",
        (
            '[data-e2e="sms-login"]',
            'div[class*="sms-login"]',
            'div[class*="smsLogin"]',
            'div[class*="loginSwitch"] span',
            'span[class*="switch"]',
        ),
        "中", f"{_SRC_PUBLIC} / {_SRC_SELF}",
        note="抖音多数版本只有「手机号登录」文案，实际命中常常来自 TEXT_GROUPS",
    ),
    "login_phone_input": _g(
        "login_phone_input", "手机号输入框",
        (
            'input[type="tel"]',
            'input[placeholder*="手机号"]',
            'input[placeholder*="手机"]',
            'input[name="mobile"]',
            'input[maxlength="11"]',
            'div[class*="login"] input[type="text"]',
        ),
        "中", f"{_SRC_PUBLIC} / {_SRC_SELF}",
    ),
    "login_code_input": _g(
        "login_code_input", "短信验证码输入框",
        (
            'input[placeholder*="验证码"]',
            'input[autocomplete="one-time-code"]',
            'input[maxlength="6"]',
            'input[name="code"]',
        ),
        "中", f"{_SRC_PUBLIC} / {_SRC_SELF}",
    ),
    "login_send_code_button": _g(
        "login_send_code_button", "发送验证码按钮",
        (
            '[data-e2e="send-sms-code"]',
            'button[class*="send-code"]',
            'button[class*="sendCode"]',
            'div[class*="sendCode"]',
            'span[class*="sendCode"]',
        ),
        "中", f"{_SRC_PUBLIC} / {_SRC_SELF}",
        note="文案「发送验证码」在 TEXT_GROUPS 里，两者都要看",
    ),
    "login_agree_checkbox": _g(
        "login_agree_checkbox", "同意协议勾选框",
        (
            'div[class*="agreement"] input[type="checkbox"]',
            'label[class*="checkbox"] input',
            'input[type="checkbox"]',
            'div[role="checkbox"]',
            'span[class*="checkbox"]',
        ),
        "低", _SRC_SELF,
        note="部分版本用自定义 div 当勾选框，需要点父节点；命中多个时必须靠坐标判断",
    ),
    "login_user_info": _g(
        "login_user_info", "登录后的用户信息（头像/昵称）",
        (
            '[data-e2e="user-info"]',
            '[data-e2e="user-avatar"]',
            'header div[class*="avatar"] img',
            'div[class*="userAvatar"] img',
            'div[class*="userInfo"] img',
            'header img[class*="avatar"]',
        ),
        "中", f"{_SRC_KEEPER} / {_SRC_SELF}",
        note="出现首个可见头像即认为「已登录」；严格模式再看昵称文本",
    ),
    "login_required": _g(
        "login_required", "未登录信号（需要重新扫码）",
        (
            # 实测：登录弹层本身就是「未登录」最强的信号
            "article#douyin_login_comp_flat_panel",
            "#douyin_login_landing_flat_container",
            '[data-e2e="login-panel"]',
            "#login-panel-new",
            'div[class*="loginPanel"]',
            'div[class*="login-panel"]',
            'img[alt*="二维码"]',
        ),
        "高", "本项目实机实测（2026-10 抖音 PC 版）",
        note="命中即判定登录态失效，需要重新扫码/短信登录",
    ),
    "login_refresh_qrcode": _g(
        "login_refresh_qrcode", "刷新二维码按钮",
        (
            '[data-e2e="refresh-qrcode"]',
            'div[class*="refresh"] span',
            'div[class*="qrcode"] div[class*="refresh"]',
        ),
        "低", _SRC_SELF,
        note="抖音二维码过期后一般会显示「点击刷新」，文案在 TEXT_GROUPS 里",
    ),
    # ---------------- 私信页 ----------------
    "chat_header": _g(
        "chat_header", "聊天面板顶部标题（当前会话对象）",
        (
            # ---- 实机：<div class="RightPanelHeaderconvHeader">李森桦 138</div> ----
            '[class*="convHeader"]',
            '[class*="RightPanelHeader"] [class*="infoContainer"]',
            '[data-e2e="chat-header"]',
            '[class*="chatHeader"]',
            '[class*="messageTitle"]',
            '[class*="chatTitle"]',
            '[class*="conversationHeader"]',
            '#messageContent [class*="title"]',
        ),
        "中", _SRC_SELF,
        note="用来确认「点开的会话就是目标好友」，避免发给错的人",
    ),
    "chat_editor_area": _g(
        "chat_editor_area", "聊天输入区容器（编辑器 + 发送按钮）",
        (
            '[data-e2e="msg-input"]',
            '[data-e2e="message-input-container"]',
            '[class*="messageMsgInput"]',
            '[class*="chatInput"]',
            '[class*="editorContainer"]',
            '[class*="inputArea"]',
        ),
        "中", _SRC_SELF,
        note="编辑器还没渲染出来时先等它出现，别急着判定失败",
    ),
    "conversation_item": _g(
        "conversation_item", "私信会话列表项",
        (
            '[data-e2e="conversation-item"]',
            '.conversationConversationItemwrapper',
            '[class*="conversationConversationItem"]',
            '[class*="conversationItem"]',
            '[class*="messageConversationItem"]',
            '#messageContent [class*="conversation"][class*="Item"]',
            'div[class*="ConversationList"] > div',
            'ul[class*="conversation"] > li',
        ),
        "中", f"{_SRC_KEEPER}（实测 4 种 DOM 变体）",
        note="不解析内部结构，整块 innerText 拿回来交给 matcher 打分，改版也不怕",
    ),
    "conversation_list": _g(
        "conversation_list", "会话列表容器",
        (
            '[data-e2e="conversation-list"]',
            '[class*="conversationListContainer"]',
            '[class*="conversationList"]',
            'div[class*="ConversationList"]',
            "#messageContent",
        ),
        "中", f"{_SRC_KEEPER} / {_SRC_SELF}",
        note="用于滚动加载更多会话（列表只渲染首屏）",
    ),
    "message_input": _g(
        "message_input", "会话内消息输入框（Slate 编辑器 contenteditable）",
        (
            # ---- 实机（2026-10 登录态）真实结构 ----
            # <div data-e2e="msg-input" class="messageMsgInputcontainer">
            #   <div class="messageMsgInputinputRow">
            #     <div class="... messageEditorinputArea" data-slate-editor="true"
            #          contenteditable="true" data-placeholder="发送消息">
            #       <div class="ace-line">...
            'div[data-slate-editor="true"]',
            '[data-e2e="msg-input"] [contenteditable="true"]',
            'div[class*="messageEditorinputArea"]',
            'div[data-placeholder="发送消息"]',
            # ---- Draft.js 旧版兜底 ----
            '[data-e2e="message-input"] div[contenteditable="true"]',
            'div[contenteditable="true"][data-e2e*="input"]',
            'div[class*="messageMsgInput"] div[contenteditable="true"]',
            'div[class*="DraftEditor"] div[contenteditable="true"]',
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][data-contents="true"]',
            'div[contenteditable="true"]',
        ),
        "高", f"{_SRC_KEEPER}（三策略注入实测）",
        prefer="last",
        note="必须取**最后一个**可见 contenteditable：聊天框在弹层/DOM 顺序上都靠后",
    ),
    "send_button": _g(
        "send_button", "发送按钮",
        (
            # ⚠️ 实机结论：`.messageMsgInputinputAction` 是**图片/文件上传**按钮
            # （内含 input[type=file]），点它只会弹文件选择框——绝不能当发送按钮用。
            # 当前版本抖音私信是 **Enter 发送**（Shift+Enter 换行），所以这里只保留
            # 真正的「发送」按钮选择器；命不中时由 task_runner 按 Enter 提交。
            '[data-e2e="e2e-send-msg-btn"]',
            '[data-e2e="send-msg-btn"]',
            'button[class*="sendBtn"]',
            'div[class*="sendBtn"]',
            'button[class*="send-button"]',
            '[data-e2e*="send"][role="button"]',
            'div[class*="messageMsgInput"] button',
            'button[class*="send"]',
        ),
        "中", f"{_SRC_KEEPER} / {_SRC_PUBLIC}",
        note="兜底是「发送」文案 + Enter 键提交，见 TEXT_GROUPS['send_button_text']",
    ),
    "message_list": _g(
        "message_list", "消息气泡列表（发送结果校验用）",
        (
            # ---- 实机：.messageMessageListlist / [data-e2e="msg-item-content"] ----
            '[class*="messageMessageListlist"]',
            '[class*="messageMessageListwrapper"]',
            '[data-e2e="msg-item-content"]',
            '[data-e2e="message-list"]',
            '[class*="messageMessageList"]',
            '#messageContent [class*="messageList"]',
            '[class*="ChatMessageList"]',
            '[class*="messageList"]',
        ),
        "中", f"{_SRC_KEEPER}",
        note="校验「刚发的文案出现在列表里」是第二重证据（第一重是输入框清空）",
    ),
    "search_input": _g(
        "search_input", "会话/用户搜索框（按 sec_uid 或昵称找人）",
        (
            'input[data-e2e*="search"]',
            'input[placeholder*="搜索"]',
            'div[class*="search"] input',
        ),
        "低", f"{_SRC_KEEPER} / {_SRC_SELF}",
    ),
    "im_entry": _g(
        "im_entry", "用户主页的「私信」入口",
        (
            '[data-e2e="im-entry"]',
            '[data-e2e="message-entry"]',
            'button[class*="message"]',
        ),
        "中", _SRC_KEEPER,
        note="文案「私信」「发消息」在 TEXT_GROUPS 里",
    ),
}

#: 文本组：``querySelectorAll`` 不支持 ``:has-text()``，所以用 JS 扫可见节点文本。
#: 用法与 CSS 组一致（诊断报告里也会逐条列出命中数）。
TEXT_GROUPS: dict[str, SelectorGroup] = {
    "login_sms_entry_text": _g(
        "login_sms_entry_text", "短信登录入口（文案）",
        ("手机号登录", "验证码登录", "短信登录", "手机验证码登录", "手机号+验证码登录"),
        "中", f"{_SRC_KEEPER} / {_SRC_PUBLIC}",
    ),
    "login_send_code_text": _g(
        "login_send_code_text", "发送验证码按钮（文案）",
        ("发送验证码", "获取验证码", "发送短信", "重新发送", "重新获取"),
        "中", _SRC_PUBLIC,
    ),
    "login_agree_text": _g(
        "login_agree_text", "同意协议文案",
        ("我已阅读并同意", "用户协议", "隐私政策", "同意并继续"),
        "中", _SRC_SELF,
    ),
    "login_scan_success_text": _g(
        "login_scan_success_text", "「已扫码待确认」文案（第二档信号）",
        ("扫描成功", "扫码成功", "已扫码", "请在手机上确认", "请在手机端确认",
         "在手机上点击确认", "请在手机上点击确认", "确认登录"),
        "中", f"{_SRC_PUBLIC} / {_SRC_SELF}",
        note="命中任一即认为进入 scanned 状态；真机首次运行请用 diagnose 校准",
    ),
    "login_refresh_text": _g(
        "login_refresh_text", "二维码刷新文案",
        ("点击刷新", "刷新二维码", "二维码已失效", "二维码失效", "重新获取二维码"),
        "中", _SRC_SELF,
    ),
    "login_required_text": _g(
        "login_required_text", "未登录文案",
        ("扫码登录", "登录后查看", "立即登录", "请先登录", "登录抖音"),
        "中", _SRC_SELF,
    ),
    "send_button_text": _g(
        "send_button_text", "发送按钮文案",
        ("发送", "送出", "Send"),
        "中", _SRC_KEEPER,
    ),
    "im_entry_text": _g(
        "im_entry_text", "「私信」入口文案",
        ("私信", "发消息", "发送消息"),
        "中", _SRC_KEEPER,
    ),
}

#: 会被当作「平台拦截/发送失败」的页面提示原文（用于失败归类，不用于成功判定）
BLOCKER_PHRASES: tuple[str, ...] = (
    "不是好友", "不是对方好友", "无法发送", "发送失败", "操作频繁", "操作过于频繁",
    "请完成验证", "安全验证", "验证码", "账号异常", "登录已过期", "请先登录",
    "对方已设置隐私", "对方拒收", "消息已发出，但被对方拒收",
)


def group(name: str) -> SelectorGroup:
    """按名字取组（CSS 优先，其次文本组）。"""
    if name in GROUPS:
        return GROUPS[name]
    if name in TEXT_GROUPS:
        return TEXT_GROUPS[name]
    raise KeyError(f"未定义的选择器组: {name}")


def is_text_group(name: str) -> bool:
    return name in TEXT_GROUPS


def group_names(include_text: bool = True) -> list[str]:
    names = list(GROUPS)
    if include_text:
        names += list(TEXT_GROUPS)
    return names


def selector_snapshot(include_text: bool = True) -> dict[str, dict]:
    """给诊断报告用的快照，**保证报告与代码里的表永远一致**。"""
    out: dict[str, dict] = {}
    for name, item in GROUPS.items():
        out[name] = {"label": item.label, "kind": "css", "selectors": list(item.selectors),
                     "confidence": item.confidence, "source": item.source, "note": item.note,
                     "prefer": item.prefer}
    if include_text:
        for name, item in TEXT_GROUPS.items():
            out[name] = {"label": item.label, "kind": "text", "selectors": list(item.selectors),
                         "confidence": item.confidence, "source": item.source, "note": item.note,
                         "prefer": "first"}
    return out


# --------------------------------------------------------------------------
# 查询 JS
# --------------------------------------------------------------------------
def query_group_js() -> str:
    """一次性查询所有组：每组的每个选择器给 count / visible / sample / rect。

    诊断与「找不到好友」排障都靠它：一眼能区分「抖音改版了」和「页面压根没加载」。
    """
    return _js(
        "  const groups = payload.groups || [];"
        "  const out = {};"
        "  for (const g of groups) {"
        "    const rows = [];"
        "    for (const sel of (g.selectors || [])) {"
        "      const row = { selector: sel, total: 0, visible: 0, sample: '', rect: null,"
        "                    error: '' };"
        "      try {"
        "        let nodes;"
        "        if (g.kind === 'text') {"
        "          nodes = Array.from(document.querySelectorAll('div,span,button,a,p,li,label,h1,h2,h3'))"
        "            .filter((el) => { const t = (el.textContent || '').trim();"
        "              return t && t.length <= 24 && t.indexOf(sel) !== -1; });"
        "        } else {"
        "          nodes = Array.from(document.querySelectorAll(sel));"
        "        }"
        "        row.total = nodes.length;"
        "        for (const el of nodes) {"
        "          if (!isVisible(el)) continue;"
        "          row.visible += 1;"
        "          if (!row.sample) { row.sample = sampleOf(el); row.rect = rectOf(el); }"
        "        }"
        "      } catch (e) { row.error = String(e).slice(0, 120); }"
        "      rows.push(row);"
        "    }"
        "    out[g.name] = rows;"
        "  }"
        "  return { url: location.href, title: document.title, readyState: document.readyState,"
        "           groups: out, at: Date.now() };"
    )


def find_first_js() -> str:
    """按降级顺序找第一个可见元素，返回坐标与文本。

    返回 ``{found, selector, index, total, visible, text, rect}``；
    ``prefer='last'`` 时在同一族里取**最后一个**可见节点（聊天输入框用）。
    语义：**第一个「有可见命中」的选择器家族胜出**（不是第一个非空返回，
    见 ``visible_texts_js`` 的注释）。
    """
    return _js(
        "  const g = payload.group || {};"
        "  const preferLast = g.prefer === 'last';"
        "  for (const sel of (g.selectors || [])) {"
        "    let nodes = [];"
        "    try {"
        "      if (g.kind === 'text') {"
        "        nodes = Array.from(document.querySelectorAll('div,span,button,a,p,li,label'))"
        "          .filter((el) => { const t = (el.textContent || '').trim();"
        "            return t && t.length <= 24 && t.indexOf(sel) !== -1; });"
        "      } else {"
        "        nodes = Array.from(document.querySelectorAll(sel));"
        "      }"
        "    } catch (e) { continue; }"
        "    const visible = nodes.filter(isVisible);"
        "    if (!visible.length) continue;"
        "    const el = preferLast ? visible[visible.length - 1] : visible[0];"
        "    return { found: true, selector: sel, index: nodes.indexOf(el),"
        "      total: nodes.length, visible: visible.length, text: sampleOf(el),"
        "      rect: rectOf(el), tag: el.tagName, editable: !!el.isContentEditable };"
        "  }"
        "  return { found: false, selector: '', index: -1, total: 0, visible: 0, text: '',"
        "           rect: null };"
    )


def visible_texts_js() -> str:
    """把一组选择器命中的**可见文本**全取回来（取条目最多的那一族）。

    为什么不「第一个非空就返回」：改版后旧选择器往往**仍然能选中几个零散节点**
    （吸顶栏、推荐位），第一个非空就返回会让整个列表错位成噪音，而真正命中的
    选择器根本没被尝试 —— 现象就是「登录成功但找不到好友」，极难自查。
    """
    return _js(
        "  const g = payload.group || {};"
        "  const limit = payload.limit || 200;"
        "  let best = [];"
        "  let bestSelector = '';"
        "  for (const sel of (g.selectors || [])) {"
        "    let nodes = [];"
        "    try {"
        "      if (g.kind === 'text') {"
        "        nodes = Array.from(document.querySelectorAll('div,span,button,a,p,li,label'))"
        "          .filter((el) => { const t = (el.textContent || '').trim();"
        "            return t && t.length <= 24 && t.indexOf(sel) !== -1; });"
        "      } else {"
        "        nodes = Array.from(document.querySelectorAll(sel));"
        "      }"
        "    } catch (e) { continue; }"
        "    const texts = [];"
        "    for (const el of nodes) {"
        "      if (!isVisible(el)) continue;"
        "      const text = sampleOf(el);"
        "      if (text) texts.push(text);"
        "      if (texts.length >= limit) break;"
        "    }"
        "    if (texts.length > best.length) { best = texts; bestSelector = sel; }"
        "  }"
        "  return { texts: best, count: best.length, selector: bestSelector };"
    )


def scroll_list_top_js() -> str:
    """把会话列表滚回**顶部**。

    为什么需要：`harvest_conversations()` 为了抓全量会把列表滚到底，之后
    `open_conversation()` 再去找好友时，顶部的人已经被虚拟列表移出 DOM ——
    实测就是这个顺序导致「明明有七小只却报找不到」。
    """
    return _js(
        "  const pick = (el) => {"
        "    let node = el;"
        "    while (node && node !== document.body) {"
        "      if (node.scrollHeight > node.clientHeight + 10) return node;"
        "      node = node.parentElement;"
        "    }"
        "    return null;"
        "  };"
        "  let row = null;"
        "  for (const sel of (payload.items || [])) {"
        "    let nodes = [];"
        "    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }"        "    const visible = nodes.filter(isVisible);"
        "    if (visible.length) { row = visible[0]; break; }"
        "  }"
        "  if (!row) return { ok: false, reason: 'no_row' };"
        "  const box = pick(row);"
        "  if (!box) return { ok: false, reason: 'no_scrollable' };"
        "  box.scrollTop = 0;"
        "  return { ok: true, top: Math.round(box.scrollTop) };"
    )


def open_conversation_js() -> str:
    """在会话列表里点开某个昵称对应的会话（找不到就返回 ``found=False``）。

    为什么必须有这一步：抖音私信页**默认不选中任何会话**，右侧输入框根本
    不存在。旧实现直接往「输入框」里写字，于是四种策略全部失败，报
    ``input_failed``。

    ⚠️ 实机（2026-10 登录态）考察到的真实结构::

        <div data-e2e="conversation-item" class="conversationConversationItemwrapper">
          <div class="…ItemrowArea1"><img/></div>
          <div class="…ItemrowArea2">
            <div class="…ItemtitleWrapper">
              <div class="conversationConversationItemtitle">李洪亮的主人</div>
              <div class="ConversationItemTagNextToTitlewrapper">🔥366 21:13</div>
            </div>
            <div>李墨凡：[分享视频]</div>
          </div>
        </div>

    所以：**只能点整行 wrapper**（React 的 onClick 挂在这里）。之前取「包含昵称的
    最短元素」，点到的是里面的昵称 div —— 点击不生效，面板永远不出现。
    这里显式排除「祖先也是会话行」的嵌套元素。
    """
    return _js(
        "  const want = String(payload.nickname || '').trim();"
        "  if (!want) return { found: false, reason: 'no_nickname' };"
        "  const g = payload.group || {};"
        "  let candidates = [];"
        "  let bestSelector = '';"
        "  for (const sel of (g.selectors || [])) {"
        "    let nodes = [];"
        "    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }"
        "    nodes = nodes.filter(isVisible);"
        "    if (!nodes.length) continue;"
        "    candidates = nodes.filter((el) => !nodes.some((other) => other !== el && other.contains(el)));"
        "    bestSelector = sel;"
        "    break;"
        "  }"
        "  if (!candidates.length) return { found: false, reason: 'no_rows' };"
        "  let best = null;"
        "  let bestLen = Infinity;"
        "  for (const el of candidates) {"
        "    const text = String(el.innerText || el.textContent || '').trim();"
        "    if (!text || text.indexOf(want) === -1) continue;"
        "    if (text.length < bestLen) { best = el; bestLen = text.length; }"
        "  }"
        "  if (!best) return { found: false, reason: 'no_match', rows: candidates.length };"
        "  try { best.scrollIntoView({ block: 'center' }); } catch (e) {}"
        "  const rect = rectOf(best);"
        "  let clicked = false;"
        "  try { best.click(); clicked = true; } catch (e) { clicked = false; }"
        "  return { found: true, clicked: clicked, selector: bestSelector,"
        "           text: sampleOf(best), rect: rect };"
    )


def chat_header_js() -> str:
    """读聊天面板顶部当前会话的标题（用于确认「打开的是对的人」）。"""
    return _js(
        "  const out = { found: false, text: '', selector: '' };"
        "  for (const sel of (payload.selectors || [])) {"
        "    let nodes = [];"
        "    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }"
        "    const visible = nodes.filter(isVisible);"
        "    if (!visible.length) continue;"
        "    const text = String(visible[0].innerText || visible[0].textContent || '').trim();"
        "    if (!text) continue;"
        "    out.found = true; out.text = text.slice(0, 120); out.selector = sel;"
        "    return out;"
        "  }"
        "  return out;"
    )


def click_first_js(native: bool = True) -> str:
    """点击第一个可见匹配。

    ``native=True`` 用 ``el.click()``：它派发的是**真实 DOM 事件**，
    React 的根节点委托监听能收到，所以对抖音的按钮有效（比 ``dispatchEvent``
    构造的合成事件更接近真人）。点击失败时调用方应改用 CDP 坐标点击兜底。
    """
    body = (
        "  const g = payload.group || {};"
        "  const preferLast = g.prefer === 'last';"
        "  for (const sel of (g.selectors || [])) {"
        "    let nodes = [];"
        "    try {"
        "      if (g.kind === 'text') {"
        "        nodes = Array.from(document.querySelectorAll('div,span,button,a,p,li,label'))"
        "          .filter((el) => { const t = (el.textContent || '').trim();"
        "            return t && t.length <= 24 && t.indexOf(sel) !== -1; });"
        "      } else {"
        "        nodes = Array.from(document.querySelectorAll(sel));"
        "      }"
        "    } catch (e) { continue; }"
        "    const visible = nodes.filter(isVisible);"
        "    if (!visible.length) continue;"
        "    const el = preferLast ? visible[visible.length - 1] : visible[0];"
        "    const rect = rectOf(el);"
        "    try { el.scrollIntoView({ block: 'center' }); } catch (e) {}"
        "    let clicked = false;"
        "    if (payload.native !== false) {"
        "      try { el.click(); clicked = true; } catch (e) { clicked = false; }"
        "    }"
        "    return { clicked: clicked, selector: sel, text: sampleOf(el), rect: rect };"
        "  }"
        "  return { clicked: false, selector: '', text: '', rect: null };"
    )
    return _js(body)


def set_input_value_js() -> str:
    """给原生 ``<input>`` 写值（短信手机号/验证码用）。

    必须走 ``HTMLInputElement.prototype.value`` 的 setter 再派发 ``input``
    事件：直接 ``el.value = x`` 不会触发 React 的 onChange（React 劫持了
    value 的 setter，只有原生 setter + input 事件才会被识别）。
    """
    return _js(
        "  const g = payload.group || {};"
        "  const value = payload.value == null ? '' : String(payload.value);"
        "  for (const sel of (g.selectors || [])) {"
        "    let nodes = [];"
        "    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }"
        "    const visible = nodes.filter(isVisible);"
        "    if (!visible.length) continue;"
        "    const el = visible[0];"
        "    try {"
        "      el.focus();"
        "      const proto = el.tagName === 'TEXTAREA'"
        "        ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;"
        "      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
        "      setter.call(el, value);"
        "      el.dispatchEvent(new Event('input', { bubbles: true }));"
        "      el.dispatchEvent(new Event('change', { bubbles: true }));"
        "    } catch (e) { return { ok: false, selector: sel, reason: String(e).slice(0, 120) }; }"
        "    return { ok: true, selector: sel, value: el.value, rect: rectOf(el) };"
        "  }"
        "  return { ok: false, selector: '', reason: 'no_visible_input' };"
    )


def scan_blockers_js() -> str:
    """扫描页面上「不是好友/发送失败/要验证」这类提示。

    失败时用它给出可读懂的原因，而不是笼统报「发送失败」。
    """
    return _js(
        "  const phrases = payload.phrases || [];"
        "  const found = [];"
        "  for (const el of document.querySelectorAll('div,span,p,section,li')) {"
        "    if (!isVisible(el)) continue;"
        "    const text = String(el.innerText || '').trim();"
        "    if (!text || text.length > 40) continue;"
        "    for (const phrase of phrases) {"
        "      if (text.indexOf(phrase) !== -1) { found.push(text); break; }"
        "    }"
        "    if (found.length >= 5) break;"
        "  }"
        "  return Array.from(new Set(found)).slice(0, 5);"
    )


def draft_paste_js() -> str:
    """策略 2：构造 ``ClipboardEvent('paste')`` 触发 Draft.js 的 onChange。

    Draft.js 用 ``beforeinput``/``paste`` 同步内部 EditorState，单纯改 DOM
    文本它不认（下次 onChange 会把 DOM 覆盖回去），所以必须派发真事件。
    """
    return _js_editor(
        "  const g = payload.group || {};"
        "  const text = payload.text == null ? '' : String(payload.text);"
        "  let editors = [];"
        "  for (const sel of (g.selectors || [])) {"
        "    try { editors = Array.from(document.querySelectorAll(sel)).filter(isVisible); }"
        "    catch (e) { continue; }"
        "    if (editors.length) break;"
        "  }"
        "  if (!editors.length) return { ok: false, reason: 'no_visible_contenteditable' };"
        "  const el = g.prefer === 'last' ? editors[editors.length - 1] : editors[0];"
        "  try { el.scrollIntoView({ block: 'center' }); } catch (e) {}"
        "  try { el.focus(); } catch (e) {}"
        "  try {"
        "    const range = document.createRange();"
        "    range.selectNodeContents(el);"
        "    range.collapse(false);"
        "    const selection = window.getSelection();"
        "    selection.removeAllRanges();"
        "    selection.addRange(range);"
        "  } catch (e) {}"
        "  try {"
        "    const dt = new DataTransfer();"
        "    dt.setData('text/plain', text);"
        "    const event = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true,"
        "      cancelable: true, composed: true });"
        "    const notPrevented = el.dispatchEvent(event);"
        "    return { ok: true, handled: !notPrevented, value: readEditor(el) };"
        "  } catch (e) { return { ok: false, reason: 'paste_throw:' + String(e) }; }"
    )


def exec_insert_js() -> str:
    """策略 3：``document.execCommand('insertText')``（Chromium 仍支持，会触发 input）。"""
    return _js_editor(
        "  const g = payload.group || {};"
        "  const text = payload.text == null ? '' : String(payload.text);"
        "  let editors = [];"
        "  for (const sel of (g.selectors || [])) {"
        "    try { editors = Array.from(document.querySelectorAll(sel)).filter(isVisible); }"
        "    catch (e) { continue; }"
        "    if (editors.length) break;"
        "  }"
        "  if (!editors.length) return { ok: false, reason: 'no_visible_contenteditable' };"
        "  const el = g.prefer === 'last' ? editors[editors.length - 1] : editors[0];"
        "  try { el.focus(); } catch (e) {}"
        "  let ok = false;"
        "  try { ok = document.execCommand('insertText', false, text); }"
        "  catch (e) { return { ok: false, reason: 'exec_throw:' + String(e) }; }"
        "  return { ok: ok, value: readEditor(el) };"
    )


def read_editor_js() -> str:
    """读输入框当前文本（Draft.js 内容在 ``[data-contents]`` 子节点里，innerText 最稳）。"""
    return _js_editor(
        "  const g = payload.group || {};"
        "  let editors = [];"
        "  for (const sel of (g.selectors || [])) {"
        "    try { editors = Array.from(document.querySelectorAll(sel)).filter(isVisible); }"
        "    catch (e) { continue; }"
        "    if (editors.length) break;"
        "  }"
        "  if (!editors.length) return { found: false, value: '' };"
        "  const el = g.prefer === 'last' ? editors[editors.length - 1] : editors[0];"
        "  return { found: true, value: readEditor(el) };"
    )


def clear_editor_js() -> str:
    """清空输入框（全选后 delete，Draft.js 会同步 EditorState）。"""
    return _js_editor(
        "  const g = payload.group || {};"
        "  let editors = [];"
        "  for (const sel of (g.selectors || [])) {"
        "    try { editors = Array.from(document.querySelectorAll(sel)).filter(isVisible); }"
        "    catch (e) { continue; }"
        "    if (editors.length) break;"
        "  }"
        "  if (!editors.length) return { ok: false, reason: 'no_editor' };"
        "  const el = g.prefer === 'last' ? editors[editors.length - 1] : editors[0];"
        "  try { el.focus(); } catch (e) {}"
        "  try {"
        "    const range = document.createRange();"
        "    range.selectNodeContents(el);"
        "    const selection = window.getSelection();"
        "    selection.removeAllRanges();"
        "    selection.addRange(range);"
        "    document.execCommand('delete', false, null);"
        "  } catch (e) { return { ok: false, reason: String(e).slice(0, 120) }; }"
        "  return { ok: true, value: readEditor(el) };"
    )


def last_message_js() -> str:
    """在消息列表里找目标文案（发送成功的第二重证据）。"""
    return _js(
        "  const group = payload.group || {};"
        "  const needle = payload.needle || '';"
        "  const result = { matched: false, selector: '', itemCount: 0, sample: '', found: false };"
        "  for (const sel of (group.selectors || [])) {"
        "    let nodes = [];"
        "    try { nodes = Array.from(document.querySelectorAll(sel)).filter(isVisible); }"
        "    catch (e) { continue; }"
        "    if (!nodes.length) continue;"
        "    const text = nodes.map((el) => String(el.innerText || '').trim())"
        "      .filter(Boolean).join('\\n');"
        "    if (!text) continue;"
        "    result.found = true;"
        "    if (!result.selector) { result.selector = sel; result.itemCount = nodes.length;"
        "      result.sample = text.slice(-120); }"
        "    if (needle && text.indexOf(needle) !== -1) {"
        "      result.matched = true; result.selector = sel; result.itemCount = nodes.length;"
        "      result.sample = text.slice(-120); return result;"
        "    }"
        "  }"
        "  return result;"
    )


def scroll_list_js() -> str:
    """把会话列表容器滚到底（列表只渲染首屏，新会话在底部）。"""
    return _js(
        "  const group = payload.group || {};"
        "  const items = payload.items || [];"
        "  let anchor = null;"
        "  for (const sel of items) {"
        "    try { const nodes = Array.from(document.querySelectorAll(sel)).filter(isVisible);"
        "      if (nodes.length) { anchor = nodes[0]; break; } } catch (e) {}"
        "  }"
        "  if (!anchor) {"
        "    for (const sel of (group.selectors || [])) {"
        "      try { const nodes = Array.from(document.querySelectorAll(sel)).filter(isVisible);"
        "        if (nodes.length) { anchor = nodes[0]; break; } } catch (e) {}"
        "    }"
        "  }"
        "  if (!anchor) return { ok: false, reason: 'no_anchor' };"
        "  let el = anchor;"
        "  while (el && el !== document.body && el.parentElement) {"
        "    const st = window.getComputedStyle(el);"
        "    if ((st.overflowY === 'auto' || st.overflowY === 'scroll')"
        "        && el.scrollHeight > el.clientHeight + 20) break;"
        "    el = el.parentElement;"
        "  }"
        "  if (!el || el === document.body) return { ok: false, reason: 'no_scrollable_ancestor' };"
        "  const before = el.scrollTop;"
        "  el.scrollTop = el.scrollHeight;"
        "  el.dispatchEvent(new Event('scroll', { bubbles: true }));"
        "  return { ok: true, moved: el.scrollTop !== before, scrollTop: el.scrollTop,"
        "           scrollHeight: el.scrollHeight };"
    )


def read_login_user_js() -> str:
    """读登录后的用户信息（昵称/头像/uid 尝试）。

    昵称优先从 ``login_user_info`` 的文本取；``uid`` 是尽力而为——抖音把
    ``uid`` 放在 ``window.__INIT_PROPS__`` 之类的内联 JSON 里，各版本字段名不同，
    取不到就返回空串（**不要**编造）。
    """
    return _js(
        "  const group = payload.group || {};"
        "  let nickname = '';"
        "  let avatar = '';"
        "  let selector = '';"
        "  for (const sel of (group.selectors || [])) {"
        "    let nodes = [];"
        "    try { nodes = Array.from(document.querySelectorAll(sel)).filter(isVisible); }"
        "    catch (e) { continue; }"
        "    if (!nodes.length) continue;"
        "    selector = sel;"
        "    const el = nodes[0];"
        "    if (el.tagName === 'IMG') { avatar = el.src || ''; nickname = el.alt || ''; }"
        "    else {"
        "      const img = el.querySelector ? el.querySelector('img') : null;"
        "      if (img) avatar = img.src || '';"
        "      nickname = sampleOf(el);"
        "    }"
        "    break;"
        "  }"
        "  let uid = '';"
        "  try {"
        "    const html = document.documentElement.innerHTML;"
        "    const m = html.match(/\"(?:uid|user_id|userId|sec_uid)\"\\s*:\\s*\"([A-Za-z0-9_\\-]{6,64})\"/);"
        "    if (m) uid = m[1];"
        "  } catch (e) {}"
        "  const loggedOut = (payload.loginRequired || []).some((sel) => {"
        "    try { return Array.from(document.querySelectorAll(sel)).some(isVisible); }"
        "    catch (e) { return false; }"
        "  });"
        "  return { nickname: nickname, avatar: avatar, uid: uid, selector: selector,"
        "           loggedOut: loggedOut, url: location.href };"
    )


# --------------------------------------------------------------------------
# 高层封装：在活的 CDP 会话上执行
# --------------------------------------------------------------------------
def payload_for(name: str, **extra: Any) -> dict:
    """组装 JS 入参（统一 kind/prefer，避免调用方每处都重复拼）。"""
    item = group(name)
    payload = {
        "group": {
            "name": item.name,
            "kind": "text" if is_text_group(name) else "css",
            "selectors": list(item.selectors),
            "prefer": item.prefer,
        },
    }
    payload.update(extra)
    return payload


def find_first(cdp: Any, name: str, *, timeout: float | None = None) -> dict | None:
    """按降级顺序找第一个可见元素；找不到返回 ``None``。"""
    result = cdp.call(find_first_js(), payload_for(name), timeout=timeout)
    if isinstance(result, dict) and result.get("found"):
        return result
    return None


def visible_texts(cdp: Any, name: str, *, limit: int = 200,
                  timeout: float | None = None) -> list[str]:
    result = cdp.call(visible_texts_js(), payload_for(name, limit=int(limit)), timeout=timeout)
    if isinstance(result, dict):
        return [str(t) for t in (result.get("texts") or [])]
    return []


def text_present(cdp: Any, name: str, *, timeout: float | None = None) -> bool:
    """文本组里有没有出现（用于「已扫码待确认」这类多信号判定）。"""
    return bool(visible_texts(cdp, name, limit=5, timeout=timeout))


def click_first(cdp: Any, name: str, *, native: bool = True,
                timeout: float | None = None) -> dict | None:
    """用 ``el.click()`` 点第一个可见匹配；点完再检查一次可见性由调用方决定。"""
    result = cdp.call(click_first_js(native=native), payload_for(name, native=bool(native)),
                      timeout=timeout)
    if isinstance(result, dict) and result.get("clicked"):
        return result
    return None


def click_at(cdp: Any, rect: dict | None) -> bool:
    """用 CDP 真实鼠标事件点击坐标（``el.click()`` 无效时的兜底）。"""
    if not rect:
        return False
    try:
        cdp.click(float(rect.get("cx") or 0), float(rect.get("cy") or 0))
        return True
    except Exception:            # noqa: BLE001 - 点击失败由调用方决定是否重试
        return False


def click_group(cdp: Any, name: str, *, timeout: float | None = None) -> bool:
    """两级点击：先 ``el.click()``，失败再按坐标用真实鼠标事件点。"""
    hit = click_first(cdp, name, native=True, timeout=timeout)
    if hit:
        return True
    found = find_first(cdp, name, timeout=timeout)
    if found and click_at(cdp, found.get("rect")):
        return True
    hit = click_first(cdp, name, native=True, timeout=timeout)
    return bool(hit)


def set_input_value(cdp: Any, name: str, value: str, *, timeout: float | None = None) -> dict:
    result = cdp.call(set_input_value_js(), payload_for(name, value=value), timeout=timeout)
    return result if isinstance(result, dict) else {"ok": False, "reason": "bad_result"}


def read_editor(cdp: Any, name: str = "message_input", *, timeout: float | None = None) -> str:
    result = cdp.call(read_editor_js(), payload_for(name), timeout=timeout)
    if isinstance(result, dict):
        return str(result.get("value") or "")
    return ""


def format_report(report: dict[str, list[dict]]) -> str:
    """把查询结果排成人能读的多行文本（命中的排前面）。"""
    lines: list[str] = []
    for name, rows in (report or {}).items():
        best = max((int(r.get("visible") or 0) for r in rows), default=0)
        lines.append(f"[{name}] 最多可见 {best} 个")
        for row in sorted(rows, key=lambda r: -int(r.get("visible") or 0)):
            if not row.get("visible"):
                continue
            lines.append(f"    {int(row.get('visible') or 0):>3} 可见 / "
                         f"{int(row.get('total') or 0):>3} 命中  {row.get('selector')}"
                         f"   样本={row.get('sample')!r}")
            if len(lines) > 400:
                return "\n".join(lines) + "\n...(已截断)"
    return "\n".join(lines) if lines else "（所有选择器都没有可见命中）"


def build_query_payload(include_text: bool = True) -> dict:
    """给 ``query_group_js`` 的参数（诊断与排障共用）。"""
    groups = []
    for name, item in GROUPS.items():
        groups.append({"name": name, "kind": "css", "selectors": list(item.selectors),
                       "prefer": item.prefer})
    if include_text:
        for name, item in TEXT_GROUPS.items():
            groups.append({"name": name, "kind": "text", "selectors": list(item.selectors),
                           "prefer": "first"})
    return {"groups": groups}
