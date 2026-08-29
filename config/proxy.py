# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
from config.env_loader import apply_env_overrides
import random
import secrets
import string
import base64
import json
import logging
import re
import threading
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# 代理订阅地址：支持纯文本/Base64/JSON/常见 Clash proxies 节点格式。
# 订阅内容只保存在内存，不写入 .env 或代码仓库。
PROXY_SUBSCRIPTION_URL: str = ""
PROXY_SUBSCRIPTION_ENABLED: bool = True
PROXY_SUBSCRIPTION_REFRESH_MINUTES: int = 30
PROXY_SUBSCRIPTION_TIMEOUT: int = 15
# Clash/VLESS 等节点由服务器上的 Mihomo 转换为本地 SOCKS/HTTP 代理。
PROXY_SUBSCRIPTION_BRIDGE_PROXY: str = "socks5h://127.0.0.1:7890"

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3

logger = logging.getLogger(__name__)
_SUBSCRIPTION_LOCK = threading.Lock()
_SUBSCRIPTION_PROXIES: list[str] = []
_SUBSCRIPTION_LAST_REFRESH = 0.0
_SUBSCRIPTION_LAST_ERROR = ""
_SUBSCRIPTION_LAST_FORMAT = ""
_SUBSCRIPTION_LAST_COUNT = 0


def _with_task_sticky_session(proxy: str) -> str:
    """为 CliProxy 动态出口增加任务级 sticky sid，避免同一浏览器会话频繁换 IP。"""
    value = str(proxy or "").strip()
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        host = str(parsed.hostname or "").lower()
        username = unquote(parsed.username or "")
        if not host.endswith("cliproxy.io") or not username or "-sid-" in username:
            return value
        alphabet = string.ascii_letters + string.digits
        sid = "".join(secrets.choice(alphabet) for _ in range(8))
        sticky_username = f"{username}-sid-{sid}-t-10"
        password = unquote(parsed.password or "")
        auth = quote(sticky_username, safe="-")
        if parsed.password is not None:
            auth += f":{quote(password, safe='')}"
        host_port = parsed.hostname or ""
        if parsed.port:
            host_port += f":{parsed.port}"
        return urlunsplit((parsed.scheme, f"{auth}@{host_port}", parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return value


def _proxy_uri(value: str) -> str:
    value = str(value or "").strip()
    if not re.match(r"^(?:https?|socks(?:4|5|5h))://[^\s]+$", value, re.I):
        return ""
    try:
        parsed = urlsplit(value)
        if not parsed.hostname or not parsed.port:
            return ""
    except Exception:
        return ""
    return value


def _extract_subscription_values(value) -> list[str]:
    """Extract proxy URI strings and host/port node mappings from JSON-like data."""
    out: list[str] = []
    if isinstance(value, str):
        uri = _proxy_uri(value)
        if uri:
            out.append(uri)
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(_extract_subscription_values(item))
        return out
    if not isinstance(value, dict):
        return out
    protocol = str(value.get("type") or value.get("protocol") or value.get("scheme") or "http").lower()
    host = value.get("server") or value.get("host") or value.get("hostname")
    port = value.get("port")
    if host and port and protocol in {"http", "https", "socks4", "socks5", "socks5h"}:
        auth = ""
        username = value.get("username") or value.get("user")
        password = value.get("password") or value.get("pass")
        if username is not None:
            auth = quote(str(username), safe="")
            if password is not None:
                auth += ":" + quote(str(password), safe="")
            auth += "@"
        out.append(f"{protocol}://{auth}{host}:{int(port)}")
    for key, child in value.items():
        if key.lower() in {"proxies", "proxy", "nodes", "data", "items", "list", "servers"} or isinstance(child, (dict, list)):
            out.extend(_extract_subscription_values(child))
    return out


def _parse_subscription_text(text: str) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    values: list[str] = []
    try:
        parsed = json.loads(text)
        values.extend(_extract_subscription_values(parsed))
    except Exception:
        pass
    # URI-per-line / comma-separated text.
    values.extend(re.findall(r"(?im)(?:(?:https?|socks(?:4|5|5h))://[^\s,\"']+)", text))
    if not values:
        # Base64 subscriptions commonly contain one URI per line after decode.
        compact = re.sub(r"\s+", "", text)
        if len(compact) >= 16 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
            try:
                decoded = base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4)).decode("utf-8", "ignore")
                if decoded != text:
                    values.extend(_parse_subscription_text(decoded))
            except Exception:
                pass
    if not values:
        # Minimal Clash YAML support: parse proxy blocks with server/port/type.
        blocks = re.split(r"(?m)^\s*-\s+name:\s*", text)
        for block in blocks[1:]:
            def field(name: str) -> str:
                m = re.search(rf"(?im)^\s*{re.escape(name)}:\s*([^\s#]+)", block)
                return m.group(1).strip("'\"") if m else ""
            values.extend(_extract_subscription_values({
                "type": field("type"), "server": field("server"), "port": field("port"),
                "username": field("username"), "password": field("password"),
            }))
    deduped = []
    seen = set()
    for value in values:
        uri = _proxy_uri(value)
        if uri and uri not in seen:
            seen.add(uri)
            deduped.append(uri)
    return deduped


def _decode_subscription_payload(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "").strip())
    if len(compact) < 16 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return ""
    try:
        decoded = base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4)).decode("utf-8", "ignore")
        return decoded if decoded and decoded != text else ""
    except Exception:
        return ""


def _bridge_node_count(text: str) -> int:
    """Count non HTTP/SOCKS subscription nodes handled by Mihomo."""
    text = str(text or "")
    uri_count = len(re.findall(r"""(?im)(?:vless|vmess|trojan|ss|ssr|hysteria2?|tuic)://[^\s,"']+""", text))
    yaml_count = len(re.findall(r"(?im)^\s*-\s*(?:name|type):", text)) if re.search(r"(?im)^\s*proxies\s*:", text) else 0
    return max(uri_count, yaml_count)


def refresh_proxy_subscription(*, force: bool = False) -> dict:
    """Fetch and cache the configured proxy subscription."""
    global _SUBSCRIPTION_PROXIES, _SUBSCRIPTION_LAST_REFRESH, _SUBSCRIPTION_LAST_ERROR, _SUBSCRIPTION_LAST_FORMAT, _SUBSCRIPTION_LAST_COUNT
    url = str(PROXY_SUBSCRIPTION_URL or "").strip()
    if not bool(PROXY_SUBSCRIPTION_ENABLED) or not url:
        return {"ok": True, "count": 0, "skipped": True, "error": ""}
    now = time.time()
    interval = max(0, int(PROXY_SUBSCRIPTION_REFRESH_MINUTES or 0)) * 60
    with _SUBSCRIPTION_LOCK:
        if not force and _SUBSCRIPTION_LAST_REFRESH and now - _SUBSCRIPTION_LAST_REFRESH < interval:
            return {"ok": bool(_SUBSCRIPTION_PROXIES) or _SUBSCRIPTION_LAST_FORMAT == "clash", "count": _SUBSCRIPTION_LAST_COUNT, "format": _SUBSCRIPTION_LAST_FORMAT, "cached": True, "error": _SUBSCRIPTION_LAST_ERROR}
        try:
            import requests
            timeout = max(3, int(PROXY_SUBSCRIPTION_TIMEOUT or 15))
            response = requests.get(url, headers={"User-Agent": "turb-gpt-free-register/1.0"}, timeout=timeout)
            if response.status_code == 403:
                # Some subscription providers whitelist Clash/Mihomo clients.
                response = requests.get(url, headers={"User-Agent": "clash.meta"}, timeout=timeout)
            response.raise_for_status()
            raw_text = response.text
            parsed = _parse_subscription_text(raw_text)
            decoded_text = _decode_subscription_payload(raw_text)
            fmt = "uri"
            bridge_count = _bridge_node_count(raw_text) or _bridge_node_count(decoded_text)
            if not parsed and bridge_count:
                # VLESS/VMess/Trojan nodes are consumed by a local Mihomo bridge,
                # not passed directly to Chromium as HTTP/SOCKS URLs.
                parsed = []
                fmt = "clash"
                node_count = bridge_count
                if node_count <= 0:
                    raise ValueError("Clash 订阅中未找到节点")
            elif not parsed:
                raise ValueError("订阅响应中未找到可用代理")
            else:
                node_count = len(parsed)
            _SUBSCRIPTION_PROXIES = parsed
            _SUBSCRIPTION_LAST_FORMAT = fmt
            _SUBSCRIPTION_LAST_COUNT = node_count
            _SUBSCRIPTION_LAST_ERROR = ""
            _SUBSCRIPTION_LAST_REFRESH = now
            logger.info("代理订阅刷新成功：count=%s format=%s", node_count, fmt)
            return {"ok": True, "count": node_count, "format": fmt, "cached": False, "error": ""}
        except Exception as exc:
            _SUBSCRIPTION_LAST_ERROR = f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"
            _SUBSCRIPTION_LAST_REFRESH = now
            logger.warning("代理订阅刷新失败：%s", _SUBSCRIPTION_LAST_ERROR)
            return {"ok": False, "count": _SUBSCRIPTION_LAST_COUNT, "format": _SUBSCRIPTION_LAST_FORMAT, "cached": False, "error": _SUBSCRIPTION_LAST_ERROR}


def proxy_subscription_status() -> dict:
    return {
        "configured": bool(str(PROXY_SUBSCRIPTION_URL or "").strip()),
        "enabled": bool(PROXY_SUBSCRIPTION_ENABLED),
        "count": _SUBSCRIPTION_LAST_COUNT,
        "format": _SUBSCRIPTION_LAST_FORMAT,
        "last_refresh": _SUBSCRIPTION_LAST_REFRESH or None,
        "error": _SUBSCRIPTION_LAST_ERROR,
    }


def pick_proxy() -> str:
    """抽取代理；先按需刷新订阅，CliProxy 继续生成独立 sticky sid。"""
    try:
        refresh_proxy_subscription()
    except Exception:
        pass
    pool = list(PROXY_POOL or []) + list(_SUBSCRIPTION_PROXIES)
    if not pool and _SUBSCRIPTION_LAST_FORMAT == "clash":
        pool = [_proxy_uri(PROXY_SUBSCRIPTION_BRIDGE_PROXY)]
    return _with_task_sticky_session(random.choice(pool)) if pool else ""


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_SUBSCRIPTION_URL': 'str',
    'PROXY_SUBSCRIPTION_ENABLED': 'bool',
    'PROXY_SUBSCRIPTION_REFRESH_MINUTES': 'int',
    'PROXY_SUBSCRIPTION_TIMEOUT': 'int',
    'PROXY_SUBSCRIPTION_BRIDGE_PROXY': 'str',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY = pick_proxy()
