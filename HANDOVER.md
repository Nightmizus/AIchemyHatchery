# 炼丹社Hatchery 交接手册

写给下一位接手者（人或 AI）。读完整份再动手；第 7 节"雷区"必读。

## 1. 项目是什么

校园/社团用的一键 AI 建站控制台（AIchemyHatchery）。用户在左侧聊天框用中文说需求，AI 直接改网站；网站可以发布到 `hatchery.mizusumi.com/pages/<slug>`。

- 仓库：GitHub `Nightmizus/AIchemyHatchery`（private），本地工作目录 `C:\Users\Mizusumi\Documents\Codex\2026-08-24\zu-yi\work`
- 多人在改：主人用 Kimi Code（本手册作者）+ Codex CLI 同时在同一个工作区写代码，Codex 会直接推 main。改代码前先 `git status` + `git log` 看现场，**不要替别人提交他们的半成品**
- 提交/推送/合并**每次都要用户明确许可**，别自作主张

## 2. 技术结构

- `server.py`：单文件 Python 后端（http.server + ThreadingHTTPServer），端口 4173。`python server.py serve` 启动
- 前端：`index.html`（编辑器）、`viewer.html/viewer.js`（发布站运行时）、`script.js`、`auth.js`；样式 `styles.css` + `mica.css`（深色 GitHub 风）+ `ai-chat.css`（聊天区）
- 数据库：**生产用 Neon**（托管 PG，连接串在 .env 的 `NEON_DATABASE_URL`）；**本地开发用 PGlite**（`.pgsrv/serve.mjs`，127.0.0.1:5433，postgres/postgres）。共享表 `"User"`/`"CampusUser"` 是 sdszwebsite 的，**只读写、绝不 CREATE/ALTER**；读可选列一律走 `user_column_name()`/`row_value()` 列探测（生产表列不齐，硬读会 500）。`hatchery_*` 表启动自建
- AI 子系统（dsh）：`deepseek_harness_adapter.py` 用 JSON-RPC 驱动官方运行时；`deepseek_harness/site-tools.mjs` 是文件工具插件，**沙盒在 `sites/<user_id>/`**（AI 只能读写当前网站的文件夹，平台源码它碰不到——这是用户定的策略，别放开）；`llm-kimi.mjs` 注册 kimi provider（KIMI_API_KEY → k3）；运行时二进制 Windows 在 `.deepseek-harness/runtime/`，Linux 用 pip 包 `deepseek-harness-runtime-bin`（Docker 镜像内置，entrypoint 自动定位）
- 账号：注册已迁移到 SDSZ 统一账号（登录页有 SSO 按钮，`SSO_SECRET`/`SDSZ_BASE_URL` 走 env；**sdsz 侧的 SSO 端点还没实现**，目前只有单边）。写死站长：campusId `20264689` 永远是管理员

## 3. 部署拓扑（现状）

**网站本体（唯一在用的）**：ruixiuzhang 的 Mac（tailscale 主机名 studio/128.local）上的 colima Docker。容器 `hatchery`，4173 端口，`--restart unless-stopped`，卷挂 `~/hatchery/published` 和 `~/hatchery/sites`，env 来自 `~/hatchery/.env`。
- 我的 tailnet 里它叫 `100.114.192.6`（共享节点）；ruixiuzhang 自己的 tailnet 里叫 `100.114.192.7`

**公网链路（当前是断的，见待办）**：
```
浏览器 → Cloudflare(橙云) → Worker hatchery-proxy → hatchery.groovin.cn → 39.106.77.105 nginx → tailnet → Docker
```

**其他机器**：
- `ubuntu-vm 100.102.20.81`：裸机 systemd 部署（`~/hatchery`，hatchery.service），已是冗余备份；它到 60.205 的反向隧道服务（hatchery-tunnel.service）已无意义，可停
- `39.106.77.105`：用户的阿里云 ECS，nginx 网关。**目前整机卡死**（TCP 通、sshd/nginx 不应答），需阿里云控制台重启
- `60.205.201.242`：别人的阿里云网关（ruixiuzhang 那 tailnet 的），上面 hatchery.groovin.cn 的反代现在指向 Docker

## 4. 服务器连接方式

本机是 Windows + Git Bash，**没有 sshpass**，密码登录统一用 askpass 模式：

```bash
printf '#!/bin/sh\necho "<密码>"\n' > /tmp/.askpass.sh && chmod +x /tmp/.askpass.sh
DISPLAY=:0 SSH_ASKPASS=/tmp/.askpass.sh SSH_ASKPASS_REQUIRE=force \
  ssh -o StrictHostKeyChecking=accept-new -o NumberOfPasswordPrompts=1 <用户>@<IP> "<命令>"
rm -f /tmp/.askpass.sh
```

- 跳板：本机 tailnet 够不到 128.local 和 60.205 那个 tailnet——先 ssh 到 `60.205.201.242`，再在它上面用同样模式 ssh 到 `100.114.192.7`。嵌套 ssh 记得加 `-n`（不然 stdin 被吃掉，脚本会莫名其妙死掉）
- 各机器的账号密码在 `.handover-secrets.local`（本目录，已 gitignore，别提交别外发）
- Mac 上 SSH 非交互 PATH 不带 `/opt/homebrew/bin`，docker/colima 命令要用全路径 `PATH=/opt/homebrew/bin:$PATH`
- 杀 Windows 进程别用 Git Bash 的 `kill`（不靠谱），用 `powershell Stop-Process -Id <PID> -Force`

## 5. 本地开发环境

- 起两个东西：PGlite（`cd .pgsrv && node serve.mjs`，日志重定向到文件！直接挂着会被任务系统按输出量杀掉）+ `python server.py serve`
- 测试账号：`test` / 见 secrets 文件（admin，本地 PGlite）
- 浏览器级测试：`.shots-venv/`（playwright + msedge 无头 + psycopg2 注入一次性 console_session；探针范例 `probe-*.py`）。**注意**：探针里 psycopg2 连接用完要么 close 要么 commit——挂着隐式读事务会把 PGlite 全局写锁拖死，整个服务假性卡死（排查了半天）
- API 级压测：`.shots-venv/ai-hammer.py`（POST 用 curl 传输，别用 urllib——本机代理环境下 urllib 大 body 会卡）
- node 在 `~/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe`（系统 PATH 没有）

## 6. 待办清单（按优先级）

1. **公网恢复**：39.106 卡死中，用户去阿里云控制台重启 → 重启后给它配 hatchery.groovin.cn 正式证书（certbot webroot，/var/www/acme 和 :80 server block 已就位）→ 或把 groovin.cn 的 DNS 指回 60.205（用它在 Cloudflare 的备案域名身份）二选一
2. **踢掉 groovin**：Cloudflare Tunnel（128.local 跑 cloudflared 出站直连，整个阿里云层都不需要了）。需要 CF token 带 `Account.Cloudflare Tunnel Edit`。Worker 代码在本地 `/tmp/worker-classic.js`（会话没了就重写，逻辑见第 3 节链路图）
3. **本地改动上线到 Docker**：有一批未部署的本地改动（AI 任务排队、dsh 停滞帽+秒表+心跳、pg-scope 固定定位围堵、管理后台真名、发送按钮圆形/未配置态、模块禁拖禁删、ChatGPT 式主页）。流水线：`tar czf`（排除 node_modules/.pgsrv/sites/published/.deepseek-harness/.shots-venv/.git/.env）→ scp 60.205 → scp 128.local → `docker build -t hatchery .` → `docker rm -f hatchery && docker run -d --name hatchery --restart unless-stopped -p 4173:4173 --env-file ~/hatchery/.env -v ~/hatchery/published:/app/published -v ~/hatchery/sites:/app/sites hatchery`
4. **泛域子域名 HTTPS**:CF 免费版证书不含 `*.hatchery.mizusumi.com`（二级子域），要么买 Advanced Certificate($10/月），要么长期用 `/pages/<slug>` 路径
5. **提交工作区**：我和 Codex 的大量改动都没提交。提交前逐文件 diff 给用户过目
6. 老站点 `李紫複`（中文 slug）连 `/pages/` 都 404——历史数据，slug 规则不兼容，用户决定要不要迁
7. mizusumi.com 的 ICP 备案：要根治阿里云拦截只有这一条正路，域名所有者去办

## 7. 雷区（都踩过，别再踩）

- **AI 文件权限沙盒**是用户钦定的（只能碰 `sites/<uid>/`）；AI 不再能改平台代码，所以"让 AI 加代码级功能"（如论坛新功能）现在做不到，需求来了要说明
- Codex 并发改文件：Edit 前必 Read 最新内容，写前先 git status
- Git Bash 嵌套 ssh 吃 stdin（加 `-n`）；heredoc 里 `\n` 转义会被吃掉一层；python 写 `/tmp` 在 Windows 落到 `C:\tmp`，和 Git Bash 的 /tmp 不是一个地方
- 服务器"自重启"假象：`/api/server/restart` 会让老进程退出、新进程接替——任务系统报 failed 不等于真死，先 curl 验证
- Windows 上 `http.server` 允许同端口多实例同时 LISTEN（SO_REUSEADDR 语义），杀不干净就分流——起了新实例先 `netstat -ano | grep 4173` 确认只有一个
- PGlite 被任务系统杀掉过一次（输出超 16MiB）→ 日志写文件；WASM 数据目录被强杀会损坏 → 重置 data 目录后手工重建兼容表（User/CampusUser + test 账号，模板在 secrets 文件里）
- dsh 运行时偶发"活但不说话"（Kimi 侧问题），现在有 300s 停滞帽兜底+清晰报错，别再往下挖
- **用户 Kimi 额度有限**，冒烟测试一次真实调用就烧几千到几万 token，测试前先想清楚必要性
- 用户对未经请示的合并/部署/动别人机器**非常敏感**——动任何共享状态前先说

## 8. 快速自检（接手后 5 分钟）

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4173/          # 本地
curl -s -o /dev/null -w "%{http_code}\n" http://100.114.192.6:4173/     # Docker(tailnet)
curl -s -o /dev/null -w "%{http_code}\n" https://hatchery.mizusumi.com/ # 公网（修好前是断的）
```

三个 200 且 `/api/ai/status` 返回 `configured:true` 就是健康的。
