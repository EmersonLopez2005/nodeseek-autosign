# NodeSeek 登录代理镜像
#
# 重要：Cloudflare 能识别 headless shell，实测无头模式下整页挑战永远过不去。
# 所以容器里默认用 xvfb + 有头浏览器（等价于真人在跑），这是过 CF 的关键。
#
# 默认引擎是 Camoufox（Firefox 定制版）：NodeSeek 的 CF 会把 Chromium 的
# SwiftShader / headless 直接判成 bot，Camoufox 的指纹伪装实测能过。
# 想换轻量 Chromium：
#   docker build --build-arg ENGINE=patchright -t nodeseek-login-agent:chromium .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive

ARG ENGINE=camoufox

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl tzdata xvfb xauth \
      fonts-noto-cjk fonts-liberation fonts-dejavu-core \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 libcairo2 \
      libatspi2.0-0 libx11-xcb1 libxcb1 libxext6 libxi6 libxtst6 \
      libglib2.0-0 libdbus-1-3 libexpat1 libgtk-3-0 \
      libdbus-glib-1-2 libxt6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt \
    && if [ "$ENGINE" = "camoufox" ]; then \
         pip install "camoufox[geoip]" && python -m camoufox fetch ; \
       else \
         pip install patchright && patchright install --with-deps chromium ; \
       fi

COPY . .
RUN chmod +x entrypoint.sh

ENV AGENT_ENGINE=${ENGINE} \
    AGENT_HEADLESS=false \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=3000

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:3000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["--host", "0.0.0.0", "--port", "3000"]
