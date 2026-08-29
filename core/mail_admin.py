"""mail.apisaver 管理端邮箱清理适配器。"""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


def delete_email(email: str, *, reason: str = "") -> dict[str, Any]:
    from config import email as cfg
    if not bool(getattr(cfg, "MAIL_ADMIN_DELETE_FAILED", False)):
        return {"ok": False, "skipped": True, "reason": "disabled"}
    target = str(email or "").strip()
    if not target:
        return {"ok": False, "skipped": True, "reason": "empty_email"}
    base = str(getattr(cfg, "MAIL_ADMIN_BASE", "") or "").strip().rstrip("/")
    path = str(getattr(cfg, "MAIL_ADMIN_DELETE_PATH", "") or "").strip()
    if not base or not path:
        raise ValueError("MAIL_ADMIN_BASE/MAIL_ADMIN_DELETE_PATH 未配置")
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "turb-gpt-free-register/mail-admin"}
    key = str(getattr(cfg, "MAIL_ADMIN_AUTH_KEY", "") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    password = str(getattr(cfg, "MAIL_ADMIN_PASSWORD", "") or "").strip()
    body = {"email": target}
    if password:
        body["password"] = password
    response = requests.post(f"{base}{path if path.startswith('/') else '/' + path}", headers=headers, json=body, timeout=max(3, int(getattr(cfg, "MAIL_ADMIN_TIMEOUT", 20) or 20)))
    try:
        payload = response.json()
    except ValueError:
        payload = {"text": response.text[:300]}
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"mail 管理删除失败 HTTP {response.status_code}: {payload}")
    logger.info("[MailAdmin] 已删除失败邮箱: email=%s", target)
    return {"ok": True, "status_code": response.status_code, "response": payload, "reason": reason}

