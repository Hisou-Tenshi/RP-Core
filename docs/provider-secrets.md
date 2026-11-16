# Provider site names and Actions secret migration

Route IDs and labels use upstream site names: `codeyu` (Codeyu), `coderplan`
(CoderPlan), and `totokens` (To-Tokens). Claude's Codeyu routes retain separate
`base`, `prime`, and `max` tiers. To-Tokens remains first for Claude, Gemini, and
GPT. Grok Codeyu is first for Grok. Renaming does not change request protocols.

The console uses **checked = enabled**. Unchecking disables a route; checking it
again enables it. The persisted `path_policies.disabled` array keeps its existing
meaning. An empty array means every configured route is enabled. A route still
needs its credential and must pass the existing health gate to be attempted.

## New secret names

Use `PROVIDER_SITE[_TIER]_API_KEY`. The same names are passed by the instance,
health probe, and full-validation workflows. Keys remain separate per provider
and tier; no official key is reused as a proxy credential.

For the current installation, create these **repository secrets in
`Hisou-Tenshi/RP-Core` → Settings → Secrets and variables → Actions → Secrets**.
Do not create them in Tenshi, Calling, or Vercel. The replacement keys can use
the same values as their corresponding old secrets; the added Grok Codeyu key
uses the value you configured separately.

| Previously used secret | New secret |
|---|---|
| `CLAUDE_PROXY_KEY_3` | `CLAUDE_TOTOKENS_API_KEY` |
| `CLAUDE_PROXY_KEY_BASE` | `CLAUDE_CODEYU_BASE_API_KEY` |
| `CLAUDE_PROXY_KEY_PRIME` | `CLAUDE_CODEYU_PRIME_API_KEY` |
| `CLAUDE_PROXY_KEY_MAX` | `CLAUDE_CODEYU_MAX_API_KEY` |
| `CLAUDE_PROXY_KEY_2` | `CLAUDE_CODERPLAN_API_KEY` |
| `GEMINI_PROXY3_KEY` | `GEMINI_TOTOKENS_API_KEY` |
| `GEMINI_PROXY_KEY` | `GEMINI_CODEYU_API_KEY` |
| `GEMINI_PROXY2_KEY` | `GEMINI_CODERPLAN_API_KEY` |
| `GPT_PROXY3_KEY` | `GPT_TOTOKENS_API_KEY` |
| `GPT_PROXY_KEY` | `GPT_CODEYU_API_KEY` |
| `GPT_PROXY2_KEY` | `GPT_CODERPLAN_API_KEY` |
| `GROK_PROXY2_KEY` | `GROK_TOTOKENS_API_KEY` |
| `GROK_PROXY_KEY` | `GROK_CODERPLAN_API_KEY` |
| Newly added | `GROK_CODEYU_API_KEY` |

`CLAUDE_PROXY_KEY` was only a legacy fallback for an absent
`CLAUDE_PROXY_KEY_BASE`. Prefer the effective Base credential previously used;
the new code reads only `CLAUDE_CODEYU_BASE_API_KEY`.

The registered `tenshi` instance runs in the `tenshi` GitHub Environment. There
are currently no secrets in that Environment, so repository secrets are used.
Forks can use Environment secrets to isolate different instances; an Environment
secret with the same name takes precedence. The separate manual full-validation
workflow uses repository secrets.

## Public endpoint settings

Proxy URLs and To-Tokens GPT/Grok protocols live in the public model catalog and
remain editable through the console. Claude Codeyu uses `https://codeyu.shop`;
GPT and Grok Codeyu use `https://codeyu.shop/v1`. No provider URL needs to be
stored as an Actions secret or variable.

Official provider keys, OpenRouter, MagmaRouter, Telegram and GitHub secrets keep
their current names. Vercel needs no provider credentials.

## Cutover order

1. Keep the old secrets while the deployed workflows still use their old names.
2. Add the replacement repository secrets.
3. Verify names/scopes and the reviewed code locally; commit and publish the code
   only after the replacement settings are ready.
4. Run the public non-generating health probe, inspect the new site-labelled
   routes, then confirm the next real dialogue uses the new configuration.
5. Remove obsolete numbered secrets after successful cutover. Do not paste key
   values into chat, commits, test reports, workflow inputs, or logs.

Historical call counters accept old numbered route IDs and aggregate them under
the new site IDs. The console also displays old health snapshots with site names
and matches their history strips across the rename. Historical health files and
private conversations are not rewritten.
