"""image.apisaver 额度监控和低负载自动注册控制器。"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_state: dict[str, Any] = {
    "enabled": False, "running": False, "quota": None, "cpu_percent": None,
    "workers": None, "last_check_at": None, "last_trigger_at": None,
    "last_error": "", "last_trigger": None, "last_live_check_at": None,
    "last_live_check": None, "live_cursor": 0,
    "last_midnight_cleanup_at": None, "last_midnight_cleanup": None,
}


def _cfg():
    from config import image_api
    return image_api


def _quota_url(cfg) -> str:
    explicit = str(getattr(cfg, "IMAGE_API_QUOTA_URL", "") or "").strip()
    if explicit:
        return explicit
    base = str(getattr(cfg, "IMAGE_API_BASE", "") or "").strip().rstrip("/")
    path = str(getattr(cfg, "IMAGE_API_QUOTA_PATH", "/api/accounts") or "/api/accounts").strip()
    return f"{base}{path if path.startswith('/') else '/' + path}"


def _extract_quota(value: Any) -> float | None:
    """从常见 API 响应中递归读取 quota/balance/credits/remaining。"""
    keys = ("quota", "balance", "credits", "remaining", "available_credits", "availableQuota")
    if isinstance(value, dict):
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return float(raw)
            if isinstance(raw, str):
                try:
                    return float(raw.replace(",", "").strip())
                except ValueError:
                    pass
        for child in value.values():
            found = _extract_quota(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        found_values = []
        for child in value:
            found = _extract_quota(child)
            if found is not None:
                found_values.append(found)
        if found_values:
            # 账号列表通常每项带有独立余额，监控应使用池总额度。
            return float(sum(found_values))
    return None


def _cpu_percent() -> float:
    try:
        import psutil
        return float(psutil.cpu_percent(interval=0.2))
    except Exception:
        try:
            load = os.getloadavg()[0]
            cpus = max(1, os.cpu_count() or 1)
            return min(100.0, float(load) * 100.0 / cpus)
        except Exception:
            return 0.0


def _scaled_workers(cfg, cpu: float) -> int:
    low = max(1, int(getattr(cfg, "IMAGE_API_REGISTER_WORKERS_MIN", 1) or 1))
    high = max(low, int(getattr(cfg, "IMAGE_API_REGISTER_WORKERS_MAX", 4) or 4))
    idle = float(getattr(cfg, "IMAGE_API_CPU_IDLE_PERCENT", 35) or 35)
    busy = max(idle + 1, float(getattr(cfg, "IMAGE_API_CPU_BUSY_PERCENT", 80) or 80))
    if cpu <= idle:
        return high
    if cpu >= busy:
        return low
    ratio = (busy - cpu) / (busy - idle)
    return max(low, min(high, int(round(low + (high - low) * ratio))))


def fetch_quota() -> dict[str, Any]:
    cfg = _cfg()
    url = _quota_url(cfg)
    if not url:
        raise RuntimeError("IMAGE_API_BASE/IMAGE_API_QUOTA_URL 未配置")
    key = str(getattr(cfg, "IMAGE_API_AUTH_KEY", "") or "").strip()
    headers = {"Accept": "application/json", "User-Agent": "turb-gpt-free-register/quota-monitor"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    response = requests.get(url, headers=headers, timeout=max(3, int(getattr(cfg, "IMAGE_API_TIMEOUT", 20) or 20)))
    response.raise_for_status()
    body = response.json()
    quota = _extract_quota(body)
    if quota is None:
        raise RuntimeError("额度接口响应中未找到 quota/balance/credits/remaining 数值")
    return {"url": url, "quota": quota, "response": body}


def _active_jobs() -> int:
    try:
        from core import db
        return sum(1 for row in db.list_jobs(limit=1000) if row.get("status") in ("pending", "running", "stopping"))
    except Exception:
        return 0


def check_once(*, trigger_register: bool = True) -> dict[str, Any]:
    cfg = _cfg()
    cpu = _cpu_percent()
    now = time.time()
    result: dict[str, Any] = {"cpu_percent": cpu, "active_jobs": _active_jobs()}
    try:
        result.update(fetch_quota())
        with _lock:
            _state.update({"quota": result["quota"], "cpu_percent": cpu, "last_check_at": now, "last_error": ""})
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        with _lock:
            _state.update({"cpu_percent": cpu, "last_check_at": now, "last_error": result["error"]})
        return result

    threshold = float(getattr(cfg, "IMAGE_API_QUOTA_THRESHOLD", 1) or 1)
    idle_limit = float(getattr(cfg, "IMAGE_API_CPU_IDLE_PERCENT", 35) or 35)
    cooldown = max(30, int(getattr(cfg, "IMAGE_API_TRIGGER_COOLDOWN_SECONDS", 900) or 900))
    should_register = (
        trigger_register and bool(getattr(cfg, "IMAGE_API_AUTO_REGISTER", False))
        and result["quota"] <= threshold and cpu <= idle_limit and result["active_jobs"] == 0
    )
    if should_register:
        with _lock:
            last = float(_state.get("last_trigger_at") or 0)
        if now - last >= cooldown:
            from core import registration_service
            count = max(1, min(200, int(getattr(cfg, "IMAGE_API_REGISTER_COUNT", 1) or 1)))
            workers = _scaled_workers(cfg, cpu)
            jobs = registration_service.submit_registration(count=count, workers=workers)
            result.update({"triggered": True, "submitted": len(jobs), "workers": workers, "count": count})
            with _lock:
                _state.update({"last_trigger_at": now, "workers": workers, "last_trigger": result})
            logger.info("[ImageQuota] 额度 %.4f <= %.4f 且 CPU %.1f%% 空闲，自动提交 %s 个注册任务 workers=%s", result["quota"], threshold, cpu, count, workers)
    else:
        result["triggered"] = False
    return result


def schedule_live_checks() -> dict[str, Any]:
    """按批次把账号查活任务加入现有后台队列。

    用游标轮转账号，避免账号总数大于批量上限时每轮只查到前几项。
    ``live_check_service`` 会原子跳过已经排队/运行中的账号。
    """
    cfg = _cfg()
    batch_size = max(1, min(500, int(getattr(cfg, "IMAGE_API_LIVE_CHECK_BATCH_SIZE", 20) or 20)))
    try:
        from core import db, live_check_service
        rows = list(db.list_accounts(limit=5000, archived=False) or [])
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}", "queued": 0}

    candidates = []
    for row in rows:
        email = str(row.get("email") or "").strip()
        if not email or str(row.get("live_check_status") or "") in {"queued", "running"}:
            continue
        if str(row.get("live_check_status") or "") == "deactivated":
            continue
        candidates.append(row)
    if not candidates:
        result = {"ok": True, "queued": 0, "busy": 0, "available": 0, "total": len(rows)}
        with _lock:
            _state.update({"last_live_check_at": time.time(), "last_live_check": result})
        return result

    with _lock:
        cursor = int(_state.get("live_cursor") or 0) % len(candidates)
    selected = [candidates[(cursor + i) % len(candidates)] for i in range(min(batch_size, len(candidates)))]
    queued, busy, failed = [], [], []
    for row in selected:
        acc_id = int(row.get("id") or 0)
        email = str(row.get("email") or "").strip()
        try:
            item = live_check_service.enqueue_account_live_check(
                account_id=acc_id, email=email, trigger="image-scheduled", proxy=None,
            )
        except Exception as exc:
            failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
            continue
        if item.get("accepted"):
            queued.append({"id": acc_id, "email": email})
        elif item.get("busy"):
            busy.append({"id": acc_id, "email": email})
        else:
            failed.append({"id": acc_id, "email": email, "error": item.get("error") or "入队失败"})

    result = {
        "ok": True, "queued": len(queued), "busy": len(busy), "failed": failed,
        "selected": len(selected), "available": len(candidates), "total": len(rows),
        "emails": queued,
    }
    with _lock:
        _state.update({
            "last_live_check_at": time.time(), "last_live_check": result,
            "live_cursor": (cursor + len(selected)) % max(1, len(candidates)),
        })
    logger.info("[ImageQuota] 定时查活入队：queued=%s busy=%s failed=%s batch=%s", len(queued), len(busy), len(failed), len(selected))
    return result


def cleanup_failed_accounts(*, force: bool = False) -> dict[str, Any]:
    """清理达到失败阈值或已废的本地账号，并删除对应临时邮箱。"""
    cfg = _cfg()
    if not force and not bool(getattr(cfg, "IMAGE_API_MIDNIGHT_CLEANUP_ENABLED", False)):
        return {"ok": True, "skipped": True, "deleted": 0, "mail_deleted": 0}
    try:
        from core import db
        from core.live_check_service import _delete_failed_account_and_mail
        rows = list(db.list_accounts(limit=5000, archived=False) or [])
    except Exception as exc:
        return {"ok": False, "deleted": 0, "mail_deleted": 0, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
    threshold = max(1, int(getattr(cfg, "IMAGE_API_LIVE_CHECK_DELETE_FAILURE_THRESHOLD", 2) or 2))
    deleted, mail_deleted, skipped = [], 0, []
    for row in rows:
        status = str(row.get("live_check_status") or "")
        failures = int(row.get("live_check_failed_count") or 0)
        if status != "deactivated" and not (status == "failed" and failures >= threshold):
            continue
        acc_id = int(row.get("id") or 0)
        email = str(row.get("email") or "").strip()
        if not acc_id or not email:
            continue
        item = _delete_failed_account_and_mail(acc_id, email, reason=f"定时清理: status={status} failures={failures}")
        if item.get("deleted"):
            deleted.append(item)
            mail_deleted += int(bool(item.get("mail_deleted")))
        else:
            skipped.append(item)
    result = {"ok": True, "deleted": len(deleted), "mail_deleted": mail_deleted, "items": deleted, "skipped": skipped}
    with _lock:
        _state.update({"last_midnight_cleanup_at": time.time(), "last_midnight_cleanup": result})
    logger.info("[ImageQuota] 失败账号定时清理：deleted=%s mail_deleted=%s", len(deleted), mail_deleted)
    return result


def status() -> dict[str, Any]:
    cfg = _cfg()
    with _lock:
        result = dict(_state)
    result.update({
        "enabled": bool(getattr(cfg, "IMAGE_API_MONITOR_ENABLED", False)),
        "auto_register": bool(getattr(cfg, "IMAGE_API_AUTO_REGISTER", False)),
        "threshold": float(getattr(cfg, "IMAGE_API_QUOTA_THRESHOLD", 1) or 1),
        "active_jobs": _active_jobs(),
        "live_check_enabled": bool(getattr(cfg, "IMAGE_API_LIVE_CHECK_ENABLED", False)),
        "live_check_interval_seconds": max(30, int(getattr(cfg, "IMAGE_API_LIVE_CHECK_INTERVAL_SECONDS", 3600) or 3600)),
        "live_check_batch_size": max(1, int(getattr(cfg, "IMAGE_API_LIVE_CHECK_BATCH_SIZE", 20) or 20)),
        "live_check_sync_image": bool(getattr(cfg, "IMAGE_API_LIVE_CHECK_SYNC_IMAGE", True)),
        "live_check_delete_failed": bool(getattr(cfg, "IMAGE_API_LIVE_CHECK_DELETE_FAILED", False)),
        "live_check_delete_failure_threshold": max(1, int(getattr(cfg, "IMAGE_API_LIVE_CHECK_DELETE_FAILURE_THRESHOLD", 2) or 2)),
        "midnight_cleanup_enabled": bool(getattr(cfg, "IMAGE_API_MIDNIGHT_CLEANUP_ENABLED", False)),
        "midnight_cleanup_hour": max(0, min(23, int(getattr(cfg, "IMAGE_API_MIDNIGHT_CLEANUP_HOUR", 0) or 0))),
    })
    return result


def _loop() -> None:
    with _lock:
        _state["running"] = True
    while not _stop.is_set():
        cfg = _cfg()
        if bool(getattr(cfg, "IMAGE_API_MONITOR_ENABLED", False)):
            check_once()
        live_enabled = bool(getattr(cfg, "IMAGE_API_LIVE_CHECK_ENABLED", False))
        live_interval = max(30, int(getattr(cfg, "IMAGE_API_LIVE_CHECK_INTERVAL_SECONDS", 3600) or 3600))
        if live_enabled:
            with _lock:
                last_live = float(_state.get("last_live_check_at") or 0)
            if not last_live or time.time() - last_live >= live_interval:
                try:
                    schedule_live_checks()
                except Exception:
                    logger.exception("[ImageQuota] 定时查活调度异常")
        if bool(getattr(cfg, "IMAGE_API_MIDNIGHT_CLEANUP_ENABLED", False)):
            now_local = time.localtime()
            cleanup_hour = max(0, min(23, int(getattr(cfg, "IMAGE_API_MIDNIGHT_CLEANUP_HOUR", 0) or 0)))
            with _lock:
                last_cleanup = float(_state.get("last_midnight_cleanup_at") or 0)
            last_local = time.localtime(last_cleanup) if last_cleanup else None
            same_day = bool(last_local and last_local.tm_yday == now_local.tm_yday and last_local.tm_year == now_local.tm_year)
            if now_local.tm_hour == cleanup_hour and not same_day:
                try:
                    cleanup_failed_accounts()
                except Exception:
                    logger.exception("[ImageQuota] 定时失败账号清理异常")
        quota_wait = max(10, int(getattr(cfg, "IMAGE_API_QUOTA_POLL_SECONDS", 60) or 60))
        wait_seconds = min(quota_wait, live_interval) if live_enabled else quota_wait
        _stop.wait(wait_seconds)
    with _lock:
        _state["running"] = False


def ensure_started() -> None:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="image-quota-monitor", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()
