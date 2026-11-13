# -*- coding: utf-8 -*-
"""DeepFlood 一键登录实测脚本（不是定时任务的一部分，手动跑）。

用途：在真机上验证「复用 NS profile + 注入 NS cookie → 点一键登录」这条链路
到底通不通，并把每一步打印出来。cron 里的逻辑和它完全一致，只是这里有详细日志。

用法：
    # 用第 1 个 NS 账号（NODESEEK_COOKIE_1.txt + cookie/profiles/<账号1>）
    .venv/bin/python df_probe.py 1

    # 第 2 个账号
    .venv/bin/python df_probe.py 2

    # 只检查环境/登录态，不真的点登录
    .venv/bin/python df_probe.py 1 --check-only

    # 顺便把 DF 的 cookie 存到 cookie/DEEPFLOOD_COOKIE_<n>.txt
    .venv/bin/python df_probe.py 1 --save

    # 自定义用哪个 NS 账号的 profile（默认按 DF_NS_ACCOUNT 或账号名）
    .venv/bin/python df_probe.py 1 --ns-account 账号B

注意：
  - 必须在有 camoufox/playwright 的机器上跑（VPS 上就是 .venv）；
  - 无 DISPLAY 时会自动拉起 Xvfb（browser_engine 里已实现）；
  - 跑之前最好确认 NS 那边 cookie 还有效（bash install_cron.sh --status 第 6 节）。
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.join(_HERE, "agent")
for p in (_HERE, _AGENT):
    if p not in sys.path:
        sys.path.insert(0, p)

# 读 .env（和主脚本一致）
_ENV_FILE = os.path.join(_HERE, ".env")
if os.path.exists(_ENV_FILE):
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_FILE, override=False)
    except ImportError:
        print("!! 没装 python-dotenv，.env 不会生效")


def _safe(name: str) -> str:
    import re
    s = re.sub(r"[^0-9A-Za-z._-]+", "_", name or "").strip("_")
    return s or "account"


def _read_ns_cookie(index: int) -> str:
    path = os.path.join(_HERE, "cookie", f"NODESEEK_COOKIE_{index}.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def _resolve_ns_account(index: int, override: str = "") -> str:
    if override:
        return override.strip()
    raw = (os.getenv("DF_NS_ACCOUNT", "") or "").strip()
    if raw:
        items = [x.strip() for x in raw.replace("&", ",").split(",") if x.strip()]
        if len(items) == 1:
            return items[0]
        if len(items) > index - 1:
            return items[index - 1]
    # 回落到 DF 账号名
    raw_df = (os.getenv("DF_USER_PASS", "") or "").strip()
    items = [x.strip() for x in raw_df.split("&") if x.strip()]
    if len(items) >= index * 2 - 1:
        return items[index * 2 - 2]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("index", nargs="?", type=int, default=1, help="NS/DF 账号序号（1-based）")
    ap.add_argument("--ns-account", default="", help="强制指定用哪个 NS 账号的 profile")
    ap.add_argument("--check-only", action="store_true", help="只检查登录态，不点登录")
    ap.add_argument("--save", action="store_true", help="登录成功后写入 DEEPFLOOD_COOKIE_<n>.txt")
    ap.add_argument("--engine", default=os.getenv("AGENT_ENGINE", "auto"))
    args = ap.parse_args()

    idx = max(1, args.index)
    ns_account = _resolve_ns_account(idx, args.ns_account)
    if not ns_account:
        print("✗ 无法确定 NS 账号名。用 --ns-account 指定，或配 DF_NS_ACCOUNT")
        return 2

    ns_cookie = _read_ns_cookie(idx)
    profile = os.path.join(_HERE, "cookie", "profiles", _safe(ns_account))

    print("=" * 66)
    print(f"账号序号      : {idx}")
    print(f"NS 账号(profile): {ns_account}")
    print(f"profile 目录  : {profile}  ({'存在' if os.path.isdir(profile) else '不存在'})")
    print(f"NS cookie     : {'有（%d 字符）' % len(ns_cookie) if ns_cookie else '没有'}")
    print(f"引擎          : {args.engine}")
    print("=" * 66)

    try:
        import deepflood_login as D
        from browser_engine import browser_page, resolve_engine
    except Exception as e:
        print(f"✗ 导入失败：{type(e).__name__}: {e}")
        print("  先在 VPS 上确认：.venv/bin/python -c \"import camoufox\"")
        return 2

    try:
        print(f"实际使用的引擎：{resolve_engine(args.engine)}")
    except Exception as e:
        print(f"✗ 没有可用浏览器引擎：{e}")
        return 2

    print("\n--- 启动浏览器（持久化 profile，有头模式）---")
    try:
        with browser_page(
            engine=args.engine,
            headless=False,
            user_data_dir=profile,
            proxy=os.getenv("AGENT_PROXY", ""),
        ) as page:
            # 1. 注入 NS cookie
            if ns_cookie:
                n = D.inject_ns_cookie(page, ns_cookie)
                print(f"注入 NS cookie：{n} 条")
            else:
                print("没有 NS cookie 可注入，只能用 profile 里现存的登录态")

            # 2. 验证 NS 登录态
            print("\n--- 检查 NS 登录态 ---")
            ok = D.ns_session_ok(page)
            print(f"NS 登录态：{'✅ 已登录' if ok else '❌ 未登录（cookie 过期或 profile 没有登录态）'}")
            if not ok:
                print("\n一键登录需要 NS 处于登录态，先解决这个：")
                print("  · 检查 cookie/NODESEEK_COOKIE_%d.txt 是否还有效" % idx)
                print("  · 或先跑一次 NS 登录：.venv/bin/python browser_login.py --user <账号>")
                if args.check_only:
                    return 1

            if args.check_only:
                print("\n--check-only：到此为止")
                return 0 if ok else 1

            # 3. 走一键登录
            print("\n--- 执行 DF 一键登录 ---")
            r = D.login_via_ns_connect(page, timeout=180)

            if not r.get("cookie"):
                print(f"\n❌ 一键登录失败：[{r.get('error')}] {r.get('message', '')}")
                print("\n常见原因：")
                print("  NS_NOT_LOGGED_IN        profile/cookie 里 NS 没登录 → 先解决 NS 登录态")
                print("  NO_LOGIN_BUTTON         DF 改版了，nsSignIn 页按钮选择器要更新")
                print("  NO_POPUP                弹窗被拦 → 检查是否有头模式（不能 headless）")
                print("  NO_CONNECT_BUTTON       NS 授权页改版")
                print("  SESSION_NOT_ESTABLISHED 该 NS 账号没绑定 DF 账号（先去 DF 网页绑一次）")
                return 1

            print("\n✅ 一键登录成功")
            print(f"cookie 长度：{len(r['cookie'])}")
            print(f"有效期     ：{r.get('cookie_life', '?')}")
            print(f"cookie 预览：{r['cookie'][:120]}...")

            if args.save:
                out = os.path.join(_HERE, "cookie", f"DEEPFLOOD_COOKIE_{idx}.txt")
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "w", encoding="utf-8") as f:
                    f.write(r["cookie"])
                print(f"已写入：{out}")

            # 4. 顺便验证签到接口（不真的签，只看 cookie 认不认）
            print("\n--- 验证 DF cookie 是否被接口接受 ---")
            try:
                import requests
                s = requests.Session()
                s.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://www.deepflood.com/board",
                })
                for part in r["cookie"].split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        s.cookies.set(k.strip(), v.strip(), domain="www.deepflood.com")
                resp = s.get("https://www.deepflood.com/api/account/credit/page-1", timeout=20)
                data = resp.json() if resp.text.strip().startswith("{") else {}
                if data.get("success"):
                    print("✅ cookie 有效（credit 接口返回 success）")
                else:
                    print(f"⚠ credit 接口返回：{data.get('message', resp.text[:80])}")
                    print("   （不影响签到 —— 签到接口才是权威，脚本里已做 lenient 处理）")
            except Exception as e:
                print(f"⚠ 验证请求失败（可忽略）：{type(e).__name__}: {e}")

            return 0
    except Exception as e:
        print(f"\n✗ 浏览器阶段异常：{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
