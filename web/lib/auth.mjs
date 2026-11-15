// SPDX-License-Identifier: AGPL-3.0-only
import {Auth} from '@auth/core';
import GitHub from '@auth/core/providers/github';
import {getToken} from '@auth/core/jwt';
import {settings, requireWriter} from './github.mjs';

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
      authorization: {params: {scope: 'read:user public_repo'}}, checks: ['pkce', 'state']})],
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
          token.githubId = String(profile.id);
          token.login = profile.login;
          token.providerExpiresAt = account.expires_at || null;
        }
        return token;
      },
      async session({session, token}) {
        // The OAuth access token stays only in the encrypted, HttpOnly session.
        return {expires: session.expires, user: {login: token.login, githubId: token.githubId}};
      },
      async redirect({url}) {
        if (url.startsWith('/') && !url.startsWith('//')) return `${origin}${url}`;
        try { if (new URL(url).origin === origin) return url; } catch {}
        return origin;
      },
    },
    logger: {error(error) { console.warn('OAuth request failed:', error.type || error.name || 'AuthError'); }, warn() {}, debug() {}},
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
