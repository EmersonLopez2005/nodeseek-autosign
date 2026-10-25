#!/usr/bin/env bash
# 安装 / 更新 NodeSeek 自动签到的定时任务。
#
# 设计要点：
#   1. 自动按「北京时间」换算本机时区对应的触发时刻
#      —— 服务器在 UTC / 美国 / 欧洲都不会错位（这是手写 cron 最容易踩的坑）
#   2. crontab 里**只留一行任务**，环境变量由 run_sign.sh 自己 export
#
#      ⚠️ 千万别把 HOME=/root PATH=... 写在调度字段的同一行！
#         cron 的解析器（Vixie/Debian 的 load_env()）遇到 '=' 会把**整行**
#         当成「环境变量赋值」，'=' 后面的内容（含空格）全算进变量值，
#         不做任何分词。结果就是这一行**一个 job 都不生成**，
#         到点永远不触发，而且**不报任何错**（crontab -l 还看得到它）。
#         —— 这个坑本项目真踩过：定时任务装完看着一切正常，却一次都没跑过。
#
#         所以：环境变量要么独占一行，要么干脆别写（让 run_sign.sh 自己 export）。
#   3. 幂等：重复执行不会产生重复条目
#   4. 每次改动前自动备份原 crontab
#   5. 文件锁和心跳都放在 run_sign.sh 里，锁被占用时会留下痕迹
#
# 用法：
#   bash install_cron.sh                        # 默认北京时间 00:10 触发
#   NS_SIGN_BJ_TIME=07:20 bash install_cron.sh  # 改成北京时间 07:20
#   bash install_cron.sh --status               # 诊断：cron 装没装 / 跑没跑 / 日志在哪
#   bash install_cron.sh --selftest             # 实测：cron 到底会不会执行条目（最多等 100 秒）
#   bash install_cron.sh --remove               # 卸载
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
      | grep -v "run_sign.sh" | grep -v "$APP_DIR" | crontab - || true
    echo "✅ 已移除本项目的定时任务"
  else
    echo "ℹ️  crontab 里没有本项目的条目，无需卸载"
  fi
  crontab -l 2>/dev/null | tail -n +1 || true
  exit 0
fi

# ---------------------------------------------------------------- 自测
# 装一条每分钟执行的临时条目，实测 cron 到底会不会执行我们写的行。
# 这是唯一能证明「cron 真的在跑」的可靠办法 —— 别的地方都可能骗人。
if [ "${1:-}" = "--selftest" ] || [ "${1:-}" = "selftest" ]; then
  TEST_FILE="/tmp/ns_cron_selftest.log"
  rm -f "$TEST_FILE"
  BACKUP="$(mktemp /tmp/ns_cron_backup.XXXXXX)"
  crontab -l > "$BACKUP" 2>/dev/null || true

  # 注意：cron 里 % 是换行符，命令里不能出现裸 %，所以用 date -Is
  {
    cat "$BACKUP"
    echo "* * * * * echo \$(date -Is) >> $TEST_FILE"
  } | crontab - 2>/dev/null || true

  echo "=== cron 触发自测 ==="
  echo "已临时装入一条每分钟执行的条目，最多等 100 秒 …"
  echo "（自测期间你的 crontab 会被临时改一下，测完自动还原）"
  echo
  i=0
  while [ "$i" -lt 20 ]; do
    [ -s "$TEST_FILE" ] && break
    sleep 5
    i=$((i + 1))
  done

  # 还原
  if [ -s "$BACKUP" ]; then
    crontab "$BACKUP" 2>/dev/null || true
  else
    crontab -r 2>/dev/null || true
  fi
  rm -f "$BACKUP"

  echo
  if [ -s "$TEST_FILE" ]; then
    echo "✅ cron 正常触发！最后一条执行记录："
    tail -n 3 "$TEST_FILE"
    echo
    echo "结论：cron 守护进程和 crontab 解析都是好的。"
    echo "      如果定时签到仍然没跑，问题就在条目内容或 run_sign.sh ——"
    echo "      跑 bash install_cron.sh --status 看条目和日志。"
  else
    echo "❌ 100 秒内 cron 一次都没执行 —— cron 守护进程或 crontab 解析有问题。"
    echo
    echo "排查："
    echo "  systemctl status cron            # 守护进程在不在"
    echo "  grep -a CRON /var/log/syslog | tail -20   # cron 自己的日志"
    echo "  crontab -l | cat -A              # 看有没有全角字符/行尾没换行"
  fi
  exit 0
fi

# ---------------------------------------------------------------- 诊断
if [ "${1:-}" = "--status" ] || [ "${1:-}" = "status" ]; then
  # 诊断脚本必须「尽力把信息打完」——
  # 之前忘了关 set -e，而下面那些 grep 在「没找到」时返回 1，
  # 于是一旦日志里没有匹配项，整个脚本就在半路被杀掉，
  # 输出停在 2.5 节，第 3~7 节（心跳 / 签到日志 / cookie 状态）全看不到。
  # 诊断工具自己断了，比什么都误导人。
  set +e

  echo "=== 1. crontab 里有没有本项目的条目 ==="
  if crontab -l 2>/dev/null | grep -q "$TAG"; then
    crontab -l 2>/dev/null | grep -A1 "$TAG"

    # 反模式检测：只查**本项目自己**的行。
    # 判断标准：这一行在冒号之前含有 '='，且 '=' 左边是个合法的变量名
    # （cron 的 load_env() 遇到 '=' 会把整行吃掉，生成 0 个 job）。
    # 不能对整个 crontab 扫 —— 别人的条目里写 '=' 是合法的。
    MYLINES="$(crontab -l 2>/dev/null | grep -A1 "$TAG" | tail -n 1)"
    if printf '%s\n' "$MYLINES" \
      | grep -qE '^[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+[0-9*]'; then
      echo
      echo "  ❌ 致命问题：检测到「环境变量和调度字段写在同一行」！"
      echo "     cron 会把整行当成环境变量赋值，等于一条 job 都没装 —— 到点永不触发。"
      echo "     修复：bash install_cron.sh   （新版会把环境变量挪进 run_sign.sh）"
    fi
  else
    echo "❌ 没有！定时任务压根没装（跑 bash install_cron.sh 安装）"
  fi

  echo
  echo "=== 2. cron 服务在跑吗 ==="
  if command -v systemctl >/dev/null 2>&1; then
    for u in cron crond; do
      if systemctl list-unit-files "${u}.service" 2>/dev/null | grep -q "^${u}.service"; then
        s=$(systemctl is-active "$u" 2>/dev/null || true)
        [ -z "$s" ] && s="unknown"
      else
        s="not-found"
      fi
      echo "  $u : $s"
    done
  else
    echo "  （无 systemctl，跳过）"
  fi

  echo
  echo "=== 2.5 cron 自己的日志（看它到底执行过没有）==="
  LOGSHOWN=0
  if command -v journalctl >/dev/null 2>&1; then
    OUT="$(journalctl -u cron --since "3 days ago" --no-pager 2>/dev/null | tail -n 25 || true)"
    if [ -n "$OUT" ]; then
      echo "$OUT"
      LOGSHOWN=1
    fi
  fi
  if [ "$LOGSHOWN" = "0" ] && [ -f /var/log/syslog ]; then
    grep -a "CRON" /var/log/syslog 2>/dev/null | tail -n 25 || true
    LOGSHOWN=1
  fi
  if [ "$LOGSHOWN" = "0" ]; then
    echo "  （读不到 cron 日志：既没有 journalctl 也没有 /var/log/syslog）"
  fi
  echo
  echo "  --- 我们的条目被 cron 执行过的记录（cron 成功触发时会打这一行）---"
  HITS=""
  if [ -f /var/log/syslog ]; then
    HITS="$(grep -a "run_sign.sh" /var/log/syslog 2>/dev/null | tail -n 10 || true)"
  fi
  if [ -z "$HITS" ] && command -v journalctl >/dev/null 2>&1; then
    HITS="$(journalctl -u cron --since "7 days ago" --no-pager 2>/dev/null | grep -a "run_sign.sh" | tail -n 10 || true)"
  fi
  if [ -n "$HITS" ]; then
    echo "$HITS"
  else
    echo "  ❌ 没有任何执行记录 —— cron 从来没触发过这条任务"
    echo "     （如果刚装上还没到点，这行是正常的；等到点后再看一次）"
  fi

  echo
  echo "=== 3. 时区对照 ==="
  echo "  本机时区 : $(date +'%Z %z')  ($(date '+%F %T'))"
  BJ_NOW=""
  for c in "$APP_DIR/.venv/bin/python" "$(command -v python3 2>/dev/null || true)" \
           "$(command -v python 2>/dev/null || true)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then
      BJ_NOW=$("$c" -c "import datetime;print(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%F %T'))" 2>/dev/null || true)
      [ -n "$BJ_NOW" ] && break
    fi
  done
  echo "  北京时间 : ${BJ_NOW:-（算不了，用 date 看本机时钟）}"

  echo
  echo "=== 4. 上次运行记录（logs/last_run.txt）==="
  if [ -f "$LOG_DIR/last_run.txt" ]; then
    cat "$LOG_DIR/last_run.txt"
  else
    echo "  ❌ 没有任何运行记录 —— 定时任务从来没成功执行过"
  fi

  echo
  echo "=== 5. 签到日志尾部（logs/sign.log）==="
  if [ -f "$LOG_DIR/sign.log" ]; then
    echo "  文件大小 : $(wc -c < "$LOG_DIR/sign.log") 字节"
    echo "  最后修改 : $(date -r "$LOG_DIR/sign.log" '+%F %T' 2>/dev/null || stat -c '%y' "$LOG_DIR/sign.log" 2>/dev/null || echo '?')"
    echo "  ---- 尾部 40 行 ----"
    tail -n 40 "$LOG_DIR/sign.log"
  else
    echo "  ❌ 没有 sign.log —— 从来没跑起来过"
  fi

  echo
  echo "=== 6. cookie 与通知状态 ==="
  ls -l "$APP_DIR/cookie/" 2>/dev/null || echo "  (没有 cookie/ 目录)"

  echo
  echo "=== 7. 想确认 cron 真的会执行？跑一次实测 ==="
  echo "  bash install_cron.sh --selftest    # 临时装一条每分钟的条目，看它跑不跑"
  echo "  bash install_cron.sh --dryrun      # 用 cron 的极简环境预演一次（不实际签到）"
  exit 0
fi

# ---------------------------------------------------------------- 预演
# 用「cron 的真实环境」跑一遍我们那条命令，但不真的签到。
# 目的：提前暴露「手动跑正常、cron 里跑失败」的那类问题
# （PATH 太短找不到 python、没有 HOME 读不到 .env、没有 DISPLAY 起不了浏览器）。
# --selftest 只能证明 cron 会执行条目，证明不了我们的条目内容是对的。
if [ "${1:-}" = "--dryrun" ] || [ "${1:-}" = "dryrun" ]; then
  echo "=== cron 环境预演（不实际签到）==="
  echo "模拟 cron 的极简环境：无 DISPLAY / 无 tty / 短 PATH / 只有 HOME"
  echo

  # cron 实际会给的最小环境（参考 Debian cron 的默认 env）
  RUNNER="$APP_DIR/run_sign.sh"
  [ -f "$RUNNER" ] || { echo "✗ 找不到 $RUNNER"; exit 1; }

  echo "--- 用 env -i 清空环境后执行 run_sign.sh --dryrun ---"
  echo
  if env -i \
      HOME="${HOME:-/root}" \
      PATH="/usr/bin:/bin" \
      SHELL="/bin/sh" \
      /bin/bash "$RUNNER" --dryrun 2>&1; then
    echo
    echo "✅ 预演通过 —— cron 环境里能跑通"
  else
    echo
    echo "❌ 预演失败 —— 这就是 cron 里跑不起来的原因（上面的错误就是根因）"
    exit 1
  fi

  echo
  echo "提示：预演只检查「环境与依赖」，不会真的签到。"
  echo "      想真跑一次签到：bash run_sign.sh"
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

# cron 守护进程不跑的话，条目写得再对也不会触发
CRON_DAEMON=""
if command -v systemctl >/dev/null 2>&1; then
  for u in cron crond; do
    if [ "$(systemctl is-active "$u" 2>/dev/null || echo no)" = "active" ]; then
      CRON_DAEMON="$u"
      break
    fi
  done
fi
if [ -n "$CRON_DAEMON" ]; then
  echo "cron    : ✅ $CRON_DAEMON 服务在跑"
else
  echo "cron    : ⚠️  没检测到运行中的 cron 服务！"
  echo "          条目写进去也不会触发。装并启动："
  echo "            apt-get update && apt-get install -y cron"
  echo "            systemctl enable --now cron"
fi

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

# crontab 里只放一行干净调用。三个关键点：
#   1) 调 run_sign.sh 而不是直接调 python —— cron 的命令行对 % 和引号极其敏感，
#      而且 flock 拿不到锁时会静默跳过（crontab 里看不出来）；
#   2) **不写任何环境变量前缀** —— 环境变量和调度写同一行会让整行变成
#      「环境变量赋值」，一条 job 都不会生成（本项目真踩过这个坑）；
#      HOME / PATH 由 run_sign.sh 自己 export，更稳，也不会影响别的条目；
#   3) 路径全用绝对路径 —— cron 的工作目录是 $HOME，不是项目目录。
CRON_LINE="$M $H * * * cd $APP_DIR && mkdir -p $LOG_DIR && bash $APP_DIR/run_sign.sh >> $LOG_DIR/cron.log 2>&1"

BACKUP="$APP_DIR/logs/crontab.backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$APP_DIR/logs"
crontab -l > "$BACKUP" 2>/dev/null || true
echo
echo "已备份原 crontab → $BACKUP"

# 幂等：先删掉旧的同类条目，再写新的
# 注意：老版本写的是「环境变量 + 调度」同一行的条目，
# 要一并清掉，否则会同时存在两条、跑两遍。
crontab -l 2>/dev/null \
  | grep -v "$TAG" \
  | grep -v "multi_site_sign_cookie_only" \
  | grep -v "run_sign.sh" \
  | grep -v "$APP_DIR" \
  > /tmp/ns_cron_new.$$ || true
# 保证保留下来的内容以换行结尾 —— cron 会忽略文件里最后一行（如果它没有换行符）
if [ -s /tmp/ns_cron_new.$$ ]; then
  printf '\n' >> /tmp/ns_cron_new.$$
fi
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
  执行     : bash $APP_DIR/run_sign.sh
  日志     : $LOG_DIR/sign.log   （签到过程）
             $LOG_DIR/cron.log    （cron 层面报错，比如命令找不到）
             $LOG_DIR/last_run.txt（上次运行时间 + 退出码，心跳）

接下来：

1) 立刻手动跑一次，确认在 cron 环境（无终端）下也能工作：

   cd $APP_DIR && bash run_sign.sh

   ⚠️ 平时它只发两个 HTTP 请求（用现成 cookie 签到），不开浏览器、不需要验证码。
      只有 cookie 快到期要续期时才会开浏览器；那时如果 NodeSeek 要邮箱验证码，
      cron 里没法交互输入，脚本会**发 Telegram 告警**告诉你 —— 不会静默失败。

2) 实测 cron 到底会不会执行（强烈建议跑一次，1 分钟出结果）：

   bash install_cron.sh --selftest

   它会临时装一条每分钟的条目，看它有没有真的跑起来，测完自动还原。

3) 查状态与日志（一条命令全看完）：

   bash install_cron.sh --status

4) 卸载：

   bash install_cron.sh --remove
EOF
