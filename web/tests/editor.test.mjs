// SPDX-License-Identifier: AGPL-3.0-only
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import {validateConfig} from '../lib/validation.mjs';

const html = readFileSync(new URL('../public/index.html',import.meta.url),'utf8');
const source = readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
const config = JSON.parse(readFileSync(new URL('../../src/rp_core/compat_runtime/model-config.json',import.meta.url),'utf8'));
const schema = JSON.parse(readFileSync(new URL('../lib/model-config.schema.json',import.meta.url),'utf8'));

async function page({catalog=config,healthResult={available:false,latest:null,history:[],calls:null}}={}) {
  const dom=new JSDOM(html,{url:'https://console.example/#models',runScripts:'outside-only'});
  dom.window.structuredClone=structuredClone;
  const writes=[],reads=[];
  dom.window.fetch=async(path,options)=>{
    const originalPath=path;
    path=new URL(path,'https://console.example').pathname;
    if(!options.method || options.method==='GET')reads.push(originalPath);
    if(path==='/api/session')return Response.json({authenticated:true,login:'owner',repository:'example/framework',branch:'main',oauth_ready:true});
    if(path==='/api/instances')return Response.json({instances:[{id:'example'}]});
    if(path.startsWith('/api/health'))return Response.json(healthResult);
    if(path==='/api/config'&&options.method==='PUT'){
      writes.push(JSON.parse(options.body));
      return Response.json({saved:true,sha:'b'.repeat(40),config_hash:'d'.repeat(64),commit:'c'.repeat(40),url:'https://github.com/example/framework/commit/test'});
    }
    if(path==='/api/config')return Response.json({config:catalog,sha:'a'.repeat(40),config_hash:'d'.repeat(64),schema,repository:'example/framework',branch:'main'});
    throw new Error('Unexpected request: '+path);
  };
  // Run our own application, with no external scripts or network access.
  await dom.window.eval(`(async()=>{${source}\n})()`);
  return {dom,writes,reads};
}

test('editing a visible note enables save immediately and persists exactly the edited value',async()=>{
  const {dom,writes}=await page();
  try {
    const {document,Event}=dom.window;
    const note=document.querySelector('[aria-label="模型备注 1"]');
    const button=document.querySelector('#save');
    assert.equal(button.disabled,true);
    note.value='Synthetic console save test';
    note.dispatchEvent(new Event('input',{bubbles:true}));
    assert.equal(button.disabled,false);
    assert.match(document.querySelector('#dirty-label').textContent,/未保存/);
    await button.onclick();
    assert.equal(writes.length,1);
    assert.equal(writes[0].config.models[0].note,'Synthetic console save test');
    assert.equal(writes[0].sha,'a'.repeat(40));
    assert.equal(button.disabled,true);
    assert.match(document.querySelector('#notification').textContent,/已保存配置/);
  } finally {dom.window.close();}
});

test('request settings and embedding edits are included without requiring a blur event',async()=>{
  const {dom,writes}=await page();
  try {
    const {document,Event}=dom.window;
    const embedding=document.querySelector('#embedding-max');
    embedding.value='8500';embedding.dispatchEvent(new Event('input',{bubbles:true}));
    const reasoning=document.querySelector('[data-env="GPT_REASONING_EFFORT"]');
    reasoning.value='high';reasoning.dispatchEvent(new Event('change',{bubbles:true}));
    assert.equal(document.querySelector('#save').disabled,false);
    await document.querySelector('#save').onclick();
    assert.equal(writes[0].config.embedding.max_input_chars,8500);
    assert.equal(writes[0].config.environment.GPT_REASONING_EFFORT,'high');
  } finally {dom.window.close();}
});

test('page refreshes use fresh data URLs while keeping authenticated writes unchanged',async()=>{
  const {dom,reads,writes}=await page();
  try {
    await dom.window.document.querySelector('#reload').onclick();
    const configReads=reads.filter(path=>new URL(path,'https://console.example').pathname==='/api/config');
    assert.equal(configReads.length,2);
    assert.notEqual(configReads[0],configReads[1]);
    assert.ok(configReads.every(path=>new URL(path,'https://console.example').searchParams.has('_rp_read')));
    assert.equal(writes.length,0);
  } finally {dom.window.close();}
});

test('all model route selectors start blank with a left label and disabled add button',async()=>{
  const {dom,writes}=await page();
  try {
    const {document}=dom.window;
    const selectors=[...document.querySelectorAll('[data-chain-select]')];
    assert.equal(selectors.length,6);
    for(const select of selectors){
      assert.equal(select.value,'');
      assert.match(select.options[0].textContent,/请选择/);
      assert.equal(select.previousElementSibling.tagName,'LABEL');
      assert.equal(select.previousElementSibling.textContent,'添加模型');
      assert.equal(select.previousElementSibling.htmlFor,select.id);
      const button=[...document.querySelectorAll('[data-add-chain]')].find(b=>b.dataset.addChain===select.dataset.chainSelect);
      assert.equal(button.disabled,true);
      button.onclick();
    }
    assert.equal(document.querySelector('#save').disabled,true);
    assert.equal(writes.length,0);
  } finally {dom.window.close();}
});

test('selecting is not an addition; explicit add inserts once and resets to blank',async()=>{
  const {dom,writes}=await page();
  try {
    const {document,Event}=dom.window;
    const select=document.querySelector('[data-chain-select="low_tier_pool"]');
    const button=document.querySelector('[data-add-chain="low_tier_pool"]');
    const selected=select.options[1].value;
    select.value=selected;
    select.dispatchEvent(new Event('change',{bubbles:true}));
    assert.equal(button.disabled,false);
    assert.equal(document.querySelector('#save').disabled,true);
    button.onclick();
    assert.equal(document.querySelector('[data-chain-select="low_tier_pool"]').value,'');
    assert.equal(document.querySelector('[data-add-chain="low_tier_pool"]').disabled,true);
    await document.querySelector('#save').onclick();
    assert.deepEqual(writes[0].config.low_tier_pool,[...config.low_tier_pool,selected]);
    assert.equal(writes[0].config.low_tier_pool.filter(id=>id===selected).length,1);
    assert.deepEqual(validateConfig(writes[0].config),[]);
  } finally {dom.window.close();}
});

test('renaming a model preserves route references through intermediate edits and saves a valid catalog',async()=>{
  const catalog=structuredClone(config);
  const index=catalog.models.findIndex(m=>m.provider==='gemini'&&m.tier==='pro');
  const oldId=catalog.models[index].id;
  catalog.dialog_tiers.high=[oldId];
  const {dom,writes}=await page({catalog});
  try {
    const {document,Event}=dom.window;
    const field=document.querySelector(`[aria-label="模型 ID ${index+1}"]`);
    for(const id of ['gemini-workflow-intermediate-fixture','gemini-workflow-renamed-fixture']){
      field.value=id;field.dispatchEvent(new Event('input',{bubbles:true}));
    }
    const slug=document.querySelector(`[aria-label="OpenRouter slug ${index+1}"]`);
    slug.value='google/gemini-workflow-renamed-fixture';slug.dispatchEvent(new Event('input',{bubbles:true}));
    await document.querySelector('#save').onclick();
    const saved=writes[0].config;
    assert.equal(saved.models[index].id,'gemini-workflow-renamed-fixture');
    assert.deepEqual(saved.dialog_tiers.high,['gemini-workflow-renamed-fixture']);
    assert.ok(![...Object.values(saved.dialog_tiers).flat(),...saved.low_tier_pool,...saved.summary_models].includes(oldId));
    assert.deepEqual(validateConfig(saved),[]);
  } finally {dom.window.close();}
});

test('all current routes start checked, with named sites and an explicit enabled label',async()=>{
  const {dom}=await page();
  try{
    const {document}=dom.window;
    const routes=[...document.querySelectorAll('[data-route-enabled]')];
    assert.ok(routes.length>20);
    assert.ok(routes.every(route=>route.checked));
    assert.ok(routes.every(route=>route.closest('label').textContent==='启用'));
    const policies=document.querySelector('.path-policy-grid').textContent;
    assert.match(policies,/Codeyu/);
    assert.match(policies,/CoderPlan/);
    assert.match(policies,/To-Tokens/);
    assert.doesNotMatch(policies,/Proxy|proxy[123]/);
    assert.equal(document.querySelector('#save').disabled,true);
  }finally{dom.window.close();}
});

test('per-vendor routes save named priorities and unchecking disables a route',async()=>{
  const {dom,writes}=await page();
  try{
    const {document,Event}=dom.window;
    const before=[...config.path_policies.gemini.order];
    document.querySelector('[data-route-move][data-provider="gemini"][data-index="0"][data-direction="1"]').click();
    assert.deepEqual(config.path_policies.gemini.order,before);
    assert.deepEqual(document.querySelector('#save').disabled,false);
    const official=document.querySelector('[data-provider="gemini"][data-route-enabled="official"]');
    official.checked=false;official.dispatchEvent(new Event('change',{bubbles:true}));
    await document.querySelector('#save').onclick();
    assert.deepEqual(writes[0].config.path_policies.gemini.order.slice(0,2),['codeyu','totokens']);
    assert.deepEqual(writes[0].config.path_policies.gemini.disabled,['official']);
    assert.deepEqual(validateConfig(writes[0].config),[]);
  }finally{dom.window.close();}
});

test('a saved disabled route starts unchecked and checking it enables it again',async()=>{
  const catalog=structuredClone(config);
  catalog.path_policies.gemini.disabled=['codeyu'];
  const {dom,writes}=await page({catalog});
  try{
    const {document,Event}=dom.window;
    const route=document.querySelector('[data-provider="gemini"][data-route-enabled="codeyu"]');
    assert.equal(route.checked,false);
    assert.ok(route.closest('.chain-item').classList.contains('route-disabled'));
    route.checked=true;route.dispatchEvent(new Event('change',{bubbles:true}));
    await document.querySelector('#save').onclick();
    assert.deepEqual(writes[0].config.path_policies.gemini.disabled,[]);
    assert.ok([...document.querySelectorAll('[data-route-enabled]')].every(input=>input.checked));
  }finally{dom.window.close();}
});

test('partial route priorities still show all routes and can move an appended route',async()=>{
  const catalog=structuredClone(config);
  catalog.path_policies.gpt.order=['official'];
  const {dom,writes}=await page({catalog});
  try{
    const {document}=dom.window;
    const routes=[...document.querySelectorAll('[data-provider="gpt"][data-route-enabled]')];
    assert.equal(routes.length,5);
    assert.equal(routes[0].dataset.routeEnabled,'official');
    document.querySelector('[data-route-move][data-provider="gpt"][data-index="4"][data-direction="-1"]').click();
    await document.querySelector('#save').onclick();
    assert.deepEqual(writes[0].config.path_policies.gpt.order,['official','totokens','codeyu','openrouter','coderplan']);
    assert.deepEqual(validateConfig(writes[0].config),[]);
  }finally{dom.window.close();}
});

test('health labels and history bridge old numbered names and new site names',async()=>{
  const ok={ok:true,verdict:'ok',severity:'green',latency_ms:10,models:{}};
  const timestamp='2026-11-16T00:00:00Z';
  for(const [oldName,newName,provider] of [
    ['Proxy 1 Base','Codeyu Base','claude'],['Gemini Proxy 3','To-Tokens','gemini'],
    ['GPT Proxy','Codeyu','gpt'],['Grok Proxy 2','To-Tokens','grok'],['Grok Proxy','CoderPlan','grok'],
  ]){
    for(const [latestName,historyName] of [[oldName,newName],[newName,oldName]]){
      const latest={instance_id:'example',generated_at_utc:timestamp,run_id:'1',providers:{[provider]:{paths:{[latestName]:ok}}}};
      const history=[{generated_at_utc:timestamp,providers:{[provider]:{paths:{[historyName]:ok}}}}];
      const {dom}=await page({healthResult:{available:true,latest,history,calls:null}});
      try{
        const {document}=dom.window;
        assert.equal(document.querySelector('.path-name').textContent,newName);
        assert.ok(document.querySelector('.history-cell').classList.contains('green'));
        assert.doesNotMatch(document.querySelector('.provider-card').textContent,/Proxy/);
      }finally{dom.window.close();}
    }
  }
});

test('health panel renders sanitized near-100 provider route counts',async()=>{
  const callStats={generated_at_utc:'2026-11-16T00:00:00Z',sample_size:100,providers:{gemini:{proxy3:{calls:31,success:29,failure:2},official:{calls:9,success:9,failure:0}}}};
  const result=await page({healthResult:{available:true,latest:{instance_id:'example',generated_at_utc:'2026-11-16T00:00:00Z',run_id:'1',config_sha256:'d'.repeat(64),providers:{}},history:[],calls:callStats}});
  try{
    const {document}=result.dom.window;
    document.querySelector('[data-tab="health"]').click();
    assert.match(document.querySelector('.call-stats').textContent,/最近 100 次通路调用/);
    assert.match(document.querySelector('.call-stats').textContent,/To-Tokens/);
    assert.match(document.querySelector('.call-stats').textContent,/31/);
    assert.match(document.querySelector('.call-stats').textContent,/2/);
  }finally{result.dom.window.close();}
});
