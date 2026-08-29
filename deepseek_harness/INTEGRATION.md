# DeepSeek Harness integration

The editor server launches an official DeepSeek Harness SDK runtime through
newline-delimited JSON-RPC. The runtime is local-only and intentionally not
committed because the Windows executable is a generated platform artifact.

Runtime location:

`../.deepseek-harness/runtime/deepseek-harness-sdk-runtime-win-x64.exe`

Required environment:

- `DEEPSEEK_API_KEY`
- optional `DEEPSEEK_MODEL` (default: `deepseek-v4-flash`)
- optional `DEEPSEEK_HARNESS_BIN` to override the runtime location

`site-agent.patch.yml` starts from the upstream `sdk-minimal` profile, disables
its shell/editor/subprocess tools, and mounts only `site-tools.mjs`. That plugin
enforces the server-provided readable/editable filename allowlists and limits
browser access to the current loopback preview.

The existing console authentication, shared cookie, and database schema remain
owned by `server.py`; Harness has no account or database tool.

## LLM providers

`deepseek_harness_adapter.py` picks the model provider via `MIAODA_LLM`
(`kimi` or `deepseek`); when unset it autodetects from the configured keys
(`KIMI_API_KEY` present → Kimi, otherwise DeepSeek).

- **DeepSeek** — built into the runtime (`deepseek-official` route); needs
  `DEEPSEEK_API_KEY`, optional `DEEPSEEK_MODEL` / `DEEPSEEK_REASONING_EFFORT`.
- **Kimi** — `llm-kimi.mjs` (next to `site-tools.mjs`), a minimal text-only
  OpenAI-compatible adapter registered as the `kimi` route. Needs
  `KIMI_API_KEY`; optional `KIMI_MODEL` (default `k3`),
  `KIMI_REASONING_EFFORT` (default `high`), `KIMI_BASE_URL`. Keys starting
  with `sk-kimi-` default to `https://api.kimi.com/coding/v1`, others to
  `https://api.moonshot.cn/v1`. Image attachments are rejected
  (text-only adapter).
