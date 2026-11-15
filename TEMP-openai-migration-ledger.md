# Additive gateway integration ledger

## Authorized objective and scope

The user requested To-Tokens as the first proxy for GPT, Claude, Gemini and Grok,
while continuing existing proxy numbering. This is authorized implementation,
deployment and verification of that addition. Existing models, prompts, histories,
databases, fallbacks and provider credentials are retained. There is no direct
OpenAI account migration, new agent framework, or request for additional AI features.

The existing user-created per-provider keys on the supplied key page are reused.
They belong only in RP-Core GitHub Actions secrets, not source, Vercel or logs.

## Owning-source evidence

- The authenticated To-Tokens “使用密钥” panels on November 15, 2026 give
  `https://drc.totokens.cc` for Anthropic, Gemini and Codex, and
  `https://drc.totokens.cc/v1` for Grok/OpenAI-compatible clients.
- The Grok panel explicitly requires Responses, rather than Chat Completions.
  The GPT panel uses `wire_api = "responses"`.
- Authenticated, non-generating model-list requests succeeded for all four keys.
- Existing RP-Core uses Anthropic/Google SDKs and an OpenAI SDK Chat boundary.
  The OpenAI SDK version is pinned in pyproject.toml. Its public Responses and
  ChatCompletion/ChatCompletionChunk types supply the compatibility boundary.

## Guidance applicability assessment

| Guidance heading | Applicability / decision |
|---|---|
| Model selection | Retain all registered model IDs; this request changes proxy priority only. |
| Instruction file naming | No instruction or persona file changes. |
| Chat → Responses | Additive adapter for the new GPT/Grok routes only; retain existing clients elsewhere. |
| Streaming | Translate text/tool deltas, terminal usage/finish reasons, failures and cleanup. |
| WebSocket mode | Not requested; existing engine uses HTTP/SSE. |
| Privacy, ZDR, residency | Stateless history replay with store:false; this is not a ZDR or residency claim. Vendor retention remains vendor policy. |
| Processing tiers | No new tier selection; unsupported options rejected at the boundary. |
| Prompt caching / keys / retention / breakpoints / diagnostics | Do not introduce vendor-dependent controls; preserve returned cached-token accounting. |
| State / instruction persistence | Replay current system instructions, assistant text, tool calls and outputs on every request; no previous_response_id dependency. |
| Reasoning / encryption / effort / preservation / summaries | Map existing reasoning_effort. Existing Chat history has no encrypted reasoning items; do not claim added reasoning-state replay. |
| Verbosity | No new sampling or verbosity policy. |
| Maximum output tokens | Translate existing completion/output limit without changing model selection. |
| Assistant phases | Not present in existing Chat history; no new orchestration policy. |
| Compaction | Existing private memory/context compression retained; no hosted compaction. |
| Structured outputs | Translate existing response_format into Responses text.format. |
| Tools / choice / parallel calls | Preserve function definitions, IDs, outputs, choice and parallel controls. |
| Tool search / programmatic tools / aligned interfaces | Not used by these request paths and not part of this proxy addition. |

## Integration plan and approval

One user-authorized stage covers public configuration, ordering, all affected client
builders, Responses compatibility, Actions credential wiring, probes and dashboard.
No additional provider, model, personal-data, or paid-generation decision is needed.

## Coverage and validation

Focused tests cover new-first/old-tail ordering, Claude model-specific paths,
Gemini dual clients, missing-key behavior, Responses tool/history translation,
stream termination/cleanup, limits, errors and retained SDK routes. Run the existing
framework and compatibility suites, website tests, public-tree audit, provenance
review and workflow lint. Live verification uses non-generating endpoints and
the production health workflow; no test chat is sent.

Local validation: 132 framework tests, 826 compatibility regressions plus 55 subtests,
and all 11 website tests passed. Non-generating checks using the four new keys passed
for Claude 7/7, GPT 1/1, Gemini 6/6 and Grok 2/2 registered models. Workflow lint,
source provenance and the public-tree audit pass. Production Actions/dashboard
verification follows the deployment; successful model listing is not a live text
generation or tool-execution claim.
