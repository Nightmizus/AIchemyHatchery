# AIchemyHatchery 部署说明

## 首次启动

需要 Python 3.10 或更高版本；当前服务端仅使用 Python 标准库。

1. 克隆私有仓库并进入仓库目录。
2. 将 `.env.example` 复制为 `.env`，按需填写 Kimi API Key、监听地址和端口。反向代理已启用 HTTPS 时，将 `ALCHEMY_HATCHERY_SECURE_COOKIES` 设为 `true`。
3. 在服务器终端交互式创建管理员：

   ```bash
   python server.py create-admin
   ```

   密码不会回显，也不会写入配置文件或 Git；数据库只保存 PBKDF2 加盐哈希。

4. 启动服务：

   ```bash
   python server.py serve
   ```

   也可以临时覆盖监听地址和端口：

   ```bash
   python server.py serve --host 0.0.0.0 --port 4173
   ```

空数据库且未创建管理员时，服务会拒绝启动并提示先运行 `create-admin`。管理员创建成功后，再从网页登录；不要把 `.env`、数据库文件或 `published/` 目录提交到 Git。

## 更新代码

停止旧进程，保留 `.env`、数据库和 `published/`，执行 `git pull --ff-only` 后重新启动。数据库表会在启动时自动补齐，不会覆盖已有账号；旧品牌版本的控制台数据库会在首次启动时自动迁移。

生产环境建议使用独立的低权限系统用户运行服务，并在 Caddy、Nginx 等反向代理后启用 HTTPS。不要直接公开数据库文件或项目目录的静态文件访问。

## Docker 部署（当前生产形态，2026-09 验证）

镜像自带 Chromium（AI 浏览器验证工具）与 dsh 运行时（`deepseek-harness-runtime-bin` wheel）。构建上下文只需 Dockerfile 里 COPY 的文件：

```bash
tar czf deploy.tgz Dockerfile server.py deepseek_harness_adapter.py frontend deepseek_harness \
  docker-entrypoint.sh requirements.txt
# Dockerfile 必须随包部署：漏了它就会用目标机上的旧 Dockerfile 静默构建出错结构的镜像（新 server.py + 旧扁平布局 = 全站 404）
# scp 到目标机后：
tar xzf deploy.tgz -C ~/hatchery
docker build -t hatchery ~/hatchery  # 必须带 Dockerfile 一起部署：tar 漏了它就会用旧 Dockerfile 静默构建出错结构的镜像
docker rm -f hatchery
docker run -d --name hatchery --restart unless-stopped -p 4173:4173 \
  -e ALCHEMY_HATCHERY_HOST=0.0.0.0 \
  --env-file ~/hatchery/.env \
  -v ~/hatchery/published:/app/published \
  -v ~/hatchery/sites:/app/sites hatchery
```

**`-e ALCHEMY_HATCHERY_HOST=0.0.0.0` 不能省**：`.env` 里如果存了 `ALCHEMY_HATCHERY_HOST=127.0.0.1`（裸跑时代留的），入口脚本会尊重它，容器绑定回环后端口映射全部失效（外部 000/502，容器内 curl 127.0.0.1:4173 正常）。`-e` 的优先级高于 `--env-file`，是唯一的无侵入覆盖方式。换容器前先 `tar czf` 备份旧代码。

## 开放发布（泛域名）

用户发布的站点同时有两个入口：`https://hatchery.mizusumi.com/pages/<slug>` 和 `https://<slug>.hatchery.mizusumi.com`。子域名路由由服务端按 Host 头自动完成（见 `server.py` 的 `site_slug_from_host`），反向代理只需把 `*.hatchery.mizusumi.com` 原样转发到本服务，不要改写路径。保留子域名（www、api、admin 等，见 `RESERVED_SITE_SLUGS`）不会被当作站点。

1. DNS：为 `*.hatchery.mizusumi.com` 添加 A/AAAA 记录指向服务器（保留 `hatchery.mizusumi.com` 本身指向同一服务）。
2. 证书：需要覆盖 `*.hatchery.mizusumi.com` 的泛域名证书（如 Let's Encrypt DNS-01 挑战）。
3. 反向代理示例：

   Caddy（自动签发泛域名证书需配置 DNS 插件）：

   ```
   hatchery.mizusumi.com, *.hatchery.mizusumi.com {
       tls {
           dns cloudflare {env.CLOUDFLARE_API_TOKEN}
       }
       reverse_proxy 127.0.0.1:4173
   }
   ```

   Nginx：

   ```nginx
   server {
       listen 443 ssl;
       server_name hatchery.mizusumi.com *.hatchery.mizusumi.com;
       ssl_certificate     /etc/letsencrypt/live/hatchery.mizusumi.com/fullchain.pem;
       ssl_certificate_key /etc/letsencrypt/live/hatchery.mizusumi.com/privkey.pem;
       location / {
           proxy_pass http://127.0.0.1:4173;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```

未配置泛域名时，发布功能不受影响，只是 `xxx.hatchery.mizusumi.com` 无法解析，`/pages/<slug>` 入口始终可用。
