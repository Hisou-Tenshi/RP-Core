// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { buildBatchPayload } from '../integrations/cloudflare/full.mjs';
import { enqueueLegacy } from '../integrations/cloudflare/private-queue.mjs';

const env = { RP_LIVE_ENABLED: 'true', INSTANCE_ID: 'tenshi', DATA_REPOSITORY: 'example/private', PUBLIC_REPOSITORY: 'example/public', DATA_TOKEN: 'synthetic-data', DISPATCH_TOKEN: 'synthetic-dispatch', TELEGRAM_WEBHOOK_SECRET: 'synthetic-webhook', PERCEPTION_SCHEDULE_SYNC_SECRET: 'synthetic-schedule' };
const identity = { allowed_user_ids: [1], telegram: { allowed_chat_ids: [1] } };
const file = value => new Response(JSON.stringify({ content: Buffer.from(JSON.stringify(value)).toString('base64') }));
const privatePayload = calls => JSON.parse(Buffer.from(JSON.parse(calls.find(call => call.options.method === 'PUT').options.body).content, 'base64').toString());

function stub(t, { failSave = false } = {}) {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    if (url.includes('/contents/io.json')) return file({schema_version: 1, driver: 'tenshi-v1'});
    if (url.includes('/contents/identity.json')) return file(identity);
    if (options.method === 'PUT') return new Response('{}', {status: failSave ? 500 : 201});
    if (url.endsWith('/dispatches')) return new Response(null, {status: 204});
    return new Response('{}', {status: 404});
  };
  return calls;
}

class KV {
  constructor() { this.map = new Map(); }
  async get(key) { return this.map.get(key) || null; }
  async put(key, value) { this.map.set(key, value); }
  async delete(key) { this.map.delete(key); }
  async list({prefix}) { return {keys: [...this.map.keys()].filter(key => key.startsWith(prefix)).map(name => ({name})), list_complete: true}; }
}

const message = (id, extra = {}) => ({message: {message_id: id, date: 1794614400, from: {id: 1}, chat: {id: 1}, ...extra}});
const request = update => new Request('https://example.invalid/webhook', {method:'POST', headers:{'X-Telegram-Bot-Api-Secret-Token': env.TELEGRAM_WEBHOOK_SECRET}, body:JSON.stringify(update)});

test('full worker defaults off and rejects forged updates before data access', async t => {
  const calls = stub(t);
  assert.equal((await worker.fetch(request(message(1, {text:'synthetic'})), {...env, RP_LIVE_ENABLED:''}, {})).status, 503);
  assert.equal((await worker.fetch(new Request('https://example.invalid', {method:'POST'}), env, {})).status, 403);
  assert.equal(calls.length, 0);
});

test('full ingress preserves voice and quotes only in private storage', async t => {
  const calls = stub(t);
  const update = message(1, {voice:{file_id:'synthetic-voice'}, quote:{text:'synthetic-private-quote'}});
  assert.equal((await worker.fetch(request(update), env, {})).status, 200);
  const saved = privatePayload(calls);
  assert.equal(saved.legacy_payload.file_type, 'voice');
  assert.equal(saved.legacy_payload.reply_to.quote_text, 'synthetic-private-quote');
  const dispatched = JSON.parse(calls.find(call => call.url.endsWith('/dispatches')).options.body);
  assert.deepEqual(Object.keys(dispatched.client_payload).sort(), ['event_id','instance_id']);
  assert.ok(!JSON.stringify(dispatched).includes('synthetic-private-quote'));
});

test('full ingress cannot dispatch before private persistence succeeds', async t => {
  const calls = stub(t, {failSave:true});
  await assert.rejects(enqueueLegacy(env, 'reply', {user_id:1, chat_id:1, text:'synthetic'}));
  assert.ok(!calls.some(call => call.url.endsWith('/dispatches')));
});

test('album batches preserve attachment list, ordering, caption, location and quote', () => {
  const merged = buildBatchPayload([{chat_id:1,user_id:1,msg_id:2,date:20,file_id:'b',file_type:'photo',text:'second',reply_to:{quote_text:'quote'}}, {chat_id:1,user_id:1,msg_id:1,date:10,file_id:'a',file_type:'photo',text:'first',location:{latitude:1,longitude:2}}]);
  assert.equal(merged.text, 'first' + String.fromCharCode(10,10) + 'second');
  assert.deepEqual(merged.file_ids.map(file => file.file_id), ['a','b']);
  assert.equal(merged.reply_to.quote_text, 'quote');
  assert.equal(merged.location.latitude,1);
});

test('KV album debounce dispatches one complete private batch', async t => {
  const calls = stub(t);
  const pending = [];
  const context = {waitUntil(promise) {pending.push(promise);}};
  const configured = {...env, KV:new KV(), MSG_BATCH_FIRST_WAIT_MS:15, MSG_BATCH_QUIET_MS:20, MSG_BATCH_MAX_WAIT_MS:100, MSG_BATCH_POLL_MS:5};
  await worker.fetch(request(message(1, {photo:[{file_id:'a'}]})), configured, context);
  await worker.fetch(request(message(2, {photo:[{file_id:'b'}]})), configured, context);
  await Promise.all(pending);
  assert.equal(calls.filter(call => call.url.endsWith('/dispatches')).length, 1);
  assert.deepEqual(privatePayload(calls).legacy_payload.file_ids.map(file => file.file_id), ['a','b']);
});

test('scheduled perception bypasses random cooldown and keeps notes private', async t => {
  const calls = stub(t);
  const kv = new KV();
  await kv.put('last_github_dispatch_ms', String(Date.now()));
  await kv.put('perception_schedule', JSON.stringify({entries:[{id:'synthetic-schedule-id', trigger_at_ms:Date.now(), note:'synthetic private schedule note'}]}));
  const pending = [];
  await worker.scheduled({}, {...env, KV:kv}, {waitUntil(promise) {pending.push(promise);}});
  await Promise.all(pending);
  const saved = privatePayload(calls);
  assert.equal(saved.full_task, 'perception');
  assert.equal(saved.legacy_payload.scheduled_force,true);
  assert.equal(saved.legacy_payload.note,'synthetic private schedule note');
  assert.ok(!calls.find(call => call.url.endsWith('/dispatches')).options.body.includes('private schedule note'));
});
