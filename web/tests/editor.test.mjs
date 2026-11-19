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
    const select=document.querySelector('[data-chain-select="tier:minimal"]');
    const button=document.querySelector('[data-add-chain="tier:minimal"]');
    const selected=[...select.options].find(option=>option.value)?.value;
    assert.ok(selected);
    select.value=selected;
    select.dispatchEvent(new Event('change',{bubbles:true}));
    assert.equal(button.disabled,false);
    assert.equal(document.querySelector('#save').disabled,true);
    button.onclick();
    assert.equal(document.querySelector('[data-chain-select="tier:minimal"]').value,'');
    assert.equal(document.querySelector('[data-add-chain="tier:minimal"]').disabled,true);
    await document.querySelector('#save').onclick();
    assert.deepEqual(writes[0].config.dialog_tiers.minimal,[...config.dialog_tiers.minimal,selected]);
    assert.equal(writes[0].config.dialog_tiers.minimal.filter(id=>id===selected).length,1);
    assert.deepEqual(validateConfig(writes[0].config),[]);
  } finally {dom.window.close();}
});

test('renaming a model preserves route references through intermediate edits and saves a valid catalog',async()=>{
  const catalog=structuredClone(config);
  const index=catalog.models.findIndex(m=>m.provider==='gemini'&&m.tier==='max');
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

test('current routes start checked unless saved as disabled, with named sites and an explicit enabled label',async()=>{
  const {dom}=await page();
  try{
    const {document}=dom.window;
    const routes=[...document.querySelectorAll('[data-route-enabled]')];
    assert.ok(routes.length>20);
    // The shared config may legitimately keep a route disabled (saved via the console), so the
    // rendered state must match path_policies[provider].disabled exactly: enabled by default,
    // unchecked only when a saved policy disables that route.
    const expected=route=>!(config.path_policies[route.dataset.provider].disabled||[]).includes(route.dataset.routeEnabled);
    assert.ok(routes.every(route=>route.checked===expected(route)));
    assert.ok(routes.every(route=>expected(route)===!route.closest('.chain-item').classList.contains('route-disabled')));
    assert.ok(routes.every(route=>route.closest('label').textContent==='启用'));
    const policies=document.querySelector('.path-policy-grid').textContent;
    assert.match(policies,/Codeyu/);
    assert.match(policies,/CoderPlan/);
    assert.match(policies,/To-Tokens/);
    assert.match(policies,/77/);
    assert.match(policies,/Dragon/);
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
    assert.deepEqual(writes[0].config.path_policies.gemini.order.slice(0,2),['totokens','dragon']);
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
    // Checking the saved-disabled route re-enables every route of that provider; a route that
    // another provider's saved policy keeps disabled (deepseek fireworks_pro in the live config)
    // must stay unchecked rather than being forcibly re-enabled.
    assert.ok([...document.querySelectorAll('[data-provider="gemini"][data-route-enabled]')].every(input=>input.checked));
    assert.deepEqual(writes[0].config.path_policies.deepseek.disabled,config.path_policies.deepseek.disabled);
  }finally{dom.window.close();}
});

test('partial route priorities still show all routes and can move an appended route',async()=>{
  const catalog=structuredClone(config);
  catalog.path_policies.gpt.order=['official'];
  const {dom,writes}=await page({catalog});
  try{
    const {document}=dom.window;
    const routes=[...document.querySelectorAll('[data-provider="gpt"][data-route-enabled]')];
    assert.equal(routes.length,7);
    assert.equal(routes[0].dataset.routeEnabled,'official');
    document.querySelector('[data-route-move][data-provider="gpt"][data-index="4"][data-direction="-1"]').click();
    await document.querySelector('#save').onclick();
    assert.deepEqual(writes[0].config.path_policies.gpt.order,['official','totokens','codeyu','dragon','77','coderplan','openrouter']);
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

test('health panel orders the shared call window by configured provider and route rather than counts',async()=>{
  const catalog=structuredClone(config);
  [catalog.providers[0],catalog.providers[1]]=[catalog.providers[1],catalog.providers[0]];
  catalog.path_policies.gemini.order=['official',...catalog.path_policies.gemini.order.filter(route=>route!=='official')];
  const callStats={generated_at_utc:'2026-11-16T00:00:00Z',window:500,sample_size:500,providers:{claude:{official:{calls:9,success:9,failure:0}},gemini:{proxy3:{calls:460,success:458,failure:2},official:{calls:31,success:31,failure:0}}}};
  const result=await page({catalog,healthResult:{available:true,latest:{instance_id:'example',generated_at_utc:'2026-11-16T00:00:00Z',run_id:'1',config_sha256:'d'.repeat(64),providers:{}},history:[],calls:callStats}});
  try{
    const {document}=result.dom.window;
    document.querySelector('[data-tab="health"]').click();
    assert.match(document.querySelector('.call-stats h2').textContent,/最近 500 次通路调用 · 共 500 次/);
    assert.deepEqual([...document.querySelectorAll('.call-stats tbody tr')].map(row=>[...row.cells].map(cell=>cell.textContent)),[
      ['Gemini','Official','31','31','0'],['Gemini','To-Tokens','460','458','2'],['Claude','Official','9','9','0'],
    ]);
  }finally{result.dom.window.close();}
});

test('health panel does not label an old per-provider snapshot as a shared window',async()=>{
  const timestamp='2026-11-16T00:00:00Z';
  const latest={instance_id:'example',generated_at_utc:timestamp,run_id:'1',providers:{}};
  const calls={generated_at_utc:timestamp,window_per_provider:100,sample_size:0,providers:{}};
  const {dom}=await page({healthResult:{available:true,latest,history:[],calls}});
  try{
    assert.match(dom.window.document.querySelector('.call-stats h2').textContent,/旧统计口径/);
  }finally{dom.window.close();}
});

test('health marks partial availability in the badge and history without recoloring healthy checks',async()=>{
  const latest={instance_id:'example',generated_at_utc:'2026-11-16T00:00:00Z',run_id:'1',providers:{claude:{models_ok:1,models_total:2,enabled:true,paths:{'Proxy 3':{ok:true,verdict:'ok',severity:'green',latency_ms:10,models:{'claude-opus-5-5':{ok:true,verdict:'ok'},'claude-haiku-5-5':{ok:false,verdict:'auth_denied'}}}}}}};
  const sample=(timestamp,run_id,status)=>({generated_at_utc:timestamp,run_id,providers:{claude:{paths:{'To-Tokens':{ok:true,verdict:'ok',...status}}}}});
  const history=[
    sample('2026-11-15T06:00:00Z','10',{models_ok:2,models_total:2}),
    sample('2026-11-15T12:00:00Z','11',{models_ok:1,models_total:2}),
    sample('2026-11-15T18:00:00Z','12',{severity:'warn',verdict:'quota_exhausted'}),
    sample(latest.generated_at_utc,latest.run_id,{}),
  ];
  const {dom}=await page({healthResult:{available:true,latest,history,calls:null}});
  try{
    const {document}=dom.window;
    const pill=document.querySelector('.provider-card .pill');
    assert.match(pill.className,/warn/);
    assert.equal(pill.textContent,'部分可用');
    assert.equal([...document.querySelectorAll('.model-state')].filter(el=>el.classList.contains('red')).length,1);
    const cells=[...document.querySelectorAll('.history-cell')];
    assert.deepEqual(cells.map(cell=>cell.className),['history-cell green','history-cell warn','history-cell warn','history-cell warn']);
    assert.match(cells.at(-1).title,/部分可用$/);
  }finally{dom.window.close();}
});

test('dragging a provider reorders the fallback chain and saves it',async()=>{
  const {dom,writes}=await page();
  try{
    const {document,Event}=dom.window;
    const items=document.querySelectorAll('[data-provider-move]');
    const source=document.querySelector('[data-drag-kind="provider"][data-drag-index="0"]');
    const target=document.querySelector('[data-drag-kind="provider"][data-drag-index="2"]');
    assert.ok(items.length);
    const drop=new Event('drop',{bubbles:true,cancelable:true});
    drop.dataTransfer={getData:()=>JSON.stringify({kind:'provider',index:0})};
    target.dispatchEvent(drop);
    assert.equal(document.querySelector('[data-drag-kind="provider"][data-drag-index="2"] code').textContent,config.providers[0].label);
    await document.querySelector('#save').onclick();
    assert.equal(writes[0].config.providers[2].key,config.providers[0].key);
  }finally{dom.window.close();}
});

test('the model registry renders one table per provider in provider order',async()=>{
  const {dom}=await page();
  try{
    const {document}=dom.window;
    assert.equal(document.querySelector('#provider-filter'),null);
    const titles=[...document.querySelectorAll('#models .model-group-title')].map(node=>node.textContent.replace(/\d+$/,'').trim());
    assert.deepEqual(titles,config.providers.map(p=>p.label));
    const tables=document.querySelectorAll('#models table.model-table');
    assert.equal(tables.length,config.providers.length);
  } finally {dom.window.close();}
});

test('the model registry sorts all models without changing edit or save indices',async()=>{
  const catalog=structuredClone(config);
  const fixtures=[
    ['old-max',4,'max',null,true],
    ['same-lite',5,'lite',null,true],
    ['manual-second',3,'max',2,true],
    ['same-flash',5,'flash',null,true],
    ['same-pro-b',5,'pro',null,true],
    ['new-snapshot',6,'lite',null,false],
    ['same-max',5,'max',null,true],
    ['manual-first',1,'lite',0,false],
    ['same-pro-a',5,'pro',null,true],
  ];
  const start=catalog.models.length;
  catalog.models.push(...fixtures.map(([id,generation,tier,order,in_chain])=>({
    id:`registry-fixture-${id}`, provider:'claude', generation, tier, order, in_chain, vision:false, note:'', slug:'',
  })));
  const {dom,writes}=await page({catalog});
  try {
    const {document,Event}=dom.window;
    const search=document.querySelector('#model-search');
    search.value='registry-fixture';
    search.dispatchEvent(new Event('input',{bubbles:true}));
    const rows=()=>[...document.querySelectorAll('#models [data-model]')];
    const displayed=()=>rows().map(row=>row.querySelector('[data-field="id"]').value.replace('registry-fixture-',''));
    assert.deepEqual(displayed(),['manual-first','manual-second','new-snapshot','same-max','same-pro-a','same-pro-b','same-flash','same-lite','old-max']);
    assert.equal(document.querySelector('#save').disabled,true);

    const order=rows()[2].querySelector('[data-field="order"]');
    order.value='1';
    order.dispatchEvent(new Event('input',{bubbles:true}));
    await document.querySelector('#save').onclick();
    assert.deepEqual(displayed().slice(0,3),['manual-first','new-snapshot','manual-second']);
    assert.deepEqual(writes[0].config.models,catalog.models.map((model,i)=>i===start+5?{...model,order:1}:model));
  } finally {dom.window.close();}
});
