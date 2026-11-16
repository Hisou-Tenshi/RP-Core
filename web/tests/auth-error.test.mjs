// SPDX-License-Identifier: AGPL-3.0-only
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {randomBytes} from 'node:crypto';
import {handle} from '../lib/app.mjs';
import {authFacts, scrubAuthText} from '../lib/auth.mjs';

const env = {RP_PUBLIC_REPOSITORY:'example/framework', RP_PUBLIC_BRANCH:'main', RP_APP_ORIGIN:'https://console.example',
  RP_ADMIN_IDS:'1,2', AUTH_SECRET:randomBytes(32).toString('hex'), GITHUB_CLIENT_ID:'test', GITHUB_CLIENT_SECRET:'test'};
const get = (path, override = env) => handle(new Request(env.RP_APP_ORIGIN + path), {env: override, fetcher: () => { throw new Error('No network allowed'); }});

test('auth error page explains the failure without echoing secrets or codes', async () => {
  const response = await get('/api/auth/error?error=Configuration');
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /text\/html/);
  const body = await response.text();
  assert.match(body, /登录未完成/);
  assert.match(body, /Configuration/);
  assert.match(body, /https:\/\/console\.example\/api\/auth\/callback\/github/);
  assert.match(body, /2/, 'admin count is surfaced');
});

test('auth error page escapes attacker-controlled error codes and stays usable when OAuth is unset', async () => {
  const body = await (await get('/api/auth/error?error=<script>alert(1)</script>')).text();
  assert.ok(!body.includes('<script>alert(1)</script>'));
  assert.match(body, /&lt;script&gt;/);
  const partial = {RP_PUBLIC_REPOSITORY:'example/framework', RP_APP_ORIGIN:'https://console.example'};
  const unset = await get('/api/auth/error', partial);
  assert.equal(unset.status, 200);
  assert.match(await unset.text(), /AUTH_SECRET/);
  assert.equal((await get('/api/auth/error', {})).status, 503, 'project metadata is still required');
});

test('auth diagnostics report presence, never values', async () => {
  const data = await (await get('/api/auth/diagnostics')).json();
  assert.equal(data.ready, true);
  assert.equal(data.origin, 'https://console.example');
  assert.equal(data.admins, 2);
  assert.deepEqual(data.env, {AUTH_SECRET: true, GITHUB_CLIENT_ID: true, GITHUB_CLIENT_SECRET: true, RP_ADMIN_IDS: true, RP_APP_ORIGIN: true});
  assert.ok(!JSON.stringify(data).includes(env.AUTH_SECRET));
});

test('scrubAuthText removes OAuth codes, states and token-shaped strings', () => {
  const text = scrubAuthText('failed ?code=abc123&state=xyz789&error_description=bad gh oops gho_secrettoken123456');
  assert.ok(!text.includes('abc123'));
  assert.ok(!text.includes('xyz789'));
  assert.ok(!text.includes('gho_secrettoken123456'));
  assert.equal(scrubAuthText('x'.repeat(900)).length, 400);
});

test('authFacts degrades to nulls when the project metadata is missing', () => {
  const facts = authFacts({});
  assert.equal(facts.origin, null);
  assert.equal(facts.admins, 0);
  assert.equal(facts.env.AUTH_SECRET, false);
});
