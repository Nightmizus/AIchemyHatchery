# Personal API configuration

Signed-in users can open **Avatar → 我的 API** to save a Base URL, API key and
model ID for an OpenAI-compatible Chat Completions service. These settings apply
to subsequent website generation/chat/plan runs and to HatcheryStudy's default
provider (existing Study access restrictions remain). Explicit legacy Study
providers continue to use their own settings. In-flight runs keep their snapshot.

The model must support SSE streaming and function/tool calls for website editing.
The generic adapter currently supports text; image attachments are rejected by
the shared serializer. Providers exposing only Responses or Anthropic Messages
are not compatible. Native web search additionally requires the configured
gateway to support Anthropic Messages with native search; it does not fall back
to the platform's credentials. Plain public web fetching remains available.

## Storage and deployment

- Install `requirements.txt`. Startup automatically creates the additive
  `hatchery_personal_api` table, keyed by the authenticated account ID.
- Configurations are encrypted using Fernet and bound to their owning user ID.
  Neither read nor write responses return the key, and it is never included in
  queued jobs, browser storage, prompts or website files.
- Set **HATCHERY_API_ENCRYPTION_KEY** to a stable Fernet key in production.
  Generate one using the command in `.env.example`. Keep it in secret storage;
  back it up separately from the database. Never change it without migrating
  existing ciphertext. Every application replica must use the same key.
- Without this variable, a key is generated once at `.private-api-config/key`.
  Persist this directory when replacing a container, and back it up. Losing the
  key requires users to re-enter their settings. This directory is Git-ignored.
- Only public HTTPS endpoints on port 443 are accepted. Both Python requests and
  the child adapter connect to checked IPs while verifying TLS for the original
  host; they do not follow redirects with credentials or use ambient proxies.
  Personal native search uses a task-local, authenticated loopback bridge that
  forwards only Messages requests through that same checked Python HTTPS layer.
  DNS is revalidated at each outgoing connection, and the verified address is
  pinned through TLS. Native search keeps its existing parsing and citations.
- Blank key preserves the stored value for the same base URL; changing the URL
  requires a new key. Clearing deletes the configuration and restores defaults.
- Connection testing reads `/models` and checks the selected model. It does not
  generate content or verify billing, streaming, or tool-call support. Some
  gateways do not expose model metadata; settings can still be saved.

## HTTP API

`GET /api/account/api-config` returns `configured`, `baseUrl`, `model`, `hasKey`.
If existing ciphertext cannot be decrypted, it instead returns empty fields plus
`needsReconfiguration: true` and a recovery message. The UI requires a fresh key
and offers both replacement and deletion. Execution still fails closed until
the user recovers or clears the settings; unreadable credentials never silently
fall back to the shared service.
`POST` accepts `{action: "save", baseUrl, apiKey, model}`, `{action: "test"}` or
`{action: "clear"}`. Existing session authentication and same-origin protection
apply. The user ID always comes from the server session.

Rollback: revert the feature code. The extra database table can remain unused;
drop it only after backing up settings if permanent removal is intended.

Tests: `python scripts/personal_api_tests.py` and
`node --test scripts/personal_api_adapter_tests.mjs`. The Python suite includes
an optional real Harness initialization check that does not issue a model call.
For browser integration, install the Python Playwright development package and
Microsoft Edge, then run `python scripts/personal_api_browser_tests.py`.
Run `python scripts/personal_api_search_tests.py` for the local TLS, DNS rebinding,
redirect, account-isolation and actual native-search regressions (no paid calls).
