# -*- coding: utf-8 -*-
"""
NodeSeek 登录代理（Login Agent）—— 跑在家宽机器上。

架构：

    青龙(QL)  ──HTTP+Token──▶  家宽机器: 本服务  ──真浏览器──▶  nodeseek.com
        │                          │
        └── 拿到 cookie 后用 curl_cffi 签到（或者让本服务直接代签）

为什么这么设计：
    登录必须由真实浏览器在同一条“干净线路”上完成（Turnstile + 浏览器指纹 token）；
    而 cookie（sessionid / cf_clearance）本身不绑 IP，可以直接给青龙用。

启动：
    python3 nodeseek_login_agent.py --host 0.0.0.0 --port 3000

环境变量：
    AGENT_TOKEN            必填，调用方需带 Authorization: Bearer <token>
    AGENT_ENGINE           auto | camoufox | patchright | playwright
    AGENT_HEADLESS         true | false（默认 true；调试可设 false 看窗口）
    AGENT_PROXY            可选，http://user:pass@host:port（每台机器可挂自己的家宽出口）
    AGENT_DATA_DIR         默认 ./data
    AGENT_MAX_CONCURRENCY  默认 1（同一台机器同时开几个浏览器，别开太多）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request

from browser_engine import browser_page, resolve_engine
import login_core
from login_core import LoginError

DATA_DIR = os.getenv("AGENT_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
PROFILE_DIR = os.path.join(DATA_DIR, "profiles")
COOKIE_DIR = os.path.join(DATA_DIR, "cookies")
LOG_DIR = os.path.join(DATA_DIR, "logs")

for _d in (DATA_DIR, PROFILE_DIR, COOKIE_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

AGENT_TOKEN = os.getenv("AGENT_TOKEN", "").strip()
ENGINE = os.getenv("AGENT_ENGINE", "auto").strip()
HEADLESS = os.getenv("AGENT_HEADLESS", "true").strip().lower() not in ("0", "false", "no")
PROXY = os.getenv("AGENT_PROXY", "").strip()
MAX_CONCURRENCY = int(os.getenv("AGENT_MAX_CONCURRENCY", "1"))

app = Flask(__name__)

_sem = threading.Semaphore(MAX_CONCURRENCY)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_last_cookie: dict[str, dict] = {}


def account_lock(account: str) -> threading.Lock:
    with _locks_guard:
        if account not in _locks:
            _locks[account] = threading.Lock()
        return _locks[account]


def slugify(name: str) -> str:
    """账号名转成安全的目录名，避免中文/特殊字符搞坏路径。"""
    s = re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("_")
    return s or "account"


def cookie_path(account: str) -> str:
    return os.path.join(COOKIE_DIR, f"{slugify(account)}.txt")


def save_cookie(account: str, cookie: str, meta: dict):
    payload = {"account": account, "cookie": cookie, "ts": time.time(),
               "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **meta}
    with open(cookie_path(account), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(COOKIE_DIR, f"{slugify(account)}.cookie"), "w", encoding="utf-8") as f:
        f.write(cookie)
    _last_cookie[account] = payload


def load_cookie(account: str) -> dict | None:
    if account in _last_cookie:
        return _last_cookie[account]
    p = cookie_path(account)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            _last_cookie[account] = data
            return data
        except Exception:
            return None
    return None


def auth_ok() -> bool:
    if not AGENT_TOKEN:
        return False
    got = request.headers.get("Authorization", "")
    if got.lower().startswith("bearer "):
        got = got[7:].strip()
    return got == AGENT_TOKEN


def require_auth():
    if not auth_ok():
        return jsonify({"ok": False, "error": "UNAUTHORIZED", "message": "token 不正确"}), 401
    return None


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        engine = resolve_engine(ENGINE)
    except Exception as e:
        engine = f"none ({e})"
    return jsonify({
        "ok": True,
        "service": "nodeseek-login-agent",
        "version": "1.0.0",
        "engine": engine,
        "headless": HEADLESS,
        "proxy": bool(PROXY),
        "max_concurrency": MAX_CONCURRENCY,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.post("/login")
def api_login():
    denied = require_auth()
    if denied:
        return denied

    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    account = (body.get("account") or username).strip()
    totp_secret = body.get("totp_secret") or os.getenv("AGENT_TOTP_SECRET") or None
    fresh = bool(body.get("fresh"))  # fresh=True 时清掉 profile，重来一个全新指纹

    if not username or not password:
        return jsonify({"ok": False, "error": "BAD_REQUEST", "message": "username/password 必填"}), 400

    profile = os.path.join(PROFILE_DIR, slugify(account))
    if fresh:
        import shutil
        shutil.rmtree(profile, ignore_errors=True)

    lock = account_lock(account)
    if not lock.acquire(timeout=240):
        return jsonify({"ok": False, "error": "BUSY", "message": "该账号正在登录中"}), 429

    started = time.time()
    acquired = False
    try:
        acquired = _sem.acquire(timeout=int(os.getenv("AGENT_QUEUE_TIMEOUT", "300")))
        if not acquired:
            return jsonify({"ok": False, "error": "BUSY",
                            "message": "浏览器并发已满，请稍后重试"}), 429
        print(f"[login] 开始 {account} / engine={ENGINE} profile={profile}", flush=True)
        with browser_page(
            engine=ENGINE, headless=HEADLESS, user_data_dir=profile, proxy=PROXY,
        ) as page:
            result = login_core.login(page, username, password, totp_secret=totp_secret)

        save_cookie(account, result["cookie"], {
            "username": username,
            # cookie 的到期信息：让上层能判断「还剩几天，要不要提前换新」，
            # 而不是等 cookie 掉了才发现要重新登录。
            "expiry": result.get("expiry"),
            "cookie_life": result.get("cookie_life"),
            "device_memory_expired": bool(result.get("device_memory_expired")),
        })
        cost = round(time.time() - started, 1)
        print(f"[login] 成功 {account} 用时 {cost}s "
              f"cookie={result['cookie'][:60]}… "
              f"有效期={result.get('cookie_life')}", flush=True)
        return jsonify({
            "ok": True,
            "account": account,
            "username": username,
            "cookie": result["cookie"],
            "expiry": result.get("expiry"),
            "cookie_life": result.get("cookie_life"),
            "device_memory_expired": bool(result.get("device_memory_expired")),
            "cost": cost,
            "engine": resolve_engine(ENGINE),
        })
    except LoginError as e:
        print(f"[login] 失败 {account} {e.code} {e.message}", flush=True)
        code = 401 if e.code == "BAD_CREDENTIALS" else 424
        # device_memory_expired=True 表示「这台设备之前登录过，
        # 这次却仍被要求邮箱验证」——设备记忆失效了，上层要告警。
        return jsonify({
            "ok": False,
            "error": e.code,
            "message": e.message,
            "device_memory_expired": bool(getattr(e, "device_memory_expired", False)),
            "cost": round(time.time() - started, 1),
        }), code
    except Exception as e:
        print(f"[login] 异常 {account} {type(e).__name__}: {e}", flush=True)
        return jsonify({"ok": False, "error": "INTERNAL", "message": f"{type(e).__name__}: {e}"}), 500
    finally:
        if acquired:
            _sem.release()
        lock.release()


@app.post("/sign")
def api_sign():
    """可选：整段签到都在家宽机器上完成（青龙出口 IP 被 CF 拦时用）。"""
    denied = require_auth()
    if denied:
        return denied

    body = request.get_json(silent=True) or {}
    account = (body.get("account") or "").strip()
    cookie = (body.get("cookie") or "").strip()

    if not cookie and account:
        saved = load_cookie(account)
        cookie = (saved or {}).get("cookie", "")
    if not cookie:
        return jsonify({"ok": False, "error": "NO_COOKIE", "message": "没有可用 cookie"}), 400

    profile = os.path.join(PROFILE_DIR, slugify(account or "sign"))
    try:
        with _sem:
            with browser_page(
                engine=ENGINE, headless=HEADLESS, user_data_dir=profile, proxy=PROXY,
            ) as page:
                res = login_core.sign_in_page(page, cookie=cookie)
        return jsonify({"ok": True, "account": account, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": "INTERNAL", "message": f"{type(e).__name__}: {e}"}), 500


@app.get("/cookie/<account>")
def api_cookie(account: str):
    denied = require_auth()
    if denied:
        return denied
    data = load_cookie(account)
    if not data:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    return jsonify({"ok": True, **data})


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def cli_selftest():
    print("=" * 60)
    print("自检：引擎 / 浏览器 / 网络 / 登录页可访问性")
    print("=" * 60)
    try:
        engine = resolve_engine(ENGINE)
    except Exception as e:
        print(f"✗ 没有可用浏览器引擎：{e}")
        return 1
    print(f"✓ 使用引擎：{engine}")

    profile = os.path.join(PROFILE_DIR, "_selftest")
    try:
        with browser_page(engine=ENGINE, headless=HEADLESS, user_data_dir=profile, proxy=PROXY) as page:
            page.goto(login_core.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            challenged = login_core._page_is_challenge(page)
            print(f"✓ 标题：{page.title()!r}｜整页挑战：{'是' if challenged else '否'}")
            if challenged:
                ok = login_core._wait_out_challenge(page, timeout=40)
                print(f"✓ 挑战处理完成：{ok}｜标题：{page.title()!r}")

            has_user = page.locator("#stacked-email").count()
            has_pwd = page.locator("#stacked-password").count()
            print(f"✓ 表单元素：用户名 {has_user}｜密码 {has_pwd}")
            box = login_core._widget_box(page)
            print(f"✓ Turnstile 组件：{'在 (' + str(int(box['x'])) + ',' + str(int(box['y'])) + ')' if box else '未出现'}")

            page.screenshot(path=os.path.join(DATA_DIR, "selftest_login.png"), full_page=True)

            # 新开一个页面测出口 IP，避免把登录页导航走
            p2 = page.context.new_page()
            print(f"✓ 出口 IP：{_outbound_ip(p2)}")
            p2.close()
            print(f"✓ 截图：{os.path.join(DATA_DIR, 'selftest_login.png')}")

            if HEADLESS:
                print("提示：如果 Turnstile 一直不出 token，请改成 AGENT_HEADLESS=false + Xvfb（见部署指南）")
        return 0
    except Exception as e:
        print(f"✗ 自检失败：{type(e).__name__}: {e}")
        return 1


def _outbound_ip(page) -> str:
    try:
        page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=20000)
        return (page.inner_text("body") or "").strip()
    except Exception:
        return "未知"


def cli_once(args):
    account = args.account or args.user
    profile = os.path.join(PROFILE_DIR, slugify(account))
    print(f"一次性登录：{account} → profile {profile}")
    try:
        with browser_page(engine=ENGINE, headless=HEADLESS, user_data_dir=profile,
                          proxy=PROXY) as page:
            result = login_core.login(page, args.user, args.password,
                                      totp_secret=os.getenv("AGENT_TOTP_SECRET"))
        save_cookie(account, result["cookie"], {"username": args.user})
        print("\n登录成功，cookie（已保存到 " + cookie_path(account) + "）：\n")
        print(result["cookie"])
        return 0
    except LoginError as e:
        print(f"\n登录失败：{e.code} - {e.message}")
        return 2


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(cli_selftest())

    parser = argparse.ArgumentParser(description="NodeSeek 登录代理")
    parser.add_argument("--host", default=os.getenv("AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AGENT_PORT", "3000")))
    parser.add_argument("--once", action="store_true", help="只登录一次并打印 cookie，不起服务")
    parser.add_argument("--user")
    parser.add_argument("--password")
    parser.add_argument("--account")
    args = parser.parse_args()

    if args.once:
        if not args.user or not args.password:
            parser.error("--once 需要 --user 和 --password")
        sys.exit(cli_once(args))

    if not AGENT_TOKEN:
        print("!! 警告：未设置 AGENT_TOKEN，接口将拒绝所有请求（仅 /health 可用）")

    print(f"NodeSeek 登录代理启动：http://{args.host}:{args.port}  engine={resolve_engine(ENGINE)}")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
