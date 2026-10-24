# NodeSeek 多账号自动签到（浏览器登录版）

NodeSeek / DeepFlood 多账号自动签到。Cookie 失效时用**真实浏览器**重新登录，不再依赖 CloudFreed 打码。

> 2026-10 起 NodeSeek 的登录接口增加了 Cloudflare Turnstile、FingerprintJS 指纹、`security_token` 三重校验，
> 纯 HTTP 请求（含打码平台）已经无法登录。本项目改成「真浏览器登录 → 拿 cookie → 纯 HTTP 签到」，
> 可直接跑在 VPS 上，也可以把登录那一步单独放到住宅/家宽 IP 的机器上。

---

## 一、先搞清楚一件事：决定成败的是 IP，不是性能

浏览器登录只要 **1 核 1G** 就能跑（Chromium 单实例约 400~700MB）。

真正卡人的是出口 IP 的「干净度」：

| IP 类型 | 整页挑战 | Turnstile | 结论 |
|---|---|---|---|
| 家宽 / 住宅 / 企业宽带 | 几秒自动过 | 正常出 token | 能跑 |
| 普通机房 VPS（搬瓦工、Vultr、阿里云…） | 大概率卡在 "Just a moment" | 组件加载不出来或无限循环 | 大概率跑不通 |
| 少人用的原生 IP VPS | 有时能过 | 有时正常 | 需要实测 |

**别猜，跑一条命令就知道：**

```bash
python3 check_env.py
```

它会查出口 IP 的 ASN、是不是机房 IP，然后真的打开登录页走一遍挑战，最后给结论。
看到 `IP 类型：机房/云服务器` 且挑战不过，就别在代码上折腾了，换 IP 才有用。

---

## 二、三种部署形态

| 形态 | 说明 | 适合 |
|---|---|---|
| **A. 全部在本机** | VPS 上直接跑脚本，浏览器也在本机 | 单台机器、IP 干净 |
| **B. 青龙在 Docker，登录在宿主机** | 青龙调 `http://172.17.0.1:3000` | 青龙容器重建不丢浏览器环境（推荐） |
| **C. 登录放在另一台机器** | 青龙调 `http://家宽IP:3000` | 青龙机器 IP 不干净，但有家宽机器 |

三种形态代码是同一套，只差环境变量。

---

## 三、快速开始（Deiban / Ubuntu）

```bash
git clone <本仓库地址> nodeseek-autosign
cd nodeseek-autosign

# 装系统依赖 + Chromium + venv + 生成 .env（约 3~5 分钟）
bash install_vps.sh

# 体检：先确认这台机器的 IP 行不行
.venv/bin/python check_env.py

# 填账号
vi .env            # NS_USER_PASS=用户名&密码

# 跑一次
.venv/bin/python multi_site_sign_cookie_only.py
```

跑通了再接定时任务：

```cron
# 每天 08:10 左右（加随机分钟，避免固定特征）
10 8 * * * cd /opt/nodeseek-autosign && .venv/bin/python multi_site_sign_cookie_only.py >> logs/sign.log 2>&1
```

### 青龙在 Docker 里（形态 B）

浏览器环境装在**宿主机**，青龙容器只负责调用：

```bash
# 宿主机
bash install_vps.sh --agent      # 注册 systemd 服务，监听 0.0.0.0:3000
curl -s http://127.0.0.1:3000/health
```

青龙环境变量：

```
LOGIN_AGENT_URL=http://172.17.0.1:3000
LOGIN_AGENT_TOKEN=<.env 里的 AGENT_TOKEN>
NS_USER_PASS=用户1&密码1&用户2&密码2
```

> 安全：agent 只应在内网/本机可达。如果 VPS 有公网 IP，务必用防火墙挡掉 3000 端口，
> 只放行 docker 网段：`ufw allow from 172.17.0.0/16 to any port 3000`。

---

## 四、多账号模式

一个环境变量搞定，`&` 分隔、两两一组：

```bash
# 3 个账号
NS_USER_PASS=alice&alice_pass&bob&bob_pass&carol&carol_pass
```

每个账号都是**完全隔离**的：

| 项目 | 隔离方式 |
|---|---|
| 浏览器 profile | `cookie/profiles/<用户名>/`，指纹、cf_clearance、localStorage 各自独立 |
| cookie 缓存 | `cookie/<站点>_<序号>.json` |
| 失败隔离 | 一个账号登录失败不会中断其他账号，最后汇总结果 |
| 通知 | 一条汇总消息，标明哪个账号失败 |

登录是**串行**的（`AGENT_MAX_CONCURRENCY=1`）。同一个 IP 同时开多个浏览器登录，Cloudflare 立刻能看出异常，
所以宁可慢一点。3 个账号大约 1~2 分钟。

**账号数量建议：** 同一个出口 IP 上 5 个以内比较稳。再多，尤其是新注册账号，容易连带触发风控。
账号多的话，用 `LOGIN_AGENT_URLS` 分散到多台机器：

```bash
LOGIN_AGENT_URLS=http://ip1:3000,http://ip2:3000,http://ip3:3000
```

同一账号会按用户名哈希**固定**走同一台机器（指纹稳定，不会每次换 IP 触发验证），失败才顺延到下一台。

### 兼容的旧写法

```bash
NS_USER=alice&bob            # 配合 NS_PASS=pass1&pass2
NS_COOKIE=sessionid=xxx&sessionid=yyy   # 纯 cookie，不登录
```

有 cookie 就先用 cookie，失效了才去登录。

---

## 五、主要配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `NS_USER_PASS` | — | 账号密码，`&` 分隔，两两一组 |
| `LOGIN_AGENT_URL` | 空 | 空 = 用本机浏览器；填了 = 用远程 agent |
| `LOGIN_AGENT_URLS` | 空 | 多台 agent，按账号哈希固定分配 |
| `LOGIN_AGENT_TOKEN` | — | 和 agent 的 `AGENT_TOKEN` 一致 |
| `LOGIN_AGENT_TIMEOUT` | 240 | 登录超时秒数，要等 Turnstile，别设小 |
| `LOGIN_LOCAL_FALLBACK` | true | agent 失败后是否再用本机浏览器试一次 |
| `LOGIN_LOCAL_HEADLESS` | false | 本地浏览器默认有头；设 true 走无头（过不了 CF） |
| `AGENT_ENGINE` | auto | `patchright` / `camoufox` / `playwright` |
| `AGENT_PROXY` | 空 | 给浏览器挂代理，如 `http://user:pass@host:port` |
| `NS_TOTP_SECRET` | 空 | 账号开了 2FA 时填 Base32 密钥 |
| `NS_RANDOM` | true | 签到时间随机抖动 |

---

## 六、排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 卡在 "Just a moment" | IP 被 CF 判高风险 | 换 IP 或挂住宅代理，换引擎没用 |
| 页面弹「人机验证服务加载失败」 | 无头模式 / 点错了隐藏元素 | 用有头（默认），代码已改成坐标点击 |
| `TURNSTILE_NETWORK` | 访问不到 `challenges.cloudflare.com` | 检查 DNS / 出网，机房 IP 常见 |
| `BAD_CREDENTIALS` | 密码错，或需要邮箱验证 | 先手动网页登录一次 |
| `NEED_2FA` | 账号开了两步验证 | 配 `NS_TOTP_SECRET` |
| `PAGE_ERROR` | 站点改版，选择器失效 | 看 `data/debug_login_page.png`，改 `agent/login_core.py` |
| `xvfb 相关报错` | 没装 Xvfb | `apt install -y xvfb`（install 脚本已装） |
| 签到 403 但登录正常 | 签到这一步被 WAF 拦 | 用 `POST /sign` 让 agent 代签 |

更详细的部署说明见 [部署指南_账号密码自动登录.md](部署指南_账号密码自动登录.md)。

---

## 七、目录结构

```
.
├── install_vps.sh                 一键安装（系统依赖 / Chromium / venv / systemd）
├── check_env.py                   VPS 体检：IP 类型 + 真实挑战测试
├── multi_site_sign_cookie_only.py 主脚本：签到 + 登录恢复
├── browser_login.py               登录适配层（远程 agent 优先，本地兜底）
├── notify.py                      通知（TG / 企业微信 / 钉钉 / Bark…）
├── agent/
│   ├── browser_engine.py          引擎适配 + 自动 Xvfb + 反检测参数
│   ├── login_core.py              登录流程（挑战、Turnstile、表单、2FA）
│   ├── nodeseek_login_agent.py    Flask 服务：/login /sign /cookie /health
│   ├── Dockerfile
│   └── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## 八、说明

- 本工具只做「自动点击签到」，不刷帖、不刷积分、不做任何互动行为。
- 请遵守 NodeSeek 的站点规则；账号因自动化被封与本项目无关。
- 密码保存在本地 `.env`，注意文件权限（`chmod 600 .env`），不要提交到仓库。
- `.gitignore` 已排除 `.env`、`cookie/`、`data/`、`.venv/`。
