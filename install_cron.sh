#!/usr/bin/env bash
# 安装 / 更新 NodeSeek 自动签到的定时任务。
#
# 设计要点：
#   1. 自动按「北京时间」换算本机时区对应的触发时刻
#      —— 服务器在 UTC / 美国 / 欧洲都不会错位（这是手写 cron 最容易踩的坑）
#   2. 幂等：重复执行不会产生重复条目
#   3. 每次改动前自动备份原 crontab
#   4. flock 防止上一轮没跑完就叠一轮
#   5. 日志落在项目 logs/sign.log
#
# 用法：
#   bash install_cron.sh                 # 默认北京时间 00:10 触发
#   NS_SIGN_BJ_TIME=07:20 bash install_cron.sh    # 改成北京时间 07:20
#   bash install_cron.sh --remove        # 卸载
#
# 为什么默认 00:10：
#   NodeSeek 每日签到状态在北京时间 0~2 点刷新，且刷新后越早签到，
#   当天签到人数越少、随机奖励（试试手气）越容易抽到高值。
#   00:10 既避开整点的拥挤，又保证落在「当天」窗口内。

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

BJ_TIME="${NS_SIGN_BJ_TIME:-00:10}"
LOCK_FILE="${NS_SIGN_LOCK:-/tmp/nodeseek-sign.lock}"
LOG_DIR="$APP_DIR/logs"
TAG="# nodeseek-autosign"

# ---------------------------------------------------------------- 卸载
if [ "${1:-}" = "--remove" ] || [ "${1:-}" = "remove" ]; then
  if crontab -l 2>/dev/null | grep -q "$TAG"; then
    crontab -l 2>/dev/null | grep -v "$TAG" | grep -v "multi_site_sign_cookie_only" \
      | crontab - || true
    echo "✅ 已移除本项目的定时任务"
  else
    echo "ℹ️  crontab 里没有本项目的条目，无需卸载"
  fi
  crontab -l 2>/dev/null | tail -n +1 || true
  exit 0
fi

# ---------------------------------------------------------------- 环境自检
echo "=== 环境检查 ==="
echo "项目目录 : $APP_DIR"

PY=""
for c in "$APP_DIR/.venv/bin/python" "$APP_DIR/venv/bin/python" \
         "$(command -v python3 2>/dev/null || true)" "$(command -v python 2>/dev/null || true)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "✗ 找不到 python 解释器（试过 .venv/bin/python / venv/bin/python / python3）"
  exit 1
fi
echo "python  : $PY"

if ! command -v flock >/dev/null 2>&1; then
  echo "✗ 缺少 flock 命令（util-linux）。装它：apt-get install -y util-linux"
  exit 1
fi
echo "flock   : $(command -v flock)"

if "$PY" -c "import dotenv" 2>/dev/null; then
  echo "dotenv  : ✅ 已安装（.env 会自动加载）"
else
  echo "dotenv  : ⚠️  未安装 —— .env 不会被自动加载！"
  echo "          装它：$PY -m pip install python-dotenv"
  echo "          （不装的话 cron 里读不到 .env 的账号密码）"
fi

if [ ! -f "$APP_DIR/.env" ]; then
  echo "✗ 没找到 $APP_DIR/.env —— 请先完成账号配置"
  exit 1
fi

# ------------------------------------------- 换算「北京时间 HH:MM」→ 本机时钟
echo
echo "=== 时区换算 ==="
read -r H M <<EOF
$(NS_SIGN_BJ_TIME="$BJ_TIME" "$PY" - <<'PY'
import os, datetime
hh, mm = [int(x) for x in os.environ.get("NS_SIGN_BJ_TIME", "00:10").split(":")]
bj = datetime.timezone(datetime.timedelta(hours=8))
local = datetime.datetime.now().astimezone().tzinfo
dt = datetime.datetime(2000, 1, 2, hh, mm, tzinfo=bj).astimezone(local)
print(dt.hour, dt.minute)
PY
)
EOF

echo "本机时区   : $(date +'%Z %z')  ($(date))"
echo "北京时间   : $(TZ=Asia/Shanghai date)"
echo "目标时刻   : 北京时间每天 $BJ_TIME"
echo "对应本机   : 每天 $(printf '%02d:%02d' "$H" "$M")"

# ---------------------------------------------------------------- 写入
mkdir -p "$LOG_DIR"

CRON_LINE="HOME=${HOME:-/root} $M $H * * * cd $APP_DIR && mkdir -p $LOG_DIR && flock -n $LOCK_FILE $PY multi_site_sign_cookie_only.py >> $LOG_DIR/sign.log 2>&1"

BACKUP="$APP_DIR/logs/crontab.backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$APP_DIR/logs"
crontab -l > "$BACKUP" 2>/dev/null || true
echo
echo "已备份原 crontab → $BACKUP"

# 幂等：先删掉旧的同类条目，再写新的
crontab -l 2>/dev/null \
  | grep -v "$TAG" \
  | grep -v "multi_site_sign_cookie_only" \
  > /tmp/ns_cron_new.$$ || true
cat /tmp/ns_cron_new.$$ > /tmp/ns_cron_final.$$
printf '%s 每日签到（北京时间 %s 触发，本机 %02d:%02d）\n%s\n' \
  "$TAG" "$BJ_TIME" "$H" "$M" "$CRON_LINE" >> /tmp/ns_cron_final.$$
crontab /tmp/ns_cron_final.$$
rm -f /tmp/ns_cron_new.$$ /tmp/ns_cron_final.$$

echo
echo "=== 写入后的 crontab ==="
crontab -l | grep -A1 "$TAG" || crontab -l

cat <<EOF

✅ 定时任务已装好。

  触发时刻 : 每天 $(printf '%02d:%02d' "$H" "$M")（本机时钟）= 北京时间 $BJ_TIME
  执行     : $PY multi_site_sign_cookie_only.py
  日志     : $LOG_DIR/sign.log
  锁       : $LOCK_FILE（flock，避免重叠执行）

接下来：

1) 先手动跑一次，确认在 cron 环境（无终端）下也能工作：

   cd $APP_DIR && .venv/bin/python multi_site_sign_cookie_only.py

   ⚠️ 平时它只发两个 HTTP 请求（用现成 cookie 签到），不开浏览器、不需要验证码。
      只有 cookie 快到期要续期时才会开浏览器；那时如果 NodeSeek 要邮箱验证码，
      cron 里没法交互输入，脚本会**发 Telegram 告警**告诉你 —— 不会静默失败。

2) 查状态与日志：

   crontab -l | grep nodeseek
   tail -f $LOG_DIR/sign.log

3) 卸载：

   bash install_cron.sh --remove
EOF
