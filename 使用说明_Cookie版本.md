# Cookie 模式使用说明

> 这是可选路径。如果你已经配好了 `NS_USER_PASS`（账号密码 + 浏览器登录），
> 完全可以不看这份文档 —— 脚本会自己处理 cookie 失效。
>
> 只有在下面两种情况才需要手工 cookie：
> 1. 只想用 cookie、暂时不想装浏览器环境
> 2. 账号开了 2FA 或邮箱验证，自动登录过不去

## 两种模式的关系

```
有 NS_COOKIE ──> 直接用 cookie 签到
                    │
                    └─ cookie 失效 ──> 用 NS_USER_PASS 走浏览器登录 ──> 拿到新 cookie
                                          │
                                          └─ 没配账号密码 ──> 跳过，提示更新 cookie
```

cookie 只是「省掉登录这一步」，不是替代登录。论坛现在的风控下，
纯手工 cookie 的有效期有限，长期跑还是要配账号密码。

## 配置

### 单账号

```bash
NS_COOKIE=session=abc123; pjwt=eyJhbGciOi...; cf_clearance=xyz
```

### 多账号

`&` 分隔，顺序对应 `NS_USER_PASS` 里的账号顺序：

```bash
NS_COOKIE=session=aaa; pjwt=aaa.jwt; cf_clearance=bbb&session=ccc; pjwt=ccc.jwt; cf_clearance=ddd
```

### 同时配账号密码（推荐）

```bash
NS_USER_PASS=alice&pass1&bob&pass2
NS_COOKIE=session=aaa; pjwt=aaa.jwt; cf_clearance=bbb&session=ccc; pjwt=ccc.jwt; cf_clearance=ddd
```

这样第一个账号用第一条 cookie，失效了用 `alice` 的密码登录；
第二个账号同理。两边数量不必一致，脚本按序号取。

## 怎么拿到 Cookie

1. 浏览器登录 NodeSeek
2. F12 → Application（应用）→ Cookies → `https://www.nodeseek.com`
3. 至少要有 `session`（服务端会话）和 `pjwt`（登录 JWT）；有 `cf_clearance` 更好（有效期约 30 分钟到几小时）

> **实测更正（2026-10）**：早期文档写的 `sessionid` 这个名字在现在的站点上**并不存在**，
> 登录成功后拿到的是 `session` + `pjwt` + `smac`。
> 脚本判定「已登录」看的就是这几个字段，所以 cookie 里没有 `sessionid` 是正常的。

## 用油猴脚本自动同步（可选）

`nodeseek_cookie_sync.user.js` 可以把你浏览器里的 cookie 自动推到青龙环境变量，
省掉手工复制：

1. 装 Tampermonkey，导入这个脚本
2. 脚本菜单里填青龙地址、Client ID、Client Secret
3. 打开 NodeSeek，脚本会把 `NS_COOKIE` 合并写入青龙（按账号 uid 合并，不会覆盖其他账号）

## 常见问题

**Q: 只配 cookie 能长期跑吗？**
A: 不能。`session` / `pjwt` 会过期，`cf_clearance` 更短。需要配 `NS_USER_PASS` 让脚本自动续。

**Q: cookie 里包含 `&` 怎么办？**
A: cookie 值里不会出现裸 `&`（都是 `;` 分隔），所以可以放心用 `&` 分隔多个账号。

**Q: 签到返回 403 但 cookie 没过期？**
A: 那是签到接口的 WAF 拦了出口 IP，不是 cookie 问题。用浏览器代签（`POST /sign`）或换 IP。

**Q: 环境变量里同时有 NS_COOKIE 和旧文件里的 cookie，用哪个？**
A: 环境变量优先，避免旧文件覆盖你刚配的值。

**Q: 怎么确认 cookie 还有效？**
A: 跑一次脚本看日志，`签到成功` 就是有效；返回未登录会用账号密码自动重登。
