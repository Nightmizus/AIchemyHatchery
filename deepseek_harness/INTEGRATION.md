# DeepSeek Harness integration

## Website security audit

The 漏洞测试 panel audits the signed-in user's created website. `POST
/api/security/run` accepts only an idempotency `requestId`; ownership, directory,
publish/preview route and read-only mode are selected by the server. Jobs share
the durable AI queue, per-account serialization, model settings, personal API
configuration, usage tracking and stalled-session recovery. `GET
/api/security/latest` returns only that account's latest report, even after a
different tab acknowledges completion. The report is also retained in chat history.

`site_security.py` performs bounded anonymous GETs to fixed paths below the own
site's `/pages/<slug>/` or `/preview/<id>/` prefix on the local application. It
never probes the console root, follows redirects, sends cookies, executes page
JavaScript or persists response bodies. Errors and catch-all 200 responses are
not passes or confirmed leaks. A confirmed file leak requires matching the
response to the own file. Public proxy headers, authenticated workflows and
backend writes are explicitly outside these HTTP checks. Existing served-TLS
verification is reused for configured site subdomains.

The model receives the measured results and reads the own source, including
`backend/`, for evidence-based analysis. Security mode registers only
`list_files/read_file/search_files`; `security-audit.patch.yml` disables native
web tools, and no backend capability is issued. Markdown reports distinguish
confirmed issues, static-analysis candidates, proposed fixes and untested areas;
the audit cannot apply fixes. Model failure leaves completed HTTP evidence visible.

Regressions: `scripts/site_security_tests.py` (local HTTP, disposable PostgreSQL,
real native tool registry) and `scripts/site_security_browser_tests.py` (Chromium).
Neither calls a model. `scripts/site_security_smoke.py --live` explicitly opts
into one configured-model audit of disposable synthetic vulnerable source;
it consumes API usage but never scans a production website.

## 控制台嵌入预览（2026-10-19）

`frontend/site-preview.js` 管理右侧 iframe。文件列表接口提供基于路径、纳秒修改时间、大小和 inode 的 `previewRevision`；同一页面版本保持 iframe，设备尺寸切换、窗口重新获得焦点及未变化的文件同步不重新加载。并发文件同步合并，过期初始化响应忽略。页面内容变化、选择其他页面或手动重试才导航。

加载时显示深色提示；8 秒显示慢加载提示，30 秒显示失败及重试入口，迟到的成功响应仍可恢复。预览 HTML 注入只在嵌入请求中使用的随机令牌回执，在 `DOMContentLoaded` 时通知控制台，无需等待所有图片加载；父页面同时验证 iframe 来源与本次令牌。保留原有不含 `allow-same-origin` 的 sandbox，不让网站访问控制台。

预览资源每次仍查询真实归属和封禁状态，合并为一次数据库查询。HTML 保持 `no-store`；CSS、脚本和图片使用 `private, no-cache, must-revalidate`，浏览器每次重新验证，未变化返回 304 并避免读取文件。ETag 使用纳秒修改时间、大小和 inode，防止同一秒内等长修改命中旧缓存。

回归：`python scripts/site_files_tests.py` 与安装 Playwright 的 Python 执行 `scripts/preview_browser_tests.py`。覆盖真实临时文件 HTTP、条件缓存、沙箱、慢资源、错误重试、超时恢复、过期响应和预览状态保留，不访问生产或调用模型。原生部署需同步 `server.py`、`site_files.py`、`frontend/index.html`、`frontend/script.js`、`frontend/site-preview.js`、`frontend/ai-chat.css`；新增脚本沿用控制台内容哈希版本。

## Gallery 分类与管理（2026-10-19）

`/gallery` 按“合作社团”“其他网站”依次展示公开收录且文件存在的网站。页面支持搜索、移动端和无 JavaScript 浏览。`gallery.py` 负责渲染与分类持久化，模板、样式和搜索分别位于 `frontend/gallery.html`、`gallery.css`、`gallery.js`；CSS/JS 使用内容哈希版本号。

分类保存于平台私有表 `hatchery_gallery_categories`，不读取网站自己声明的分类。初始化迁移只执行一次，将当时已存在的 `cogito`、`racingbusiness`、`sdszrootca`、`sdszemergingmarketssociety` 归入合作社团；以后的新发布网站一律默认其他网站。管理员在“管理后台 → 已发布网站”一键调整，接口 `POST /api/admin/sites/category` 要求管理员身份、同源请求和布尔值 `isPartner`，并记录审计。它支持仅磁盘存在的历史网站，保持原有收录开关。重新发布及改名保留分类；新账号使用旧路径不会继承旧分类。

回归：`python scripts/gallery_category_tests.py`（临时真实 PostgreSQL + 发布回归）和安装 Playwright 的 Python 执行 `scripts/gallery_browser_tests.py`（真实分类接口、搜索、管理员操作、桌面与移动端截图；其余控制台服务使用测试数据）。两者不连接生产库或调用模型。原生部署需包含 `gallery.py` 和三个 Gallery 前端文件，并同步 `server.py`、`frontend/script.js`、`frontend/mica.css`；Dockerfile 已包含新模块。

## Runtime

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

### 浏览模板（2026-10-19）

控制台提供 24 个模块的名称/用途搜索、分类筛选、只读布局示例、跨筛选多选（每次最多 8 个）、目标 HTML 页面选择与补充要求。选择和要求按账号保存在当前标签页的 sessionStorage，关闭或刷新可恢复；附件沿用聊天输入区，受理失败时不清空。登出清除页面内的账号状态。

示例由独立的无脚本 sandbox iframe 展示，CSP 禁止脚本、表单和非样式网络资源，不加载网站后台或演示登录。模块代码只作结构参考：不调用旧编辑器的建页函数，不生成假详情页；账号、论坛、投票、报名等明确要求 AI 接通本站真实服务与权限检查，不能把演示状态当作已完成的功能。

“交给 AI 添加”明确是本次修改操作，提交 `mode=full` 和所选 `activeFile`，不改变聊天的默认权限模式。仍通过原有鉴权、账号目录绑定及持久队列执行；浏览时无需等待其他任务，已有任务时新请求由服务器排队。提交期间锁定本次操作，收到有效 jobId/requestId 后才清空选择与已发送附件。失败保留输入；网络重试使用稳定请求编号，模板请求的待确认记录与普通聊天分开。关闭面板不会撤销已提交任务。

回归：`scripts/template_browser_tests.py` 使用实际控制台、临时网站文件和真实请求校验接口，仅替换任务存储/执行端，不调用付费模型或修改生产网站。覆盖全部模块、无副作用预览、搜索与筛选、快速开关/重复点击、失败保留、排队时提交、断网及丢失确认重试、刷新、账号隔离、键盘与 320/390px 布局。与 `scripts/ai_queue_browser_tests.py`、`scripts/ai_markdown_browser_tests.py`、`scripts/preview_browser_tests.py` 一起回归。示例不是生成后网站功能已通过测试的保证；AI 生成的真实网站仍需针对实际内容验证。

### 对话、队列与恢复

最终回复与计划在浏览器中统一进行 Markdown 渲染，已有服务端历史在加载、轮询和刷新后同样生效；数据库仍保存原始报告，不修改用户消息或工具日志。段落、单换行、列表、标题、代码块、引用、表格和链接可正常显示，执行计划与后续上下文读取保留原始 Markdown。解析器为本地自托管的 markdown-it 15.0.3 UMD 构建（NPM `dist/browser/markdown-it.umd.min.js`；MIT 许可证保存在 `frontend/markdown-it.LICENSE.txt`），不依赖 CDN。禁用原生 HTML 与图片自动加载，沿用解析器的危险 URL 校验，外链使用 `noopener noreferrer`。系统提示要求在严格 JSON 的 summary/plan 字符串中使用 Markdown 和真实换行，不改变外层协议、网站权限或运行时限制。回归：`node scripts/ai_markdown_tests.mjs`、`scripts/ai_markdown_browser_tests.py`（真实控制台与历史渲染、受控提交接口、无模型调用）。

异步与同步请求共用 PostgreSQL 的 `hatchery_ai_jobs`。客户端传入稳定的 `requestId`，相同请求重试返回同一任务；同账号按入队顺序执行，跨账号遵守 `HATCHERY_AI_MAX_PARALLEL`。浏览器只提交请求和读取历史，不能用 `messagesHtml` 覆盖已存会话。确认接收结果不会删除任务结果。

2026-10-15 排队修复：跨账号并发默认 2，`.env` 中的 `HATCHERY_AI_MAX_PARALLEL` 支持运行中热更新（1–4），调小上限不会取消在途任务。`/api/ai/run/active` 返回不含其他账号标识/内容的排队原因、并发占用和前方等待数，区分“本账号前一条未结束”和“全局容量已满”。前端只有取得有效 jobId/requestId 确认才显示服务器排队；提交与轮询请求有超时，明确 404 会清除本地幽灵等待，但不会自动重放执行。损坏结果缓冲保留为 `.invalid` 私有文件，不得阻塞其他任务。控制台 HTML 强制不缓存，JS/CSS URL 由文件内容哈希生成，修复不会再依赖人工改资源版本号。回归新增 `scripts/ai_queue_browser_tests.py`，与真实临时 PostgreSQL 的任务测试配套执行，不调用真实模型。

会话自恢复：`HATCHERY_AI_STALL_SECONDS=180`，原生进程退出、通信管道断开、初始化/投递超时、原生会话冲突、无完整结果或持续无有效进展时，可在同一个持久任务和网站锁内续作。`HATCHERY_AI_AUTO_RECOVERIES=1` 默认最多自动恢复一次（0 关闭，硬上限 2）。按用户要求，任务及恢复流程均不设总执行时长上限；旧 `DEEPSEEK_HARNESS_TIMEOUT_SECONDS` 不再生效。只保留初始化/投递确认的 60 秒通信超时和无有效进展检测；持续有效进展不会因运行时间过长被终止。同步 HTTP 等待 25 秒后返回 202，原持久任务继续执行，不代表任务超时或失败。每次使用新的原生进程与随机尝试 ID，用户账号对话 ID 不变；保留失败尝试日志与 `.ai-job-results/recoveries/<runId>/` 私有检查点，刷新当前文件清单后继续，不能从旧页面快照覆盖已保存成果。真实 `assistant/attempt` 流式进展计入看门狗，其他会话通知不能延长等待。配额/认证错误不自动重试；若有未确认完成的后台写操作，停止恢复以防重复写入。已确认操作仅以名称记录，命令和密钥不进恢复记录；新能力令牌重新按同一账号/目录/模式绑定，旧令牌在尝试结束时撤销，不再因固定时长到期而打断长任务。无法取得所有尝试的 usage 时总用量标未知，不把最后一次调用当全部用量。回归为 `scripts/ai_recovery_tests.py` 与 `scripts/ai_watchdog_tests.py`，均不调用真实模型。

执行中任务有租约。服务中断后，过期任务显示为中断，排队任务保留，系统不会自动重放已经开始的文件操作。`.ai-job-results/` 是数据库暂不可用时的私有结果缓冲，`.ai-backups/site-runs/` 保存修改前的文件；备份和缓冲需要持久磁盘与服务账号写入权限，不能放进公共站点或发布包。`site_files.py` 的锁同时覆盖线程和 Linux 进程。

默认发布链接为当前入口的 `/pages/<slug>/`。只有 DNS 和 HTTPS 已就绪时才设置 `ALCHEMY_HATCHERY_SITE_SUBDOMAINS_ENABLED=1`。本次生产按用户选择继续使用 IP，未启用域名。首次使用旧站点会导出真实页面和资源，保留原 JSON 私有备份；只读聊天与规划本身不做文件初始化。

本地回归入口：`scripts/ai_jobs_tests.py`（临时 PostgreSQL）、`scripts/ai_watchdog_tests.py`、`scripts/ai_context_tests.py`、`scripts/site_files_tests.py`、`scripts/site_file_tools_tests.mjs` 和 `scripts/ai_browser_regressions.py`。浏览器测试的模型边界是固定执行器，不能作为真实 AI 质量或容量证据。`scripts/ai_regression_support.py` 按 jobId/requestId/sessionId 关联终态，超时即中止顺序流程。旧内存队列测试器已停用，历史压测原件保留。

## 发布与 HTTPS 就绪（2026-10-15）

启用子域名后，`public_site_url()` 对本机 TLS 代理执行带 SNI 的实际握手，验证信任链、有效期和主机名；未通过时，发布响应、账号地址与 Gallery 都使用 `/pages/<slug>/`。发布响应和文件接口通过 `httpsPending` 告知前端等待，发布弹窗自动查询并在验证通过后切换到独立域名。证书文件已生成、签发命令成功或服务处于 active 均不能代替此检查。

原生部署将 `ops/hatchery-cert-sync.py` 安装为 root 所有的 `/usr/local/sbin/hatchery-cert-sync`，三个同名 systemd 单元安装到 `/etc/systemd/system/`。`ops/certbot-sitecustomize.py` 安装为 `/usr/local/lib/hatchery-certbot/sitecustomize.py`，向 ACME 网络客户端实际传入 180 秒超时；仅在导入后修改模块常量无法改变 Python 已绑定的默认参数。path 单元即时响应发布，timer 每两分钟补偿丢失触发与失败重试；普通失败从两分钟开始退避至十五分钟，CA 限流至少等待一小时。脚本会确认 nginx 实际提供的新证书；同一轮签发期间的新请求由下一次 path/timer 激活处理。脚本与单元随源码提交，避免仅在线上反复修补。

默认 TLS 代理为 `127.0.0.1:443`。容器环境需用 `ALCHEMY_HATCHERY_TLS_PROBE_HOST` / `ALCHEMY_HATCHERY_TLS_PROBE_PORT` 指向可达的代理；探测失败会保留可用路径地址。

回归：`.venv/bin/python scripts/site_https_tests.py`（临时真实 TLS + 本地发布与失败重试测试），以及安装 Playwright 的 Python 执行 `scripts/site_https_browser_tests.py`（发布后链接自动切换）。两者不调用真实 CA 或模型。

## 本网站动态服务权限（2026-10-15）

动态服务在未配置的环境默认关闭。应用 `.env` 的 `HATCHERY_DYNAMIC_SITE_KEYS` 可以填账号 ID 的 SHA-256（逗号分隔）逐站授权，也可以填 `*` 对全部现有及以后新增的账号开放。全账号模式同时要求 root 私有的 `/etc/hatchery-sites/policy.json` 设置 `{"allowAllAccounts":true}`；broker 首次收到已初始化网站的请求时，以非特权 `hatchery` 身份独立查询真实账号及数据库中的网站归属，验证服务端绑定的账号 ID、SHA-256 和目录完全一致，再原子写入 root 所有的 `/etc/hatchery-sites/allowed-sites.json`，绑定实际目录的 `[st_dev, st_ino]`。不能用网站元数据或模型传入的路径证明归属；并发授权串行合并，已有绑定不能被其他账号接管。目录改名保留 inode，因此授权可随发布路径迁移。生产现按用户要求面向全部账号开放，无需逐个维护白名单；尚未建站的账号在初始化网站后首次使用时自动绑定，不预创建网站或启动后台进程。

`ops/site-runtime-broker.py` 安装为 root 所有的 `/usr/local/sbin/hatchery-site-runtime`，配合同名 `ops/hatchery-site-runtime.service` 与 `ops/hatchery-sites.slice`。应用和 AI 不获得 root、sudo、systemctl 或宿主 shell。应用通过受限 Unix socket 请求 broker，AI 使用任务内、可撤销、不可自选账号/目录的能力令牌。只读模式只能查看状态、日志和密钥名称。

每站点有独立 Unix 身份、只读源码 `/app`、私有网络和受限 systemd 文件系统。仅本站私有 `/data` 可持久写入，位于独立 512 MiB ext4 卷；`/tmp` 为限额临时文件系统。源码通过原有文件工具修改。SQLite 默认 `/data/site.sqlite3`；密钥由 root 私有存储注入本站进程，平台 `.env`、数据库凭据、控制 socket、其他网站及宿主目录均不挂载。服务自身限制 192 MiB 内存、50% CPU、64 个任务；整组站点另有总资源上限。

`site_service` 提供 `exec/install/deploy/stop/status/logs/set_secrets/delete_secrets/secret_names`。后台入口必须在私有 `backend/` 目录；Python/Node 监听 `SOCKET_PATH` 指定的 Unix socket，平台将本站 `/_api/`、路径发布和预览中的对应地址代理到后台，移除此地址前缀。代理验证 socket 的实际进程 UID，控制台 Cookie 不转发，预览不接收后台 Cookie。服务进程和普通命令不能访问网络；pip/npm 安装单独允许公网仓库，禁止私网、云元数据和平台 IP。Python 只安装 wheel，npm 禁用安装脚本。更换依赖或密钥后重新部署服务。

broker 需要原生 Linux/systemd、`nodejs/npm/python3-pip/acl/e2fsprogs`；不适用于直接在应用 Docker 容器内开启宿主权限。上线前须实际验证 RootDirectory、独立 UID、只读源码、平台网络隔离及包安装网络的 BPF 策略。回归入口为 `scripts/site_runtime_tests.py` 与 `node scripts/site_runtime_tool_tests.mjs`；无需调用真实 AI。

原测试网站的修正由 `scripts/repair_recorded_test_site.py` 明确限定在 AIUI40 测试目录。补建标记标注来源，不把原来失败的压测改成通过。`scripts/deploy_ai_repairs.py` 默认只打包，显式 `--deploy` 才执行已审阅的原生部署与备份；它不会提交 Git、修改 DNS 或发起模型调用。线上只读与交互检查见 `scripts/verify_ai_repairs_live.py`。

原始运行时日志的补充恢复见 `scripts/recover_ai_runtime_evidence.py` 与 `scripts/restore_runtime_fragments.py`：36 条回复可对应既有记录，其中 21 条更完整，两个缺失区块有原始编辑内容。恢复文本按来源归档，不将旧失败轮次改判为通过。
