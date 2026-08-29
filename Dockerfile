FROM python:3.12-slim

WORKDIR /app

# 安装依赖（bcrypt + psycopg2-binary）；清掉代理变量避免容器内访问宿主代理失败
COPY requirements.txt .
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    pip install --no-cache-dir -r requirements.txt psycopg2-binary

# 复制应用代码
COPY server.py deepseek_harness_adapter.py ./
COPY index.html viewer.html script.js styles.css auth.js viewer.js ./
COPY scripts ./scripts
COPY deepseek_harness ./deepseek_harness

# 数据目录（SQLite + published/）由卷挂载提供
# .env 由 docker-compose env_file 注入

EXPOSE 4173

CMD ["python", "server.py", "serve", "--host", "0.0.0.0", "--port", "4173"]
