#!/usr/bin/env bash
# NodeSeek 自动登录 · VPS 一键安装（Debian 12/13、Ubuntu 22.04+）
#
#   bash install_vps.sh              # 装依赖 + 浏览器 + venv
#   bash install_vps.sh --agent      # 额外注册 systemd 常驻服务（推荐给青龙用）
#   bash install_vps.sh --check      # 只体检，不安装
#
# 设计说明：有头浏览器是过 Cloudflare 的前提，所以装的是完整 Chromium + Xvfb，
# 不是 headless shell。浏览器体积约 180MB，装完整个环境约 600MB 磁盘。

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
MODE="${1:-}"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 前置检查
if [ "$(id -u)" != "0" ] && ! command -v sudo >/dev/null 2>&1; then
  red "需要 root 或 sudo"
  exit 1
fi

SUDO=""
[ "$(id -u)" != "0" ] && SUDO="sudo"

if [ "$MODE" = "--check" ]; then
  step "仅体检模式"
  if [ -x "$PY" ]; then
    "$PY" "$APP_DIR/check_env.py" "${@:2}"
  else
    ylw "venv 还没建，只能查系统层面："
    python3 "$APP_DIR/check_env.py" --skip-browser
  fi
  exit 0
fi

step "系统信息"
. /etc/os-release 2>/dev/null || true
echo "  ${PRETTY_NAME:-unknown}  $(uname -m)"
ARCH="$(uname -m)"
if [ "$ARCH" != "x86_64" ] && [ "$ARCH" != "aarch64" ]; then
  ylw "  非 x86_64/aarch64，Chromium 可能没有现成构建"
fi

MEM_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
echo "  内存：${MEM_MB}MB   CPU：$(nproc)"

# ---------------------------------------------------------------- swap
if [ "$MEM_MB" -gt 0 ] && [ "$MEM_MB" -lt 1600 ] && ! swapon --show | grep -q .; then
  step "内存偏小，创建 1G swap（防止 Chromium 被 OOM 杀掉）"
  $SUDO fallocate -l 1G /swapfile || $SUDO dd if=/dev/zero of=/swapfile bs=1M count=1024
  $SUDO chmod 600 /swapfile
  $SUDO mkswap /swapfile >/dev/null
  $SUDO swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
  grn "  swap 已启用"
fi

# ---------------------------------------------------------------- 系统依赖
step "安装系统依赖（Chromium 运行库 + Xvfb + 中文字体）"
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq

# libgtk-3-0 / libdbus-glib-1-2 / libxt6：Camoufox(Firefox) 的硬依赖。
# Chromium 跑得动不代表 Firefox 也跑得动，GTK3 缺了会报
# "libgtk-3.so.0: cannot open shared object file" 直接退出。
# libgl1 / libgl1-mesa-dri / libegl1 / libgles2：Xvfb 下的软件 OpenGL（llvmpipe）。
# 没有它们，Chromium 的 GPU 进程起不来，WebGL 直接 NO_WEBGL 或暴露 SwiftShader
# —— 都是 Cloudflare 已知的 bot 特征，Turnstile 会 onError 拒绝交互。
PKGS="python3 python3-venv python3-pip ca-certificates curl xvfb \
libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
libgbm1 libatspi2.0-0 libx11-xcb1 libxcb1 libxext6 libxi6 libxtst6 \
libpango-1.0-0 libcairo2 libdbus-1-3 libexpat1 libglib2.0-0 \
libgl1 libgl1-mesa-dri libegl1 libgles2 \
libgtk-3-0 libdbus-glib-1-2 libxt6 \
fonts-noto-cjk fonts-wqy-zenhei fonts-liberation"

# Debian 13(trixie) / Ubuntu 24.04 改过一些包名（libasound2 → libasound2t64 等），
# 逐个用 apt-cache 探一遍再一次性装，比批量失败后逐包重试干净
AVAIL=""
MISSING=""
for p in $PKGS; do
  if apt-cache show "$p" >/dev/null 2>&1; then
    AVAIL="$AVAIL $p"
  else
    MISSING="$MISSING $p"
  fi
done
if [ -n "$MISSING" ]; then
  ylw "  这些包在当前源里没有，已跳过：$MISSING"
fi

# shellcheck disable=SC2086
$SUDO apt-get install -y -qq --no-install-recommends $AVAIL 2>&1 | grep -v '^$' | tail -3 || true

# 音频库是 Chromium 硬依赖，包名各发行版不同，单独补
if ! apt-cache show libasound2 >/dev/null 2>&1; then
  $SUDO apt-get install -y -qq --no-install-recommends libasound2t64 >/dev/null 2>&1 || \
    ylw "  libasound2/t64 都装不上，Chromium 可能会缺库"
fi

grn "  系统依赖完成"

# ---------------------------------------------------------------- venv
step "创建 Python 虚拟环境"
if [ ! -x "$PY" ]; then
  python3 -m venv "$VENV"
fi
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$APP_DIR/requirements.txt" || {
  ylw "  requirements.txt 装失败，改用 agent/requirements.txt"
  "$PY" -m pip install -q -r "$APP_DIR/agent/requirements.txt"
}
# 读 .env 必须有它，缺了会导致 NS_USER_PASS 读不到
"$PY" -c "import dotenv" 2>/dev/null || "$PY" -m pip install -q python-dotenv
grn "  Python 依赖完成"

# ---------------------------------------------------------------- 浏览器
step "安装 Chromium（完整版，不是 headless shell）"
"$VENV/bin/patchright" install chromium 2>&1 | tail -2 || {
  red "  Chromium 下载失败，检查磁盘空间或网络（约需 200MB）"
  exit 1
}
# 顺手装上系统依赖，官方脚本最清楚缺什么
"$VENV/bin/patchright" install-deps chromium >/dev/null 2>&1 || true
grn "  Chromium 完成"

# ---------------------------------------------------------------- .env
step "初始化配置"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  TOKEN="$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)"
  sed -i "s|^AGENT_TOKEN=.*|AGENT_TOKEN=$TOKEN|" "$APP_DIR/.env" 2>/dev/null || true
  grn "  已生成 .env，AGENT_TOKEN=$TOKEN"
  ylw "  记得在里面填 NS_USER_PASS=用户名&密码"
else
  ylw "  .env 已存在，跳过"
fi
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

# ---------------------------------------------------------------- systemd
if [ "$MODE" = "--agent" ]; then
  step "注册 systemd 常驻服务（nodeseek-login-agent）"
  TOKEN="$(grep -E '^AGENT_TOKEN=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' )"
  PORT="$(grep -E '^AGENT_PORT=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"')"
  PORT="${PORT:-3000}"

  cat > /tmp/nodeseek-agent.service <<EOF
[Unit]
Description=NodeSeek Login Agent (browser)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=AGENT_TOKEN=$TOKEN
Environment=AGENT_ENGINE=patchright
Environment=AGENT_HEADLESS=false
Environment=HOME=/root
Environment=DISPLAY=
ExecStart=$PY $APP_DIR/agent/nodeseek_login_agent.py --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=10
# 浏览器要 fork 很多子进程，别限制太死
LimitNOFILE=65535
# 平时 agent 只占几十 MB；登录那 30~60 秒才吃内存。
# 降一点优先级，别和业务抢 CPU
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
# 软内存上限：超过后内核优先回收缓存，不会像 MemoryMax 那样直接杀进程
MemoryHigh=1G

[Install]
WantedBy=multi-user.target
EOF

  $SUDO mv /tmp/nodeseek-agent.service /etc/systemd/system/nodeseek-agent.service
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now nodeseek-agent >/dev/null 2>&1
  sleep 3
  if $SUDO systemctl is-active --quiet nodeseek-agent; then
    grn "  服务已启动：systemctl status nodeseek-agent"
    echo "  健康检查：curl -s http://127.0.0.1:$PORT/health"
  else
    red "  服务没起来，看日志：journalctl -u nodeseek-agent -n 40 --no-pager"
  fi
fi

# ---------------------------------------------------------------- 收尾
step "安装完成"
cat <<EOF

  目录：$APP_DIR
  配置：$APP_DIR/.env
  Python：$PY

接下来：

  1) 体检（最重要，先看这台机器的 IP 行不行）
       $PY $APP_DIR/check_env.py

  2) 用真账号试一次
       cd $APP_DIR && $VENV/bin/python multi_site_sign_cookie_only.py

  3) 青龙（跑在 docker 里时，用宿主机网桥地址访问 agent）
       LOGIN_AGENT_URL=http://172.17.0.1:${PORT:-3000}
       LOGIN_AGENT_TOKEN=<.env 里的 AGENT_TOKEN>

EOF

grn "搞定。"
