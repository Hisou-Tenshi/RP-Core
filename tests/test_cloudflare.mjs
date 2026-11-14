// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import worker, { normalize } from '../integrations/cloudflare/telegram.mjs';

const update = JSON.parse(await readFile(new URL('./fixtures/telegram-update.json', import.meta.url)));
const identity = { allowed_user_ids: [123], telegram: { allowed_chat_ids: [123] } };
const env = { DATA_REPOSITORY: 'example/private-data', PUBLIC_REPOSITORY: 'example/public-framework', INSTANCE_ID: 'tenshi', DATA_TOKEN: 'synthetic-data', DISPATCH_TOKEN: 'synthetic-dispatch', WEBHOOK_SECRET: 'synthetic-webhook' };
const request = () => new Request('https://example.invalid/telegram', { method: 'POST', headers: { 'X-Telegram-Bot-Api-Secret-Token': env.WEBHOOK_SECRET }, body: JSON.stringify(update) });
const file = value => new Response(JSON.stringify({ content: Buffer.from(JSON.stringify(value)).toString('base64') }), { status: 200 });

test('normalization uses stable opaque IDs and private authorization', async () => {
  const event = await normalize(update, identity);
  assert.equal(event.id, 'telegram-' + createHash('sha256').update('123:1').digest('hex').slice(0, 40));
  assert.equal(event.occurred_at, '2026-01-01T00:00:00Z');
  assert.equal(await normalize(update, { allowed_user_ids: [] }), null);
});

test('webhook authorization happens before any API call', async t => {
  const original = globalThis.fetch; t.after(() => { globalThis.fetch = original; });
  globalThis.fetch = () => { throw new Error('must not fetch'); };
  const response = await worker.fetch(new Request('https://example.invalid', { method: 'POST' }), env);
  assert.equal(response.status, 403);
});

test('private input is persisted before public dispatch with no body leakage', async t => {
  const original = globalThis.fetch; t.after(() => { globalThis.fetch = original; });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, ...options });
    if (url.includes('/contents/io.json')) return file({ schema_version: 1, driver: 'native' });
    if (url.includes('/contents/identity.json')) return file(identity);
    if (options.method === 'PUT') return new Response('{}', { status: 201 });
    if (url.endsWith('/dispatches')) return new Response(null, { status: 204 });
    return new Response('{}', { status: 404 });
  };
  assert.equal((await worker.fetch(request(), env)).status, 200);
  const saved = calls.find(c => c.method === 'PUT');
  const dispatched = calls.find(c => c.url.endsWith('/dispatches'));
  assert.ok(calls.indexOf(saved) < calls.indexOf(dispatched));
  assert.ok(Buffer.from(JSON.parse(saved.body).content, 'base64').toString().includes('synthetic message'));
  assert.ok(!dispatched.body.includes('synthetic message'));
  assert.deepEqual(Object.keys(JSON.parse(dispatched.body).client_payload).sort(), ['event_id', 'instance_id']);
});

test('failed private persistence never dispatches', async t => {
  const original = globalThis.fetch; t.after(() => { globalThis.fetch = original; });
  let dispatched = false;
  globalThis.fetch = async (url, options) => {
    if (url.includes('/contents/io.json')) return file({ schema_version: 1, driver: 'native' });
    if (url.includes('/contents/identity.json')) return file(identity);
    if (url.endsWith('/dispatches')) dispatched = true;
    return new Response('{}', { status: options.method === 'PUT' ? 500 : 404 });
  };
  assert.equal((await worker.fetch(request(), env)).status, 503);
  assert.equal(dispatched, false);
});
