// SPDX-License-Identifier: AGPL-3.0-only
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';

const html = readFileSync(new URL('../public/index.html',import.meta.url),'utf8');
const source = readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
const config = JSON.parse(readFileSync(new URL('../../src/rp_core/compat_runtime/model-config.json',import.meta.url),'utf8'));
const schema = JSON.parse(readFileSync(new URL('../lib/model-config.schema.json',import.meta.url),'utf8'));

async function page() {
  const dom=new JSDOM(html,{url:'https://console.example/#models',runScripts:'outside-only'});
  dom.window.structuredClone=structuredClone;
  const writes=[];
  dom.window.fetch=async(path,options)=>{
    if(path==='/api/session')return Response.json({authenticated:true,login:'owner',repository:'example/framework',branch:'main',oauth_ready:true});
    if(path==='/api/instances')return Response.json({instances:[{id:'example'}]});
    if(path.startsWith('/api/health'))return Response.json({available:false,latest:null,history:[]});
    if(path==='/api/config'&&options.method==='PUT'){
      writes.push(JSON.parse(options.body));
      return Response.json({saved:true,sha:'b'.repeat(40),config_hash:'d'.repeat(64),commit:'c'.repeat(40),url:'https://github.com/example/framework/commit/test'});
    }
    if(path==='/api/config')return Response.json({config,sha:'a'.repeat(40),config_hash:'d'.repeat(64),schema,repository:'example/framework',branch:'main'});
    throw new Error('Unexpected request: '+path);
  };
  // Run our own application, with no external scripts or network access.
  await dom.window.eval(`(async()=>{${source}\n})()`);
  return {dom,writes};
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
