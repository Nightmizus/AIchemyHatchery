# DeepSeek Harness integration

The editor server launches an official DeepSeek Harness SDK runtime through
newline-delimited JSON-RPC. The runtime is local-only and intentionally not
committed because the Windows executable is a generated platform artifact.

Runtime: the installed `deepseek-harness` Python package (`.venv/bin/dsh`) on Linux,
or `DEEPSEEK_HARNESS_BIN` for an explicitly selected installation. A legacy Windows
runtime location is also supported by the resolver.

Required environment:

- `DEEPSEEK_API_KEY`
- optional `DEEPSEEK_MODEL` (default: `deepseek-v4-flash`)
- optional `DEEPSEEK_HARNESS_BIN` to override the runtime location

This site's `.env.example` selects `MIAODA_LLM=deepseek` and
`DEEPSEEK_MODEL=DeepSeek-V4.1-Flash`, the exact model ID advertised by
`https://lightingtheword.com/v1/models`. Set the gateway base with
`HATCHERY_DEEPSEEK_BASE_URL=https://lightingtheword.com/v1` and supply its key
in the private `.env`. The server passes the endpoint to the native
`deepseek-official` adapter as the launch variable `DEEPSEEK_BASE_URL`.
Do not put `DEEPSEEK_BASE_URL` itself in the project's `.env`: DSH rejects
network bootstrap settings in discovered dotenv files. No custom DeepSeek
adapter or Kimi/GLM route is involved.

Model and reasoning settings are server-owned; the chat UI has no model
selector and request payloads cannot override those settings. The context
ring uses the latest model call's input (including cached input) plus output,
and the runtime's `request/context.contextWindow`. These facts are persisted
inside each chat's existing message HTML, independently of other sessions.
Billing totals still sum all calls in a run and are not used as context
occupancy. Before usage is available, or while composing, the ring explicitly
shows an estimate using `HATCHERY_AI_CONTEXT_WINDOW` (default 1,000,000 tokens).

`site-agent.patch.yml` starts from the upstream `sdk-minimal` profile, disables
its shell/editor/subprocess tools, and mounts `site-tools.mjs`. That plugin
confines all file operations to the server-selected website directory and limits
preview browser access to the current loopback preview.

The profile also loads the installed Harness packages `dsh-web`,
`dsh-web-search-deepseek`, `dsh-web-fetch-http`, `dsh-tool-web`, and
`dsh-tool-call-timeout-policy`. Their native `web_search` and `web_fetch` tools
are available in chat, plan, and full modes. Fetch reads public HTTP(S) pages;
the native provider enforces URL/address/redirect checks and response limits.
It does not execute page JavaScript or grant filesystem/shell access.

Search uses the native Anthropic-compatible Messages API with the
`web_search_20250305` server tool. Each search makes a model request. The
adapter supplies a separate `DEEPSEEK_SEARCH_BASE_URL` launch variable, using
`HATCHERY_DEEPSEEK_SEARCH_BASE_URL` when set or the existing
`HATCHERY_DEEPSEEK_BASE_URL` gateway otherwise. Official DeepSeek chat bases
map to `https://api.deepseek.com/anthropic/v1`; other gateways must support
`<base>/messages` and native search result blocks. Model and key default to
`DEEPSEEK_MODEL` / `DEEPSEEK_API_KEY`, with optional
`HATCHERY_DEEPSEEK_SEARCH_MODEL` / `HATCHERY_DEEPSEEK_SEARCH_API_KEY` overrides.
Only the active chat key and this search key enter the child environment;
both are redacted from runtime diagnostics. Keep the endpoint setting in the
`HATCHERY_` variable in `.env`, since DSH restricts network bootstrap variables
in discovered dotenv files. No fallback fabricates results or silently switches
search providers when a gateway rejects native search.

`scripts/ai_web_tools_tests.py` boots the installed runtime in all three modes
and exercises its tool registry against a local search fixture. It verifies
the native Messages request/result format, error reporting, public-fetch URL
restrictions, and the absence of write tools in chat/plan. `--live-fetch` also
reads `https://example.com/`; neither form submits a real model/search request.

The existing console authentication, shared cookie, and database schema remain
owned by `server.py`; Harness has no account or database tool.

## LLM providers

`deepseek_harness_adapter.py` picks the model provider via `MIAODA_LLM`
(`kimi`, `glm` or `deepseek`); when unset it autodetects from the configured
keys (`KIMI_API_KEY` present → Kimi, else `GLM_API_KEY` present → GLM,
otherwise DeepSeek).

- **DeepSeek** — built into the runtime (`deepseek-official` route); needs
  `DEEPSEEK_API_KEY`, optional `DEEPSEEK_MODEL` / `DEEPSEEK_REASONING_EFFORT`.
- **Kimi** — `llm-kimi.mjs` (next to `site-tools.mjs`), a minimal text-only
  OpenAI-compatible adapter registered as the `kimi` route. Needs
  `KIMI_API_KEY`; optional `KIMI_MODEL` (default `k3`),
  `KIMI_REASONING_EFFORT` (default `high`), `KIMI_BASE_URL`. Keys starting
  with `sk-kimi-` default to `https://api.kimi.com/coding/v1`, others to
  `https://api.moonshot.cn/v1`. Image attachments are rejected
  (text-only adapter).
- **GLM (智谱 BigModel, 国内)** — `llm-glm.mjs`, same shape as the Kimi
  adapter, registered as the `glm` route. Needs `GLM_API_KEY` (a
  `open.bigmodel.cn` key, `<id>.<secret>` form); optional `GLM_MODEL`
  (default `glm-5.3`; also `glm-5.3-flash`, `glm-4.7`),
  `GLM_REASONING_EFFORT` (default `high`), `GLM_BASE_URL` (default
  `https://open.bigmodel.cn/api/paas/v4`; GLM Coding Plan subscribers set
  `https://open.bigmodel.cn/api/coding/paas/v4`). Reasoning is sent in
  bigmodel's `thinking: {type: enabled|disabled}` form, with
  `reasoning_effort` added only for `glm-5.2+`; `tool_stream: true` is
  requested so tool-call arguments stream incrementally. Text-only.

## Direct website files (2026-10-11)

`site_files.py` owns directory initialization, one-time legacy migration, metadata,
path checks and file inventory. `server.py` resolves the directory from the
logged-in user's database ownership record. A client-supplied slug, filename or
`context.site` cannot select another user's workspace.

- Published: `published/<slug>/`. AI writes directly here; saved files immediately
  affect the live website. There is no JSON page model or per-run reconstruction.
- Before first publish: `published/.drafts/<sha256(user-id)>/`. First publish moves
  this directory into its public slug. Renaming a slug moves the whole directory
  and updates site-member database records rather than deleting them.
- `site.json` contains only `format: "files"`, `siteName`, `description`, `slug`,
  `url`, `createdAt`, `updatedAt`. It never contains page HTML/CSS/JS, layout,
  elements, images or chat history. The publisher owns `slug` and `url`.
- `index.html`, nested HTML pages, CSS, JS and assets are the actual website.
  Arbitrary files can be created/edited/moved/deleted inside the directory.
  PHP/Python/Node files are stored as files, not executed as server programs.
- New attachments are permanent files under `assets/uploads/`, with relative URLs.
  `runtime.json` retains legacy forum/registration data; account/session records
  remain in the existing database. Neither runtime.json nor site.json is publicly
  downloadable through the static file route.

The tool plugin provides `list_files`, `read_file` (UTF-8 or base64), `search_files`,
`write_file`, `replace_file`, `make_directory`, `move_file`, `delete_file`, and the
preview browser tools. Text and binary assets are supported. Tools reject absolute
paths, `..`, symlinks and hardlinks. `site.json` writes enforce the metadata schema.
Chat/plan modes do not register mutation tools. One per-user lock protects AI edits
and publishing, including synchronous API calls; AI cannot edit a directory while
it is being renamed. Writes replace individual files atomically. A failed model
run may leave already-saved edits in place; the UI reports this and reloads files.

`GET /api/site/files` returns metadata, filenames, HTML entry points and preview
URL. `POST` initializes/migrates once. `POST /api/publish` accepts metadata, slug and
gallery choice; it never packages page content. The console uses a sandboxed iframe
of `/preview/<id>/` and does not apply HTML into the console DOM. JavaScript modules,
CSS, images and nested pages are served directly, with GET/HEAD and directory slash
redirects. Use relative URLs so sites work both under a path and on a subdomain.
On site subdomains, user `script.js`, `styles.css`, etc. take precedence over console
assets. Preview and `/pages/<slug>/` documents have an opaque sandbox origin, so
console cookies/storage are inaccessible. Legacy member login/community APIs need
the published site's independent origin; path previews are for page/JS inspection.

Existing published JSON or editor drafts are exported once into physical HTML and
local `.hatchery/` compatibility CSS/JS. Available old attachment URLs are copied to
`assets/imported/`; already-missing media cannot be recovered automatically. Page content is in the HTML body, not in
JSON. A private backup is created at `.ai-backups/file-migrations/` before conversion.
Conflicting existing files stop migration instead of being overwritten. Subsequent
loads and AI runs ignore stale legacy page snapshots. Old database draft/preview
snapshots remain for recovery and are no longer the content source after migration.

Validation commands:

```sh
node scripts/site_file_tools_tests.mjs
.venv/bin/python scripts/site_files_tests.py
.venv/bin/python scripts/ai_context_tests.py
# Opt-in: spends real provider tokens, temporary sites only; includes browser check.
.venv/bin/python scripts/site_files_real_smoke.py
```

Production rollout must include `site_files.py`, the server/adapter, tool plugin,
and changed frontend files. Back up published directories, the ownership/member
index and existing previews/drafts before migration. This change does not configure
DNS, nginx path routing or TLS for the independent site domains.
## 持久任务与修复验证 2026 年 10 月

异步与同步请求共用 PostgreSQL 的 `hatchery_ai_jobs`。客户端传入稳定的 `requestId`，相同请求重试返回同一任务；同账号按入队顺序执行，跨账号遵守 `HATCHERY_AI_MAX_PARALLEL`。浏览器只提交请求和读取历史，不能用 `messagesHtml` 覆盖已存会话。确认接收结果不会删除任务结果。

执行中任务有租约。服务中断后，过期任务显示为中断，排队任务保留，系统不会自动重放已经开始的文件操作。`.ai-job-results/` 是数据库暂不可用时的私有结果缓冲，`.ai-backups/site-runs/` 保存修改前的文件；备份和缓冲需要持久磁盘与服务账号写入权限，不能放进公共站点或发布包。`site_files.py` 的锁同时覆盖线程和 Linux 进程。

默认发布链接为当前入口的 `/pages/<slug>/`。只有 DNS 和 HTTPS 已就绪时才设置 `ALCHEMY_HATCHERY_SITE_SUBDOMAINS_ENABLED=1`。本次生产按用户选择继续使用 IP，未启用域名。首次使用旧站点会导出真实页面和资源，保留原 JSON 私有备份；只读聊天与规划本身不做文件初始化。

本地回归入口：`scripts/ai_jobs_tests.py`（临时 PostgreSQL）、`scripts/ai_watchdog_tests.py`、`scripts/ai_context_tests.py`、`scripts/site_files_tests.py`、`scripts/site_file_tools_tests.mjs` 和 `scripts/ai_browser_regressions.py`。浏览器测试的模型边界是固定执行器，不能作为真实 AI 质量或容量证据。`scripts/ai_regression_support.py` 按 jobId/requestId/sessionId 关联终态，超时即中止顺序流程。旧内存队列测试器已停用，历史压测原件保留。

## 发布与 HTTPS 就绪（2026-10-15）

启用子域名后，`public_site_url()` 对本机 TLS 代理执行带 SNI 的实际握手，验证信任链、有效期和主机名；未通过时，发布响应、账号地址与 Gallery 都使用 `/pages/<slug>/`。发布响应和文件接口通过 `httpsPending` 告知前端等待，发布弹窗自动查询并在验证通过后切换到独立域名。证书文件已生成、签发命令成功或服务处于 active 均不能代替此检查。

原生部署将 `ops/hatchery-cert-sync.py` 安装为 root 所有的 `/usr/local/sbin/hatchery-cert-sync`，三个同名 systemd 单元安装到 `/etc/systemd/system/`。`ops/certbot-sitecustomize.py` 安装为 `/usr/local/lib/hatchery-certbot/sitecustomize.py`，向 ACME 网络客户端实际传入 180 秒超时；仅在导入后修改模块常量无法改变 Python 已绑定的默认参数。path 单元即时响应发布，timer 每两分钟补偿丢失触发与失败重试；普通失败从两分钟开始退避至十五分钟，CA 限流至少等待一小时。脚本会确认 nginx 实际提供的新证书；同一轮签发期间的新请求由下一次 path/timer 激活处理。脚本与单元随源码提交，避免仅在线上反复修补。

默认 TLS 代理为 `127.0.0.1:443`。容器环境需用 `ALCHEMY_HATCHERY_TLS_PROBE_HOST` / `ALCHEMY_HATCHERY_TLS_PROBE_PORT` 指向可达的代理；探测失败会保留可用路径地址。

回归：`.venv/bin/python scripts/site_https_tests.py`（临时真实 TLS + 本地发布与失败重试测试），以及安装 Playwright 的 Python 执行 `scripts/site_https_browser_tests.py`（发布后链接自动切换）。两者不调用真实 CA 或模型。

## 本网站动态服务权限（2026-10-15）

动态服务按网站单独开通，默认关闭。应用 `.env` 的 `HATCHERY_DYNAMIC_SITE_KEYS` 填账号 ID 的 SHA-256；root 所有的 `/etc/hatchery-sites/allowed-sites.json` 将同一标识绑定到实际网站目录的 `[st_dev, st_ino]`。目录改名保留 inode，因此授权可随发布路径迁移；不同网站即使提交相同标识，也不能选择其他目录。当前仅授权 `sleepsdsz` 的网站拥有者。

`ops/site-runtime-broker.py` 安装为 root 所有的 `/usr/local/sbin/hatchery-site-runtime`，配合同名 `ops/hatchery-site-runtime.service` 与 `ops/hatchery-sites.slice`。应用和 AI 不获得 root、sudo、systemctl 或宿主 shell。应用通过受限 Unix socket 请求 broker，AI 使用任务内、可撤销、不可自选账号/目录的能力令牌。只读模式只能查看状态、日志和密钥名称。

每站点有独立 Unix 身份、只读源码 `/app`、私有网络和受限 systemd 文件系统。仅本站私有 `/data` 可持久写入，位于独立 512 MiB ext4 卷；`/tmp` 为限额临时文件系统。源码通过原有文件工具修改。SQLite 默认 `/data/site.sqlite3`；密钥由 root 私有存储注入本站进程，平台 `.env`、数据库凭据、控制 socket、其他网站及宿主目录均不挂载。服务自身限制 192 MiB 内存、50% CPU、64 个任务；整组站点另有总资源上限。

`site_service` 提供 `exec/install/deploy/stop/status/logs/set_secrets/delete_secrets/secret_names`。后台入口必须在私有 `backend/` 目录；Python/Node 监听 `SOCKET_PATH` 指定的 Unix socket，平台将本站 `/_api/`、路径发布和预览中的对应地址代理到后台，移除此地址前缀。代理验证 socket 的实际进程 UID，控制台 Cookie 不转发，预览不接收后台 Cookie。服务进程和普通命令不能访问网络；pip/npm 安装单独允许公网仓库，禁止私网、云元数据和平台 IP。Python 只安装 wheel，npm 禁用安装脚本。更换依赖或密钥后重新部署服务。

broker 需要原生 Linux/systemd、`nodejs/npm/python3-pip/acl/e2fsprogs`；不适用于直接在应用 Docker 容器内开启宿主权限。上线前须实际验证 RootDirectory、独立 UID、只读源码、平台网络隔离及包安装网络的 BPF 策略。回归入口为 `scripts/site_runtime_tests.py` 与 `node scripts/site_runtime_tool_tests.mjs`；无需调用真实 AI。

原测试网站的修正由 `scripts/repair_recorded_test_site.py` 明确限定在 AIUI40 测试目录。补建标记标注来源，不把原来失败的压测改成通过。`scripts/deploy_ai_repairs.py` 默认只打包，显式 `--deploy` 才执行已审阅的原生部署与备份；它不会提交 Git、修改 DNS 或发起模型调用。线上只读与交互检查见 `scripts/verify_ai_repairs_live.py`。

原始运行时日志的补充恢复见 `scripts/recover_ai_runtime_evidence.py` 与 `scripts/restore_runtime_fragments.py`：36 条回复可对应既有记录，其中 21 条更完整，两个缺失区块有原始编辑内容。恢复文本按来源归档，不将旧失败轮次改判为通过。
