// SPDX-License-Identifier: AGPL-3.0-only
// Auth.js hides nearly every callback failure behind `error=Configuration`, and its
// default error page is an English dead end. Serve our own page with the concrete
// cause, the expected callback URL and the environment facts the console needs.

const hints = {
  Configuration: ['GitHub 回调地址与 RP_APP_ORIGIN 不一致，或 GITHUB_CLIENT_SECRET 与 OAuth App 不匹配。',
    'Auth.js 用 Configuration 表示"回调过程中发生了未分类异常"，具体原因见下面的诊断行。'],
  AccessDenied: ['这个 GitHub 账号不在 RP_ADMIN_IDS 里，或者对公开框架仓库没有写权限。'],
  OAuthCallbackError: ['GitHub 拒绝了授权码换取 token（client secret 错误或回调地址不匹配）。'],
  Verification: ['登录链接已过期或已被使用，请重新点击登录。'],
  MissingCSRF: ['页面停留过久导致 CSRF 令牌失效，请刷新后重试。'],
  OAuthAccountNotLinked: ['该邮箱已绑定其他登录方式。'],
  InvalidCheck: ['浏览器没有把登录时写入的 state Cookie 带回回调地址，Auth.js 因此判定登录被篡改。',
    '常见于 App 内置浏览器（微信、飞书等）、无痕模式、隐私插件或跨站 Cookie 被拦截；请换普通窗口重新登录。'],
  OperationProcessingError: ['GitHub 返回的 token 响应不是预期格式，通常是 client secret 与这个 OAuth App 不匹配。'],
};

const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

export function renderAuthError({error = '', issue = null, facts = {}, current = false} = {}) {
  const code = String(error || 'Configuration');
  // Configuration is Auth.js's catch-all; when we captured the real failure, explain that
  // one instead of repeating "something unspecified went wrong".
  const hintKey = code === 'Configuration' && issue?.causeName ? issue.causeName : code;
  const [first, second] = hints[hintKey] || ['登录没有完成。请重试；若持续失败，请检查下面的诊断信息。'];
  const callbackUrl = facts.origin ? `${facts.origin}/api/auth/callback/github` : '（尚未配置 RP_APP_ORIGIN）';
  const when = issue?.at ? new Date(issue.at).toLocaleTimeString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '';
  const cause = issue
    ? [issue.type, issue.causeName].filter(Boolean).join(' · ')
    : '';
  const detail = issue ? (issue.causeMessage || issue.message || '') : '';
  const missing = Object.entries(facts.env || {}).filter(([, set]) => !set).map(([name]) => name);
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f4f7fb">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#131c2a">
<title>登录未完成 · RP-Core</title>
<style>
:root{color-scheme:light dark;--bg:#f4f7fb;--panel:#fff;--line:#dde5ef;--line-soft:#e9eff7;--text:#111c2b;--muted:#56657a;--accent:#1f6fe0;--accent-soft:#e9f1ff;--accent-soft-border:#bed5fb;--bad:#bf3742;--bad-bg:#fdebed;--bad-border:#f0b8bd;--code:#f2f6fb}
@media (prefers-color-scheme:dark){:root{--bg:#131c2a;--panel:#1a2434;--line:#2b394c;--line-soft:#263346;--text:#e9eff8;--muted:#94a3b8;--accent:#6b9dff;--accent-soft:rgba(107,157,255,.14);--accent-soft-border:rgba(107,157,255,.42);--bad:#ff9198;--bad-bg:rgba(255,145,152,.14);--bad-border:rgba(255,145,152,.38);--code:#0f1724}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);padding:28px 18px;
  font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",Helvetica,Arial,sans-serif}
main{width:100%;max-width:660px}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:18px;color:var(--muted);font-size:12px;letter-spacing:2px}
.brand b{display:grid;place-items:center;width:34px;height:34px;border-radius:9px;background:var(--accent-soft);color:var(--accent);letter-spacing:0;font-size:15px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:26px 26px 22px;box-shadow:0 18px 40px -32px rgba(15,23,42,.5)}
h1{margin:0 0 6px;font-size:21px;letter-spacing:-.4px}
p{margin:0 0 12px;color:var(--muted)}
.badge{display:inline-block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--bad);background:var(--bad-bg);border:1px solid var(--bad-border);border-radius:6px;padding:2px 8px;margin-bottom:14px}
h2{margin:22px 0 8px;font-size:13px;letter-spacing:.5px;color:var(--text)}
ul{margin:0 0 4px;padding-left:18px;color:var(--muted)}
li{margin-bottom:4px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;margin:8px 0 0;font-size:12.5px}
dt{color:var(--muted);white-space:nowrap}
dd{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
code{background:var(--code);border:1px solid var(--line-soft);border-radius:5px;padding:1px 5px;font-size:12px}
.diag{background:var(--accent-soft);border:1px solid var(--accent-soft-border);border-radius:9px;padding:12px 14px;margin-top:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}
a.button{display:inline-flex;align-items:center;gap:6px;text-decoration:none;border-radius:8px;padding:9px 16px;font-size:13px;border:1px solid var(--line);color:var(--text);background:var(--panel)}
a.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.note{font-size:12px;color:var(--muted);margin:-4px 0 0}
@media (prefers-color-scheme:dark){a.primary{color:#0a1526}}
</style>
</head>
<body><main>
  <div class="brand"><b>rp·</b>RP-CORE · 运行控制台</div>
  <section class="card">
    <h1>登录未完成</h1>
    <span class="badge">${escape(code)}</span>
    <p>${escape(first)}</p>
    ${second ? `<p>${escape(second)}</p>` : ''}
    ${cause ? `<div class="diag">诊断${when ? `（记录于 ${escape(when)}）` : ''}：${escape(cause)}${detail ? ` — ${escape(detail)}` : ''}</div>` : ''}
    ${issue ? `<p class="note">${current ? '这是本次登录尝试失败的原因。' : '这是服务器最近两分钟内记录的登录失败；如果你刚刚没有重新登录，它可能来自其他会话。'}</p>` : ''}
    <h2>GitHub OAuth App 回调地址</h2>
    <p><code>${escape(callbackUrl)}</code></p>
    <h2>当前环境</h2>
    <dl>
      <dt>管理站域名</dt><dd>${escape(facts.origin || '未配置')}</dd>
      <dt>公开仓库</dt><dd>${escape(facts.repository || '未配置')}</dd>
      <dt>管理员数量</dt><dd>${escape(facts.admins ?? 0)}</dd>
      <dt>缺少变量</dt><dd>${missing.length ? escape(missing.join(', ')) : '无'}</dd>
    </dl>
    <h2>可以检查</h2>
    <ul>
      <li>GitHub OAuth App 的 callback URL 必须与上面的地址完全一致。</li>
      <li><code>GITHUB_CLIENT_ID</code> / <code>GITHUB_CLIENT_SECRET</code> 必须来自同一个 OAuth App，改过 secret 后要在 Vercel 重新部署。</li>
      <li><code>RP_APP_ORIGIN</code> 必须是登录时访问的域名（例如 <code>${escape(facts.origin || 'https://your-console.vercel.app')}</code>）。</li>
      <li>浏览器或隐私插件拦截 Cookie 会丢失 state（诊断显示 InvalidCheck），请换普通窗口再试。</li>
    </ul>
    <div class="actions"><a class="button primary" href="/">返回控制台</a><a class="button" href="/api/auth/diagnostics">诊断 JSON</a></div>
  </section>
</main></body></html>`;
}
