// SPDX-License-Identifier: AGPL-3.0-only
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {health:'通路探活',models:'模型注册表',routing:'路由配置',settings:'请求与向量'};
const descriptions = {
  health:'从真实运行环境检查连接、鉴权和模型可用性。',
  models:'统一登记模型能力与代际，所有角色共享同一份模型目录。',
  routing:'调整对话分档、随机池与厂商容灾顺序。',
  settings:'管理公开连接参数与向量设置，密钥保留在 Actions secrets。',
};
const state = {tab:'health',config:null,original:null,sha:null,session:{},registry:[],health:null,filter:'all',search:'',busy:false};
let poll;
const time = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}) : '尚无记录';
const dirty = () => state.config && JSON.stringify(state.config) !== JSON.stringify(state.original);
const editable = () => state.session.authenticated && !state.busy;

function message(text, type = '', link) {
  const box = $('notification'); box.hidden = false; box.className = type; box.replaceChildren(document.createTextNode(text));
  if (link) {const a=document.createElement('a');a.href=link;a.target='_blank';a.rel='noreferrer';a.textContent=' 查看 GitHub ↗';box.append(a);}
}
async function api(path, options = {}) {
  const response = await fetch(path,{cache:'no-store',...options,headers:{'Content-Type':'application/json',...options.headers}});
  const result = await response.json();
  if (!response.ok) throw new Error([result.error,...(result.details || [])].join(' · '));
  return result;
}
function updateDirty() {
  const changed = dirty();
  $('dirty-label').textContent = changed ? '有未保存的公共配置修改' : '配置已与公开仓同步';
  $('save-help').textContent = state.session.authenticated ? '保存生成 Git 提交；后续任务自动使用最新版。' : '登录后可修改并提交配置。';
  $('save').disabled = !editable() || !changed;
  $('review').disabled = !changed;
  $('reload').disabled = state.busy;
  if (changed) sessionStorage.setItem('rp-core-public-draft',JSON.stringify({sha:state.sha,config:state.config}));
  else sessionStorage.removeItem('rp-core-public-draft');
}
function tab(name) {
  state.tab = labels[name] ? name : 'health';
  for (const key of Object.keys(labels)) $(key).hidden = key !== state.tab;
  document.querySelectorAll('[data-tab]').forEach(button => button.classList.toggle('active',button.dataset.tab===state.tab));
  $('breadcrumb').textContent = $('page-title').textContent = labels[state.tab];
  $('page-description').textContent = descriptions[state.tab];
  $('savebar').hidden = state.tab === 'health';
  location.hash = state.tab;
  updateDirty();
}
function color(item) {
  if (!item || item.verdict === 'unknown') return 'unknown';
  if (item.verdict === 'not_configured') return 'not_configured';
  return item.severity === 'warn' || item.verdict === 'rate_limited' ? 'warn' : item.ok === true ? 'green' : item.ok === false ? 'red' : 'unknown';
}
const verdicts = {ok:'可用',not_configured:'未配置',dns_fail:'DNS 失败',tcp_fail:'连接失败',tls_fail:'TLS 失败',tls_cert_invalid:'证书不受信',auth_denied:'鉴权拒绝',blocked:'被拦截',model_not_found:'模型不可用',rate_limited:'限流',quota_exhausted:'额度问题',bad_request:'请求被拒',upstream_error:'上游错误',timeout:'超时',http_error:'请求异常',unknown:'未判定'};
const badge = item => `<span class="pill ${color(item)}">${escape(verdicts[item?.verdict] || '未判定')}</span>`;
function modelCell(item) {
  const status = color(item); const glyph={green:'✓',red:'×',warn:'!',not_configured:'–',unknown:'·'}[status];
  return `<span class="model-state ${status}" title="${escape(verdicts[item?.verdict] || '未判定')}${item?.status ? ' · HTTP '+item.status:''}">${glyph}</span>`;
}
function renderHealth() {
  const result=state.health; const data=result?.latest;
  if (!data) {
    $('health').innerHTML='<div class="empty-state"><strong>等待 RP-Core 首次探活</strong>结果来自 RP-Core Actions 中的当前实例环境。<br>管理员登录后可手动触发；定时任务每 6 小时运行一次。</div>';
    return;
  }
  const all=Object.values(data.providers).flatMap(p=>Object.values(p.paths));
  const counts={green:0,warn:0,red:0,not_configured:0,unknown:0};all.forEach(p=>counts[color(p)]++);
  const tiles=[['green','可用通路','✓','连接与鉴权正常'],['warn','需关注 / 未判定','!','限流或端点未给出结论'],['red','不可用通路','×','鉴权、网络或模型检查失败'],['not_configured','未配置通路','–','尚未配置对应凭据']];
  const age=Date.now()-new Date(data.generated_at_utc).getTime();
  const stale=age>26*3600000;
  const mismatch=state.configHash && data.config_sha256 !== state.configHash;
  const reportUrl=`https://github.com/${state.session.repository}/actions/runs/${data.run_id}`;
  let html=`<div class="summary-grid">${tiles.map(([key,title,glyph,note])=>`<div class="stat-card"><span class="stat-label">${title}</span><span class="stat-icon ${key}">${glyph}</span><div class="stat-number ${key}">${counts[key]+(key==='warn'?counts.unknown:0)}</div><small>${note}</small></div>`).join('')}</div>`;
  html+=`<div class="health-meta"><span><span class="status-dot"></span>凭据来源 <strong>RP-Core Actions / ${escape(data.instance_id)}</strong></span><span>${stale?'⚠ 结果已过期 · ':''}${mismatch?'⚠ 等待新配置探活 · ':''}更新于 ${time(data.generated_at_utc)} (UTC+8)</span><a href="${escape(reportUrl)}" target="_blank" rel="noreferrer">查看本次运行 ↗</a></div>`;
  html+='<div class="section-heading"><div><h2>模型 × 通路</h2><p>非生成检查 · 只验证可达性、鉴权和模型支持，不代表真实生成质量</p></div><div class="legend"><span><i style="background:#64bea7"></i>可用</span><span><i style="background:#a88b52"></i>限流</span><span><i style="background:#a95c69"></i>不可用</span></div></div>';
  if (stale || mismatch) html+='<div class="hint">过期结果或与当前模型配置不一致的结果不会用于阻断运行时通路。可触发新探活后再检查。</div>';
  for (const [provider,item] of Object.entries(data.providers)) {
    const models=[...new Set(Object.values(item.paths).flatMap(p=>Object.keys(p.models || {})))];
    const label=state.config?.providers.find(p=>p.key===provider)?.label || provider;
    const paths=Object.entries(item.paths);
    html+=`<details class="provider-card" open><summary><span class="provider-logo">${escape(label.slice(0,1))}</span><span class="provider-name">${escape(label)}</span><span class="provider-meta"><span class="count">${item.models_ok ?? '–'} / ${item.models_total ?? '–'} 模型可用</span><span class="${item.enabled?'green':'red'}">${paths.filter(([,p])=>p.ok).length} / ${paths.length} 通路可用</span></span></summary><div class="table-scroll"><table><thead><tr><th>运行通路</th><th>当前状态</th><th>响应时间</th><th>最近 ${Math.min(result.history.length,32)} 次检查</th>${models.map(m=>`<th class="model-head" title="${escape(m)}">${escape(m)}</th>`).join('')}</tr></thead><tbody>`;
    for (const [name,path] of paths) {
      const samples=result.history.slice(-32);
      html+=`<tr><td class="path-name">${escape(name)}</td><td>${badge(path)}</td><td class="latency">${path.latency_ms === null?'–':Math.round(path.latency_ms)+' ms'}</td><td><div class="history-strip">${samples.map(s=>`<span class="history-cell ${color(s.providers?.[provider]?.paths?.[name])}" title="${time(s.generated_at_utc)} · ${escape(verdicts[s.providers?.[provider]?.paths?.[name]?.verdict] || '无记录')}"></span>`).join('')}</div></td>${models.map(m=>`<td>${modelCell(path.models?.[m])}</td>`).join('')}</tr>`;
    }
    html+='</tbody></table></div></details>';
  }
  $('health').innerHTML=html;
}
function options(values,selected) {return values.map(v=>`<option value="${escape(v)}"${v===selected?' selected':''}>${escape(v)}</option>`).join('');}
function renderModels() {
  if (!state.config) return;
  const disabled=editable()?'':' disabled';const config=state.config;
  const filtered=config.models.map((m,i)=>[m,i]).filter(([m])=>(state.filter==='all'||m.provider===state.filter)&&`${m.id} ${m.note}`.toLowerCase().includes(state.search.toLowerCase()));
  $('models').innerHTML=`<div class="hint">这份注册表直接供完整运行时与探活脚本读取。代际越高越优先；同代际按 Pro → Flash → Lite。显式顺序优先于代际。<br>新增模型需使用现有客户端支持的厂商协议；模型名是否实际可用以探活与服务商为准。</div><div class="toolbar"><input id="model-search" type="search" placeholder="搜索模型名称或备注" value="${escape(state.search)}" aria-label="搜索模型"><select id="provider-filter" aria-label="按厂商筛选"><option value="all">全部厂商 · ${config.models.length}</option>${config.providers.map(p=>`<option value="${p.key}"${p.key===state.filter?' selected':''}>${escape(p.label)}</option>`).join('')}</select><button id="add-model" class="button"${disabled}>＋ 新增模型</button></div><div class="provider-card"><div class="table-scroll"><table class="model-table"><thead><tr><th>原生模型 ID</th><th>厂商</th><th>档位</th><th>代际</th><th>顺序</th><th>主链</th><th>视觉</th><th>OpenRouter 同款 slug</th><th>备注</th><th></th></tr></thead><tbody>${filtered.map(([m,i])=>`<tr data-model="${i}"><td><input class="model-id" aria-label="模型 ID ${i+1}" data-field="id" value="${escape(m.id)}"${disabled}></td><td><select data-field="provider" aria-label="模型厂商 ${i+1}"${disabled}>${options(config.providers.map(p=>p.key),m.provider)}</select></td><td><select data-field="tier" aria-label="模型档位 ${i+1}"${disabled}>${options(['pro','flash','lite'],m.tier)}</select></td><td><input type="number" step="0.1" min="0" data-field="generation" aria-label="模型代际 ${i+1}" value="${m.generation}"${disabled}></td><td><input type="number" min="0" data-field="order" aria-label="模型顺序 ${i+1}" value="${m.order ?? ''}" placeholder="自动"${disabled}></td><td><input type="checkbox" data-field="in_chain" aria-label="进入主链 ${i+1}"${m.in_chain?' checked':''}${disabled}></td><td><input type="checkbox" data-field="vision" aria-label="支持视觉 ${i+1}"${m.vision?' checked':''}${disabled}></td><td><input class="model-slug" data-field="slug" aria-label="OpenRouter slug ${i+1}" value="${escape(m.slug)}"${disabled}></td><td><input class="model-note" data-field="note" aria-label="模型备注 ${i+1}" value="${escape(m.note)}"${disabled}></td><td><button class="button secondary danger" data-delete-model="${i}"${disabled}>移除</button></td></tr>`).join('')}</tbody></table></div></div>`;
  $('model-search').oninput=e=>{state.search=e.target.value;const position=e.target.selectionStart;renderModels();$('model-search').focus();$('model-search').setSelectionRange(position,position);};
  $('provider-filter').onchange=e=>{state.filter=e.target.value;renderModels();};
  $('add-model').onclick=()=>{config.models.push({id:`new-model-${config.models.length+1}`,provider:state.filter==='all'?'claude':state.filter,tier:'flash',generation:0,slug:'',note:'',in_chain:true,vision:false,order:null});state.search='';renderModels();updateDirty();};
  $('models').querySelectorAll('[data-field]').forEach(input=>input.onchange=()=>{
    const m=config.models[Number(input.closest('[data-model]').dataset.model)];const key=input.dataset.field;const previous=m[key];
    m[key]=input.type==='checkbox'?input.checked:input.type==='number'?(input.value===''&&key==='order'?null:Number(input.value)):input.value;
    if (key==='id') {
      for (const chain of [...Object.values(config.dialog_tiers),config.low_tier_pool,config.summary_models]) chain.forEach((id,i)=>{if(id===previous)chain[i]=m.id;});
    }
    renderRouting();updateDirty();
  });
  $('models').querySelectorAll('[data-delete-model]').forEach(button=>button.onclick=()=>{const i=Number(button.dataset.deleteModel);const id=config.models[i].id;
    if ([...Object.values(config.dialog_tiers),config.low_tier_pool,config.summary_models].some(c=>c.includes(id))) {message('该模型仍被路由引用，请先在路由配置中移除引用。','error');return;}
    config.models.splice(i,1);renderModels();renderRouting();updateDirty();});
}
const chainFor = name => name.startsWith('tier:') ? state.config.dialog_tiers[name.slice(5)] : state.config[name];
function chainCard(name,title,description) {
  const disabled=editable()?'':' disabled';const chain=chainFor(name);
  const candidates=state.config.models.filter(m=>!chain.includes(m.id)&&(name!=='summary_models'||m.provider==='claude'));
  return `<article class="panel route-card"><h2>${title}</h2><p class="route-description">${description}</p><div class="chain">${chain.length?chain.map((id,i)=>`<div class="chain-item"><span class="chain-index">${String(i+1).padStart(2,'0')}</span><code>${escape(id)}</code><button aria-label="上移 ${escape(id)}" data-chain="${name}" data-index="${i}" data-move="-1"${i===0||!editable()?' disabled':''}>↑</button><button aria-label="下移 ${escape(id)}" data-chain="${name}" data-index="${i}" data-move="1"${i===chain.length-1||!editable()?' disabled':''}>↓</button><button aria-label="移除 ${escape(id)}" data-chain="${name}" data-index="${i}" data-move="remove"${disabled}>×</button></div>`).join(''):`<div class="chain-empty">${name==='summary_models'?'自动使用完整 Claude 对话链，首选最高 Pro 模型。':'此档没有静态链首，由随机池和后续容灾接管。'}</div>`}</div><div class="chain-add"><select aria-label="添加到 ${title}" data-chain-select="${name}"${disabled}>${candidates.map(m=>`<option value="${escape(m.id)}">${escape(m.id)}</option>`).join('')}</select><button class="button secondary" data-add-chain="${name}"${!candidates.length||!editable()?' disabled':''}>添加</button></div></article>`;
}
function renderRouting() {
  if (!state.config) return;
  const disabled=editable()?'':' disabled';
  $('routing').innerHTML='<div class="hint">对话从当前分档开始，依次尝试后续档位、厂商容灾组与 DeepSeek 链尾。Low 档先从随机池抽取首选。保存后，所有实例共享同一套规则。</div><div class="route-grid">'+
    [['tier:max','Max · 最高档','复杂任务的静态链首'],['tier:high','High · 高档','高档位的静态链首'],['tier:medium','Medium · 标准档','标准档位的静态链首'],['tier:low','Low · 低档','通常留空，使用下方随机池'],['low_tier_pool','Low 随机池','每次调用重新抽取一个首选模型'],['summary_models','总结模型链','现有总结客户端使用 Claude 协议']].map(a=>chainCard(...a)).join('')+'</div>'+
    `<div class="section-heading"><div><h2>厂商容灾顺序</h2><p>同一轮按以下顺序轮转；Lite 从第二轮开始。取消参与只影响容灾组。</p></div></div><div class="panel"><div class="provider-order">${state.config.providers.map((p,i)=>`<div class="chain-item"><span class="chain-index">${i+1}</span><code>${escape(p.label)}</code><label title="参与容灾"><input type="checkbox" data-provider-enabled="${i}" aria-label="${escape(p.label)} 参与容灾"${p.in_fallback_chain?' checked':''}${disabled}></label><button data-provider-move="${i}" data-direction="-1" aria-label="上移 ${escape(p.label)}"${i===0||!editable()?' disabled':''}>↑</button><button data-provider-move="${i}" data-direction="1" aria-label="下移 ${escape(p.label)}"${i===state.config.providers.length-1||!editable()?' disabled':''}>↓</button></div>`).join('')}</div></div>`;
  $('routing').querySelectorAll('[data-move]').forEach(b=>b.onclick=()=>{const a=chainFor(b.dataset.chain),i=Number(b.dataset.index);if(b.dataset.move==='remove')a.splice(i,1);else{const j=i+Number(b.dataset.move);[a[i],a[j]]=[a[j],a[i]];}renderRouting();updateDirty();});
  $('routing').querySelectorAll('[data-add-chain]').forEach(b=>b.onclick=()=>{const select=[...$('routing').querySelectorAll('[data-chain-select]')].find(s=>s.dataset.chainSelect===b.dataset.addChain);if(select.value)chainFor(b.dataset.addChain).push(select.value);renderRouting();updateDirty();});
  $('routing').querySelectorAll('[data-provider-enabled]').forEach(b=>b.onchange=()=>{state.config.providers[Number(b.dataset.providerEnabled)].in_fallback_chain=b.checked;updateDirty();});
  $('routing').querySelectorAll('[data-provider-move]').forEach(b=>b.onclick=()=>{const a=state.config.providers,i=Number(b.dataset.providerMove),j=i+Number(b.dataset.direction);[a[i],a[j]]=[a[j],a[i]];renderRouting();updateDirty();});
}
function renderSettings() {
  if (!state.config) return;
  const disabled=editable()?'':' disabled',e=state.config.embedding;
  const properties=state.schema.properties.environment.properties;
  $('settings').innerHTML=`<div class="panel"><div class="panel-header"><div><h2>向量与嵌入</h2><p>读取现有记忆库与缓存；修改配置不会自动重建数据库。</p></div></div><div class="field-grid"><label class="field">嵌入模型<input id="embedding-model" value="${escape(e.model)}"${disabled}></label><label class="field">向量维度<input id="embedding-dimensions" type="number" min="64" max="8192" value="${e.dimensions}"${disabled}><small>已有索引依赖此维度，切换需另行迁移。</small></label><label class="field">单次最大输入字符<input id="embedding-max" type="number" min="128" value="${e.max_input_chars}"${disabled}></label></div><div class="hint">更换嵌入模型或维度会影响历史向量兼容性。请先规划现有缓存与索引的迁移，再保存这两项。</div></div><div class="panel"><h2>公共请求参数</h2><p>留空表示沿用运行环境或框架默认值。这里保存的是公开设置，请勿填入 API key、带令牌的 URL 或个人信息。</p><div class="env-grid">${Object.entries(properties).map(([name,spec])=>`<label class="field">${escape(name)}${spec.enum?`<select data-env="${name}"${disabled}><option value="">框架 / 环境默认</option>${options(spec.enum,state.config.environment[name])}</select>`:`<input data-env="${name}" type="${name.includes('URL')?'url':'text'}" value="${escape(state.config.environment[name] || '')}" placeholder="框架 / 环境默认"${disabled}>`}</label>`).join('')}</div></div>`;
  $('embedding-model').onchange=x=>{e.model=x.target.value;updateDirty();};
  $('embedding-dimensions').onchange=x=>{e.dimensions=Number(x.target.value);updateDirty();};
  $('embedding-max').onchange=x=>{e.max_input_chars=Number(x.target.value);updateDirty();};
  $('settings').querySelectorAll('[data-env]').forEach(x=>x.onchange=()=>{if(x.value.trim())state.config.environment[x.dataset.env]=x.value.trim();else delete state.config.environment[x.dataset.env];updateDirty();});
}
async function loadConfig({restore=false}={}) {
  const data=await api('/api/config');state.config=data.config;state.original=structuredClone(data.config);state.sha=data.sha;state.configHash=data.config_hash;state.schema=data.schema;
  if (restore) {try {const draft=JSON.parse(sessionStorage.getItem('rp-core-public-draft'));if(draft?.sha===state.sha){state.config=draft.config;message('已恢复本次会话中尚未保存的公共配置草稿。');}}catch{}}
  $('repository').textContent=`${data.repository} / ${data.branch} · ${data.sha.slice(0,7)}`;
  $('source-link').href=`https://github.com/${data.repository}`;
  renderModels();renderRouting();renderSettings();renderHealth();updateDirty();
}
async function loadHealth() {if(!$('instance').value)return;state.health=await api('/api/health?instance='+encodeURIComponent($('instance').value));renderHealth();}
async function authAction(action) {
  const {csrfToken}=await api('/api/auth/csrf');
  const form=document.createElement('form');form.method='POST';form.action='/api/auth/'+action;
  for(const [name,value] of Object.entries({csrfToken,callbackUrl:location.origin})) {const input=document.createElement('input');input.type='hidden';input.name=name;input.value=value;form.append(input);}
  document.body.append(form);form.submit();
}
$('login').onclick=()=>authAction('signin/github').catch(e=>message(e.message,'error'));
$('logout').onclick=()=>authAction('signout').catch(e=>message(e.message,'error'));
$('instance').onchange=()=>loadHealth().catch(e=>message(e.message,'error'));
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
$('reload').onclick=async()=>{if(dirty()&&!confirm('重新载入将放弃当前页面中未保存的修改，是否继续？'))return;try{await loadConfig();message('已读取公开仓最新配置。','success');}catch(e){message(e.message,'error');}};
$('review').onclick=()=>{const changes={};for(const key of Object.keys(state.config))if(JSON.stringify(state.config[key])!==JSON.stringify(state.original[key]))changes[key]={before:state.original[key],after:state.config[key]};$('review-content').textContent=JSON.stringify(changes,null,2);$('review-dialog').showModal();};
$('close-review').onclick=()=>$('review-dialog').close();
$('save').onclick=async()=>{if(!editable()||!dirty())return;const snapshot=structuredClone(state.config);state.busy=true;renderModels();renderRouting();renderSettings();updateDirty();try {
  const result=await api('/api/config',{method:'PUT',body:JSON.stringify({sha:state.sha,config:snapshot})});
  state.sha=result.sha;state.configHash=result.config_hash;state.original=snapshot;sessionStorage.removeItem('rp-core-public-draft');
  message(`已保存配置，提交 ${result.commit.slice(0,7)}。后续任务读取新版本；新探活将自动运行。`,'success',result.url);
  $('repository').textContent=`${state.session.repository} / ${state.session.branch} · ${result.sha.slice(0,7)}`;
} catch(e) {message(e.message,'error');} finally {state.busy=false;renderModels();renderRouting();renderSettings();renderHealth();updateDirty();}};
$('probe').onclick=async()=>{const b=$('probe');b.disabled=true;try {
  const result=await api('/api/probe',{method:'POST',body:JSON.stringify({instance_id:$('instance').value})});
  message('已提交 RP-Core 探活任务。完整通路检查可能需要数分钟，结果会自动刷新。','success',result.url);
  const before=state.health?.latest?.generated_at_utc;let checks=0;clearInterval(poll);
  poll=setInterval(async()=>{try{await loadHealth();if(state.health?.latest?.generated_at_utc!==before){clearInterval(poll);b.disabled=!editable();message('新的 RP-Core 探活结果已载入。','success');}else if(++checks>=100){clearInterval(poll);b.disabled=!editable();message('探活仍未更新，请在 GitHub 查看排队或失败原因。','',result.url);}}catch{}},15000);
}catch(e){message(e.message,'error');b.disabled=!editable();}};

async function init() {
  try {
    const [session,registry]=await Promise.all([api('/api/session'),api('/api/instances')]);state.session=session;state.registry=registry.instances;
    $('identity').textContent=session.authenticated?'@'+session.login:'只读访问';
    $('login').hidden=session.authenticated;$('logout').hidden=!session.authenticated;
    $('login').disabled=!session.oauth_ready;$('login').textContent=session.oauth_ready?'GitHub 登录 ↗':'OAuth 配置中';
    $('probe').disabled=!session.authenticated;
    $('instance').innerHTML=registry.instances.map(i=>`<option value="${escape(i.id)}">${escape(i.id)}</option>`).join('');
    await loadConfig({restore:true});await loadHealth();tab(location.hash.slice(1));
    if(new URLSearchParams(location.search).has('error'))message('GitHub 登录未完成，或此账号不在管理员名单中。','error');
  }catch(e){message(e.message,'error');$('health').innerHTML='<div class="empty-state"><strong>暂时无法载入控制台</strong>请检查网络与管理站环境配置后刷新。</div>';}
}
await init();
