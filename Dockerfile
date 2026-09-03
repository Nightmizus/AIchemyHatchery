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

# 数据目录（published/、sites/）由卷挂载提供；.env 由 --env-file 注入
EXPOSE 4173

ENTRYPOINT ["./docker-entrypoint.sh"]
