#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPS 环境自检：回答一个问题 —— 这台机器能不能跑通浏览器登录？

用法：
    python3 check_env.py              # 默认有头（会自动起 Xvfb）
    python3 check_env.py --headless   # 对比测试无头模式
    python3 check_env.py --url https://www.nodeseek.com/signIn.html

它会依次检查：
    1. 系统资源（CPU / 内存）—— 浏览器够不够跑
    2. 浏览器引擎是否可用
    3. 出口 IP 的 ASN，以及是不是机房 IP（这是决定性因素）
    4. challenges.cloudflare.com（Turnstile 后端）通不通
    5. 真实打开登录页：整页挑战过不过、Turnstile 出不出 token

最后给出结论：能跑 / 勉强 / 别浪费服务器。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(HERE, "agent")
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

OK = "  [OK]  "
WARN = "  [!!]  "
BAD = "  [XX]  "

results = {}


def say(tag, msg):
    print(f"{tag}{msg}", flush=True)


def section(title):
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}", flush=True)


# ---------------------------------------------------------------- 系统资源
def check_system():
    section("1. 系统资源")
    say(OK, f"{platform.system()} {platform.release()} / Python {platform.python_version()}")

    cores = os.cpu_count() or 1
    mem_gb = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_gb = int(line.split()[1]) / 1024 / 1024
                    break
    except Exception:
        pass

    say(OK if cores >= 1 else WARN, f"CPU 核心：{cores}")
    if mem_gb is None:
        say(WARN, "内存：读不到（非 Linux？）")
    elif mem_gb < 0.9:
        say(BAD, f"内存：{mem_gb:.2f} GB —— Chromium 单实例要 400~700MB，"
                 f"256MB 小鸡基本开不起来")
    elif mem_gb < 1.5:
        say(WARN, f"内存：{mem_gb:.2f} GB —— 勉强够，建议加 swap "
                  f"（fallocate -l 1G /swapfile && mkswap /swapfile && swapon /swapfile）")
    else:
        say(OK, f"内存：{mem_gb:.2f} GB")

    results["cores"] = cores
    results["mem_gb"] = mem_gb


# ---------------------------------------------------------------- 引擎
def check_engine():
    section("2. 浏览器引擎")
    try:
        import browser_engine

        try:
            name = browser_engine.resolve_engine(os.getenv("AGENT_ENGINE", "auto"))
            say(OK, f"可用引擎：{name}")
            results["engine"] = name
        except Exception as e:
            say(BAD, f"没有可用引擎：{e}")
            results["engine"] = None

        if os.name == "posix":
            has_display = bool(os.getenv("DISPLAY"))
            import shutil

            has_xvfb = bool(shutil.which("Xvfb"))
            if has_display:
                say(OK, f"DISPLAY={os.environ['DISPLAY']}")
            elif has_xvfb:
                say(OK, "无 DISPLAY，但有 Xvfb —— 运行时会自动拉起虚拟显示")
            else:
                say(BAD, "既无 DISPLAY 也无 Xvfb —— 有头模式起不来。"
                         "apt install -y xvfb")
            results["xvfb"] = has_display or has_xvfb
    except Exception as e:
        say(BAD, f"导入 browser_engine 失败：{type(e).__name__}: {e}")
        results["engine"] = None


# ---------------------------------------------------------------- 出口 IP
def _get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def check_ip():
    section("3. 出口 IP（决定性因素）")
    info = {}
    for fn in (_ip_api, _ipinfo):
        try:
            info = fn()
            if info.get("ip"):
                break
        except Exception:
            continue

    if not info.get("ip"):
        say(WARN, "查不到出口 IP（IP 查询站不通），跳过这一项")
        results["ip"] = None
        return

    say(OK, f"出口 IP：{info['ip']}")
    if info.get("country"):
        say(OK, f"归属：{info.get('country')} {info.get('city', '')}".rstrip())
    if info.get("asn"):
        say(OK, f"ASN：{info['asn']} {info.get('org', '')}".rstrip())

    hosting = info.get("hosting")
    if hosting is None:
        say(WARN, "这个 IP 库没给机房标记，只能看 ASN 自己判断")
    elif hosting:
        say(BAD, "IP 类型：机房/云服务器（hosting=true）")
        say(BAD, "  -> Cloudflare 对机房 IP 的风险评分很高，")
        say(BAD, "     整页挑战大概率过不去 / Turnstile 无限循环。")
    else:
        say(OK, "IP 类型：住宅或企业宽带（hosting=false）")

    results["ip"] = info.get("ip")
    results["hosting"] = hosting
    results["asn"] = info.get("asn", "")


def _ip_api():
    d = _get_json("http://ip-api.com/json/?fields=status,country,city,isp,org,as,"
                  "hosting,proxy,mobile,query")
    if d.get("status") != "success":
        return {}
    return {
        "ip": d.get("query"),
        "country": d.get("country"),
        "city": d.get("city"),
        "asn": (d.get("as") or "").split(" ")[0],
        "org": d.get("isp") or d.get("org") or "",
        "hosting": d.get("hosting"),
        "proxy": d.get("proxy"),
    }


def _ipinfo():
    d = _get_json("https://ipinfo.io/json")
    return {
        "ip": d.get("ip"),
        "country": d.get("country"),
        "city": d.get("city"),
        "asn": (d.get("org") or "").split(" ")[0],
        "org": d.get("org") or "",
        "hosting": None,
    }


# ---------------------------------------------------------------- CF 后端
def check_cf_backend():
    section("4. Turnstile 后端连通性")
    hosts = ["challenges.cloudflare.com", "www.nodeseek.com"]
    reachable = 0
    for h in hosts:
        t0 = time.time()
        try:
            ip = socket.gethostbyname(h)
            sock = socket.create_connection((h, 443), timeout=8)
            sock.close()
            say(OK, f"{h} ({ip}) 443 可达，{time.time() - t0:.0f}ms")
            reachable += 1
        except Exception as e:
            say(BAD, f"{h} 不可达：{type(e).__name__}: {e}")
    results["cf_reachable"] = reachable == len(hosts)


def _widget_text(page, limit=90):
    """把 Turnstile iframe 里的文字抓出来，方便判断卡在哪一步。"""
    for fr in page.frames:
        if "challenges.cloudflare.com" not in (fr.url or ""):
            continue
        try:
            txt = (fr.locator("body").inner_text() or "").strip()
        except Exception:
            continue
        txt = " ".join(txt.split())
        return txt[:limit]
    return ""


# ---------------------------------------------------------------- 真实挑战
def check_challenge(url, headless, wait=45):
    section("5. 真实打开登录页（最关键）")
    try:
        import browser_engine
        import login_core
    except Exception as e:
        say(BAD, f"导入失败：{e}")
        results["challenge"] = "import_error"
        return

    profile = os.path.join(HERE, "cookie", "profiles", "_envcheck")
    shot = os.path.join(HERE, "data", "envcheck.png")
    os.makedirs(os.path.dirname(shot), exist_ok=True)

    say(OK, f"模式：{'无头' if headless else '有头'}   profile：{profile}")
    try:
        with browser_engine.browser_page(
            engine=os.getenv("AGENT_ENGINE", "auto"),
            headless=headless,
            user_data_dir=profile,
            proxy=os.getenv("AGENT_PROXY", ""),
        ) as page:
            t0 = time.time()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            passed = login_core._wait_out_challenge(page, timeout=wait)
            cost = time.time() - t0
            if passed:
                say(OK, f"整页挑战：已通过（{cost:.0f}s）")
            else:
                say(BAD, f"整页挑战：{wait}s 内没过去")
                say(BAD, "  -> 这台机器的 IP 被 Cloudflare 判定为高风险。")
                say(BAD, "     换引擎也没用，只能换 IP（住宅/家宽/静态原生 IP）。")

            title = page.title()
            say(OK if passed else WARN, f"页面标题：{title!r}")

            has_user = page.locator("#stacked-email").count() > 0
            say(OK if has_user else BAD,
                f"登录表单：{'已加载（#stacked-email 存在）' if has_user else '没找到，可能还在挑战页'}")

            box = login_core._widget_box(page)
            if box:
                say(OK, f"Turnstile 组件：在 ({box['x']:.0f}, {box['y']:.0f})")
            else:
                say(WARN, "Turnstile 组件：当前页面没定位到")

            tok = None
            state = "unknown"
            last_state = None
            # 只点一次：Turnstile 计算期间重复点击会取消验证重新开始。
            # 先静默等 8 秒（托管模式常常自动过），再点一次，然后等 22 秒。
            deadline = time.time() + 30
            ts_start = time.time()
            clicked = False
            while time.time() < deadline:
                tok = login_core._turnstile_token(page)
                state = login_core.turnstile_state(page)
                if state != last_state:
                    tag = OK if state == "success" else (
                        BAD if state == "error" else WARN)
                    say(tag, f"Turnstile 组件状态：{state}")
                    wtxt = _widget_text(page)
                    if wtxt:
                        say(tag, f"  组件内文字：{wtxt!r}")
                    last_state = state
                # 8 秒后无条件点一次：不看 state（verifying 可能是误判，不点就永远干等）
                if tok or state == "success":
                    break
                if (not clicked) and (time.time() - ts_start) > 8:
                    login_core._click_turnstile_checkbox(page)
                    clicked = True
                    say(WARN, "已点击 Turnstile 复选框，等待组件计算…")
                page.wait_for_timeout(1000)

            if tok:
                say(OK, f"Turnstile token：已获取（{len(tok)} 字符）")
                results["challenge"] = "ok"
            elif state == "success":
                say(OK, "Turnstile 组件：已验证通过")
                say(OK, "  读不到 token 是正常的 —— 本站走 callback 模式，")
                say(OK, "  token 只存在页面 JS 变量里，不写进 hidden input。")
                results["challenge"] = "widget_ok"
            elif state == "error":
                say(BAD, "Turnstile 组件报错（验证失败）—— 组件本身的网络/风控问题")
                results["challenge"] = "widget_error"
            elif state == "verifying":
                say(WARN, "Turnstile 组件：还在转圈计算（30s 没算完，通常是被风控降分）")
                results["challenge"] = "pending"
            elif state == "pending" and box:
                say(WARN, "Turnstile 组件：停在待验证状态（点击没生效，或需要人工做图像题）")
                results["challenge"] = "pending"
            elif box:
                say(WARN, "Turnstile 组件：定位到但读不到状态")
                results["challenge"] = "unknown"
            else:
                say(BAD, "Turnstile 组件：没加载出来")
                results["challenge"] = "no_widget"

            try:
                page.screenshot(path=shot, full_page=True)
                say(OK, f"截图：{shot}")
            except Exception:
                pass
    except Exception as e:
        say(BAD, f"打开浏览器失败：{type(e).__name__}: {e}")
        results["challenge"] = "error"


# ---------------------------------------------------------------- 结论
def summary():
    section("结论")
    eng = results.get("engine")
    ch = results.get("challenge")
    hosting = results.get("hosting")

    if not eng:
        say(BAD, "没有可用浏览器引擎，先按 README 装 patchright。")
        return

    if ch in ("ok", "widget_ok"):
        say(OK, "这台机器可以用：整页挑战通过，Turnstile 组件正常。")
        say(OK, "下一步实测真账号：")
        say(OK, "  AGENT_ENGINE=patchright AGENT_HEADLESS=false \\")
        say(OK, "  .venv/bin/python agent/nodeseek_login_agent.py --once \\")
        say(OK, "      --user 用户名 --password '密码'")
        return

    if ch == "pending":
        say(WARN, "挑战过了，但 Turnstile 一直停在待验证 —— 有两种可能：")
        say(WARN, "  1) 点击坐标没落在复选框上（看 data/envcheck.png 里的实际画面）")
        say(WARN, "  2) 组件要求人工做图像题，自动化过不去")
        say(WARN, "先直接测真登录，如果登录成功说明只是读状态的问题。")
        return

    if ch in ("unknown",):
        say(WARN, "挑战过了但状态不明。直接跑一次真登录看结果。")
        return

    if ch in ("widget_error",):
        say(BAD, "Turnstile 组件本身报错（不是登录逻辑的问题）。")
        say(BAD, "多半是这个 IP 在 Cloudflare 那边评分不够，或者组件网络被拦。")
        return

    if ch == "no_widget" and hosting:
        say(BAD, "IP 是机房 IP，Turnstile 组件都没加载出来，这台机器跑不通。")
        say(BAD, "解决方向：换住宅/家宽/原生 IP，或给浏览器挂住宅代理"
                 "（AGENT_PROXY=http://user:pass@host:port）。")
        return

    if ch in ("no_widget", "error"):
        say(BAD, "浏览器能开但验证过不去。先确认出口 IP 不是机房 IP，再试 --headless 对比。")
        return

    say(BAD, "整页挑战过不去 —— IP 信誉不够。换 IP 比换代码有用。")


def main():
    ap = argparse.ArgumentParser(description="VPS 环境自检")
    ap.add_argument("--headless", action="store_true", help="用无头模式测试（默认有头）")
    ap.add_argument("--url", default="https://www.nodeseek.com/signIn.html")
    ap.add_argument("--wait", type=int, default=45, help="等整页挑战的超时秒数")
    ap.add_argument("--skip-browser", action="store_true", help="只查 IP 和环境，不开浏览器")
    args = ap.parse_args()

    print("NodeSeek 自动登录 · VPS 环境自检")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")

    check_system()
    check_engine()
    check_ip()
    check_cf_backend()
    if args.skip_browser:
        section("5. 真实打开登录页")
        print("  （已跳过 --skip-browser）")
    else:
        check_challenge(args.url, args.headless, args.wait)
    summary()
    print()


if __name__ == "__main__":
    main()
