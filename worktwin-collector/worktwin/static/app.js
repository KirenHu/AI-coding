/* WorkTwin: deliberately small, native-feeling, dependency-free client. */
const icon = n => `<svg aria-hidden="true"><use href="#i-${n}"/></svg>`;
const esc = x => String(x ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
const kindNames={fact:'业务知识',decision:'决策记录',process:'流程指引',preference:'个人偏好'};
const titles={knowledge:'我的知识库',twins:'我的数字分身',sources:'信息采集',settings:'设置'};
const state={page:'knowledge',knowledge:[],sources:[],twins:[],project:'all',kind:'all',query:'',twinId:null,modelReady:false,shareReady:false,proposals:[],settings:{},lifecycle:'active',chats:{},dirty:false,overlayDirty:false,scrollPosition:0};
const el=id=>document.getElementById(id);
const content=el('page-content');
let toastTimer;
function notify(message){const t=el('toast');t.textContent=message;t.className='show';clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.className='',3500)}
function formatTime(value){return value?String(value).replace('T',' ').slice(0,16):'—'}
function short(value,length=90){const s=String(value||'').replace(/\s+/g,' ').trim();return esc(s.length>length?s.slice(0,length)+'…':s)}
function pageHeader(eyebrow,title,description,action=''){return `<div class="page-title-row"><div><div class="eyebrow">${esc(eyebrow)}</div><h1 class="page-title">${esc(title)}</h1><p class="page-caption">${esc(description)}</p></div>${action}</div>`}
function emptyState(ico,title,description,action=''){return `<div class="empty-state"><div class="empty-graphic">${icon(ico)}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`}
async function api(path,options={}){
  const opts={method:options.method||'GET',headers:{'X-Worktwin-Token':window.__WORKTWIN_TOKEN__},...options};
  if(options.body && typeof options.body==='object'){
    opts.body=JSON.stringify(options.body);
    opts.headers={...opts.headers,'Content-Type':'application/json'};
  }
  const response=await fetch('/api/'+path,opts);
  if(!response.ok){let reason=`请求失败 (${response.status})`;try{const data=await response.json();reason=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail)}catch{}throw new Error(reason)}
  return response.json();
}
async function perform(fn,refresh){try{await fn();if(refresh)await go(refresh,true);return true}catch(e){notify('操作未完成：'+e.message);return false}}
async function busy(button,fn){if(button.disabled)return;button.disabled=true;try{return await fn()}finally{if(button.isConnected)button.disabled=false}}
function confirmDiscard(){return !state.dirty||confirm('有尚未保存的修改，确定放弃吗？')}
function watchChanges(node){node.addEventListener('input',()=>state.dirty=true);node.addEventListener('change',()=>state.dirty=true)}
window.addEventListener('beforeunload',e=>{if(state.dirty||state.overlayDirty){e.preventDefault();e.returnValue=''}});
function dialog(title,body,footer,wide=false){
  if(!closeOverlay())return false;
  el('overlay-root').innerHTML=`<div class="overlay" id="overlay"><div class="dialog ${wide?'wide':''}" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="dialog-header"><h2>${esc(title)}</h2><button class="icon-button" data-close aria-label="关闭">${icon('close')}</button></div><div class="dialog-body">${body}</div>${footer?`<div class="dialog-footer">${footer}</div>`:''}</div></div>`;
  el('overlay').onclick=e=>{if(e.target.id==='overlay'||e.target.closest('[data-close]'))closeOverlay()};
  document.addEventListener('keydown',onEscape);
}
function onEscape(e){if(e.key==='Escape'){const preview=el('overlay-root').querySelector('.source-preview');if(preview){preview.remove();return}closeOverlay()}}
function closeOverlay(force=false){if(!force&&state.overlayDirty&&!confirm('有尚未保存的修改，确定放弃吗？'))return false;state.overlayDirty=false;el('overlay-root').innerHTML='';document.removeEventListener('keydown',onEscape);return true}
async function refreshConnection(){
  try{const info=await api('settings');state.settings=info;state.modelReady=!!info.enterprise_model_ready;state.shareReady=!!info.cloud_sync;
    const node=el('model-status');const labels={not_configured:'模型未配置',configured:'已配置 · 尚未测试',connected:'模型连接正常',error:'模型调用失败'};
    node.innerHTML=`<span class="pulse-dot ${info.model_status==='connected'?'':'off'}"></span>${labels[info.model_status]||'模型未配置'}`;node.title=info.model_error||'打开设置';node.onclick=()=>go('settings');
    el('edition-label').textContent=info.edition==='enterprise'?'企业版':'个人版';
  }catch(e){el('model-status').textContent='本地服务不可用'}
}
async function go(page,force=false){
  if(state.versionMismatch)return;
  if(!force && page===state.page)return;
  if(state.dirty&&!confirmDiscard())return;state.dirty=false;
  if(window.__JOB_POLL__){clearInterval(window.__JOB_POLL__);window.__JOB_POLL__=null;}
  if(page!=='knowledge')state.scrollPosition=0;
  state.page=page;
  document.querySelectorAll('.nav-link').forEach(node=>node.classList.toggle('active',node.dataset.page===page));
  el('page-breadcrumb').textContent=titles[page];
  content.innerHTML='<div class="loading">正在读取本地数据…</div>';
  try{
    if(page==='knowledge')await renderKnowledge();
    else if(page==='sources')await renderSources();
    else if(page==='twins')await renderTwins();
    else if(page==='settings')await renderSettings();
  }catch(e){content.innerHTML=`<div class="loading-error">无法加载：${esc(e.message)} <button class="btn secondary small" id="retry">重试</button></div>`;el('retry').onclick=()=>go(page,true)}
}

// Information sources: category + explicit capture / enterprise-model permission.
const typeLabel={folder:'本地文件夹',codex:'Codex',claude:'Claude Code'};
function sourceName(s){const kind=sourceType(s);const legacy={folder:['工作文件','本地工作文件'],codex:['Codex 对话'],claude:['Claude Code 对话']};return legacy[kind]?.includes(s.name)?typeLabel[kind]:s.name}
function sourceType(s){return s.adapter||s.kind}
async function renderSources(){
  const [sources,stats,jobs]=await Promise.all([api('sources'),api('stats'),api('ai/jobs')]);state.sources=sources;
  const count=k=>sources.filter(s=>sourceType(s)===k).length;
  const types=[['folder','本地文件夹', 'Word、PDF、Markdown、代码等授权目录','folder'],['codex','Codex','历史会话与后续产生的对话','book'],['claude','Claude Code','历史会话与后续产生的对话','spark']];
  content.innerHTML=pageHeader('INFORMATION SOURCES','信息采集','只采集你允许的工作资料。随时暂停，也可以彻底撤销授权。',`<button class="btn secondary" id="scan">${icon('refresh')} 立即检查更新</button>`)+
    `<div class="source-overview">${types.map(([kind,name,desc,ico])=>`<div class="source-type"><div class="type-symbol">${icon(ico)}</div><b>${name}</b><small>${desc}</small><button class="btn secondary small" data-add-source="${kind}">${icon('plus')} ${count(kind)?'再添加':'授权采集'}</button></div>`).join('')}</div>`+
    `<div class="group-heading"><h2>已授权的数据范围</h2><span class="soft-caption">${sources.length} 个数据源</span></div>`+
    `<div class="card">${sources.length?sources.map(sourceRow).join(''):emptyState('folder','还没有授权任何数据源','选择上方的信息类型，授权工作目录后即可自动、增量采集。')}</div>`+
    `<div class="scan-status"><span>系统会自动检测文件变化 · 最近扫描：${esc(stats.last_scan)}</span><span>${stats.documents} 份已索引资料 · ${stats.ai_jobs.queued} 项待整理 · ${stats.ai_jobs.running} 项处理中 · ${stats.ai_jobs.error} 项失败</span></div>`+
    `<div class="processing-panel"><h2>AI 整理状态</h2>${!state.modelReady?'<p>请先到设置完成模型配置。已授权的资料会保留在本机。</p><button class="btn secondary small" id="source-settings">去设置</button>':stats.ai_jobs.error?'<button class="btn secondary small" id="retry-ai">重试失败任务</button>':'<p class="soft-caption">后台自动处理，只需要关注失败或待核对的结果。</p>'}${jobs.filter(j=>j.state!=='done').slice(0,10).map(j=>`<div class="job-row"><span>${esc(j.title)}<small>${esc(j.source_name)}</small></span><span>${{queued:'等待整理',running:'正在整理',error:'整理失败'}[j.state]}${j.error?`<small>${esc(j.error)} · 可重试</small>`:''}</span></div>`).join('')}</div>`+
    `<div class="status-note" style="margin-top:23px">${icon('shield')}<div><b>采集权限与 AI 处理权限分开控制。</b> 本地采集不会自动上传原始文件；只有启用“允许 AI 整理”的数据源，才会在定时任务中把相关文本发送给当前模型服务。停止采集保留本地知识；彻底移除会删除该来源及其派生知识。</div></div>`;
  el('scan').onclick=()=>busy(el('scan'),async()=>{if(await perform(()=>api('scan',{method:'POST'}),'sources'))notify('已安排检查更新')});
  el('source-settings')?.addEventListener('click',()=>go('settings'));
  el('retry-ai')?.addEventListener('click',()=>busy(el('retry-ai'),async()=>{if(await perform(()=>api('ai/jobs/retry',{method:'POST'}),'sources'))notify('失败任务已重新排队')}));
  content.querySelectorAll('[data-add-source]').forEach(b=>b.onclick=()=>addSource(b.dataset.addSource));
  content.querySelectorAll('[data-collect-toggle]').forEach(b=>b.onchange=()=>busy(b,async()=>{if(!await perform(()=>api(`sources/${b.dataset.collectToggle}/toggle`,{method:'POST'}),'sources'))b.checked=!b.checked}));
  content.querySelectorAll('[data-ai-toggle]').forEach(b=>b.onchange=()=>busy(b,async()=>{if(!await perform(()=>api(`sources/${b.dataset.aiToggle}/ai`,{method:'PUT',body:{allow_ai:b.checked}}),'sources'))b.checked=!b.checked}));
  content.querySelectorAll('[data-share-toggle]').forEach(b=>b.onchange=()=>busy(b,async()=>{if(!await perform(()=>api(`sources/${b.dataset.shareToggle}/share`,{method:'PUT',body:{allow_share:b.checked}}),'sources'))b.checked=!b.checked}));
  content.querySelectorAll('[data-remove-source]').forEach(b=>b.onclick=async()=>{
    const id=Number(b.dataset.removeSource);const row=state.sources.find(x=>x.id===id);
    if(!confirm(`彻底移除「${row?sourceName(row):'数据源'}」？这会删除其索引、AI 提炼知识以及相关分身的知识授权，无法撤回。`))return;
    await perform(()=>api(`sources/${id}`,{method:'DELETE'}),'sources');
  });
  if(window.__JOB_POLL__){clearInterval(window.__JOB_POLL__);window.__JOB_POLL__=null;}
  if(stats.ai_jobs && (stats.ai_jobs.queued>0 || stats.ai_jobs.running>0)){
    window.__JOB_POLL__=setInterval(async()=>{
      if(state.page!=='sources'){clearInterval(window.__JOB_POLL__);window.__JOB_POLL__=null;return}
      try{
        const [newStats,newJobs]=await Promise.all([api('stats'),api('ai/jobs')]);
        const scanStatus=content.querySelector('.scan-status');
        if(scanStatus){
          scanStatus.innerHTML=`<span>系统会自动检测文件变化 · 最近扫描：${esc(newStats.last_scan)}</span><span>${newStats.documents} 份已索引资料 · ${newStats.ai_jobs.queued} 项待整理 · ${newStats.ai_jobs.running} 项处理中 · ${newStats.ai_jobs.error} 项失败</span>`;
        }
        if(newStats.ai_jobs.queued===0 && newStats.ai_jobs.running===0){
          clearInterval(window.__JOB_POLL__);window.__JOB_POLL__=null;
          notify('AI 整理任务已全部完成');
          await go('sources',true);
        }
      }catch{}
    },3500);
  }
}
function sourceRow(s){const kind=sourceType(s),name=sourceName(s);return `<div class="source-item"><div class="source-summary"><div class="source-name">${esc(name)} <span class="state-label ${s.enabled?'':'grey'}">${s.enabled?'采集中':'已暂停'}</span></div><div class="source-path" title="${esc(s.root)}">${esc(s.root)}</div><div class="source-caption"><span class="soft-caption">${esc(typeLabel[kind]||'工作资料')} · ${s.document_count} 份资料</span>${s.last_error?`<span class="state-label danger">${esc(s.last_error)}</span>`:''}</div></div>
  <label class="permission-cell"><input class="toggle" type="checkbox" aria-label="允许采集 ${esc(name)}" data-collect-toggle="${s.id}" ${s.enabled?'checked':''}/> 允许采集</label>
  <label class="permission-cell"><input class="toggle" type="checkbox" aria-label="允许 AI 整理 ${esc(name)}" data-ai-toggle="${s.id}" ${s.allow_ai?'checked':''}/> 允许 AI 整理</label>
  <label class="permission-cell"><input class="toggle" type="checkbox" aria-label="允许分身分享 ${esc(name)}" data-share-toggle="${s.id}" ${s.allow_share?'checked':''}/> 允许分身分享</label>
  <button class="icon-button" title="撤销来源并清除知识" aria-label="删除 ${esc(name)}" data-remove-source="${s.id}">${icon('trash')}</button></div>`}
async function addSource(initial='folder'){
  const defaults=await api('default-paths');
  dialog('授权信息采集',`<div class="choice-tabs">${[['folder','本地文件夹'],['codex','Codex'],['claude','Claude Code']].map(([type,name])=>`<button class="choice-tab" data-type="${type}">${name}</button>`).join('')}</div>
    <div class="field"><label for="new-source-name">数据源名称</label><input id="new-source-name" autocomplete="off"/></div>
    <div class="field"><label for="new-source-path">授权文件夹</label><div style="display:flex;gap:9px"><input id="new-source-path" autocomplete="off" spellcheck="false"/><button class="btn secondary" id="browse-folder" type="button">选择…</button></div><div class="field-note" id="path-note"></div></div>
    <label class="check-row"><input id="new-source-ai" type="checkbox"/> <span><b>允许 AI 自动整理这些资料</b><br/>内容将发送到当前配置的模型服务，自动提炼可搜索的知识；不勾选则只在本地建立索引。</span></label>
    <div class="permission-note">后续仅采集这个已授权目录及其子目录；默认忽略密钥、.env、node_modules 和 .git 等内容。你可以随时撤销授权。</div>`,
    `<button class="btn secondary" data-close>取消</button><button class="btn" id="save-source">授权并开始采集</button>`);
  let kind=initial;
  function choose(type){kind=type;el('new-source-name').value={folder:'本地文件夹',codex:'Codex',claude:'Claude Code'}[type];el('new-source-path').value=type==='codex'?defaults.codex:type==='claude'?defaults.claude:'';el('path-note').textContent=type==='folder'?'请明确选择允许采集的工作目录。':`默认位置${(type==='codex'?defaults.codex_exists:defaults.claude_exists)?'已检测到':'尚未发现'}，你也可以自行修改。`;document.querySelectorAll('[data-type]').forEach(x=>x.classList.toggle('active',x.dataset.type===type))}
  document.querySelectorAll('[data-type]').forEach(x=>x.onclick=()=>choose(x.dataset.type));choose(initial);
  el('browse-folder').onclick=async()=>{try{const result=await api('pick-folder',{method:'POST'});el('new-source-path').value=result.path}catch(e){notify(e.message)}};
  el('save-source').onclick=()=>busy(el('save-source'),async()=>{
    const body={name:el('new-source-name').value.trim(),root:el('new-source-path').value.trim(),kind,allow_ai:el('new-source-ai').checked};
    if(!body.name||!body.root){notify('请填写名称并选择工作目录');return}
    try{await api('sources',{method:'POST',body});closeOverlay();notify('已授权，首次采集将在后台开始');go('sources',true)}catch(e){notify(e.message)}
  });
}

// Knowledge: Notion-like collection sidebar + page list + editable document pane.
function visibleKnowledge(){return state.knowledge.filter(k=>state.lifecycle==='archived'?k.status==='archived':state.lifecycle==='draft'?k.status==='draft':state.lifecycle==='confirmed'?k.status==='confirmed':state.lifecycle==='review'?k.needs_review:state.lifecycle==='disabled'?k.quality==='noise':k.status!=='archived'&&k.quality!=='noise')}
function entryProject(k){return k.scope==='global'?'跨项目通用':k.project||k.evidence?.find(x=>x.project)?.project||'范围待核对'}
const scopeNames={project:'项目内',session:'本次讨论',global:'跨项目通用',unknown:'范围待核对'};
const outcomeNames={none:'',reported:'报告完成，尚未核实',supported:'已有验证依据',accepted:'用户已验收'};
function availableToTwin(k){return !!k.can_use}
async function loadKnowledge(){let rows=[],offset=0;while(true){const batch=await api(`knowledge?status=all&limit=1000&offset=${offset}`);rows.push(...batch);if(batch.length<1000)break;offset+=batch.length}return rows}
async function renderKnowledge(){
  [state.knowledge,state.proposals]=await Promise.all([loadKnowledge(),api('knowledge/proposals')]);
  const visible=visibleKnowledge(),projects=[...new Set(visible.map(entryProject))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
  if(state.project!=='all'&&state.project!=='reviews'&&!projects.includes(state.project))state.project='all';
  content.innerHTML=pageHeader('KNOWLEDGE LIBRARY','我的知识库','按项目和主题整理工作知识，保留适用范围、当前结论和原始依据。',`<button class="btn secondary" id="create-knowledge">${icon('plus')} 新建知识</button>`)+
    `<div class="library-shell"><aside class="library-sidebar"><h3>知识空间</h3>
    <button class="library-choice ${state.project==='all'?'active':''}" data-project="all">${icon('book')} <span class="truncate">全部知识</span><span class="count">${visible.length}</span></button>
    <button class="library-choice ${state.project==='reviews'?'active':''}" data-project="reviews">${icon('alert')} <span class="truncate">待核对更新</span><span class="count">${state.proposals.length}</span></button>
    <h3 style="margin-top:24px">项目与分类</h3>${projects.map((name,index)=>`<button class="library-choice ${state.project===name?'active':''}" data-project-index="${index}">${icon('folder')}<span class="truncate">${esc(name)}</span><span class="count">${visible.filter(k=>entryProject(k)===name).length}</span></button>`).join('')}</aside>
    <div class="library-main"><div class="library-toolbar"><div class="search-bar">${icon('search')} <input type="search" id="knowledge-search" aria-label="搜索知识" placeholder="搜索知识标题或正文…" value="${esc(state.query)}"/></div><span class="knowledge-count" id="knowledge-count"></span></div>
    <div class="lifecycle-filters">${[['active','全部知识'],['draft','待确认'],['review','待核对'],['confirmed','已确认'],['disabled','已停用'],['archived','已归档']].map(([v,n])=>`<button class="filter-button ${state.lifecycle===v?'active':''}" data-lifecycle="${v}">${n}</button>`).join('')}</div><div class="filters">${[['all','全部'],['fact','知识'],['decision','决策'],['process','流程'],['preference','个人偏好']].map(([type,name])=>`<button class="filter-button ${state.kind===type?'active':''}" data-filter="${type}">${name}</button>`).join('')}</div><div class="knowledge-maintenance"><button class="btn secondary small" id="reprocess-knowledge">重新整理现有资料</button><span class="field-note">仅处理已允许 AI 使用的资料；待核对资料保留供复核；被替代结论只进入变更历史，不参与回答。</span></div><div id="knowledge-list"></div></div></div>`;
  content.querySelectorAll('[data-project]').forEach(x=>x.onclick=()=>{state.project=x.dataset.project;renderKnowledgeList()});
  content.querySelectorAll('[data-project-index]').forEach(x=>x.onclick=()=>{state.project=projects[Number(x.dataset.projectIndex)];renderKnowledgeList()});
  content.querySelectorAll('[data-lifecycle]').forEach(x=>x.onclick=()=>{state.lifecycle=x.dataset.lifecycle;go('knowledge',true)});
  content.querySelectorAll('[data-filter]').forEach(x=>x.onclick=()=>{state.kind=x.dataset.filter;renderKnowledgeList()});
  el('knowledge-search').oninput=e=>{state.query=e.target.value;renderKnowledgeList()};
  el('create-knowledge').onclick=()=>openKnowledge(null);
  el('reprocess-knowledge').onclick=()=>busy(el('reprocess-knowledge'),async()=>{if(!confirm('用当前模型重新整理所有已授权资料？这会产生模型调用费用，已核实结论仍需按更新规则处理；被替代正文仅保存在历史中。'))return;const r=await api('knowledge/reprocess',{method:'POST'});notify(`已安排 ${r.queued} 份资料重新整理`)});
  const automatic=document.createElement('p');automatic.className='field-note';
  automatic.textContent=state.settings.knowledge_automation?.ready?'本模型已通过真实资料验收：明确项目内的无冲突新增、纯补充可自动生效；改变结论和矛盾仍需核对。':'自动生效尚未开启：本模型与当前整理规则需要先通过真实资料验收。';
  content.querySelector('.knowledge-maintenance').appendChild(automatic);
  renderKnowledgeList();
  if(state.scrollPosition){
    const main=document.querySelector('.main-area');
    if(main)requestAnimationFrame(()=>{main.scrollTop=state.scrollPosition});
  }
}
function renderKnowledgeList(){
  const entries=visibleKnowledge().filter(k=>(state.project==='all'||entryProject(k)===state.project)&&(state.kind==='all'||k.kind===state.kind))
    .filter(k=>(k.title+' '+k.body+' '+entryProject(k)).toLowerCase().includes(state.query.toLowerCase()));
  content.querySelectorAll('.library-choice').forEach(x=>x.classList.toggle('active',x.dataset.project!==undefined?state.project===x.dataset.project:x.dataset.projectIndex!==undefined&&state.project===x.querySelector('.truncate')?.textContent));
  content.querySelectorAll('.filter-button').forEach(x=>x.classList.toggle('active',state.kind===x.dataset.filter));
  if(state.project==='reviews'){
    el('knowledge-count').textContent=`${state.proposals.length} 项`;
    el('knowledge-list').innerHTML=state.proposals.length?`<div class="knowledge-list">${state.proposals.map(p=>`<button class="knowledge-row" data-proposal="${p.id}"><span class="page-icon">${icon('alert')}</span><span class="knowledge-info"><div class="knowledge-title">${esc(p.previous_title)}</div><div class="knowledge-preview">${esc(p.origin==='curation'?'定期知识养护':{enrich:'建议补充知识',replace:'发现新版本结论',conflict:'发现可能矛盾的结论'}[p.action])} · ${short(p.reason,100)}</div><div class="knowledge-meta">${esc(p.project)} · ${esc(p.source_title)}</div></span><span class="state-label warn">待确认</span>${icon('chevron')}</button>`).join('')}</div>`:emptyState('check','暂无待核对的知识更新','当新资料与现有知识有关联或冲突时，会在这里等待你确认。');
    el('knowledge-list').querySelectorAll('[data-proposal]').forEach(x=>x.onclick=()=>openProposal(Number(x.dataset.proposal)));
    return;
  }
  el('knowledge-count').textContent=`${entries.length} 篇`;
  el('knowledge-list').innerHTML=entries.length?`<div class="knowledge-list">${entries.map(k=>`<button class="knowledge-row" data-entry="${k.id}"><span class="page-icon">${icon(k.kind==='process'?'file':k.kind==='decision'?'check':'book')}</span><span class="knowledge-info"><div class="knowledge-title">${esc(k.title)}</div><div class="knowledge-preview">${short(k.body,150)}</div><div class="knowledge-meta">${esc(entryProject(k))} · ${esc(scopeNames[k.scope]||'范围待核对')} · ${esc(k.topic||kindNames[k.kind])} · 更新于 ${formatTime(k.updated_at)}</div></span>${k.quality==='noise'?`<span class="state-label grey">已停用</span>`:k.scope==='unknown'?`<span class="state-label warn">范围待核对</span>`:k.needs_review?`<span class="state-label warn">待核对</span>`:k.status==='draft'?`<span class="state-label grey">待确认</span>`:k.status==='archived'?`<span class="state-label grey">已归档</span>`:''}<span class="arrow-right">${icon('chevron')}</span></button>`).join('')}</div>`:
    emptyState('book',state.query?'没有匹配的知识':'这里还没有知识',state.query?'尝试更短的关键词。':state.knowledge.length?'目前没有符合筛选条件的知识。':state.modelReady?'添加工作资料并允许 AI 整理后，知识会自动出现在这里。':'先选择个人版或企业版并完成模型连接，再授权一份工作资料。',!state.knowledge.length?`<button class="btn secondary small" id="goto-sources">前往信息采集 ${icon('arrow')}</button><button class="btn secondary small" id="goto-settings">${state.modelReady?'查看模型设置':'配置模型或加入企业'}</button>`:'');
  el('goto-settings')?.addEventListener('click',()=>go('settings'));
  el('goto-sources')?.addEventListener('click',()=>go('sources'));
  el('knowledge-list').querySelectorAll('[data-entry]').forEach(x=>x.onclick=()=>openKnowledge(Number(x.dataset.entry)));
}
async function openProposal(id){
  const p=state.proposals.find(x=>x.id===id);if(!p)return;
  const actionLabel=p.origin==='curation'?'知识养护建议':({enrich:'补充旧知识',replace:'替换为最新结论',conflict:'处理知识冲突'}[p.action]||'更新知识');
  dialog('核对知识更新',`<div class="proposal-intro"><span class="state-label warn">${actionLabel}</span><p>${esc(p.reason)}</p></div><div class="proposal-grid"><section><h3>当前知识 · v${p.previous_version}</h3><h4>${esc(p.previous_title)}</h4><div class="proposal-body">${esc(p.previous_body)}</div></section><section><h3>AI 建议的新版本</h3><h4>${esc(p.title)}</h4><div class="proposal-body">${esc(p.body)}</div></section></div><div class="source-reference"><h3>来自 ${esc(p.source_title)}</h3><div class="ref-quote">${esc(p.quote)}</div><button class="info-link" id="proposal-read-source">打开原始资料 ${icon('arrow')}</button></div><p class="field-note">${p.origin==='curation'?'整理建议不会立即改写已发布知识，分身仍可引用当前版本。确认后记录历史版本。':'仅供人工核对。接受后才会更新知识，自动记录旧版本；未确认时分身不可引用这篇知识。'}</p>`,
    `<button class="btn secondary" id="proposal-dismiss">忽略此更新</button><button class="btn" id="proposal-accept">确认并更新</button>`,true);
  el('proposal-read-source').onclick=()=>showDocument(p.document_id);
  for(const [button,verb] of [['proposal-accept','accept'],['proposal-dismiss','dismiss']]){
    el(button).onclick=async()=>{
      el(button).disabled=true;
      try{await api(`knowledge/proposals/${id}/${verb}`,{method:'POST'});closeOverlay();notify(verb==='accept'?'已更新知识并保留历史版本':'已忽略更新建议');await go('knowledge',true)}
      catch(e){el(button).disabled=false;notify(e.message)}
    };
  }
}
function closeDrawer(){
  if(!closeOverlay())return;
  const main=document.querySelector('.main-area');
  if(main&&state.scrollPosition)main.scrollTop=state.scrollPosition;
}
async function openKnowledge(id){
  const main=document.querySelector('.main-area');
  if(main)state.scrollPosition=main.scrollTop;
  if(!closeOverlay())return;
  let k=id?state.knowledge.find(x=>x.id===id):null;
  if(id&&!k){try{k=await api(`knowledge-item/${id}`)}catch(e){notify(e.message);return}}
  el('overlay-root').innerHTML=`<div class="drawer-mask" id="drawer-mask"><section class="detail-drawer" role="dialog" aria-modal="true" aria-label="知识详情"><div class="drawer-top"><small>我的知识库 / ${esc(k?entryProject(k):'新知识')}</small><div class="drawer-actions"><button class="icon-button" id="drawer-close" aria-label="关闭">${icon('close')}</button></div></div><div class="drawer-inner" id="drawer-inner"></div><div class="drawer-bottom" id="drawer-bottom"></div></section></div>`;
  el('drawer-mask').onclick=e=>{if(e.target.id==='drawer-mask')closeDrawer()};el('drawer-close').onclick=closeDrawer;
  document.addEventListener('keydown',onEscape);
  const details=el('drawer-inner'),footer=el('drawer-bottom');
  const originalTitle=k?.title||'',originalBody=k?.body||'';
  const sources=k?.evidence||[];
  const relations=k?await api(`knowledge/${k.id}/relations`):null;
  const versions=k?await api(`knowledge/${k.id}/history`):[];
  function linkedSection(){if(!relations)return '';const groups=[['文中链接',relations.outgoing],['提到这篇的知识',relations.backlinks],['同一份资料的其他知识',relations.same_source]];return `<section class="source-reference"><h3>关联知识</h3>${groups.filter(([_,rows])=>rows.length).map(([name,rows])=>`<p class="soft-caption">${name}</p>${rows.map(r=>`<button class="info-link relation-link" data-open-knowledge="${r.id}">${esc(r.title)} ${icon('arrow')}</button>`).join('')}`).join('')||'<p class="soft-caption">暂无已确认的关联。编辑时可用 [[K编号|显示名称]] 添加链接。</p>'}${relations.unresolved_ids.length?'<p class="field-note">部分链接已失效或知识需要复核。</p>':''}</section>`}
  function historySection(){return versions.length?`<section class="source-reference"><h3>变更历史</h3>${versions.map(v=>`<details class="history-version"><summary>v${v.version} · ${formatTime(v.changed_at)} · ${esc(v.title)}</summary><div class="proposal-body">${esc(v.body)}</div></details>`).join('')}</section>`:''}
  function referenceRows(){return sources.length?`<section class="source-reference"><h3>来源依据 <span class="soft-caption">${sources.length} 条</span></h3>${sources.map((e,i)=>`<div class="reference-row"><div class="ref-title">${icon('file')} ${esc(e.document_title)}</div><div class="ref-quote">${short(e.quote,550)}</div>${!e.is_current?'<span class="state-label warn">来源已变更，需要重新核对</span>':e.superseded?'<span class="state-label grey">旧版历史引用</span>':`<button data-read-source="${e.document_id}">查看原始资料 ${icon('arrow')}</button>`}</div>`).join('')}</section>`:''}
  function view(){details.innerHTML=`<div class="drawer-category"><span class="page-icon">${icon('book')}</span> ${esc(kindNames[k?.kind]||'个人知识')}</div><h1 class="drawer-title">${esc(k?.title||'新知识')}</h1><div class="drawer-meta">${k?.needs_review?'<span class="state-label warn">原始依据待核实</span>':k?.status==='confirmed'?'<span class="state-label">已确认</span>':k?.status==='archived'?'<span class="state-label grey">已归档</span>':'<span class="state-label grey">待确认</span>'}<span>${esc(entryProject(k||{evidence:[]}))}</span><span>${k?'版本 '+k.version:''}</span></div><section class="knowledge-scope"><b>适用范围</b><p>${esc(scopeNames[k?.scope]||'范围待核对')} · ${esc(k?.scope_detail||'请核对该要求适用于哪个项目、对象或条件')}</p><b>主题</b><p>${esc(k?.topic||'待核对')}</p>${k?.quality_reason?`<p class="field-note">${esc(k.quality_reason)}</p>`:''}${k?.outcome!=='none'&&outcomeNames[k?.outcome]?`<span class="state-label warn">${esc(outcomeNames[k.outcome])}</span>`:''}</section><div class="drawer-body markdown-body">${k?.rendered_body||esc(k?.body||'')}</div>${referenceRows()}${linkedSection()}${historySection()}`;
    footer.innerHTML=`${k?.status==='archived'?'<button class="btn secondary" id="restore-entry">恢复为待确认</button>':k?'<button class="btn secondary" id="archive-entry">归档知识</button>':''}${k&&(k.kind==='preference'||k.status==='archived')?'<button class="btn danger" id="delete-entry">彻底删除</button>':''}${k&&k.status==='draft'&&k.scope!=='unknown'&&k.quality==='useful'?'<button class="btn secondary" id="confirm-entry">确认内容</button>':''}${k&&k.quality!=='noise'?'<button class="btn secondary" id="disable-entry">停用知识</button>':''}<button class="btn" id="edit-entry">${icon('file')} 编辑内容</button>`;
    details.querySelectorAll('[data-open-knowledge]').forEach(b=>b.onclick=()=>openKnowledge(Number(b.dataset.openKnowledge)));
    details.querySelectorAll('.markdown-body a[href^="#knowledge-"]').forEach(a=>a.onclick=e=>{e.preventDefault();openKnowledge(Number(a.getAttribute('href').slice(11)))});
    el('edit-entry').onclick=edit;
    el('disable-entry')?.addEventListener('click',()=>busy(el('disable-entry'),async()=>{await api('knowledge/disable',{method:'POST',body:{knowledge_ids:[k.id]}});closeOverlay(true);notify('已停用，内容保留但不参与回答');await go('knowledge',true)}));
    el('restore-entry')?.addEventListener('click',()=>busy(el('restore-entry'),async()=>{if(await perform(()=>api(`knowledge/${k.id}/restore`,{method:'POST'}),null)){closeOverlay(true);notify('已恢复为待确认，请核对后再用于分身');await go('knowledge',true)}}));
    el('delete-entry')?.addEventListener('click',()=>busy(el('delete-entry'),async()=>{if(!confirm('彻底删除这篇知识？无法恢复。'))return;if(await perform(()=>api(`knowledge/${k.id}`,{method:'DELETE'}),null)){closeOverlay(true);notify('已删除知识');await go('knowledge',true)}}));
    el('confirm-entry')?.addEventListener('click',()=>save(k.title,k.body,k.kind,'confirmed'));
    el('archive-entry')?.addEventListener('click',async()=>{if(!confirm('将这篇知识归档并从数字分身的可用范围中移除？'))return;await save(k.title,k.body,k.kind,'archived')});
    details.querySelectorAll('[data-read-source]').forEach(x=>x.onclick=()=>showDocument(Number(x.dataset.readSource)));
  }
  async function save(title,body,kind,status,metadata={}){
    if(!title.trim()||!body.trim()){notify('标题和正文不能为空');return}
    try{const payload={title:title.trim(),body:body.trim(),kind,status,...metadata};await api(k?`knowledge/${k.id}`:'knowledge',{method:k?'PUT':'POST',body:payload});closeOverlay(true);notify('知识已保存');await go('knowledge',true)}catch(e){notify(e.message)}
  }
  function edit(){details.innerHTML=`<div class="drawer-category">编辑知识文档</div><div class="field" style="margin-top:20px"><input id="edit-k-title" class="edit-title" maxlength="130" value="${esc(originalTitle)}" placeholder="知识标题"/></div><div class="field"><label for="edit-k-kind">知识类型</label><select id="edit-k-kind">${[['fact','业务知识'],['decision','决策记录'],['process','流程指引'],['preference','个人偏好']].map(([id,name])=>`<option value="${id}" ${k?.kind===id?'selected':''}>${name}</option>`).join('')}</select></div><div class="field"><label for="edit-k-scope">适用范围</label><select id="edit-k-scope">${Object.entries(scopeNames).map(([id,name])=>`<option value="${id}" ${(k?.scope||'global')===id?'selected':''}>${esc(name)}</option>`).join('')}</select><div class="field-note">局部要求只适用于所属项目或本次讨论。仅明确通用的规则选择“跨项目通用”。</div></div><div class="field"><label for="edit-k-project">所属项目 / 讨论名称</label><input id="edit-k-project" maxlength="200" value="${esc(k?.project||'')}" placeholder="如：Mingo 提示词库"/></div><div class="field"><label for="edit-k-topic">主题</label><input id="edit-k-topic" maxlength="100" value="${esc(k?.topic||k?.title||'')}" placeholder="如：提示词交付规范"/></div><div class="field"><label for="edit-k-scope-detail">具体适用对象与条件</label><input id="edit-k-scope-detail" maxlength="500" value="${esc(k?.scope_detail||(!k?'跨项目适用的人工知识':''))}" placeholder="如：仅用于 Mingo 提示词的正式交付版本"/></div><div class="field"><label for="edit-k-quality">内容是否有可复用价值</label><select id="edit-k-quality"><option value="useful" ${k?.quality==='useful'||!k?'selected':''}>有明确价值，可以使用</option><option value="uncertain" ${k?.quality==='uncertain'?'selected':''}>待核对，暂不使用</option><option value="noise" ${k?.quality==='noise'?'selected':''}>低价值，停用</option></select></div><div class="field"><label for="edit-k-body">知识正文</label><textarea id="edit-k-body" class="edit-body" spellcheck="false" placeholder="在这里写下知识内容；支持 Markdown；知识链接写作 [[K编号|显示名称]]。">${esc(originalBody)}</textarea></div><label class="check-row"><input id="edit-k-confirmed" type="checkbox" ${k?.status==='confirmed'||!k?'checked':''}/> <span>标记为内容已核对</span></label>${referenceRows()}`;
    footer.innerHTML=`<button class="btn secondary" id="cancel-edit">取消</button><button class="btn" id="save-entry">保存知识 <span class="kbd-hint">⌘S / Ctrl+S</span></button>`;
    ['edit-k-title','edit-k-body','edit-k-kind','edit-k-confirmed','edit-k-scope','edit-k-project','edit-k-topic','edit-k-scope-detail','edit-k-quality'].forEach(id=>{el(id).addEventListener('input',()=>state.overlayDirty=true);el(id).addEventListener('change',()=>state.overlayDirty=true)});
    el('cancel-edit').onclick=()=>{if(state.overlayDirty&&!confirm('放弃尚未保存的修改？'))return;state.overlayDirty=false;k?view():closeDrawer()};
    el('save-entry').onclick=()=>busy(el('save-entry'),()=>save(el('edit-k-title').value,el('edit-k-body').value,el('edit-k-kind').value,el('edit-k-confirmed').checked?'confirmed':'draft',{scope:el('edit-k-scope').value,project:el('edit-k-project').value,topic:el('edit-k-topic').value||el('edit-k-title').value,scope_detail:el('edit-k-scope-detail').value,quality:el('edit-k-quality').value}));
    details.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='s'){e.preventDefault();el('save-entry')?.click()}});
    details.querySelectorAll('[data-read-source]').forEach(x=>x.onclick=()=>showDocument(Number(x.dataset.readSource)));
  }
  if(k)view();else edit();
}
async function showDocument(id){
  try{
    const [d,projects]=await Promise.all([api(`documents/${id}`),api('projects/verified')]);
    // Preview stacks above the knowledge drawer; closing returns to the editor
    // without discarding unsaved edits.
    const preview=document.createElement('div');preview.className='overlay source-preview';preview.style.zIndex='90';
    preview.innerHTML=`<div class="dialog wide" role="dialog" aria-modal="true" aria-label="原始资料"><div class="dialog-header"><h2>${esc(d.title)}</h2><button class="icon-button" data-close-preview>${icon('close')}</button></div><div class="dialog-body"><p class="soft-caption">${esc(d.relative_path)} · ${esc(d.source_name)}</p><div class="source-content">${esc(d.content)}</div>${d.truncated?'<p class="field-note">仅显示前 80,000 字符。</p>':''}</div><div class="dialog-footer"><button class="btn secondary" data-close-preview>返回知识</button></div></div>`;
    el('overlay-root').appendChild(preview);
    const scopeForm=document.createElement('section');scopeForm.className='source-reference';
    const projectOptions=projects.map(p=>`<option value="${esc(p.project_key)}" ${d.project_verified&&d.project_key===p.project_key?'selected':''}>${esc(p.name)} · ${p.document_count} 份已确认资料</option>`).join('');
    scopeForm.innerHTML=`<h3>核对所属项目</h3><p class="field-note">${d.project_verified?'已确认项目：'+esc(d.project):'目录只是线索，所属业务项目尚未确认。'}仅当整份资料属于同一项目时确认；同名项目不会自动合并。</p><div class="field"><label>项目关系</label><select id="source-existing-project"><option value="">创建独立项目（允许与已有项目同名）</option>${projectOptions}</select></div><div class="field"><label>项目名称</label><input id="source-project" maxlength="200" value="${esc(d.project_verified?d.project:'')}" placeholder="例如：WorkTwin"/></div><button class="btn secondary small" id="save-source-project">确认项目并重新整理此资料</button>`;
    preview.querySelector('.dialog-body').prepend(scopeForm);
    const existingSelect=scopeForm.querySelector('#source-existing-project');
    const projectInput=scopeForm.querySelector('#source-project');
    function reflectProject(){
      const linked=projects.find(p=>p.project_key===existingSelect.value);
      if(linked){projectInput.value=linked.name;projectInput.disabled=true}
      else {projectInput.disabled=false;if(d.project_verified)projectInput.value=''}
    }
    existingSelect.onchange=reflectProject;
    reflectProject();
    scopeForm.querySelector('#save-source-project').onclick=()=>busy(scopeForm.querySelector('#save-source-project'),async()=>{
      const project=projectInput.value.trim();if(!project){notify('请填写项目名称');return}
      const existing_project_key=existingSelect.value||null;
      if(await perform(()=>api(`documents/${id}/scope`,{method:'PUT',body:{project,existing_project_key}}),null)){
        notify('项目已确认，资料已安排重新整理');preview.remove()
      }
    });
    preview.onclick=e=>{if(e.target===preview||e.target.closest('[data-close-preview]'))preview.remove()};
  }catch(e){notify(e.message)}
}

// Twins: each one gets explicit knowledge IDs, never a cloned data store.
async function renderTwins(){
  state.twins=await api('twins');
  if(state.twinId && !state.twins.some(t=>t.id===state.twinId))state.twinId=null;
  if(state.twinId){await renderTwinEditor(state.twinId);return}
  content.innerHTML=pageHeader('DIGITAL TWINS','我的数字分身','为不同协作者创建分身，只选择你愿意交给它使用的知识。',`<button class="btn" id="add-twin">${icon('plus')} 创建数字分身</button>`)+
    (state.twins.length?`<div class="twin-cards">${state.twins.map((t,i)=>`<button class="twin-card" data-twin="${t.id}"><div class="twin-icon">${icon('user')}</div><div class="twin-title">${esc(t.name)}</div><div class="twin-description">${esc(t.description||'只基于你指定的知识提供回答。')}</div><div class="twin-bottom"><span>${t.knowledge_count} 篇可使用知识</span><span>配置分身 ${icon('arrow')}</span></div></button>`).join('')}</div>`:
    emptyState('users','还没有创建数字分身','创建第一个分身，并从你的知识库勾选它能够使用的内容。',`<button class="btn" id="empty-add-twin">创建第一个分身</button>`));
  el('add-twin')?.addEventListener('click',createTwin);
  el('empty-add-twin')?.addEventListener('click',createTwin);
  content.querySelectorAll('[data-twin]').forEach(x=>x.onclick=()=>{state.twinId=Number(x.dataset.twin);go('twins',true)});
}
function createTwin(){
  dialog('创建数字分身',`<div class="field"><label for="twin-name">分身名称</label><input id="twin-name" placeholder="例如：项目交接助手" maxlength="90" autofocus/></div><div class="field"><label for="twin-desc">给它一个用途（可选）</label><textarea id="twin-desc" rows="3" maxlength="500" placeholder="例如：回答关于项目方案、历史决策和操作方法的问题"></textarea></div><div class="permission-note">创建后，通过勾选知识决定分身能知道什么。未勾选的内容不会用于回答。</div>`,
    `<button class="btn secondary" data-close>取消</button><button class="btn" id="submit-twin">创建并选择知识</button>`);
  el('submit-twin').onclick=async()=>{
    const name=el('twin-name').value.trim();if(!name){notify('请输入分身名称');return}
    try{const data=await api('twins',{method:'POST',body:{name,description:el('twin-desc').value.trim()}});closeOverlay();state.twinId=data.id;go('twins',true)}catch(e){notify(e.message)}
  };
}
async function renderTwinEditor(id){
  const [t,knowledge]=await Promise.all([api(`twins/${id}`),loadKnowledge()]);
  state.knowledge=knowledge;
  const eligible=state.knowledge.filter(k=>k.status!=='archived').sort((a,b)=>entryProject(a).localeCompare(entryProject(b),'zh-CN'));
  const selected=new Set(t.knowledge_ids);
  const preview=await api(`twins/${id}/preview`);
  const groups=[...new Set(eligible.map(entryProject))];
  content.innerHTML=`<div class="back-row"><button id="twins-back">← 返回数字分身</button><span>/</span><strong>${esc(t.name)}</strong></div>`+
    pageHeader('DIGITAL TWIN','配置 '+t.name,'只使用已确认、来源有效且允许 AI 使用的授权知识。')+
    `<div class="status-note">已保存授权 ${preview.selected_count} 篇 · 可用于本地问答 ${preview.usable_count} 篇 · 可发布 ${preview.publishable_count} 篇${preview.knowledge.some(k=>k.share_reason)?`<details><summary>查看不能发布的原因</summary>${preview.knowledge.filter(k=>k.share_reason).map(k=>`<p>${esc(k.title)}：${esc(k.share_reason)}</p>`).join('')}</details>`:''}</div>`+
    `<div class="twin-editor"><div class="twin-settings"><h2>基本信息</h2><div class="field"><label for="twin-edit-name">名称</label><input id="twin-edit-name" value="${esc(t.name)}" maxlength="90"/></div><div class="field"><label for="twin-edit-desc">使用场景</label><textarea id="twin-edit-desc" maxlength="500" rows="4">${esc(t.description)}</textarea></div><button class="btn secondary small" id="save-twin-info">保存分身配置</button>
    <div class="divider"></div><h2>分身试问</h2><p class="soft-caption">仅依据右侧已保存的知识回答，不会读取未授权的原始文件。</p><label class="field-note" for="ask-scope">试问范围</label><select id="ask-scope"><option value="local">本地已保存授权</option><option value="published" ${state.shareReady?'':'disabled'}>服务端已发布版本</option></select><div class="field" style="margin-top:12px"><label for="ask-project">所属项目 / 讨论范围</label><select id="ask-project"><option value="">自动识别；范围不明时只用通用知识</option>${[...new Map(eligible.filter(k=>selected.has(k.id)&&k.project_key&&k.scope!=='global').map(k=>[k.project_key,k])).values()].map(k=>`<option value="${esc(k.project_key)}">${esc(entryProject(k))} · ${esc(scopeNames[k.scope])}</option>`).join('')}</select></div><div class="chat-composer"><input class="text-input" id="twin-question" placeholder="问它一个真实工作问题…"/><button class="btn small" id="twin-ask" ${state.modelReady?'':'disabled'}>${icon('arrow')}</button></div>${state.modelReady?'':'<p class="field-note">模型尚未配置，请前往设置完成连接。</p>'}<div id="twin-answer"></div><div class="divider"></div><h2>分享给协作者</h2><div id="sharing-panel">正在读取分享状态…</div><div class="divider"></div><button class="btn danger small" id="delete-twin">删除这个分身</button></div>
    <div class="selection-panel"><div class="selection-head"><b>可使用的知识</b><span class="soft-caption" id="selected-count">已选择 ${selected.size} 篇</span></div><div class="selection-search"><div class="search-bar">${icon('search')}<input type="search" id="twin-search" placeholder="搜索并勾选知识…"/></div><div class="selection-filter-bar"><button type="button" class="selection-filter-btn active" data-twin-filter="all">全部</button><button type="button" class="selection-filter-btn" data-twin-filter="selected">仅已选 (${selected.size})</button><button type="button" class="selection-filter-btn" data-twin-filter="unselected">仅未选</button></div></div><div class="selection-list" id="selection-list"></div><div class="selection-footer"><span class="soft-caption">名称、用途和知识授权一起保存</span><button class="btn" id="save-selections">${icon('check')} 保存授权</button></div></div></div>`;
  el('twins-back').onclick=()=>{if(!confirmDiscard())return;state.dirty=false;state.twinId=null;go('twins',true)};
  let twinFilter='all';
  function updateCounts(){
    el('selected-count').textContent=`已选择 ${selected.size} 篇`;
    content.querySelectorAll('[data-twin-filter="selected"]').forEach(b=>{b.textContent=`仅已选 (${selected.size})`});
  }
  function renderSelection(){
    const q=el('twin-search').value.toLowerCase().trim();
    el('selection-list').innerHTML=eligible.length?groups.map(project=>{
      const list=eligible.filter(k=>entryProject(k)===project &&(k.title+' '+k.body).toLowerCase().includes(q))
        .filter(k=>twinFilter==='all'?true:twinFilter==='selected'?selected.has(k.id):!selected.has(k.id));
      if(!list.length)return '';
      return `<div class="selection-group"><span>${esc(project)} · ${list.length} 篇</span><div class="selection-group-actions"><button type="button" data-select-all="${esc(project)}">全选本组</button><button type="button" data-deselect-all="${esc(project)}">取消</button></div></div>${list.map(k=>`<label class="selection-row"><input type="checkbox" data-select-entry="${k.id}" ${selected.has(k.id)?'checked':''} ${availableToTwin(k)?'':'disabled'}/><span>${esc(k.title)}${!availableToTwin(k)?`<small class="field-note">${esc(k.unavailable_reason)}</small>`:k.share_unavailable_reason?`<small class="field-note">仅本地使用：${esc(k.share_unavailable_reason)}</small>`:''}</span></label>`).join('')}`;
    }).join(''):emptyState('book','暂无可分配知识','请先在知识库生成或创建知识。');
    el('selection-list').querySelectorAll('[data-select-entry]').forEach(b=>b.onchange=()=>{const v=Number(b.dataset.selectEntry);if(b.checked)selected.add(v);else selected.delete(v);state.dirty=true;updateCounts()});
    el('selection-list').querySelectorAll('[data-select-all]').forEach(b=>b.onclick=e=>{
      e.preventDefault();
      const p=b.dataset.selectAll;
      eligible.filter(k=>entryProject(k)===p && availableToTwin(k)).forEach(k=>selected.add(k.id));
      state.dirty=true;updateCounts();renderSelection();
    });
    el('selection-list').querySelectorAll('[data-deselect-all]').forEach(b=>b.onclick=e=>{
      e.preventDefault();
      const p=b.dataset.deselectAll;
      eligible.filter(k=>entryProject(k)===p).forEach(k=>selected.delete(k.id));
      state.dirty=true;updateCounts();renderSelection();
    });
  }
  content.querySelectorAll('[data-twin-filter]').forEach(b=>b.onclick=()=>{
    twinFilter=b.dataset.twinFilter;
    content.querySelectorAll('[data-twin-filter]').forEach(x=>x.classList.toggle('active',x===b));
    renderSelection();
  });
  el('twin-search').oninput=renderSelection;renderSelection();
  async function saveTwin(button){
    if(button.disabled)return;
    const form=content.querySelector('.twin-editor');
    const controls=[...form.querySelectorAll('#twin-edit-name,#twin-edit-desc,#twin-search,[data-select-entry],[data-select-all],[data-deselect-all],[data-twin-filter],#save-twin-info,#save-selections')];
    const disabled=controls.map(control=>control.disabled);
    controls.forEach(control=>control.disabled=true);
    try{
    const name=el('twin-edit-name').value.trim();if(!name){notify('名称不能为空');return}
    const valid=[...selected].filter(v=>availableToTwin(eligible.find(k=>k.id===v)||{}));
    if(await perform(()=>api(`twins/${id}`,{method:'PUT',body:{name,description:el('twin-edit-desc').value,knowledge_ids:valid}}),null)){
      if(!form.isConnected)return;
      state.dirty=false;notify(`分身信息和 ${valid.length} 篇知识授权已保存${valid.length<selected.size?'；不可用知识已移出授权':''}`);await go('twins',true)
    }
    }finally{controls.forEach((control,index)=>{if(control.isConnected)control.disabled=disabled[index]})}
  }
  el('save-selections').onclick=()=>saveTwin(el('save-selections'));
  el('save-twin-info').onclick=()=>saveTwin(el('save-twin-info'));
  watchChanges(el('twin-edit-name'));watchChanges(el('twin-edit-desc'));
  el('delete-twin').onclick=()=>busy(el('delete-twin'),async()=>{if(!confirm('删除这个数字分身并停止其分享？知识库会保留。'))return;if(await perform(()=>api(`twins/${id}`,{method:'DELETE'}),null)){state.dirty=false;state.twinId=null;await go('twins',true)}});
  el('ask-scope').onchange=()=>{el('twin-ask').disabled=el('ask-scope').value==='published'?!state.shareReady:!state.modelReady};
  function renderChat(){el('twin-answer').innerHTML=(state.chats[id]||[]).map(turn=>`<div class="chat-question">${esc(turn.question)} <small>${turn.scope==='published'?'已发布版本':'本地授权'}</small></div>`+(turn.loading?`<div class="chat-loading"><div class="chat-loading-dots"><span></span><span></span><span></span></div><span>正在检索并提炼授权知识…</span></div>`:`<div class="chat-output">${esc(turn.answer)}</div><div class="citation-links">${turn.citations.map(c=>`<button class="info-link" data-citation="${c.knowledge_id}">[K${c.knowledge_id}] ${esc(c.title)}</button>`).join('')}</div>`)).join('');el('twin-answer').querySelectorAll('[data-citation]').forEach(b=>b.onclick=()=>openKnowledge(Number(b.dataset.citation)))}
  renderChat();
  el('twin-question').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing){e.preventDefault();el('twin-ask').click()}});
  el('twin-ask').onclick=()=>busy(el('twin-ask'),async()=>{
    const question=el('twin-question').value.trim();if(question.length<2){notify('请至少输入两个字');return}
    if(state.dirty){notify('请先保存名称、用途和知识授权，再试问');return}
    const scope=el('ask-scope').value;const turn={question,scope,answer:'',citations:[],loading:true};(state.chats[id]||=[]).push(turn);renderChat();
    try{const r=await api(`twins/${id}/ask`,{method:'POST',body:{question,scope,project_key:el('ask-project').value||null}});turn.answer=r.answer;turn.citations=r.citations;turn.loading=false;el('twin-question').value=''}catch(e){turn.answer=e.message;turn.loading=false}renderChat();
  });
  // Bind editing controls before loading optional remote sharing information.
  const mcpPanel=document.createElement('section');mcpPanel.id='mcp-panel';mcpPanel.className='source-reference';
  el('sharing-panel').parentElement.insertBefore(mcpPanel,el('sharing-panel').previousElementSibling);
  await renderMcp(id);
  await renderSharing(id);
}

async function renderMcp(id){
  const panel=el('mcp-panel');if(!panel)return;
  const info=await api(`twins/${id}/mcp`);
  panel.innerHTML=`<h2>连接其他 AI（MCP）</h2><p class="field-note">其他 AI 只能读取这个分身已保存授权中的有效知识和必要引用。未勾选的知识不会提供。仅用于本机，WorkTwin 需保持运行。</p><p>${info.enabled?'已启用':'尚未启用'}</p><button class="btn secondary small" id="generate-mcp">${info.enabled?'更新连接凭据':'启用并获取连接配置'}</button>${info.enabled?'<button class="btn danger small" id="disable-mcp">关闭 MCP</button><div class="field"><label><input type="checkbox" id="allow-mcp-logs" '+(info.allow_logs?'checked':'')+'/> 单独允许读取完整来源日志</label><p class="field-note">默认只提供必要引用。开启后，其他 AI 可读取授权知识引用的整份来源资料或会话，可能包含其他讨论内容。</p></div>':''}`;
  el('generate-mcp').onclick=()=>busy(el('generate-mcp'),async()=>{
    if(state.dirty){notify('请先保存分身的知识授权');return}
    if(info.enabled&&!confirm('更新后，原来的 MCP 连接凭据立即失效，需要重新连接。继续吗？'))return;
    try{
      const r=await api(`twins/${id}/mcp`,{method:'POST'});const config=JSON.stringify(r.config,null,2);
      dialog('MCP 连接配置',`<p class="field-note">把配置添加到支持 HTTP MCP 的 AI 工具。此配置仅在本次显示；遗失后可以更新连接凭据。</p><div class="field"><label>连接地址</label><input readonly value="${esc(r.url)}"/></div><div class="field"><label for="mcp-config">连接配置</label><textarea readonly id="mcp-config" rows="12">${esc(config)}</textarea></div>`,`<button class="btn secondary" data-close>完成</button><button class="btn" id="copy-mcp-config">复制配置</button>`);
      el('copy-mcp-config').onclick=async()=>{try{await navigator.clipboard.writeText(config);notify('已复制连接配置')}catch{el('mcp-config').select();notify('请复制选中的配置')}};
      await renderMcp(id);
    }catch(e){notify(e.message)}
  });
  el('disable-mcp')?.addEventListener('click',()=>busy(el('disable-mcp'),async()=>{if(await perform(()=>api(`twins/${id}/mcp`,{method:'DELETE'}),null)){notify('MCP 已关闭，原连接立即失效');await renderMcp(id)}}));
  el('allow-mcp-logs')?.addEventListener('change',async()=>{const box=el('allow-mcp-logs');box.disabled=true;try{await api(`twins/${id}/mcp/logs`,{method:'PUT',body:{allow_logs:box.checked}});notify(box.checked?'已单独授权完整来源日志':'已停止提供完整日志')}catch(e){box.checked=!box.checked;notify(e.message)}finally{box.disabled=false}});
}

async function openConnection(sharing=false){
  if(state.dirty){notify('请先保存或放弃当前配置，再连接服务');return}
  dialog(sharing?'连接分享服务':'连接企业服务',`<p class="soft-caption">${sharing?'这是可选的远程分享服务。接收者使用服务端配置的模型；本地仍使用你的个人模型。':'向管理员获取企业服务地址和企业 Token。模型由企业统一分配。'}</p><div class="field"><label for="enterprise-url">服务地址</label><input id="enterprise-url" value="${esc(state.settings.enterprise_url||'')}" placeholder="https://worktwin.company.example"/></div><div class="field"><label for="enterprise-token">${sharing?'分享服务 Token':'企业 Token'}</label><input id="enterprise-token" type="password" autocomplete="off"/></div>`,`<button class="btn secondary" data-close>取消</button><button class="btn" id="connect-enterprise">连接</button>`);
  el('connect-enterprise').onclick=()=>busy(el('connect-enterprise'),async()=>{try{await api(sharing?'sharing/connection':'connection',{method:'PUT',body:{url:el('enterprise-url').value.trim(),token:el('enterprise-token').value.trim()}});closeOverlay(true);await refreshConnection();await go(state.page,true);notify(sharing?'分享服务已连接':'企业凭据已验证，请测试模型连接')}catch(e){notify(e.message)}});
}
async function renderSharing(id){
  const panel=el('sharing-panel');if(!panel)return;
  const info=await api(`twins/${id}/sharing`);
  panel.innerHTML=`<p class="field-note">只发布已确认且来源允许分享的知识。持有链接的人可以访问；发布后电脑关闭仍可使用。</p>${info.error?`<p class="state-label warn">${esc(info.error)}</p><button class="btn secondary small" id="sync-sharing">重试同步</button>`:''}`+
    (!info.configured?'<button class="btn secondary small" id="share-connect">连接企业服务</button>':!info.enabled?'<button class="btn secondary small" id="publish-twin">启用分享</button>':`<button class="btn secondary small" id="new-share">创建访问链接</button><button class="btn secondary small" id="unpublish-twin">停止全部分享</button><div>${info.grants.map(g=>`<div class="share-row"><span>${esc(g.recipient)}<small> ${g.revoked?'已撤销':g.expires_at*1000<Date.now()?'已到期':'有效至 '+new Date(g.expires_at*1000).toLocaleDateString()}</small></span>${!g.revoked?`<button class="info-link" data-revoke-share="${esc(g.id)}">撤销</button>`:''}</div>`).join('')}</div>`);
  el('share-connect')?.addEventListener('click',()=>openConnection(state.settings.edition==='personal'));
  el('sync-sharing')?.addEventListener('click',()=>busy(el('sync-sharing'),async()=>{const r=await api('sharing/sync',{method:'POST'});notify(r.state==='synced'?'同步完成':r.detail);await renderSharing(id)}));
  el('publish-twin')?.addEventListener('click',()=>busy(el('publish-twin'),()=>perform(async()=>{await api(`twins/${id}/publish`,{method:'POST'});await renderSharing(id)},null)));
  el('unpublish-twin')?.addEventListener('click',()=>busy(el('unpublish-twin'),()=>perform(async()=>{const r=await api(`twins/${id}/publish`,{method:'DELETE'});notify(r.state==='synced'?'全部分享已停止':r.detail);await renderSharing(id)},null)));
  panel.querySelectorAll('[data-revoke-share]').forEach(b=>b.onclick=()=>busy(b,()=>perform(async()=>{await api(`twins/${id}/sharing/${b.dataset.revokeShare}`,{method:'DELETE'});await renderSharing(id)},null)));
  el('new-share')?.addEventListener('click',()=>{
    dialog('创建访问链接',`<div class="field"><label>给谁使用（备注）</label><input id="share-recipient" maxlength="90" placeholder="例如：产品项目接任者"/></div><div class="field"><label>有效期</label><select id="share-days"><option value="7">7 天</option><option value="30">30 天</option><option value="1">1 天</option></select></div><p class="field-note">备注不构成身份验证，链接持有人即可访问。每位协作者建议使用独立链接，便于撤销。</p>`,`<button class="btn secondary" data-close>取消</button><button class="btn" id="create-share">创建链接</button>`);
    el('create-share').onclick=()=>busy(el('create-share'),async()=>{try{const r=await api(`twins/${id}/sharing`,{method:'POST',body:{recipient:el('share-recipient').value.trim(),days:Number(el('share-days').value)}});dialog('访问链接已创建',`<p class="field-note">复制并发给指定协作者。链接仅在本次显示，遗失后请撤销并重新创建。</p><div class="field"><input readonly id="share-url" value="${esc(r.url)}"/></div>`,`<button class="btn secondary" data-close>完成</button><button class="btn" id="copy-share-url">复制链接</button>`);el('copy-share-url').onclick=async()=>{try{await navigator.clipboard.writeText(r.url);notify('已复制链接')}catch{el('share-url').select();notify('请手动复制选中的链接')}};await renderSharing(id)}catch(e){notify(e.message)}});
  });
}

// Refresh only when no editing is in progress.
setInterval(async()=>{if(state.versionMismatch||el('overlay-root').children.length||state.dirty||/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName))return;await refreshConnection();if(state.page==='knowledge'||state.page==='sources'){try{await go(state.page,true)}catch{}}},15000);
// First render and lightweight refresh, without tracking/analytics.
document.querySelectorAll('.nav-link').forEach(button=>button.onclick=()=>{go(button.dataset.page,true)});
async function startWorkbench(){
  try{
    const running=await api('health');
    if(running.version!==window.__WORKTWIN_VERSION__){
      state.versionMismatch=true;
      el('page-breadcrumb').textContent='启动检查';
      el('model-status').textContent='请重新启动应用';
      content.innerHTML=pageHeader('STARTUP','请重新启动 WorkTwin','新版界面已经打开，后台仍在运行其他版本。退出后重新打开即可继续。')+
        `<section class="settings-section"><p>界面版本：${esc(window.__WORKTWIN_VERSION__)} · 正在运行：${esc(running.version||'未知版本')}</p><ol class="setup-steps"><li>点击左下角「退出」，并确认退出</li><li>关闭这个浏览器页面</li><li>从 Mac「应用程序」或 Windows 开始菜单重新打开 WorkTwin</li></ol><p class="field-note">重新启动不会删除你的资料。</p></section>`;
      return;
    }
  }catch(e){
    content.innerHTML=`<div class="loading-error">无法连接本机 WorkTwin，请从应用程序重新打开。</div>`;
    return;
  }
  await refreshConnection();await go('knowledge',true);
}
startWorkbench();

el('quit-app').onclick=async()=>{if(!confirm('退出 WorkTwin？本地采集和整理将暂停，已发布的数字分身仍可访问。'))return;try{await api('shutdown',{method:'POST'});content.innerHTML=emptyState('check','WorkTwin 已退出','再次打开应用即可继续采集。')}catch(e){notify(e.message)}};

// Edition and model settings remain outside the three primary work areas.
async function renderSettings(){
  await refreshConnection();const info=state.settings;
  content.innerHTML=pageHeader('SETTINGS','设置','个人版使用你自己的模型；企业版使用企业统一分配的模型。')+
    `<section class="settings-section"><h2>使用版本</h2><div class="edition-options">${[['personal','个人版','自己配置模型服务和 API Key'],['enterprise','企业版','使用企业服务地址和企业 Token']].map(([value,name,desc])=>`<button class="edition-option ${info.edition===value?'active':''}" data-edition="${value}"><b>${name}</b><span>${desc}</span></button>`).join('')}</div><p class="field-note">切换版本不会移动或上传你的资料。已有分享时需要先停止分享并确认同步完成。</p></section>`+
    (info.edition==='personal'?`<section class="settings-section"><h2>模型设置</h2><p class="soft-caption">支持 OpenAI Chat Completions 兼容服务。地址和模型名称由你的模型服务商提供。</p><div class="field"><label for="personal-url">模型服务地址</label><input id="personal-url" value="${esc(info.personal_base_url||'')}" placeholder="例如 https://api.openai.com/v1"/><div class="field-note">填写服务商提供的接口根地址，不要填写 /chat/completions。DeepSeek 填 https://api.deepseek.com，模型名称填 deepseek-flash。</div></div><div class="field"><label for="personal-model">模型名称</label><input list="personal-model-options" id="personal-model" value="${esc(info.personal_model||'')}" placeholder="填写服务商提供的模型名称"/></div><div class="field"><label for="personal-key">API Key</label><input id="personal-key" type="password" autocomplete="off" placeholder="${info.has_personal_key?'已安全保存，留空保留原密钥':'填写你自己的模型密钥'}"/><div class="field-note">保存在${esc(info.secret_storage)}中，不会出现在知识导出里。</div></div><div class="model-discovery"><button class="btn secondary small" id="personal-list-models">获取可用模型</button><span class="field-note" id="personal-list-result">填写地址与 API Key 后可获取；也可以直接填写模型名称。</span><datalist id="personal-model-options"></datalist></div><button class="btn" id="save-personal-model">测试连接并保存</button> <button class="btn secondary" id="test-model" ${info.enterprise_model_ready?'':'disabled'}>测试当前模型</button><p id="settings-result" aria-live="polite">${esc(info.model_error||({connected:'模型已连接，可以开始 AI 整理和分身问答',configured:'已保存配置，尚未验证本次启动的模型连接',not_configured:'尚未配置模型，资料仍可本地采集和编辑',error:'模型调用失败，请检查设置'}[info.model_status]||''))}</p></section><section class="settings-section"><h2>远程分享服务</h2><p>本地使用不需要配置。远程分享需要持续运行的服务，接收者使用服务端模型。</p><button class="btn secondary" id="personal-share-connect">${info.cloud_sync?'更改分享服务':'连接分享服务'}</button></section>`:
    `<section class="settings-section"><h2>企业连接</h2><p>向管理员获取企业服务地址和企业 Token。员工不需要模型供应商的 API Key。</p><p>${info.enterprise_url?'当前服务：'+esc(info.enterprise_url):'尚未连接企业服务'}</p><button class="btn" id="settings-enterprise-connect">${info.enterprise_url?'重新连接企业服务':'连接企业服务'}</button> <button class="btn secondary" id="test-model" ${info.enterprise_model_ready?'':'disabled'}>测试企业模型</button><p id="settings-result" aria-live="polite">${esc(info.model_error||({connected:'模型调用已验证',configured:'企业凭据已配置，请测试实际模型调用',not_configured:'请先连接企业服务'}[info.model_status]||''))}</p></section>${info.enterprise_role==='admin'?'<section class="settings-section" id="admin-section"><h2>管理员设置</h2><p>正在读取企业模型与员工授权…</p></section>':''}`)+
    `<section class="settings-section"><h2>开始使用</h2><ol class="setup-steps"><li>完成模型设置或企业连接</li><li>在信息采集中授权一份工作资料，并允许 AI 整理</li><li>查看处理状态，核对生成的知识</li><li>创建分身、勾选已确认知识，再试问</li></ol><button class="btn secondary" id="settings-sources">前往信息采集</button></section><section class="settings-section"><h2>导出与备份</h2><p>Wiki 导出适合阅读与迁移；数据备份用于恢复本机知识、来源和配置，不包含模型密钥或企业 Token。</p><button class="btn secondary" id="export-wiki">导出 Markdown Wiki</button> <button class="btn secondary" id="backup-data">下载数据备份</button><p class="field-note" id="data-path"></p></section>`;
  content.querySelectorAll('[data-edition]').forEach(b=>b.onclick=async()=>{if(b.dataset.edition===info.edition)return;if(!confirmDiscard())return;await busy(b,async()=>{if(await perform(()=>api('edition',{method:'PUT',body:{edition:b.dataset.edition}}),null)){state.dirty=false;await go('settings',true)}})});
  el('settings-enterprise-connect')?.addEventListener('click',()=>openConnection());
  el('personal-share-connect')?.addEventListener('click',()=>openConnection(true));
  if(el('personal-list-models'))attachModelList('personal','model/personal/models');
  el('settings-sources').onclick=()=>go('sources');
  ['personal-url','personal-model','personal-key'].forEach(n=>{if(el(n))watchChanges(el(n))});
  el('save-personal-model')?.addEventListener('click',()=>busy(el('save-personal-model'),async()=>{
    el('settings-result').textContent='正在测试连接，成功后保存…';
    try{await api('model/personal',{method:'PUT',body:{base_url:el('personal-url').value.trim(),model:el('personal-model').value.trim(),api_key:el('personal-key').value.trim()}});state.dirty=false;el('personal-key').value='';await go('settings',true);notify('模型连接成功，已安全保存')}catch(e){el('settings-result').textContent=e.message;notify('未保存新模型配置')}
  }));
  el('test-model')?.addEventListener('click',()=>busy(el('test-model'),async()=>{el('settings-result').textContent='正在测试真实模型调用…';try{await api('model/test',{method:'POST'});el('settings-result').textContent='模型调用成功';await refreshConnection()}catch(e){el('settings-result').textContent=e.message;await refreshConnection()}}));
  el('export-wiki').onclick=()=>busy(el('export-wiki'),()=>downloadFile('export-wiki','WorkTwin-Wiki.zip'));
  el('backup-data').onclick=()=>busy(el('backup-data'),()=>downloadFile('backup','WorkTwin-backup.zip'));
  try{const location=await api('data-location');el('data-path').textContent='本机数据目录：'+location.path+'。恢复前请退出应用；步骤见备份中的恢复说明。'}
  catch(e){el('data-path').textContent='暂时无法读取备份目录信息，模型设置仍可使用。'}
  if(el('admin-section'))await renderAdminSettings();
}
async function downloadFile(path,name){try{const response=await fetch('/api/'+path,{headers:{'X-Worktwin-Token':window.__WORKTWIN_TOKEN__}});if(!response.ok)throw Error('导出失败，请重试');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notify('下载已开始')}catch(e){notify(e.message)}}
async function renderAdminSettings(){
  const panel=el('admin-section');if(!panel)return;
  try{
    const info=await api('admin/settings');
    panel.innerHTML=`<h2>管理员设置</h2><p>只有管理员可以配置企业模型、调用限额及员工 Token。</p><div class="field"><label for="admin-url">企业模型服务地址</label><input id="admin-url" value="${esc(info.base_url)}"/></div><div class="field"><label for="admin-model">默认模型名称</label><input list="admin-model-options" id="admin-model" value="${esc(info.model)}"/></div><div class="field"><label for="admin-key">模型 API Key</label><input id="admin-key" type="password" autocomplete="off" placeholder="${info.has_api_key?'已保存，留空保留原密钥':'填写企业的模型 API Key'}"/><div class="field-note">密钥保存在企业服务端，员工无法读取。</div></div><div class="model-discovery"><button class="btn secondary small" id="admin-list-models">获取企业可用模型</button><span class="field-note" id="admin-list-result">也可以直接填写供应商提供的模型名称。</span><datalist id="admin-model-options"></datalist></div><div class="limit-fields">${[['daily_calls','每天最多调用次数'],['daily_tokens','每天最多 Token 数'],['minute_calls','每人每分钟最多调用次数']].map(([id,label])=>`<div class="field"><label for="admin-${id}">${label}</label><input id="admin-${id}" type="number" min="1" value="${info[id]}"/></div>`).join('')}</div><button class="btn" id="save-admin-model">测试连接并保存企业配置</button><p id="admin-result" aria-live="polite"></p><h3>员工 Token</h3><p class="field-note">新增或重新分配会生成新的 Token。撤销员工 Token 时，其已有分享链接也会停用。</p><div class="field"><label for="employee-identity">员工标识</label><input id="employee-identity" placeholder="例如 employee-a 或员工邮箱"/></div><button class="btn secondary" id="issue-employee">生成员工 Token</button><div id="issued-token"></div><div class="employee-list">${info.employees.map(e=>`<div class="job-row"><span>${esc(e.identity)}</span><span>${e.enabled?'有效':'已撤销'}</span>${e.enabled?`<button class="btn danger small" data-revoke-employee="${esc(e.identity)}">撤销</button>`:''}</div>`).join('')}</div>`;
    attachModelList('admin','admin/models');
    ['admin-url','admin-model','admin-key','admin-daily_calls','admin-daily_tokens','admin-minute_calls'].forEach(n=>watchChanges(el(n)));
    el('save-admin-model').onclick=()=>busy(el('save-admin-model'),async()=>{el('admin-result').textContent='正在测试企业模型…';try{await api('admin/model',{method:'PUT',body:{base_url:el('admin-url').value.trim(),model:el('admin-model').value.trim(),api_key:el('admin-key').value.trim(),daily_calls:Number(el('admin-daily_calls').value),daily_tokens:Number(el('admin-daily_tokens').value),minute_calls:Number(el('admin-minute_calls').value)}});state.dirty=false;el('admin-key').value='';await go('settings',true);notify('企业模型与限额已保存')}catch(e){el('admin-result').textContent=e.message}});
    el('issue-employee').onclick=()=>busy(el('issue-employee'),async()=>{if(state.dirty){notify('请先保存企业模型配置');return}const identity=el('employee-identity').value.trim();if(!identity){notify('请填写员工标识');return}if(info.employees.some(e=>e.identity===identity)&&!confirm('重新生成将立即使该员工原 Token 失效，确定继续？'))return;try{const r=await api('admin/employees',{method:'POST',body:{identity}});await renderAdminSettings();el('issued-token').innerHTML=`<p>新 Token 仅在这里显示一次。请复制后交给对应员工。</p><input class="text-input" id="employee-token-value" readonly value="${esc(r.token)}"/><button class="btn secondary small" id="copy-employee-token">复制 Token</button>`;el('copy-employee-token').onclick=async()=>{try{await navigator.clipboard.writeText(r.token);notify('已复制')}catch{el('employee-token-value').select();notify('请复制选中的 Token')}}}catch(e){notify(e.message)}});
    panel.querySelectorAll('[data-revoke-employee]').forEach(b=>b.onclick=()=>busy(b,async()=>{if(state.dirty){notify('请先保存企业模型配置');return}if(!confirm(`撤销 ${b.dataset.revokeEmployee} 的 Token，并停止其分享链接？`))return;if(await perform(()=>api('admin/employees/'+encodeURIComponent(b.dataset.revokeEmployee),{method:'DELETE'}),null))await renderAdminSettings()}));
  }catch(e){panel.innerHTML=`<h2>管理员设置</h2><p>${esc(e.message)}</p>`}
}
el('settings-link').onclick=()=>go('settings');

function attachModelList(prefix,path){
  el(prefix+'-list-models').onclick=()=>busy(el(prefix+'-list-models'),async()=>{
    const url=el(prefix+'-url').value.trim(),key=el(prefix+'-key').value.trim();const result=el(prefix+'-list-result');
    result.textContent='正在获取模型列表…';
    try{const data=await api(path,{method:'POST',body:{base_url:url,api_key:key}});
      if(!result.isConnected||el(prefix+'-url').value.trim()!==url||el(prefix+'-key').value.trim()!==key)return;
      el(prefix+'-model-options').innerHTML=data.models.map(id=>`<option value="${esc(id)}"></option>`).join('');
      result.textContent=data.models.length?`获取到 ${data.models.length} 个模型，点击模型名称输入框选择。选择后仍需测试连接并保存。`:'服务没有返回模型列表，请手动填写模型名称。';
    }catch(e){if(result.isConnected)result.textContent=e.message}
  });
}
