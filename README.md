# NodeSeek 多账号自动签到（浏览器登录版）

NodeSeek 多账号自动签到。Cookie 失效时用**真实浏览器**重新登录，不再依赖 CloudFreed 打码。

> 2026-10 起 NodeSeek 的登录接口增加了 Cloudflare Turnstile、FingerprintJS 指纹、`security_token` 三重校验，
> 纯 HTTP 请求（含打码平台）已经无法登录。本项目改成「真浏览器登录 → 拿 cookie → 纯 HTTP 签到」，
> 整套装在 VPS 上就能跑，不需要额外的机器。

---

## 一、三层结构：搞清楚哪一层要什么

NodeSeek 的登录/签到其实是三件事叠在一起，各层依赖的东西不一样：

| 层 | 内容 | 需要什么 | 多久碰一次 |
|---|---|---|---|
| ① Cloudflare 挑战 | Turnstile 组件 + FingerprintJS 指纹 | 真浏览器（**Camoufox**）+ 有头 + Xvfb | 只有重新登录时 |
| ② 账号二次验证 | 邮箱动态验证码 | 绑定邮箱能收码 | 每台新设备一次 |
| ③ 日常签到 | `POST /api/attendance` | 纯 cookie + `curl_cffi` | 每天 |

跑之前先体检一下（了解自己的机器环境）：

```bash
python3 check_env.py
```

它会真的打开登录页走一遍挑战，最后给结论。

### 「每台新设备一次」——到底会不会变成「每 30 天一次」？

第②层的邮箱验证码是本项目唯一需要人工的地方，它决定了项目有没有意义。事实是：

| 事实 | 证据 |
|---|---|
| NodeSeek 会「记住设备」 | 管理员原话：「普通登录会记住用户设备，登陆过的设备不会要求邮箱验证」 |
| 同一 profile 第二次登录**免码** | 已验证：走完邮箱验证后，再次登录直接成功，没有验证码 |
| 登录 cookie 约 30 天过期 | 社区结论：「按日期看登录后一个月就得失效了」 |
| 30 天后重新登录还要不要码？ | **官方没承诺过** —— 所以项目不能靠"应该不会"来赌 |

针对这最后一条，本项目做了两件事把不确定性消掉：

**① 主动续期** —— 不等 cookie 掉。登录成功时记下每个 cookie 的到期时间，
剩余寿命不足 5 天（`NS_RENEW_BEFORE_DAYS`）就**主动重新登录换新**。
因为用的是同一个持久化 profile，NodeSeek 记得这台设备，这一步**不需要验证码**。
→ cookie 永远不会「突然掉」，也就永远不会「突然要人工」。

**② 设备记忆失效时立刻告警** —— 万一 NodeSeek 换了策略，脚本能分辨出这个信号
（profile 里有登录痕迹却仍被要求验证码），立刻推 Telegram，并直接告诉你怎么办。
→ 项目不会「静默失败好几天」。

想自己验证这个结论，不用等 30 天：

```bash
python3 diagnose_device_memory.py status     # 看现状：记住了什么、还剩几天
python3 diagnose_device_memory.py simulate   # 模拟 30 天后 cookie 过期，重新登录看要不要码
```

`simulate` 的结论直接决定要不要配自动收码：**要** → 按
[部署指南 6.5 节](部署指南_账号密码自动登录.md)配 `NS_CODE_FILE` / `NS_CODE_URL` / `NS_IMAP_*`，
脚本就能自己收验证码自己填，做到**真正无人值守**；**不要** → 现在这套就已经完全自动了。

> 自动收码**默认关闭**。不配就是「脚本发码 → 停在命令行等你把码粘进来」，
> 什么都不用配。配了之后是**脚本自己去连你的邮箱服务器（或你指定的取码接口）**取码，
> 不需要把邮箱登录密码交出去。

---

## 二、资源占用（按「尽量小」设计）

浏览器**不是常驻的**，只在 cookie 失效需要登录时开一次，登录完立刻退出：

| 阶段 | 内存 | CPU | 持续 |
|---|---|---|---|
| 常驻 agent（Flask） | 35~50 MB | 约 0 | 一直 |
| 登录（Camoufox 有头） | 峰值 500~800 MB | 瞬时 1 核 60~100% | 30~60 秒 |
| 日常签到 | < 20 MB | 忽略 | 几秒 |
| 环境占用（venv + Camoufox） | — | — | 约 2 GB 磁盘 |

省资源的具体做法：

- **有 cookie 就不开浏览器**。cookie 有效期通常很长，日常签到几乎零消耗。
- **profile 复用**。`cf_clearance` 在有效期内可以跳过挑战，连那 30 秒都省了。
- **多账号串行**（`AGENT_MAX_CONCURRENCY=1`），不会同时开好几个浏览器。
- Camoufox 启动参数已关掉后台网络、组件更新、同步、崩溃上报等无用子进程，但不碰 canvas / webgl / 字体（影响指纹）。
- Docker 侧有 `mem_limit: 1g / cpus: 1.5`，systemd 侧是 `MemoryHigh=1G` + `Nice=10`（软限制，不会直接杀进程）。

所以 1 核 1G 的机器完全够用，前提是加了 swap（`install_vps.sh` 会自动判断并创建）。

---

## 三、快速开始（Debian 12/13、Ubuntu 22.04+）

```bash
git clone https://github.com/EmersonLopez2005/nodeseek-autosign.git
cd nodeseek-autosign

# 装系统依赖 + Camoufox 浏览器 + venv + 生成 .env
# （浏览器本体约 1.3GB，下载慢是正常的；国内机器可以挂代理再跑）
bash install_vps.sh

# 体检：看看这台机器的出口 IP 和挑战情况
.venv/bin/python check_env.py

# 填账号
vi .env            # NS_USER_PASS=用户名&密码

# 跑一次（首次可能会问你要邮箱验证码，输一次就记住了这台设备）
.venv/bin/python multi_site_sign_cookie_only.py

# 确认「记住设备」生效了：看这台设备记住了什么、cookie 还剩几天
.venv/bin/python diagnose_device_memory.py status

# 想验证 30 天后 cookie 掉了还要不要人工（会删掉当前 cookie，测完重跑一次登录即可）
.venv/bin/python diagnose_device_memory.py simulate
```

跑通了再接定时任务：

```cron
# 每天 08:10（分钟数故意错开，避免固定特征）
# flock 防止上一轮没跑完就叠一轮；日志落在项目 logs/ 下
10 8 * * * cd /root/nodeseek-autosign && mkdir -p logs && flock -n /tmp/nodeseek-sign.lock .venv/bin/python multi_site_sign_cookie_only.py >> logs/sign.log 2>&1
```

> **无人值守的前提**：浏览器 profile 里已经记住了这台设备（第一次手动跑过一次登录）。
> 之后日常签到只发两个 HTTP 请求，不开浏览器、不需要验证码。
>
> cookie 约 30 天过期，脚本会**提前 5 天主动换新**（`NS_RENEW_BEFORE_DAYS`），
> 换新用的是同一个 profile，NodeSeek 记得这台设备 → 依然不需要验证码。
> 万一设备记忆真的失效了，你会收到一条「⚠️ 设备记忆失效」的 Telegram 告警，
> 里面直接写着该怎么做 —— 不会静默失败好几天。

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
| 2FA 密钥 | `NS_TOTP_SECRETS=alice:KEY1&bob:KEY2` 按用户名匹配 |
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
NS_COOKIE=session=xxx; pjwt=yyy&session=zzz; pjwt=www   # 纯 cookie，不登录
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
| `NS_RENEW_BEFORE_DAYS` | 5 | cookie 剩余寿命不足几天就主动换新（设 0 关闭） |
| `NS_CODE_FILE` | 空 | 验证码落地文件路径（自动收码，不需要交出邮箱密码） |
| `NS_CODE_URL` | 空 | HTTP 取码地址，返回纯文本验证码 |
| `NS_IMAP_HOST` / `NS_IMAP_USER` / `NS_IMAP_PASS` | 空 | IMAP 自动收码（用邮箱授权码，不是登录密码） |
| `NS_CODE_TIMEOUT` | 180 | 等验证码的最长秒数 |
| `NS_RANDOM` | true | 签到时间随机抖动 |
| `TG_BOT_TOKEN` / `TG_USER_ID` | 空 | Telegram 推送 |

通知渠道全部通过环境变量配，不用改代码，支持：

> Telegram、企业微信（机器人 / 应用）、钉钉、飞书、Bark、Server酱、PushPlus、
> PushDeer、Gotify、iGot、Qmsg、SMTP 邮件、Synology Chat、自定义 Webhook

完整变量和获取方法见 [`.env.example`](.env.example) 的「通知服务」段，配几个就推几个。

> 服务器在国内或访问不了 `api.telegram.org` 时，用 `TG_API_HOST` 填反代域名，
> 或用 `TG_PROXY_HOST` / `TG_PROXY_PORT` 走本机代理。

---

## 六、排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 卡在 "Just a moment" | IP 被 CF 判高风险 | 换 IP 或挂代理（`AGENT_PROXY`），换引擎没用 |
| 页面弹「人机验证服务加载失败」 | 无头模式 / 点错了隐藏元素 | 用有头（默认），代码已改成坐标点击 |
| 日志里 `Turnstile 组件状态：verifying` 卡 30 秒 | 组件在转圈计算，期间被重复点击会取消重来 | 已修：现在只点 1~2 次，间隔 ≥15 秒。若仍卡住说明 IP 评分不够 |
| `TURNSTILE_INTERACT`（弹「人机验证服务加载失败」） | Turnstile 把点击判成 bot：点击按住时长≈0，或 WebGL 暴露 SwiftShader | 已修：按下-松开间隔 60~140ms；去掉 `--disable-gpu`；**必须 `apt install -y libgl1-mesa-dri`** |
| `BAD_CREDENTIALS` | 密码错，或需要邮箱验证 | 先手动网页登录一次 |
| `NEED_2FA` | 账号开了两步验证 | 配 `NS_TOTP_SECRET` |
| `PAGE_ERROR` | 站点改版，选择器失效 | 看 `data/debug_login_page.png`，改 `agent/login_core.py` |
| `xvfb 相关报错` | 没装 Xvfb | `apt install -y xvfb`（install 脚本已装） |
| 签到 403 但登录正常 | 签到这一步被 WAF 拦 | 用 `POST /sign` 让 agent 代签 |
| 提示未配置 `CLOUDFREED_API_KEY` | 旧版遗留兜底逻辑 | 已修：默认不再调用 CloudFreed，不需要配 |
| 收到「⚠️ 设备记忆失效」告警 | 这台设备之前登录过，这次仍要邮箱验证码 | NodeSeek 改了策略/你换了 IP。手动跑一次登录，再按[部署指南 6.5](部署指南_账号密码自动登录.md)配自动收码根治 |
| 「当前不是交互式终端，无法手动输入验证码」 | cron 里跑且没配自动收码 | 配 `NS_CODE_FILE` / `NS_CODE_URL` / `NS_IMAP_*`（见部署指南 6.5） |
| 想知道 30 天后要不要人工 | —— | `python3 diagnose_device_memory.py simulate` |

更详细的部署说明见 [部署指南_账号密码自动登录.md](部署指南_账号密码自动登录.md)。

---

## 七、目录结构

```
.
├── install_vps.sh                 一键安装（系统依赖 / Camoufox / venv / systemd）
├── check_env.py                   VPS 体检：IP 类型 + 真实挑战测试
├── diagnose_device_memory.py      诊断：设备记忆还在不在（要不要验证码）
├── multi_site_sign_cookie_only.py 主脚本：签到 + 主动续期 + 登录恢复
├── browser_login.py               登录适配层（远程 agent 优先，本地兜底）
├── notify.py                      通知（TG / 企业微信 / 钉钉 / Bark…）
├── nodeseek_cookie_sync.user.js   油猴脚本：浏览器 cookie 同步到青龙（可选）
├── 使用说明_Cookie版本.md          Cookie 模式说明（可选路径）
├── 部署指南_账号密码自动登录.md    部署与排错详解
├── agent/
│   ├── browser_engine.py          引擎适配 + 自动 Xvfb + 反检测参数
│   ├── login_core.py              登录流程（挑战、Turnstile、表单、2FA、自动收码）
│   ├── nodeseek_login_agent.py    Flask 服务：/login /sign /cookie /health
│   ├── Dockerfile
│   └── docker-compose.yml
├── requirements.txt
└── .env.example
```

运行时自动生成：

```
cookie/
├── NODESEEK_COOKIE_1.txt          当前 cookie
├── NODESEEK_COOKIE_1.meta.json    cookie 到期元数据（主动续期靠它判断）
└── profiles/<账号>/               持久化浏览器 profile（记住设备的关键）
```

---

## 八、卸载

**最省事的做法：一键脚本**

```bash
cd nodeseek-autosign

bash uninstall.sh              # 先扫描，列出要删的一切 + 备份内容，不动文件
bash uninstall.sh --yes        # 确认后执行

# 附加选项
bash uninstall.sh --yes --purge-system-deps   # 连 xvfb/libgtk-3 等 apt 依赖一起卸
bash uninstall.sh --yes --with-swap          # 连 install_vps.sh 建的 1G swap 一起删
```

脚本会做这些事（顺序固定，先停后删）：

1. **备份** `.env`（账号密码）和 `cookie/`（登录态）到 `/root/nodeseek-backup-<时间戳>.tar.gz`（权限 600）；
2. 停用并删除 `nodeseek-agent.service`；
3. 清掉 crontab 里本项目的条目；
4. 清掉 Docker 容器与镜像；
5. 删浏览器本体缓存（Camoufox ~1.3GB、ms-playwright 等）；
6. 把项目目录整体移走再删（含 `.venv`、`cookie/profiles` 指纹 profile）；
7. 系统 apt 依赖默认**保留**（别的程序可能在用），要卸加 `--purge-system-deps`。

> ⚠️ 备份包里是**明文密码 + 登录 cookie**。别传到公开地方，用完记得删。

**换机器部署**（备份包直接带走）：

```bash
git clone https://github.com/EmersonLopez2005/nodeseek-autosign.git
cd nodeseek-autosign
bash install_vps.sh                 # 装 Camoufox + venv（浏览器本体约 1.3GB）
tar -xzf /root/nodeseek-backup-*.tar.gz -C "$PWD/"   # 恢复 .env 和 cookie
.venv/bin/python check_env.py
.venv/bin/python multi_site_sign_cookie_only.py
```

下面是不用脚本时的手动步骤。

### 8.1 systemd 服务（`install_vps.sh --agent` 装的）

```bash
sudo systemctl disable --now nodeseek-agent
sudo rm -f /etc/systemd/system/nodeseek-agent.service
sudo systemctl daemon-reload
```

### 8.2 Docker

```bash
cd agent
docker compose down -v --rmi local
```

`-v` 会连数据卷一起删（profile、cookie 全没），`--rmi local` 删掉构建的镜像。
只想停服务、保留数据就把 `-v --rmi local` 去掉。

### 8.3 定时任务

```bash
crontab -l | grep -v nodeseek-autosign | crontab -
```

这条会把含 `nodeseek-autosign` 的行全删掉。不放心就 `crontab -e` 手动删那一行。

### 8.4 目录和浏览器内核

```bash
rm -rf /opt/nodeseek-autosign      # 换成你实际 clone 的路径

# Camoufox 本体约 1.3GB
du -sh ~/.cache/camoufox ~/.cache/ms-playwright ~/.cache/patchright 2>/dev/null
rm -rf ~/.cache/camoufox ~/.cache/ms-playwright ~/.cache/patchright
```

**只想清浏览器、保留项目**用这个：

```bash
bash cleanup_browsers.sh            # 扫描并列出可清理项
bash cleanup_browsers.sh --yes      # 确认后删
```

如果安装脚本给你建过 swap：

```bash
sudo swapoff /swapfile
sudo rm -f /swapfile
sudo sed -i '/swapfile/d' /etc/fstab
```

> 只在确认那个 swapfile 是本项目建的、且系统没有其他 swap 依赖时才删。
> 删之前 `free -h` 看一眼，别把自己内存搞崩。

### 8.5 青龙侧

环境变量里删掉这些：

```
NS_USER_PASS  NS_COOKIE  NS_TOTP_SECRET  NS_TOTP_SECRETS
LOGIN_AGENT_URL  LOGIN_AGENT_URLS  LOGIN_AGENT_TOKEN  LOGIN_AGENT_TIMEOUT
LOGIN_LOCAL_FALLBACK  LOGIN_LOCAL_HEADLESS  AGENT_ENGINE  AGENT_PROXY
```

定时任务里删掉执行主脚本的那条。

### 8.6 确认清干净

```bash
systemctl status nodeseek-agent 2>&1 | head -3   # 应显示 not-found
docker ps -a | grep nodeseek                      # 应无输出
ss -lntp | grep 3000                              # 端口不应被占
ls -d /opt/nodeseek-autosign ~/.cache/camoufox ~/.cache/ms-playwright 2>/dev/null   # 应无输出
```

四条都没输出就干净了。

> 提醒：卸载会把 `cookie/`、`data/`、浏览器 profile 一起删掉，重装后要重新过一遍 CF 挑战，
> 指纹也会变。只是想「重置登录状态」的话不用卸载，删 `cookie/profiles/` 下对应账号的目录就行。

---

## 九、说明

- 本工具只做「自动点击签到」，不刷帖、不刷积分、不做任何互动行为。
- 请遵守 NodeSeek 的站点规则；账号因自动化被封与本项目无关。
- 密码保存在本地 `.env`，注意文件权限（`chmod 600 .env`），不要提交到仓库。
- `.gitignore` 已排除 `.env`、`cookie/`、`data/`、`logs/`、`.venv/`。
