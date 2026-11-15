// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { buildBatchPayload } from '../integrations/cloudflare/full.mjs';
import { enqueueLegacy } from '../integrations/cloudflare/private-queue.mjs';

const env = { RP_LIVE_ENABLED: 'true', INSTANCE_ID: 'tenshi', DATA_REPOSITORY: 'example/private', PUBLIC_REPOSITORY: 'example/public', DATA_TOKEN: 'synthetic-data', DISPATCH_TOKEN: 'synthetic-dispatch', TELEGRAM_WEBHOOK_SECRET: 'synthetic-webhook', PERCEPTION_SCHEDULE_SYNC_SECRET: 'synthetic-schedule' };
const identity = { allowed_user_ids: [1], telegram: { allowed_chat_ids: [1] } };
const file = value => new Response(JSON.stringify({ content: Buffer.from(JSON.stringify(value)).toString('base64') }));
const privatePayload = calls => JSON.parse(Buffer.from(JSON.parse(calls.find(call => call.options.method === 'PUT').options.body).content, 'base64').toString());

function stub(t, { failSave = false, failDispatch = false, configuredIdentity = identity } = {}) {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    if (url.includes('/contents/io.json')) return file({schema_version: 1, driver: 'tenshi-v1'});
    if (url.includes('/contents/identity.json')) return file(configuredIdentity);
    if (options.method === 'PUT') return new Response('{}', {status: failSave ? 500 : 201});
    if (url.endsWith('/dispatches')) return new Response(null, {status: failDispatch ? 503 : 204});
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

test('paused Worker exposes safe health and authenticates control routes', async t => {
  const calls = stub(t);
  const paused = {...env, RP_LIVE_ENABLED:'false'};
  const health = await worker.fetch(new Request('https://example.invalid/health'), paused, {});
  assert.deepEqual(await health.json(), {service:'rp-core',protocol:'rp/1',enabled:false});
  assert.equal((await worker.fetch(new Request('https://example.invalid/internal/probe', {method:'POST',body:'{}'}), paused, {})).status,403);
  assert.equal(calls.length,0);
});

test('Calling binding aliases queue a non-chat probe without changing credentials', async t => {
  const calls = stub(t);
  const legacy = {RP_LIVE_ENABLED:'false', INSTANCE_ID:'tenshi', PUBLIC_REPOSITORY:env.PUBLIC_REPOSITORY, GH_REPO:env.DATA_REPOSITORY, GH_TOKEN:env.DATA_TOKEN, CALLING_GH_TOKEN:env.DISPATCH_TOKEN, SCHEDULE_SYNC_SECRET:'synthetic-schedule'};
  const response = await worker.fetch(new Request('https://example.invalid/internal/probe',{method:'POST',headers:{Authorization:'Bearer synthetic-schedule'},body:JSON.stringify({nonce:'cutover-offline-test'})}),legacy,{});
  assert.equal(response.status,202);
  assert.equal(privatePayload(calls).full_task,'probe');
  assert.ok(!JSON.stringify(privatePayload(calls)).includes('synthetic-schedule'));
  assert.ok(calls.find(call=>call.url.endsWith('/dispatches')).options.headers.Authorization.endsWith(env.DISPATCH_TOKEN));
});

test('handoff drains acknowledged legacy KV messages into the private inbox', async t => {
  const calls=stub(t);
  const kv=new KV();
  const stamp=Date.now()-1000;
  await kv.put('msg_'+stamp+'_000000000009_synthetic',JSON.stringify({user_id:1,chat_id:1,msg_id:9,date:1,text:'synthetic pending input'}));
  const request=()=>new Request('https://example.invalid/internal/drain-inbox',{method:'POST',headers:{Authorization:'Bearer synthetic-schedule'},body:'{}'});
  const configured={...env,RP_LIVE_ENABLED:'false',KV:kv};
  const first=await worker.fetch(request(),configured,{});
  assert.equal((await first.json()).queued,1);
  assert.equal(privatePayload(calls).legacy_payload.text,'synthetic pending input');
  const second=await worker.fetch(request(),configured,{});
  assert.equal((await second.json()).queued,0);
  assert.equal(calls.filter(call=>call.url.endsWith('/dispatches')).length,1);
});

test('dashboard single-file bundle has the same paused controls', async t => {
  const {default: bundled} = await import('../integrations/cloudflare/dashboard-worker.mjs');
  stub(t);
  const health=await bundled.fetch(new Request('https://example.invalid/health'),{RP_LIVE_ENABLED:'false'},{});
  assert.deepEqual(await health.json(),{service:'rp-core',protocol:'rp/1',enabled:false});
  const response=await bundled.fetch(new Request('https://example.invalid/internal/probe',{method:'POST',headers:{Authorization:'Bearer synthetic-schedule'},body:'{"nonce":"bundled-cutover"}'}),{...env,RP_LIVE_ENABLED:'false'},{});
  assert.equal(response.status,202);
});

test('legacy credential fallback always dispatches to the new public repository', async t => {
  const original=globalThis.fetch; t.after(()=>{globalThis.fetch=original;});
  const dispatches=[];
  globalThis.fetch=async(url,options)=>{
    if(url.includes('/contents/io.json'))return file({schema_version:1,driver:'tenshi-v1'});
    if(url.includes('/contents/identity.json'))return file(identity);
    if(options.method==='PUT')return new Response('{}',{status:201});
    if(url.endsWith('/dispatches')){
      dispatches.push({url,authorization:options.headers.Authorization});
      return new Response(null,{status:dispatches.length===1?403:204});
    }
    return new Response('{}',{status:404});
  };
  await enqueueLegacy({...env,GH_TOKEN:'synthetic-fallback',CALLING_REPO:'old/never-use'},'probe',{probe_nonce:'fallback-test'});
  assert.equal(dispatches.length,2);
  assert.ok(dispatches.every(call=>call.url.includes('/example/public/')));
  assert.ok(dispatches[1].authorization.endsWith('synthetic-fallback'));
});

test('scheduled event identity stays stable across cron minutes', async t => {
  const calls=stub(t);
  const original=Date.now;
  let now=original();
  Date.now=()=>now;
  t.after(()=>{Date.now=original;});
  const payload={scheduled_force:true,trigger_at_ms:1234567890000,schedule_id:'synthetic-schedule',note:'synthetic'};
  const first=await enqueueLegacy(env,'perception',payload);
  now+=120000;
  const second=await enqueueLegacy(env,'perception',payload);
  assert.equal(first,second);
  assert.equal(calls.filter(call=>call.url.endsWith('/dispatches')).length,2);
});

test('handoff preserves due schedules without touching future entries', async t => {
  const calls=stub(t);
  const kv=new KV();
  const future=Date.now()+3600000;
  await kv.put('perception_schedule',JSON.stringify({entries:[{id:'due',trigger_at_ms:Date.now()-60000,note:'synthetic due note'},{id:'future',trigger_at_ms:future,note:'synthetic future note'}]}));
  const response=await worker.fetch(new Request('https://example.invalid/internal/drain-inbox',{method:'POST',headers:{Authorization:'Bearer synthetic-schedule'},body:'{}'}),{...env,KV:kv,RP_LIVE_ENABLED:'false'},{});
  const result=await response.json();
  assert.equal(result.scheduled_queued,1);
  assert.equal(privatePayload(calls).full_task,'perception');
  assert.equal(privatePayload(calls).legacy_payload.schedule_id,'due');
  assert.deepEqual(JSON.parse(await kv.get('perception_schedule')).entries.map(entry=>entry.id),['due','future']);
  const retry=await worker.fetch(new Request('https://example.invalid/internal/drain-inbox',{method:'POST',headers:{Authorization:'Bearer synthetic-schedule'},body:'{}'}),{...env,KV:kv,RP_LIVE_ENABLED:'false'},{});
  assert.equal((await retry.json()).scheduled_queued,0);
});

for (const failure of ['failSave', 'failDispatch']) {
  test('scheduled failure keeps its retry source: ' + failure, async t => {
    stub(t, {[failure]:true});
    const kv=new KV();
    const raw=JSON.stringify({entries:[{id:'due',trigger_at_ms:Date.now(),note:'synthetic'}]});
    await kv.put('perception_schedule',raw);
    await assert.rejects(worker.scheduled({}, {...env,KV:kv}, {waitUntil(){}}));
    assert.equal(await kv.get('perception_schedule'),raw);
    assert.equal([...kv.map.keys()].filter(key=>key.startsWith('perception_schedule_ack_')).length,0);
  });
}

test('schedule consumption cannot overwrite concurrent schedule sync', async t => {
  const original=globalThis.fetch; t.after(()=>{globalThis.fetch=original;});
  const kv=new KV();
  await kv.put('perception_schedule',JSON.stringify({entries:[{id:'due',trigger_at_ms:Date.now(),note:'synthetic'}]}));
  const replacement=JSON.stringify({entries:[{id:'new',trigger_at_ms:Date.now()+3600000,note:'new schedule'}]});
  globalThis.fetch=async(url,options)=>{
    if(url.includes('/contents/io.json'))return file({schema_version:1,driver:'tenshi-v1'});
    if(url.includes('/contents/identity.json'))return file(identity);
    if(options.method==='PUT'){
      await kv.put('perception_schedule',replacement);
      return new Response('{}',{status:201});
    }
    if(url.endsWith('/dispatches'))return new Response(null,{status:204});
    return new Response('{}',{status:404});
  };
  await worker.scheduled({}, {...env,KV:kv}, {waitUntil(){}});
  assert.equal(await kv.get('perception_schedule'),replacement);
});

test('full ingress rejects unsupported multi-owner identity before enqueue', async t => {
  const calls=stub(t,{configuredIdentity:{allowed_user_ids:[1,2]}});
  const response=await worker.fetch(request(message(1,{text:'synthetic'})),env,{});
  assert.equal(response.status,503);
  assert.ok(!calls.some(call=>call.options.method==='PUT'||call.url.endsWith('/dispatches')));
});
