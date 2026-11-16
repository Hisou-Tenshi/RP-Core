// SPDX-License-Identifier: AGPL-3.0-only
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {randomBytes} from 'node:crypto';
import {encode} from '@auth/core/jwt';
import {handle, checkMutation} from '../lib/app.mjs';
import {validateConfig} from '../lib/validation.mjs';
import {authConfig, sessionToken} from '../lib/auth.mjs';
import {requireWriter, readPublic} from '../lib/github.mjs';

const env = {RP_PUBLIC_REPOSITORY:'example/framework', RP_PUBLIC_BRANCH:'main', RP_APP_ORIGIN:'https://console.example',
  RP_ADMIN_IDS:'1', AUTH_SECRET:randomBytes(32).toString('hex'), GITHUB_CLIENT_ID:'test', GITHUB_CLIENT_SECRET:'test'};
const catalog = JSON.parse(readFileSync(new URL('../../src/rp_core/compat_runtime/model-config.json',import.meta.url)));
// Tests run both from web/ and standalone Vercel source packages.
const config = structuredClone(catalog);
const request = (path, method='GET', data, origin=env.RP_APP_ORIGIN, cookie) => new Request(env.RP_APP_ORIGIN+path,{
  method,headers:{origin,'content-type':'application/json',...(cookie?{cookie}:{})},...(data?{body:JSON.stringify(data)}:{})});
const token={accessToken:'synthetic-oauth-token',githubId:'1',login:'owner'};

test('canonical full-engine catalog passes both shape and semantic constraints',()=>assert.deepEqual(validateConfig(config),[]));
test('rejects secret injection, bad URLs, duplicate IDs, dangling routes, and empty runtime pools',()=>{
  for(const mutate of [
    c=>{c.environment.GPT_API_KEY='should-never-be-published';},
    c=>{c.environment.GPT_BASE_URL='https://example.com/v1?token=secret';},
    c=>{c.models[1].id=c.models[0].id;},
    c=>{c.dialog_tiers.max=['missing-model'];},
    c=>{c.models=c.models.filter(m=>m.provider!=='gemini'||m.tier!=='lite');},
    c=>{c.models.forEach(m=>{m.vision=false;});},
    c=>{c.summary_models=[c.models.find(m=>m.provider==='gpt').id];},
  ]) {const c=structuredClone(config);mutate(c);assert.ok(validateConfig(c).length);}
});
test('rejects config writes and dispatches without a signed session',async()=>{
  const options={env,fetcher:()=>{throw new Error('No network allowed');},getSession:async()=>null};
  for(const [path,method,data] of [['/api/config','PUT',{config,sha:'a'.repeat(40)}],['/api/probe','POST',{instance_id:'tenshi'}]]){
    const response=await handle(request(path,method,data),options);assert.equal(response.status,401);
  }
});
test('denies cross-origin mutations before doing network calls',()=>assert.throws(()=>checkMutation(request('/api/config','PUT',{},'https://evil.example'),env),{status:403}));
test('OAuth session is encrypted, authenticated and never exposes provider tokens through session JSON',async()=>{
  const cookieName='__Secure-authjs.session-token';
  const jwt=await encode({token,secret:env.AUTH_SECRET,salt:cookieName,maxAge:3600});
  const req=request('/api/session','GET',undefined,env.RP_APP_ORIGIN,`${cookieName}=${jwt}`);
  assert.equal((await sessionToken(req,env)).githubId,'1');
  const response=await handle(req,{env});const body=await response.json();
  assert.equal(body.authenticated,true);assert.ok(!JSON.stringify(body).includes(token.accessToken));
  const broken=await sessionToken(request('/api/session','GET',undefined,env.RP_APP_ORIGIN,`${cookieName}=${jwt.slice(0,20)}corrupt`),env);
  assert.equal(broken,null);
  const publicSession=await authConfig(env).callbacks.session({session:{expires:'future'},token});
  assert.ok(!JSON.stringify(publicSession).includes(token.accessToken));
});
test('unknown administrator and revoked repo permission cannot write',async()=>{
  await assert.rejects(requireWriter({...token,githubId:'2'},{env}),{status:401});
  const fetcher=async url=>Response.json(url.endsWith('/user')?{id:1,login:'owner'}:{private:false,permissions:{push:false}});
  await assert.rejects(requireWriter(token,{env,fetcher}),{status:403});
});
test('authorized save uses fixed public repo/path, current blob SHA and reports conflicts',async()=>{
  const calls=[];
  const fetcher=async(url,init)=>{
    calls.push([url,init]);
    if(url.endsWith('/user'))return Response.json({id:1,login:'owner'});
    if(url.endsWith('/repos/example/framework'))return Response.json({private:false,permissions:{push:true}});
    assert.equal(init.method,'PUT');const body=JSON.parse(init.body);
    assert.equal(body.sha,'a'.repeat(40));assert.equal(body.branch,'main');
    assert.deepEqual(JSON.parse(Buffer.from(body.content,'base64').toString()),config);
    return Response.json({content:{sha:'b'.repeat(40)},commit:{sha:'c'.repeat(40),html_url:'https://github.com/example/framework/commit/test'}});
  };
  const response=await handle(request('/api/config','PUT',{sha:'a'.repeat(40),config,repository:'attacker/repo'}),{env,fetcher,getSession:async()=>token});
  assert.equal(response.status,200);assert.equal(calls.length,3);assert.match(calls[2][0],/example\/framework\/contents\/src\/rp_core\/compat_runtime\/model-config.json$/);
  const conflict=async(url,init)=>init.method==='PUT'?new Response('{}',{status:409}):fetcher(url,init);
  const rejected=await handle(request('/api/config','PUT',{sha:'a'.repeat(40),config}),{env,fetcher:conflict,getSession:async()=>token});
  assert.equal(rejected.status,409);
});
test('invalid config never reaches GitHub and unauthenticated reads do not require a server PAT',async()=>{
  let calls=0;const fetcher=async()=>{calls++;return Response.json(config);};
  const bad=await handle(request('/api/config','PUT',{sha:'a'.repeat(40),config:{bad:true}}),{env,fetcher,getSession:async()=>token});
  assert.equal(bad.status,400);assert.equal(calls,0);
  const read=await handle(request('/api/config'),{env,fetcher});const doc=await read.json();
  assert.equal(read.status,200);assert.match(doc.sha,/^[a-f0-9]{40}$/);assert.equal(calls,1);
});

test('a refresh after a repository write does not reuse a stale raw-content cache entry',async()=>{
  const cache=new Map();let originRevision=0;
  const fetcher=async url=>{
    if(!cache.has(url))cache.set(url,{revision:++originRevision});
    return Response.json(cache.get(url));
  };
  const first=await readPublic('config/instances.json',{env,fetcher});
  const afterWrite=await readPublic('config/instances.json',{env,fetcher});
  assert.equal(first.data.revision,1);
  assert.equal(afterWrite.data.revision,2);
  assert.notEqual(first.sha,afterWrite.sha);
});

test('health endpoint publishes only the allowlisted call-count artifact',async()=>{
  const calls=[];
  const fetcher=async url=>{
    calls.push(url);
    const response=url.includes('/config/instances.json')
      ? {instances:[{id:'tenshi',environment:'tenshi'}]}
      : url.includes('/health/tenshi/calls.json?')
        ? {schema:1,sample_size:100,providers:{gemini:{proxy3:{calls:7,success:6,failure:1}}}}
        : url.includes('/health/tenshi/latest.json?')
          ? {instance_id:'tenshi',providers:{}}
          : [];
    return new Response(JSON.stringify(response),{status:200});
  };
  const response=await handle(request('/api/health?instance=tenshi'),{env,fetcher});
  const body=await response.json();
  assert.equal(response.status,200);
  assert.equal(body.calls.sample_size,100);
  assert.deepEqual(body.calls.providers.gemini.proxy3,{calls:7,success:6,failure:1});
  assert.ok(calls.some(url=>url.includes('/health/tenshi/calls.json?')));
});
