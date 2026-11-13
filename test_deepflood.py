# -*- coding: utf-8 -*-
"""DeepFlood 接入的回归测试：重点证明「NS 行为逐字未变」。"""
import io
import json
import os
import sys

os.environ.setdefault("NS_RANDOM", "true")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# --- 本机没装 curl_cffi / requests 时用最小桩顶掉，只测逻辑 ---
import types


def _install_stubs():
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass

    req = types.ModuleType("requests")

    class _S:
        def __init__(self):
            self.headers = {}
            self.cookies = types.SimpleNamespace(set=lambda *a, **k: None)

        def get(self, *a, **k):
            raise RuntimeError("网络被禁用（测试桩）")

        def post(self, *a, **k):
            raise RuntimeError("网络被禁用（测试桩）")

    req.Session = _S
    sys.modules["requests"] = req

    cffi = types.ModuleType("curl_cffi")
    cffi.requests = req
    sys.modules["curl_cffi"] = cffi


_install_stubs()

import multi_site_sign_cookie_only as M


class Resp:
    def __init__(self, payload, code=200):
        self._p = payload
        self.status_code = code
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        return self._p


class FakeSession:
    """记录最后一次 POST 的 URL，返回预设响应。"""
    def __init__(self, payload, code=200):
        self.payload = payload
        self.code = code
        self.last_post_url = None
        self.last_headers = None
        self.cookies = []

    def get(self, *a, **k):
        return Resp({}, 200)

    def post(self, url, headers=None, **k):
        self.last_post_url = url
        self.last_headers = headers
        return Resp(self.payload, self.code)


PASS = FAIL = 0


def chk(name, got, want):
    global PASS, FAIL
    if got == want:
        print(f"✅ {name}")
        PASS += 1
    else:
        print(f"❌ {name}\n     期望={want!r}\n     实际={got!r}")
        FAIL += 1


NS = M.SITES_CONFIG["nodeseek"]
DF = M.SITES_CONFIG["deepflood"]

# ---------------- sign()：NS ----------------
print("=== 1. NS 签到判定（必须与改动前一致）===")

case = FakeSession({"success": True, "message": "今天的签到收益是5个鸡腿"})
M.create_session = lambda c=None: case
st = M.sign("k=v", NS, "true")
chk("NS 成功（鸡腿）", st[0], "success")
chk("NS 用环境变量的 random=true", case.last_post_url.endswith("random=true"), True)
chk("NS 请求了正确的域名", case.last_post_url.startswith("https://www.nodeseek.com/api/attendance"), True)

case2 = FakeSession({"success": False, "message": "今天已完成签到，请勿重复操作"})
M.create_session = lambda c=None: case2
chk("NS 已完成签到 → already", M.sign("k=v", NS, "true")[0], "already")

# 关键：这句在改动前是 fail（NS 没配 already_keywords），现在必须仍是 fail
case3 = FakeSession({"success": False, "message": "今天已签到"})
M.create_session = lambda c=None: case3
chk("NS 未配置的关键词不生效（今天已签到→fail，与旧版一致）",
    M.sign("k=v", NS, "true")[0], "fail")

# NS 的 random 仍受 env 控制
case4 = FakeSession({"success": True, "message": "鸡腿"})
M.create_session = lambda c=None: case4
M.sign("k=v", NS, "false")
chk("NS 传 false 时 random=false（env 仍然生效）",
    case4.last_post_url.endswith("random=false"), True)

# ---------------- sign()：DF ----------------
print("\n=== 2. DF 签到 ===")

case5 = FakeSession({"success": True, "message": "今日签到获得鸡腿11个，当前排名第9"})
M.create_session = lambda c=None: case5
st = M.sign("k=v", DF, "false")   # 故意传 false，验证站点配置优先
chk("DF 成功", st[0], "success")
chk("DF 域名正确", case5.last_post_url.startswith("https://www.deepflood.com/api/attendance"), True)
chk("DF 即使 env 传 false 也用 random=true（试试手气）",
    case5.last_post_url.endswith("random=true"), True)

case6 = FakeSession({"success": False, "message": "今天已签到"})
M.create_session = lambda c=None: case6
chk("DF 已签到关键词生效 → already", M.sign("k=v", DF, "true")[0], "already")

case7 = FakeSession({"success": False, "message": "USER NOT FOUND", "status": 404})
M.create_session = lambda c=None: case7
chk("DF cookie 失效 → invalid", M.sign("k=v", DF, "true")[0], "invalid")

# ---------------- 不自动登录 ----------------
print("\n=== 3. DF 不触发 NS 的浏览器登录 ===")

called = {"recover": 0, "validity": 0}
M.recover_cookie = lambda *a, **k: (called.__setitem__("recover", called["recover"] + 1), (None, {}))[1]
M.check_cookie_validity = lambda *a, **k: (called.__setitem__("validity", called["validity"] + 1), True)[1]

# 造一个「配了账号密码、但没有 cookie」的场景：
# account_count 才会是 1，流程才会走到「没 cookie → 要不要自动登录」这一步。
os.environ["DF_USER_PASS"] = "df_user&df_pass"
os.environ["NS_USER_PASS"] = "ns_user&ns_pass"
os.environ["DF_COOKIE"] = ""
os.environ["NS_COOKIE"] = ""
M.load_cookies_from_file_orig = M.load_cookies_from_file
M.load_cookies_from_file = lambda *a, **k: ""
M.send = lambda *a, **k: None
M.NOTIFICATION_FILE = os.devnull
try:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    M.process_site("deepflood", DF, "true")
    sys.stdout = old
finally:
    M.load_cookies_from_file = M.load_cookies_from_file_orig

out = buf.getvalue()
chk("DF 无 cookie 时不调用 recover_cookie", called["recover"], 0)
chk("DF 给出明确提示（未开启自动登录）", "未开启自动登录" in out, True)

# NS 仍会调用 recover_cookie（证明 NS 那条路没被削弱）
called["recover"] = 0
M.load_cookies_from_file = lambda *a, **k: ""
buf2 = io.StringIO()
old = sys.stdout
sys.stdout = buf2
try:
    M.process_site("nodeseek", NS, "true")
finally:
    sys.stdout = old
chk("NS 无 cookie 时仍会尝试自动登录（行为未削弱）", called["recover"] >= 1, True)

print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
