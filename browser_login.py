# -*- coding: utf-8 -*-
"""
登录适配层（青龙侧）。

职责：把“账号密码”换成“可用 cookie”。两种模式自动切换：

  A. 远程模式（推荐）
     LOGIN_AGENT_URL=http://家宽机器IP:3000
     LOGIN_AGENT_TOKEN=xxxx
     由家宽机器上的 nodeseek_login_agent.py 用真浏览器登录，返回 cookie 字符串。

  B. 本地模式（备用）
     青龙这台机器自己装了 playwight/camoufox，直接本地起浏览器登录。
     适合“青龙就跑在家宽机器上”的情况。

多台家宽机器：
     LOGIN_AGENT_URL=http://ip1:3000,http://ip2:3000
     会把不同账号固定分散到不同机器（同一账号始终走同一台，指纹稳定）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT_DIR = os.path.join(_HERE, "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

TIMEOUT = int(os.getenv("LOGIN_AGENT_TIMEOUT", "240"))


def _agent_urls():
    raw = os.getenv("LOGIN_AGENT_URLS", "") or os.getenv("LOGIN_AGENT_URL", "")
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def _local_engine_available() -> bool:
    try:
        from browser_engine import resolve_engine
        resolve_engine(os.getenv("AGENT_ENGINE", "auto"))
        return True
    except Exception:
        return False


PLAYWRIGHT_AVAILABLE = bool(_agent_urls()) or _local_engine_available()


def _is_private_host(host: str) -> bool:
    """内网/回环地址判断。这些地址不能走 HTTP_PROXY，否则会被代理拦掉。"""
    import ipaddress
    if not host:
        return False
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        # 域名形式的组网地址（tailscale 的 xxx.ts.net 等）按公网处理
        return host.endswith(".local") or host.endswith(".lan")


def _post_json(url: str, payload: dict, token: str = "", timeout: int = TIMEOUT) -> dict:
    from urllib.parse import urlparse

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "nodeseek-auto-sign/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    # 内网地址绕过系统代理
    if _is_private_host(urlparse(url).hostname or ""):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()

    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "error": f"HTTP_{e.code}", "message": body[:300]}
    except Exception as e:
        return {"ok": False, "error": "NETWORK", "message": f"{type(e).__name__}: {e}"}


def _pick_agent(account: str) -> list:
    """同一账号固定同一台机器；失败时按顺序换下一台。"""
    urls = _agent_urls()
    if not urls:
        return []
    idx = sum(ord(c) for c in (account or "x")) % len(urls)
    return urls[idx:] + urls[:idx]


def _totp_for(user: str):
    """多账号 2FA：NS_TOTP_SECRETS=alice:BASE32&bob:BASE32 按用户名匹配。

    只开了一个账号 2FA 时可以继续用全局的 NS_TOTP_SECRET。
    """
    table = os.getenv("NS_TOTP_SECRETS", "").strip()
    if table:
        for item in table.replace("&", ",").split(","):
            if ":" not in item:
                continue
            name, secret = item.split(":", 1)
            if name.strip().lower() == (user or "").strip().lower():
                return secret.strip() or None
    return os.getenv("NS_TOTP_SECRET") or None


def browser_login(site_config, user, pwd, headless=True):
    """返回 cookie 字符串；失败返回 None。签名与主脚本里的调用保持一致。

    另外提供 browser_login_detail()，返回带到期信息的完整 dict，
    用于「主动续期」——需要知道 cookie 还能用几天。
    """
    r = browser_login_detail(site_config, user, pwd, headless)
    return r.get("cookie") if r else None


def browser_login_detail(site_config, user, pwd, headless=True):
    """完整登录，返回 {cookie, expiry, cookie_life, device_memory_expired, error}。

    失败时返回 dict（error 非空），不抛异常——调用方统一处理。
    """
    if not user or not pwd:
        return {"error": "MISSING_CREDENTIALS"}

    # 本地模式默认“有头”（无头浏览器过不了 Cloudflare）；
    # 真的想无头就显式设 LOGIN_LOCAL_HEADLESS=true
    _lh = os.getenv("LOGIN_LOCAL_HEADLESS")
    if _lh is None:
        headless = False
    else:
        headless = _lh.strip().lower() in ("1", "true", "yes")

    site = site_config.get("type", "nodeseek")
    token = os.getenv("LOGIN_AGENT_TOKEN", "").strip()
    totp_secret = _totp_for(user)

    # ---------- A. 远程 agent ----------
    errors = []
    for base in _pick_agent(user):
        payload = {"site": site, "account": user, "username": user, "password": pwd}
        if totp_secret:
            payload["totp_secret"] = totp_secret
        r = _post_json(f"{base}/login", payload, token=token)
        if r.get("ok") and r.get("cookie"):
            print(f"[browser_login] {base} 登录成功（{r.get('cost', '?')}s）"
                  f" 有效期={r.get('cookie_life', '?')}")
            return {
                "cookie": r["cookie"],
                "expiry": r.get("expiry"),
                "cookie_life": r.get("cookie_life"),
                "device_memory_expired": bool(r.get("device_memory_expired")),
            }
        errors.append(f"{base}: {r.get('error')} {r.get('message', '')}")
        print(f"[browser_login] {base} 登录失败：{r.get('error')} {r.get('message', '')}")
        if r.get("error") == "BAD_CREDENTIALS":
            # 密码错就没必要换机器重试了
            break
        if r.get("device_memory_expired"):
            # 「这台设备之前登录过，这次仍要码」——设备记忆失效了。
            # 继续换机器重试意义不大，直接把信号带出去让上层告警。
            errors.append("设备记忆疑似已失效（这台机器之前登录过，本次仍要求邮箱验证）")
            break

    # ---------- B. 本地浏览器 ----------
    if not _agent_urls() or os.getenv("LOGIN_LOCAL_FALLBACK", "true").lower() in ("1", "true", "yes"):
        try:
            from browser_engine import browser_page
            import login_core

            profile = os.path.join(_HERE, "cookie", "profiles", _safe(user))
            with browser_page(
                engine=os.getenv("AGENT_ENGINE", "auto"),
                headless=headless,
                user_data_dir=profile,
                proxy=os.getenv("AGENT_PROXY", ""),
            ) as page:
                result = login_core.login(page, user, pwd, totp_secret=totp_secret)
            print(f"[browser_login] 本地浏览器登录成功  有效期={result.get('cookie_life', '?')}")
            return {
                "cookie": result["cookie"],
                "expiry": result.get("expiry"),
                "cookie_life": result.get("cookie_life"),
                "device_memory_expired": bool(result.get("device_memory_expired")),
            }
        except Exception as e:
            # 这条以前只在有远程 agent 时才打印，导致单机模式下失败原因被完全吞掉
            code = getattr(e, "code", type(e).__name__)
            msg = getattr(e, "message", str(e))
            dme = bool(getattr(e, "device_memory_expired", False))
            print(f"[browser_login] 本地浏览器登录失败 [{code}] {msg}")
            errors.append(f"local: {code} {msg}")
            if dme:
                errors.append("设备记忆疑似已失效（这台机器之前登录过，本次仍要求邮箱验证）")
                return {"error": code, "message": msg, "device_memory_expired": True}

    if errors:
        print("[browser_login] 所有登录方式均失败：" + " | ".join(errors))
    return {"error": "LOGIN_FAILED", "message": " | ".join(errors)}


def browser_sign(site_config, cookie=None):
    """可选：整段签到交给 agent 在浏览器里做。返回 agent 的原始结果 dict。"""
    for base in _pick_agent("sign"):
        payload = {"site": site_config.get("type", "nodeseek"), "cookie": cookie or ""}
        r = _post_json(f"{base}/sign", payload, token=os.getenv("LOGIN_AGENT_TOKEN", "").strip())
        if r.get("ok"):
            return r
    return None


def _safe(name: str) -> str:
    import re
    s = re.sub(r"[^0-9A-Za-z._-]+", "_", name or "").strip("_")
    return s or "account"
