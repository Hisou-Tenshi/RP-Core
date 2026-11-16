// SPDX-License-Identifier: AGPL-3.0-only
import {Auth} from '@auth/core';
import GitHub from '@auth/core/providers/github';
import {getToken} from '@auth/core/jwt';
import {settings, requireWriter} from './github.mjs';

// OAuth failures reach the browser as the opaque `error=Configuration`. Keep the
// sanitised last failure so the console can explain what actually happened.
const issueLimit = 400;
let lastIssue = null;

export function scrubAuthText(text) {
  if (typeof text !== 'string') return '';
  return text
    .replace(/([?&](?:code|state|session_state|csrfToken|error_description)=)[^&\s]+/gi, '$1[redacted]')
    .replace(/\b(?:gho|ghu|ghs|ghp|github_pat)_[A-Za-z0-9_]+/g, '[redacted-token]')
    .slice(0, issueLimit);
}

export function lastAuthIssue() {
  return lastIssue ? {...lastIssue} : null;
}

export function authFacts(env = process.env) {
  let origin = null, repository = null, admins = 0;
  try {
    const config = settings(env);
    origin = config.origin; repository = config.repository; admins = config.adminIds.size;
  } catch {}
  return {origin, repository, admins, env: {
    AUTH_SECRET: Boolean(env.AUTH_SECRET), GITHUB_CLIENT_ID: Boolean(env.GITHUB_CLIENT_ID),
    GITHUB_CLIENT_SECRET: Boolean(env.GITHUB_CLIENT_SECRET),
    RP_ADMIN_IDS: Boolean(env.RP_ADMIN_IDS), RP_APP_ORIGIN: Boolean(env.RP_APP_ORIGIN)}};
}

export function authReady(env = process.env) {
  return Boolean(env.AUTH_SECRET && env.GITHUB_CLIENT_ID && env.GITHUB_CLIENT_SECRET && env.RP_ADMIN_IDS && env.RP_APP_ORIGIN);
}

export function authConfig(env = process.env) {
  const {origin} = settings(env);
  const secure = origin.startsWith('https:');
  return {
    secret: env.AUTH_SECRET, basePath: '/api/auth', trustHost: true, useSecureCookies: secure,
    session: {strategy: 'jwt', maxAge: 60 * 60 * 8},
    providers: [GitHub({clientId: env.GITHUB_CLIENT_ID, clientSecret: env.GITHUB_CLIENT_SECRET,
      // user:email keeps Auth.js from failing when the profile hides its email
      // and the provider has to fall back to /user/emails.
      authorization: {params: {scope: 'read:user user:email public_repo'}}, checks: ['pkce', 'state']})],
    callbacks: {
      async signIn({profile, account}) {
        try {
          await requireWriter({githubId: String(profile?.id), accessToken: account?.access_token}, {env});
          return true;
        } catch { return false; }
      },
      async jwt({token, account, profile}) {
        if (account) {
          token.accessToken = account.access_token;
          token.githubId = profile?.id === undefined ? null : String(profile.id);
          token.login = profile?.login || null;
          token.providerExpiresAt = account.expires_at || null;
        }
        return token;
      },
      async session({session, token}) {
        // The OAuth access token stays only in the encrypted, HttpOnly session.
        return {expires: session.expires, user: {login: token.login, githubId: token.githubId}};
      },
      async redirect({url}) {
        if (typeof url !== 'string') return origin;
        if (url.startsWith('/') && !url.startsWith('//')) return `${origin}${url}`;
        try { if (new URL(url).origin === origin) return url; } catch {}
        return origin;
      },
    },
    logger: {
      error(error) {
        const cause = error?.cause?.err || error?.cause || null;
        lastIssue = {at: new Date().toISOString(), type: error?.type || error?.name || 'AuthError',
          message: scrubAuthText(error?.message), causeName: cause?.name || '', causeMessage: scrubAuthText(cause?.message)};
        console.warn(`OAuth request failed: ${lastIssue.type}${lastIssue.causeName ? ` · ${lastIssue.causeName}: ${lastIssue.causeMessage}` : ''}`);
      },
      warn() {}, debug() {},
    },
  };
}

export async function sessionToken(request, env = process.env) {
  if (!authReady(env)) return null;
  const secure = settings(env).origin.startsWith('https:');
  const cookieName = secure ? '__Secure-authjs.session-token' : 'authjs.session-token';
  try {
    const token = await getToken({req: request, secret: env.AUTH_SECRET, secureCookie: secure, cookieName, salt: cookieName});
    if (token?.providerExpiresAt && Date.now() >= token.providerExpiresAt * 1000) return null;
    return token;
  } catch { return null; }
}

export async function handleAuth(request, env = process.env) {
  return Auth(request, authConfig(env));
}
