FROM python:3.12-slim

WORKDIR /app

# 依赖：应用（bcrypt + psycopg2-binary）+ 集成的 DeepSeek Harness 运行时。
# runtime-bin 按构建架构自动选择平台 wheel（linux x64/arm64），开箱即用。
COPY requirements.txt .
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    -r requirements.txt psycopg2-binary deepseek-harness-runtime-bin

# 应用代码
COPY server.py deepseek_harness_adapter.py ./
COPY index.html viewer.html script.js styles.css auth.js viewer.js mica.css ai-chat.css ./
COPY deepseek_harness ./deepseek_harness
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# 漏洞（容器以 root 运行）：任何写入或命令执行缺陷都直接拿到容器 root，并可改写 /app 下的
# server.py 等应用源码。改为固定 UID 的非特权用户；运行时目录在构建期就赋好执行位与归属。
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin appuser
RUN find /usr/local/lib/python3.12/site-packages/deepseek_harness_runtime/runtime \
        -maxdepth 1 -type f -name 'deepseek-harness-sdk-runtime-*' ! -name '*-rg*' \
        -exec chmod 0755 {} + 2>/dev/null || true
RUN mkdir -p /app/published /app/sites && chown -R appuser:appuser /app
USER appuser

# 数据目录（published/、sites/）由卷挂载提供；.env 由 --env-file 注入
# 用 bind mount 挂载数据目录时，宿主目录需先 chown 10001:10001，容器内才写得进去
EXPOSE 4173

ENTRYPOINT ["./docker-entrypoint.sh"]
