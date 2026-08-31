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

停止旧进程，保留 `.env`、`alchemy_hatchery.db` 和 `published/`，执行 `git pull --ff-only` 后重新启动。数据库表会在启动时自动补齐，不会覆盖已有账号；旧品牌版本的控制台数据库会在首次启动时自动迁移。

生产环境建议使用独立的低权限系统用户运行服务，并在 Caddy、Nginx 等反向代理后启用 HTTPS。不要直接公开数据库文件或项目目录的静态文件访问。

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
