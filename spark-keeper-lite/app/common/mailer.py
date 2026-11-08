"""邮件与短信发送：统一走 ``message_outbox`` 队列。

* 邮件用标准库 ``smtplib``（SSL/STARTTLS 都支持）。
* 短信目前内置「不真发，只记日志」的开发实现 + 阿里云风格 HTTP 接口占位，
  自托管使用者可按需替换 ``send_sms`` 里的 provider 分支。
* 未配置 SMTP 时**不报错**：写入 outbox 并打印到控制台，方便本地调试注册流程。
"""

from __future__ import annotations

import json
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from ..config import get_settings
from . import logging as logmod
from . import timeutil


def enqueue(channel: str, target: str, subject: str = "", body: str = "",
            scene: str = "", delay_seconds: int = 0) -> int:
    """入队。返回 outbox id。"""
    from .. import db

    scheduled = timeutil.to_db(timeutil.now_utc() + timeutil.timedelta(seconds=max(0, delay_seconds)))
    return db.insert(
        "message_outbox",
        {
            "channel": channel,
            "target": target,
            "subject": subject[:200],
            "body": body,
            "scene": scene[:40],
            "status": "pending",
            "scheduled_at": scheduled,
            "created_at": timeutil.utc_now_str(),
        },
    )


def _smtp_configured() -> bool:
    cfg = get_settings()
    return bool(cfg.smtp_host and (cfg.smtp_from or cfg.smtp_user))


def send_email(target: str, subject: str, body: str) -> tuple[bool, str]:
    cfg = get_settings()
    if not _smtp_configured():
        logmod.log(
            f"[邮件未配置] 发往 {target} 的「{subject}」内容：{body[:200]}",
            level="warning",
            source="common.mailer",
        )
        return False, "SMTP 未配置（邮件内容已写入日志）"
    sender = cfg.smtp_from or cfg.smtp_user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("抖音续火花系统", "utf-8")), sender))
    msg["To"] = target
    msg["Date"] = formatdate(localtime=True)
    try:
        if cfg.smtp_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15, context=context) as smtp:
                if cfg.smtp_user:
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                smtp.sendmail(sender, [target], msg.as_string())
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as smtp:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if cfg.smtp_user:
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                smtp.sendmail(sender, [target], msg.as_string())
        return True, ""
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def send_sms(target: str, body: str) -> tuple[bool, str]:
    cfg = get_settings()
    if not cfg.sms_provider:
        logmod.log(
            f"[短信未配置] 发往 {target} 的内容：{body[:120]}",
            level="warning",
            source="common.mailer",
        )
        return False, "短信通道未配置（内容已写入日志）"
    if cfg.sms_provider == "http":       # 通用 HTTP 网关（POST form）
        endpoint = cfg.sms_access_key       # access_key 复用为网关地址
        if not endpoint:
            return False, "短信网关地址未配置"
        data = urllib.parse.urlencode(
            {"phone": target, "content": body, "sign": cfg.sms_sign, "key": cfg.sms_secret_key}
        ).encode()
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                payload = resp.read(2000).decode("utf-8", "replace")
            ok = resp.status == 200
            return ok, "" if ok else f"网关返回 {resp.status}: {payload[:200]}"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
    return False, f"未实现的短信通道：{cfg.sms_provider}"


def flush(limit: int = 20) -> dict:
    """处理待发队列（调度器定期调用；也可由管理员手动触发）。"""
    from .. import db

    now = timeutil.utc_now_str()
    rows = db.query_all(
        "SELECT * FROM message_outbox WHERE status='pending' AND scheduled_at<=?"
        " ORDER BY id LIMIT ?",
        (now, int(limit)),
    )
    stats = {"sent": 0, "failed": 0, "checked": len(rows)}
    for row in rows:
        if row["channel"] == "email":
            ok, err = send_email(row["target"], row["subject"], row["body"])
        else:
            ok, err = send_sms(row["target"], row["body"])
        db.update(
            "message_outbox",
            {
                "status": "sent" if ok else "failed",
                "attempts": int(row["attempts"] or 0) + 1,
                "last_error": err[:500],
                "sent_at": now if ok else None,
            },
            "id=?",
            (row["id"],),
        )
        stats["sent" if ok else "failed"] += 1
    return stats


# --------------------------------------------------------------------------
# 业务模板
# --------------------------------------------------------------------------
def send_verify_code_email(target: str, code: str, scene: str = "register") -> tuple[bool, str]:
    titles = {
        "register": "注册验证码",
        "reset_password": "重置密码验证码",
        "login": "登录验证码",
        "bind": "绑定验证码",
    }
    title = titles.get(scene, "验证码")
    body = (
        f"您好：\n\n"
        f"您的{title}是：{code}\n"
        f"有效期 10 分钟，请尽快完成验证。\n\n"
        f"如果这不是您本人的操作，请忽略本邮件。\n"
        f"—— 抖音续火花系统"
    )
    enqueue("email", target, subject=f"【抖音续火花系统】{title}", body=body, scene=scene)
    return flush(5)["sent"] > 0, "已加入发送队列"


def send_verify_code_sms(target: str, code: str, scene: str = "register") -> tuple[bool, str]:
    templates = {
        "register": "您的注册验证码为 {code}，10 分钟内有效。",
        "reset_password": "您的重置密码验证码为 {code}，10 分钟内有效。",
        "login": "您的登录验证码为 {code}，10 分钟内有效。",
        "bind": "您的绑定验证码为 {code}，10 分钟内有效。",
    }
    body = templates.get(scene, templates["register"]).format(code=code)
    enqueue("sms", target, body=body, scene=scene)
    return flush(5)["sent"] > 0, "已加入发送队列"


def health() -> dict:
    cfg = get_settings()
    from .. import db

    pending = int(db.query_scalar("SELECT COUNT(*) FROM message_outbox WHERE status='pending'", (), 0) or 0)
    failed = int(db.query_scalar("SELECT COUNT(*) FROM message_outbox WHERE status='failed'", (), 0) or 0)
    return {
        "smtp_configured": _smtp_configured(),
        "smtp_host": cfg.smtp_host,
        "sms_provider": cfg.sms_provider or "",
        "pending": pending,
        "failed": failed,
    }


def _json_safe(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
