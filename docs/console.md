# RP-Core control console

The console is a standalone Vercel project in `web/`. It displays **RP-Core's own
Actions environment results**, edits the full engine's actual public model catalog,
and uses GitHub OAuth for authenticated Git commits. It never reads character cards,
Telegram messages, private repositories or provider credentials.

## Public configuration is the authority

`src/rp_core/compat_runtime/model-config.json` is the single public source for:

- Provider metadata and disaster recovery order.
- Model IDs, provider/tier/generation, explicit order, vision/chain flags, OpenRouter slugs.
- Four dialog tiers and the low-tier random pool.
- The optional explicit Claude summary chain (empty = automatic existing Claude chain).
- Embedding model/dimensions/input limit and allowlisted public connection/reasoning settings.

`config.models` derives its existing exported constants and helper functions from this
JSON, preserving the complete engine's API and initial routing behavior. This is not
the smaller native engine's `config/runtime/default.json`.

The public JSON and server share a strict schema plus semantic checks. Duplicate IDs,
missing route references, empty required client pools, credential fields and known
credential-like strings are rejected. Public URLs cannot contain userinfo, queries,
or fragments. Explicit `environment` entries override old nonsecret environment
defaults; omitted names preserve those defaults. Embedding settings come from JSON.

Changing an embedding model or dimension **does not rebuild memory or vector caches**.
Plan a separate data/index migration before making an incompatible change.

The web schema copy is checked in CI. Run `cd web && npm run build` after changing
the canonical schema, then commit both copies. New protocol/client families still
require a reviewed public-code change; adding a model ID cannot invent a client.

Routing add controls start with an empty “请选择…” choice and an “添加模型” label.
Selecting a candidate does not modify the configured chain; click “添加” to insert
it. After insertion, the control returns to its empty state.

## Deploy your fork

1. Create a Vercel project connected to your public fork, root directory **`web`**,
   framework **Other**, Node **22.x**, install `npm ci`, build `npm run build`,
   output **`public`**. Keep preview deployment protection enabled.
2. Add a stable production domain. Configure the values listed in `web/.env.example`
   as Production environment variables. Set your own repository, domain and numeric
   GitHub administrator IDs. An empty administrator list denies all writes.
3. Register a GitHub OAuth App with callback
   `https://YOUR-DOMAIN/api/auth/callback/github`. Use exact redirect matching, no
   wildcard, and leave device flow off. Enable expiring OAuth tokens when available.
4. Put `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` and a random 32-byte-or-longer
   `AUTH_SECRET` in Vercel as secrets. No provider API key and no server-side GitHub
   PAT are needed. Set credentials only on the production environment.
5. Redeploy. Sign in as an allowed GitHub user who can push to your public fork.

OAuth requests `read:user user:email public_repo` to identify the user (the
`user:email` scope is required so accounts with a private email address can sign in) and commit public model
configuration / trigger the public probe workflow. GitHub's OAuth scope covers
public repositories generally; **the server constrains writes to the one configured
public repository**, checks the numeric administrator allowlist and rechecks both
GitHub identity and current repository push permission on every mutation. It does
not request access to private repositories. Review this scope during GitHub consent.

Auth.js handles state, PKCE, CSRF and an encrypted HttpOnly/Secure SameSite session.
The OAuth access token is never returned by the session endpoint or stored in browser
JavaScript/localStorage. Sessions last at most eight hours and are rejected if the
upstream OAuth token has expired; log in again then. Origin checks protect config
writes and probe dispatches. Saving uses the current Git blob SHA; a conflict leaves
your draft in place and requires a reload/reconciliation, never a forced overwrite.

Reads are public. Everyone can inspect nonprivate settings and sanitized health
results; only authorized users can save or start a probe. OAuth is deliberately
disabled until all credentials and the administrator list are present.

## Site names and route switches

Route checkboxes use **checked = enabled**, unchecked = disabled. With the current
empty disabled lists all routes are checked. Proxy labels, IDs and credentials use
77, To-Tokens, Dragon, Codeyu and CoderPlan instead of numbered proxies.

Provider order on every page is Claude, Gemini, Grok, ChatGPT, DeepSeek, GLM, KIMI,
Qwen, OpenRouter, MagmaRouter. Routing rows and provider cards can be reordered by
dragging (or with the ↑/↓ buttons); the drag order is saved with the public config.
The health strips are 3px wide: green routes keep the full bar, orange
(models partially available) is half height, and red is a quarter height.

See [the Actions secret migration guide](provider-secrets.md) for the 13 new secret
names, the public Claude Codeyu URL variable, and the safe cutover order.

## Probe architecture

### Site-labelled proxy routes

Routes use their upstream site names. Each provider's first route follows the
saved `path_policies` order:

| Provider | First route | Public URL setting | Actions secret |
|---|---|---|---|
| Claude | 77 | `CLAUDE_77_BASE_URL` | `CLAUDE_77_API_KEY` |
| Gemini | Dragon | `GEMINI_DRAGON_BASE_URL` | `GEMINI_DRAGON_API_KEY` |
| Grok | Dragon | `GROK_DRAGON_BASE_URL` | `GROK_DRAGON_API_KEY` |
| GPT | To-Tokens | `GPT_TOTOKENS_BASE_URL` | `GPT_TOTOKENS_API_KEY` |
| DeepSeek | Official | `DEEPSEEK_BASE_URL` | `DEEPSEEK_API_KEY` |
| GLM | Official | `GLM_BASE_URL` | `GLM_API_KEY` |
| Kimi | Dragon | `KIMI_DRAGON_BASE_URL` | `KIMI_DRAGON_API_KEY` |

The vendor's key-use panels specify `https://drc.totokens.cc`; OpenAI-compatible
clients use its `/v1` base. GPT and Grok To-Tokens routes use the Responses protocol,
configured by `GPT_TOTOKENS_PROTOCOL` / `GROK_TOTOKENS_PROTOCOL`. The public compatibility
adapter preserves existing Chat callers, tool IDs/history, response text, streaming
deltas, finish reasons and token accounting. Existing proxies retain their transport.
Claude's model-specific proxy builder and Gemini's auxiliary dual client honor the
same new-first order. Missing new keys omit those routes and retain old fallbacks.

These are ordinary public nonsecret model settings, editable through the console;
the four independent credentials belong only in RP-Core Actions secrets.

`.github/workflows/provider-health.yml` runs every six hours, on model configuration
changes and on manual dispatch. It resolves registered instances, enters each same
GitHub Environment used by `instance.yml`, and probes using that environment's
RP-Core secrets. It checks out **only public RP-Core code**, uses a synthetic probe
card, and never obtains a private checkout or Telegram token. Provider checks use
model listing/token counting, not text/image generation. Qwen embedding is checked
through the non-generating model endpoint too; unsupported endpoints remain unknown.

Raw responses, key prefixes, URLs, upstream bodies, headers and freeform errors stay
in temporary runner files and are discarded. A whitelist serializer publishes only
status, model IDs, numeric latency/status/counts, timestamp, workflow run ID, public
instance identity and catalog SHA-256:

```
health/<instance-id>/latest.json
health/<instance-id>/history.json
```

History keeps 120 compact snapshots and never copies old Calling results. The console
reads those public files on demand, so a probe does not require a new Vercel deployment.
The full engine reads its own instance's snapshot. Expired results, a different
credential source or a different catalog hash fail open; a stale configuration's
health decisions never ban new model routes. Per-instance results and credential
environments remain separate.

The existing private diagnostic probe remains available for live ingress checks and
shadow validation. Its local outputs do not replace the shared dashboard's results.

## Local validation

```
uv run --extra dev --extra full pytest
uv run python scripts/run_compat_tests.py
cd web
npm ci
npm test
npm run build
```

For local browser development copy `web/.env.example` to ignored `web/.env.local`,
use `RP_APP_ORIGIN=http://localhost:3000`, and run `npm run dev`. A local OAuth callback
must be registered separately; never share a production session secret with previews.

Public model configuration changes apply to newly started jobs; an in-flight job
continues with the commit it checked out. Review the generated Git commit and Actions
run when saving. Roll back configuration through a normal Git revert, preserving
private character histories and databases.

CI first validates and loads the current editable model catalog through the actual
runtime. Imported historical regression cases then run against the fixed catalog in
`validation/fixtures/compat-model-config.json`, so their literal historical model IDs
and mocked service catalogs do not reject valid web configuration edits. This fixture
is used only inside the isolated regression sandbox; production and live probes keep
using the editable catalog. See `validation/fixtures/README.md` for the test boundary.
