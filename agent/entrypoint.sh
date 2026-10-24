#!/bin/sh
# 容器入口：默认用 Xvfb 起一个虚拟显示器，让 Chromium 以“有头”方式运行。
# 这不是为了好看——Cloudflare 能认出 headless shell，无头模式下 Turnstile 过不去。
set -e

if [ "${AGENT_HEADLESS}" = "true" ] || [ "${AGENT_HEADLESS}" = "1" ]; then
    echo "[entrypoint] 无头模式启动（注意：CF 可能拦截）"
    exec python nodeseek_login_agent.py "$@"
fi

echo "[entrypoint] Xvfb 有头模式启动"
exec xvfb-run -a --server-args="-screen 0 1440x900x24" \
    python nodeseek_login_agent.py "$@"
