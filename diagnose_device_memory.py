#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断：NodeSeek 的「记住设备」现在到底是什么状态。

背景（这是本项目能不能完全自动的关键）：

  NodeSeek 的登录分两步：密码 → 邮箱验证码。管理员说「普通登录会记住用户设备，
  登陆过的设备不会要求邮箱验证」——所以理论上验证码只需要人工做一次。

  但登录 cookie（session/pjwt/smac）约 30 天会过期。真正的问题是：
      cookie 过期后重新登录，这台设备还会被「记住」吗？
      · 会  → 完全自动，永远不需要人工
      · 不会 → 每 30 天就要人工收一次码，脚本就没意义了

  官方从没承诺过这一点，社区也没有可靠结论。**这个工具就是用来实测的。**

三种用法（在跑项目的机器上执行）：

  1) 看现状（只读，秒出结果，不碰任何登录流程）
       python3 diagnose_device_memory.py status

  2) 模拟「cookie 掉了一半」——删掉登录态 cookie，但保留 profile
     里的设备痕迹（localStorage / 指纹），然后看还免不免验证码
       python3 diagnose_device_memory.py simulate

  3) 完整重跑一次密码登录，看服务器这次要不要验证码
       python3 diagnose_device_memory.py probe

用法 2 的含义：真实场景是「30 天后 cookie 自然过期」，我们没法等 30 天，
但「主动删掉登录态 cookie」产生的效果是一样的——如果这时密码登录
仍然直接成功、没有 redirect 到 emailSignIn，就说明设备记忆是存在的，
且不依赖那个 session cookie；那么 30 天后也不会突然要人工。

退出码：
  0 = 免验证码（完全自动成立）
  2 = 需要邮箱验证码（设备记忆失效，必须配自动收码）
  1 = 执行出错
"""

from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT_DIR = os.path.join(_HERE, "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

PROFILE_ROOT = os.path.join(_HERE, "cookie", "profiles")
AUTH_COOKIE_NAMES = ("pjwt", "session", "smac")


def _load_env():
    env_path = os.path.join(_HERE, ".env")
    if not os.path.exists(env_path):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except Exception:
        # 没有 python-dotenv 就手动解析最简单的 KEY=VALUE
        try:
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception:
            pass


def _accounts():
    """从 NS_USER_PASS 里解析账号列表。"""
    raw = os.getenv("NS_USER_PASS", "") or os.getenv("NS_USER_PASS", "")
    items = [p.strip() for p in raw.replace(",", "&").split("&") if p.strip()]
    out = []
    i = 0
    while i + 1 < len(items) + 1 and i < len(items):
        user = items[i]
        pwd = items[i + 1] if i + 1 < len(items) else ""
        out.append((user, pwd))
        i += 2
    return out


def _profile_for(user: str) -> str:
    import re
    slug = re.sub(r"[^0-9A-Za-z._-]+", "_", user or "").strip("_") or "account"
    return os.path.join(PROFILE_ROOT, slug)


def _read_profile_cookie_db(profile_dir: str) -> list:
    """直接读 Firefox/Camoufox profile 的 cookies.sqlite。

    不用开浏览器就能看到「这台设备记住了什么」，以及每个 cookie 的到期时间。
    Camoufox 底层是 Firefox，cookie 存在 cookies.sqlite 里。
    """
    import sqlite3
    db = os.path.join(profile_dir, "cookies.sqlite")
    if not os.path.exists(db):
        return []
    rows = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        try:
            cur.execute(
                "SELECT name, host, path, expiry, isSecure FROM moz_cookies "
                "WHERE host LIKE '%nodeseek%' OR host LIKE '%nseek%'"
            )
            rows = cur.fetchall()
        finally:
            con.close()
    except Exception as e:
        print(f"  读 cookies.sqlite 失败：{type(e).__name__}: {e}")
    return rows


def cmd_status():
    """只读：看看 profile 里现在有什么、什么时候过期。"""
    _load_env()
    accounts = _accounts()
    if not accounts:
        print("没读到 NS_USER_PASS，先在 .env 里配置")
        return 1

    for user, _pwd in accounts:
        profile = _profile_for(user)
        print(f"\n{'='*56}\n账号 {user}\nprofile: {profile}")
        if not os.path.exists(profile):
            print("  profile 不存在 —— 这台设备还没登录过")
            continue

        rows = _read_profile_cookie_db(profile)
        if not rows:
            print("  profile 里没有 nodeseek 的 cookie（可能已清或换了位置）")
        else:
            print("  nodeseek cookie：")
            now = time.time()
            for name, host, path, expiry, secure in rows:
                if expiry and expiry > 0:
                    left = (expiry - now) / 86400.0
                    tag = f"还剩 {left:.1f} 天"
                else:
                    tag = "会话 cookie（关浏览器即失效）"
                mark = " ★登录态" if name in AUTH_COOKIE_NAMES else ""
                print(f"    {name:<16} {tag}{mark}")

        # localStorage 里的设备痕迹
        ls_dir = os.path.join(profile, "storage", "default", "localhost",
                              "sessionStorage")
        ls_dir2 = os.path.join(profile, "storage", "default", "https+++www.nodeseek.com",
                               "localStorage")
        found = False
        for d in (ls_dir, ls_dir2):
            if os.path.isdir(d):
                names = set(os.listdir(d))
                for key in names:
                    if "security_token" in key:
                        print(f"  发现设备痕迹：{key}（{d}）")
                        found = True
        if not found:
            print("  没在 localStorage 里找到 security_token 痕迹")

    print(f"\n{'='*56}")
    print("想进一步实测，跑：python3 diagnose_device_memory.py simulate")
    return 0


def cmd_probe():
    """真跑一次密码登录，看服务器这次要不要邮箱验证码。"""
    _load_env()
    accounts = _accounts()
    if not accounts:
        print("没读到 NS_USER_PASS")
        return 1

    try:
        from browser_engine import browser_page
        import login_core
    except Exception as e:
        print(f"导入浏览器模块失败：{e}")
        return 1

    for user, pwd in accounts:
        if not pwd:
            print(f"\n账号 {user} 没有密码，跳过")
            continue
        profile = _profile_for(user)
        print(f"\n{'='*56}\n账号 {user}  profile={profile}")
        try:
            with browser_page(
                engine=os.getenv("AGENT_ENGINE", "auto"),
                headless=False,
                user_data_dir=profile,
                proxy=os.getenv("AGENT_PROXY", ""),
            ) as page:
                result = login_core.login(page, user, pwd)
        except login_core.LoginError as e:
            if e.code == "NEED_EMAIL_VERIFY":
                print(f"\n>>> 结论：{user} **需要邮箱验证码**")
                print(">>> 设备记忆没起作用 → 必须配自动收码才能完全自动")
                return 2
            print(f"\n>>> 结论：登录失败 [{e.code}] {e.message}")
            return 1
        except Exception as e:
            print(f"\n>>> 异常：{type(e).__name__}: {e}")
            return 1

        print(f"\n>>> 结论：{user} **免验证码登录成功**")
        print(f">>> cookie 有效期：{result.get('cookie_life')}")
        print(">>> 设备记忆有效，30 天后重新登录应该也不需要人工")
        return 0
    return 1


def cmd_simulate():
    """模拟「30 天后 cookie 过期」：删掉登录态 cookie，但保留 profile。

    然后重新登录，看还免不免验证码。
    这正是 30 天后会发生的事，只是把它提前了。
    """
    _load_env()
    accounts = _accounts()
    if not accounts:
        print("没读到 NS_USER_PASS")
        return 1

    try:
        from browser_engine import browser_page
        import login_core
    except Exception as e:
        print(f"导入浏览器模块失败：{e}")
        return 1

    for user, pwd in accounts:
        if not pwd:
            print(f"\n账号 {user} 没有密码，跳过")
            continue
        profile = _profile_for(user)
        if not os.path.exists(profile):
            print(f"\n账号 {user} 的 profile 不存在，跳过")
            continue

        print(f"\n{'='*56}\n账号 {user}")
        print(f"profile: {profile}")
        print("\n[1/2] 打开浏览器，删掉登录态 cookie（模拟 30 天后过期）")
        try:
            with browser_page(
                engine=os.getenv("AGENT_ENGINE", "auto"),
                headless=False,
                user_data_dir=profile,
                proxy=os.getenv("AGENT_PROXY", ""),
            ) as page:
                # 先看一眼现状
                before = page.context.cookies()
                auth_before = [c["name"] for c in before if c["name"] in AUTH_COOKIE_NAMES]
                print(f"      删除前登录态 cookie：{auth_before or '（本来就没有）'}")

                deleted = []
                for c in before:
                    if c["name"] in AUTH_COOKIE_NAMES:
                        try:
                            page.context.clear_cookies(name=c["name"],
                                                       domain=c.get("domain"))
                            deleted.append(c["name"])
                        except Exception:
                            pass
                # 老版本 API 只支持 name+domain/path
                if not deleted:
                    try:
                        page.context.clear_cookies(name="session", domain=".nodeseek.com")
                        page.context.clear_cookies(name="pjwt", domain=".nodeseek.com")
                        page.context.clear_cookies(name="smac", domain=".nodeseek.com")
                        deleted = ["session", "pjwt", "smac"]
                    except Exception as e:
                        print(f"      清 cookie 失败：{type(e).__name__}")
                print(f"      已删除：{deleted}")

                # 关键：profile 里的 localStorage / 指纹全留着，
                # 这就是「同一台设备，只是 cookie 过期了」的真实状态
                print("\n[2/2] 用同一个 profile 重新登录（密码）…")
                try:
                    result = login_core.login(page, user, pwd)
                except login_core.LoginError as e:
                    if e.code == "NEED_EMAIL_VERIFY":
                        print(f"\n>>> 结论：删掉登录态 cookie 后**仍需要邮箱验证码**")
                        print(">>> 说明：设备记忆 = 那个登录态 cookie 本身")
                        print(">>> 结论：30 天 cookie 一掉，就必须人工收码")
                        print(">>> → 必须配 NS_CODE_FILE / NS_CODE_URL / IMAP 才能完全自动")
                        return 2
                    print(f"\n>>> 结论：登录失败 [{e.code}] {e.message}")
                    return 1
                except Exception as e:
                    print(f"\n>>> 异常：{type(e).__name__}: {e}")
                    return 1

                print(f"\n>>> 结论：**免验证码登录成功**")
                print(f">>> cookie 有效期：{result.get('cookie_life')}")
                print(">>> 说明：设备记忆独立于登录态 cookie")
                print(">>> → 30 天后 cookie 过期，重新登录也不需要人工，完全自动成立")
                return 0
        except Exception as e:
            print(f"异常：{type(e).__name__}: {e}")
            return 1
    return 1


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if mode == "status":
        return cmd_status()
    if mode == "simulate":
        return cmd_simulate()
    if mode == "probe":
        return cmd_probe()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
