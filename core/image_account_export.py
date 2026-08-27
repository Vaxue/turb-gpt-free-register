# -*- coding: utf-8 -*-
"""把注册成功的 ChatGPT access token 导入 image.apisaver 账号池。"""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


def auto_import_image_account(
    access_token: str,
    *,
    email: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """调用 chatgpt2api 管理接口导入一个账号并刷新其状态。"""
    from config import image_api as cfg

    if not bool(getattr(cfg, "IMAGE_API_AUTO_IMPORT", False)):
        return {"ok": False, "skipped": True, "reason": "disabled"}

    token = str(access_token or "").strip()
    auth_key = str(getattr(cfg, "IMAGE_API_AUTH_KEY", "") or "").strip()
    if not token:
        raise ValueError("access_token 为空")
    if not auth_key:
        raise ValueError("IMAGE_API_AUTH_KEY 未配置")

    base = str(getattr(cfg, "IMAGE_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ValueError("IMAGE_API_BASE 未配置")
    url = f"{base}/api/accounts"
    headers = {
        "Authorization": f"Bearer {auth_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "turb-gpt-free-register/image-account-export",
    }
    request_timeout = float(timeout or getattr(cfg, "IMAGE_API_TIMEOUT", 20) or 20)
    response = requests.post(url, headers=headers, json={"tokens": [token]}, timeout=request_timeout)
    try:
        body: Any = response.json()
    except ValueError:
        body = {"text": response.text[:500]}
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"image.apisaver 导入失败 HTTP {response.status_code}: {str(body)[:500]}")
    result = {"ok": True, "url": url, "status_code": response.status_code, "email": email or "", "response": body}
    logger.info("[ImageAPI] 账号已自动导入 image.apisaver: email=%s status=%s", email or "-", response.status_code)
    return result
