# AIchemySites 部署说明

## 首次启动

需要 Python 3.10 或更高版本；当前服务端仅使用 Python 标准库。

1. 克隆私有仓库并进入仓库目录。
2. 将 `.env.example` 复制为 `.env`，按需填写 Kimi API Key、监听地址和端口。反向代理已启用 HTTPS 时，将 `ALCHEMY_SITES_SECURE_COOKIES` 设为 `true`。
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

停止旧进程，保留 `.env`、`alchemy_sites.db` 和 `published/`，执行 `git pull --ff-only` 后重新启动。数据库表会在启动时自动补齐，不会覆盖已有账号。

生产环境建议使用独立的低权限系统用户运行服务，并在 Caddy、Nginx 等反向代理后启用 HTTPS。不要直接公开数据库文件或项目目录的静态文件访问。
