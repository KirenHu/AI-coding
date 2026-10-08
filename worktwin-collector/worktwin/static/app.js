/* WorkTwin: deliberately small, native-feeling, dependency-free client. */
const icon = n => `<svg aria-hidden="true"><use href="#i-${n}"/></svg>`;
const esc = x => String(x ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
const kindNames={fact:'业务知识',decision:'决策记录',process:'流程指引',preference:'偏好'};
const titles={knowledge:'我的知识库',twins:'我的数字分身',sources:'信息采集'};
const state={page:'knowledge',knowledge:[],sources:[],twins:[],project:'all',kind:'all',query:'',twinId:null,modelReady:false,proposals:[]};
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
async function perform(fn,refresh){try{await fn();if(refresh)await go(refresh,true)}catch(e){notify('操作未完成：'+e.message)}}
function dialog(title,body,footer,wide=false){
  el('overlay-root').innerHTML=`<div class="overlay" id="overlay"><div class="dialog ${wide?'wide':''}" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="dialog-header"><h2>${esc(title)}</h2><button class="icon-button" data-close aria-label="关闭">${icon('close')}</button></div><div class="dialog-body">${body}</div>${footer?`<div class="dialog-footer">${footer}</div>`:''}</div></div>`;
  el('overlay').onclick=e=>{if(e.target.id==='overlay'||e.target.closest('[data-close]'))closeOverlay()};
  document.addEventListener('keydown',onEscape);
}
function onEscape(e){if(e.key==='Escape')closeOverlay()}
function closeOverlay(){el('overlay-root').innerHTML='';document.removeEventListener('keydown',onEscape)}
async function refreshConnection(){try{const info=await api('settings');state.modelReady=!!info.enterprise_model_ready;const node=el('model-status');node.innerHTML=`<span class="pulse-dot ${state.modelReady?'':'off'}"></span>${state.modelReady?'企业知识模型已连接':'等待企业配置知识模型'}`}catch(e){el('model-status').textContent='本地服务不可用'}}
async function go(page,force=false){
  if(!force && page===state.page)return;
  state.page=page;
  document.querySelectorAll('.nav-link').forEach(node=>node.classList.toggle('active',node.dataset.page===page));
  el('page-breadcrumb').textContent=titles[page];
  content.innerHTML='<div class="loading">正在读取本地数据…</div>';
  try{
    if(page==='knowledge')await renderKnowledge();
    else if(page==='sources')await renderSources();
    else if(page==='twins')await renderTwins();
  }catch(e){content.innerHTML=`<div class="loading-error">无法加载：${esc(e.message)} <button class="btn secondary small" id="retry">重试</button></div>`;el('retry').onclick=()=>go(page,true)}
}

// Information sources: category + explicit capture / enterprise-model permission.
const typeLabel={folder:'本地工作文件',codex:'Codex 对话',claude:'Claude Code 对话'};
function sourceType(s){return s.adapter||s.kind}
async function renderSources(){
  const [sources,stats]=await Promise.all([api('sources'),api('stats')]);state.sources=sources;
  const count=k=>sources.filter(s=>sourceType(s)===k).length;
  const types=[['folder','工作文件', 'Word、PDF、Markdown、代码等授权目录','folder'],['codex','Codex','历史会话与后续产生的对话','book'],['claude','Claude Code','历史会话与后续产生的对话','spark']];
  content.innerHTML=pageHeader('INFORMATION SOURCES','信息采集','只采集你允许的工作资料。随时暂停，也可以彻底撤销授权。',`<button class="btn secondary" id="scan">${icon('refresh')} 立即检查更新</button>`)+
    `<div class="source-overview">${types.map(([kind,name,desc,ico])=>`<div class="source-type"><div class="type-symbol">${icon(ico)}</div><b>${name}</b><small>${desc}</small><button class="btn secondary small" data-add-source="${kind}">${icon('plus')} ${count(kind)?'再添加':'授权采集'}</button></div>`).join('')}</div>`+
    `<div class="group-heading"><h2>已授权的数据范围</h2><span class="soft-caption">${sources.length} 个数据源</span></div>`+
    `<div class="card">${sources.length?sources.map(sourceRow).join(''):emptyState('folder','还没有授权任何数据源','选择上方的信息类型，授权工作目录后即可自动、增量采集。')}</div>`+
    `<div class="scan-status"><span>系统会自动检测文件变化 · 最近扫描：${esc(stats.last_scan)}</span><span>${stats.documents} 份已索引资料 · ${stats.ai_jobs.queued} 项待整理</span></div>`+
    `<div class="status-note" style="margin-top:23px">${icon('shield')}<div><b>采集权限与 AI 处理权限分开控制。</b> 本地采集不会自动上传原始文件；只有启用“允许 AI 整理”的数据源，才会在定时任务中把相关文本发送给企业模型网关。停止采集保留本地知识；彻底移除会删除该来源及其派生知识。</div></div>`;
  el('scan').onclick=()=>perform(()=>api('scan',{method:'POST'}),'sources').then(()=>notify('已安排检查更新'));
  content.querySelectorAll('[data-add-source]').forEach(b=>b.onclick=()=>addSource(b.dataset.addSource));
  content.querySelectorAll('[data-collect-toggle]').forEach(b=>b.onchange=()=>perform(()=>api(`sources/${b.dataset.collectToggle}/toggle`,{method:'POST'}),'sources'));
  content.querySelectorAll('[data-ai-toggle]').forEach(b=>b.onchange=()=>perform(()=>api(`sources/${b.dataset.aiToggle}/ai`,{method:'PUT',body:{allow_ai:b.checked}}),'sources'));
  content.querySelectorAll('[data-remove-source]').forEach(b=>b.onclick=async()=>{
    const id=Number(b.dataset.removeSource);const row=state.sources.find(x=>x.id===id);
    if(!confirm(`彻底移除「${row?.name||'数据源'}」？这会删除其索引、AI 提炼知识以及相关分身的知识授权，无法撤回。`))return;
    await perform(()=>api(`sources/${id}`,{method:'DELETE'}),'sources');
  });
}
function sourceRow(s){const kind=sourceType(s);return `<div class="source-item"><div class="source-summary"><div class="source-name">${esc(s.name)} <span class="state-label ${s.enabled?'':'grey'}">${s.enabled?'采集中':'已暂停'}</span></div><div class="source-path" title="${esc(s.root)}">${esc(s.root)}</div><div class="source-caption"><span class="soft-caption">${esc(typeLabel[kind]||'工作资料')} · ${s.document_count} 份资料</span>${s.last_error?`<span class="state-label danger">${esc(s.last_error)}</span>`:''}</div></div>
  <label class="permission-cell"><input class="toggle" type="checkbox" aria-label="允许采集 ${esc(s.name)}" data-collect-toggle="${s.id}" ${s.enabled?'checked':''}/> 允许采集</label>
  <label class="permission-cell"><input class="toggle" type="checkbox" aria-label="允许企业 AI 整理 ${esc(s.name)}" data-ai-toggle="${s.id}" ${s.allow_ai?'checked':''}/> 允许 AI 整理</label>
  <button class="icon-button" title="撤销来源并清除知识" aria-label="删除 ${esc(s.name)}" data-remove-source="${s.id}">${icon('trash')}</button></div>`}
async function addSource(initial='folder'){
  const defaults=await api('default-paths');
  dialog('授权信息采集',`<div class="choice-tabs">${[['folder','工作文件'],['codex','Codex'],['claude','Claude Code']].map(([type,name])=>`<button class="choice-tab" data-type="${type}">${name}</button>`).join('')}</div>
    <div class="field"><label for="new-source-name">数据源名称</label><input id="new-source-name" autocomplete="off"/></div>
    <div class="field"><label for="new-source-path">授权文件夹</label><div style="display:flex;gap:9px"><input id="new-source-path" autocomplete="off" spellcheck="false"/><button class="btn secondary" id="browse-folder" type="button">选择…</button></div><div class="field-note" id="path-note"></div></div>
    <label class="check-row"><input id="new-source-ai" type="checkbox"/> <span><b>允许企业 AI 自动整理这些资料</b><br/>内容将发送到企业配置的模型服务，自动提炼可搜索的知识；不勾选则只在本地建立索引。</span></label>
    <div class="permission-note">后续仅采集这个已授权目录及其子目录；默认忽略密钥、.env、node_modules 和 .git 等内容。你可以随时撤销授权。</div>`,
    `<button class="btn secondary" data-close>取消</button><button class="btn" id="save-source">授权并开始采集</button>`);
  let kind=initial;
  function choose(type){kind=type;el('new-source-name').value={folder:'工作文件',codex:'Codex 对话',claude:'Claude Code 对话'}[type];el('new-source-path').value=type==='codex'?defaults.codex:type==='claude'?defaults.claude:'';el('path-note').textContent=type==='folder'?'请明确选择允许采集的工作目录。':`默认位置${(type==='codex'?defaults.codex_exists:defaults.claude_exists)?'已检测到':'尚未发现'}，你也可以自行修改。`;document.querySelectorAll('[data-type]').forEach(x=>x.classList.toggle('active',x.dataset.type===type))}
  document.querySelectorAll('[data-type]').forEach(x=>x.onclick=()=>choose(x.dataset.type));choose(initial);
  el('browse-folder').onclick=async()=>{try{const result=await api('pick-folder',{method:'POST'});el('new-source-path').value=result.path}catch(e){notify(e.message)}};
  el('save-source').onclick=async()=>{
    const body={name:el('new-source-name').value.trim(),root:el('new-source-path').value.trim(),kind,allow_ai:el('new-source-ai').checked};
    if(!body.name||!body.root){notify('请填写名称并选择工作目录');return}
    try{await api('sources',{method:'POST',body});closeOverlay();notify('已授权，首次采集将在后台开始');go('sources',true)}catch(e){notify(e.message)}
  };
}

// Knowledge: Notion-like collection sidebar + page list + editable document pane.
function visibleKnowledge(){return state.knowledge.filter(k=>k.kind!=='preference' && k.status!=='archived')}
function entryProject(k){return k.evidence?.find(x=>x.project)?.project||'我的笔记'}
function availableToTwin(k){return k.status!=='archived'&&!k.needs_review&&(!k.source_bound||k.evidence?.some(x=>x.is_current&&!x.superseded))}
async function renderKnowledge(){
  [state.knowledge,state.proposals]=await Promise.all([api('knowledge?limit=1000'),api('knowledge/proposals')]);
  const visible=visibleKnowledge(),projects=[...new Set(visible.map(entryProject))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
  if(state.project!=='all'&&state.project!=='reviews'&&!projects.includes(state.project))state.project='all';
  content.innerHTML=pageHeader('KNOWLEDGE LIBRARY','我的知识库','工作中的结论、经验和流程，自动沉淀为可阅读、可编辑的知识。',`<button class="btn secondary" id="create-knowledge">${icon('plus')} 新建知识</button>`)+
    `<div class="library-shell"><aside class="library-sidebar"><h3>知识空间</h3>
    <button class="library-choice ${state.project==='all'?'active':''}" data-project="all">${icon('book')} <span class="truncate">全部知识</span><span class="count">${visible.length}</span></button>
    <button class="library-choice ${state.project==='reviews'?'active':''}" data-project="reviews">${icon('alert')} <span class="truncate">待核对更新</span><span class="count">${state.proposals.length}</span></button>
    <h3 style="margin-top:24px">项目与分类</h3>${projects.map((name,index)=>`<button class="library-choice ${state.project===name?'active':''}" data-project-index="${index}">${icon('folder')}<span class="truncate">${esc(name)}</span><span class="count">${visible.filter(k=>entryProject(k)===name).length}</span></button>`).join('')}</aside>
    <div class="library-main"><div class="library-toolbar"><div class="search-bar">${icon('search')} <input type="search" id="knowledge-search" aria-label="搜索知识" placeholder="搜索知识标题或正文…" value="${esc(state.query)}"/></div><span class="knowledge-count" id="knowledge-count"></span></div>
    <div class="filters">${[['all','全部'],['fact','知识'],['decision','决策'],['process','流程']].map(([type,name])=>`<button class="filter-button ${state.kind===type?'active':''}" data-filter="${type}">${name}</button>`).join('')}</div><div id="knowledge-list"></div></div></div>`;
  content.querySelectorAll('[data-project]').forEach(x=>x.onclick=()=>{state.project=x.dataset.project;renderKnowledgeList()});
  content.querySelectorAll('[data-project-index]').forEach(x=>x.onclick=()=>{state.project=projects[Number(x.dataset.projectIndex)];renderKnowledgeList()});
  content.querySelectorAll('[data-filter]').forEach(x=>x.onclick=()=>{state.kind=x.dataset.filter;renderKnowledgeList()});
  el('knowledge-search').oninput=e=>{state.query=e.target.value;renderKnowledgeList()};
  el('create-knowledge').onclick=()=>openKnowledge(null);
  renderKnowledgeList();
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
  el('knowledge-list').innerHTML=entries.length?`<div class="knowledge-list">${entries.map(k=>`<button class="knowledge-row" data-entry="${k.id}"><span class="page-icon">${icon(k.kind==='process'?'file':k.kind==='decision'?'check':'book')}</span><span class="knowledge-info"><div class="knowledge-title">${esc(k.title)}</div><div class="knowledge-preview">${short(k.body,150)}</div><div class="knowledge-meta">${esc(entryProject(k))} · ${esc(kindNames[k.kind])} · 更新于 ${formatTime(k.updated_at)}</div></span>${k.needs_review?`<span class="state-label warn">来源待核实</span>`:k.status==='draft'?`<span class="state-label grey">AI 整理</span>`:''}<span class="arrow-right">${icon('chevron')}</span></button>`).join('')}</div>`:
    emptyState('book',state.query?'没有匹配的知识':'这里还没有知识',state.query?'尝试更短的关键词。':state.knowledge.length?'目前没有符合筛选条件的知识。':state.modelReady?'添加工作资料并允许企业 AI 整理后，知识会自动出现在这里。':'请先授权数据源；企业管理员配置模型网关后，将自动生成知识。',!state.knowledge.length?`<button class="btn secondary small" id="goto-sources">前往信息采集 ${icon('arrow')}</button>`:'');
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
function closeDrawer(){closeOverlay()}
async function openKnowledge(id){
  const k=id?state.knowledge.find(x=>x.id===id):null;
  el('overlay-root').innerHTML=`<div class="drawer-mask" id="drawer-mask"><section class="detail-drawer" role="dialog" aria-modal="true" aria-label="知识详情"><div class="drawer-top"><small>我的知识库 / ${esc(k?entryProject(k):'新知识')}</small><div class="drawer-actions"><button class="icon-button" id="drawer-close" aria-label="关闭">${icon('close')}</button></div></div><div class="drawer-inner" id="drawer-inner"></div><div class="drawer-bottom" id="drawer-bottom"></div></section></div>`;
  el('drawer-mask').onclick=e=>{if(e.target.id==='drawer-mask')closeDrawer()};el('drawer-close').onclick=closeDrawer;
  document.addEventListener('keydown',onEscape);
  const details=el('drawer-inner'),footer=el('drawer-bottom');
  const originalTitle=k?.title||'',originalBody=k?.body||'';
  const sources=k?.evidence||[];
  function referenceRows(){return sources.length?`<section class="source-reference"><h3>来源依据 <span class="soft-caption">${sources.length} 条</span></h3>${sources.map((e,i)=>`<div class="reference-row"><div class="ref-title">${icon('file')} ${esc(e.document_title)}</div><div class="ref-quote">${short(e.quote,550)}</div>${!e.is_current?'<span class="state-label warn">来源已变更，需要重新核对</span>':e.superseded?'<span class="state-label grey">旧版历史引用</span>':`<button data-read-source="${e.document_id}">查看原始资料 ${icon('arrow')}</button>`}</div>`).join('')}</section>`:''}
  function view(){details.innerHTML=`<div class="drawer-category"><span class="page-icon">${icon('book')}</span> ${esc(kindNames[k?.kind]||'个人知识')}</div><h1 class="drawer-title">${esc(k?.title||'新知识')}</h1><div class="drawer-meta">${k?.status==='confirmed'?'<span class="state-label">已确认</span>':k?.needs_review?'<span class="state-label warn">原始依据待核实</span>':'<span class="state-label grey">自动整理</span>'}<span>${esc(entryProject(k||{evidence:[]}))}</span><span>${k?'版本 '+k.version:''}</span></div><div class="drawer-body">${esc(k?.body||'')}</div>${referenceRows()}`;
    footer.innerHTML=`${k?'<button class="btn secondary" id="archive-entry">归档知识</button>':''}<button class="btn" id="edit-entry">${icon('file')} 编辑内容</button>`;
    el('edit-entry').onclick=edit;
    el('archive-entry')?.addEventListener('click',async()=>{if(!confirm('将这篇知识归档并从数字分身的可用范围中移除？'))return;await save(k.title,k.body,k.kind,'archived')});
    details.querySelectorAll('[data-read-source]').forEach(x=>x.onclick=()=>showDocument(Number(x.dataset.readSource)));
  }
  async function save(title,body,kind,status){
    if(!title.trim()||!body.trim()){notify('标题和正文不能为空');return}
    try{const payload={title:title.trim(),body:body.trim(),kind,status};await api(k?`knowledge/${k.id}`:'knowledge',{method:k?'PUT':'POST',body:payload});closeDrawer();notify('知识已保存');await go('knowledge',true)}catch(e){notify(e.message)}
  }
  function edit(){details.innerHTML=`<div class="drawer-category">编辑知识文档</div><div class="field" style="margin-top:20px"><input id="edit-k-title" class="edit-title" maxlength="130" value="${esc(originalTitle)}" placeholder="知识标题"/></div><div class="field"><label for="edit-k-kind">知识类型</label><select id="edit-k-kind">${[['fact','业务知识'],['decision','决策记录'],['process','流程指引']].map(([id,name])=>`<option value="${id}" ${k?.kind===id?'selected':''}>${name}</option>`).join('')}</select></div><div class="field"><label for="edit-k-body">知识正文</label><textarea id="edit-k-body" class="edit-body" spellcheck="false" placeholder="在这里写下知识内容；支持 Markdown。">${esc(originalBody)}</textarea></div><label class="check-row"><input id="edit-k-confirmed" type="checkbox" ${k?.status==='confirmed'||!k?'checked':''}/> <span>标记为内容已核对</span></label>${referenceRows()}`;
    footer.innerHTML=`<button class="btn secondary" id="cancel-edit">取消</button><button class="btn" id="save-entry">保存知识</button>`;
    el('cancel-edit').onclick=()=>k?view():closeDrawer();
    el('save-entry').onclick=()=>save(el('edit-k-title').value,el('edit-k-body').value,el('edit-k-kind').value,el('edit-k-confirmed').checked?'confirmed':'draft');
    details.querySelectorAll('[data-read-source]').forEach(x=>x.onclick=()=>showDocument(Number(x.dataset.readSource)));
  }
  if(k)view();else edit();
}
async function showDocument(id){
  try{
    const d=await api(`documents/${id}`);
    // Preview stacks above the knowledge drawer; closing returns to the editor
    // without discarding unsaved edits.
    const preview=document.createElement('div');preview.className='overlay';preview.style.zIndex='90';
    preview.innerHTML=`<div class="dialog wide" role="dialog" aria-modal="true" aria-label="原始资料"><div class="dialog-header"><h2>${esc(d.title)}</h2><button class="icon-button" data-close-preview>${icon('close')}</button></div><div class="dialog-body"><p class="soft-caption">${esc(d.relative_path)} · ${esc(d.source_name)}</p><div class="source-content">${esc(d.content)}</div>${d.truncated?'<p class="field-note">仅显示前 80,000 字符。</p>':''}</div><div class="dialog-footer"><button class="btn secondary" data-close-preview>返回知识</button></div></div>`;
    el('overlay-root').appendChild(preview);
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
  const [t,knowledge]=await Promise.all([api(`twins/${id}`),api('knowledge?limit=1000')]);
  state.knowledge=knowledge;
  const eligible=visibleKnowledge().filter(availableToTwin).sort((a,b)=>entryProject(a).localeCompare(entryProject(b),'zh-CN'));
  const selected=new Set(t.knowledge_ids.filter(x=>eligible.some(k=>k.id===x)));
  const groups=[...new Set(eligible.map(entryProject))];
  content.innerHTML=`<div class="back-row"><button id="twins-back">← 返回数字分身</button><span>/</span><strong>${esc(t.name)}</strong></div>`+
    pageHeader('DIGITAL TWIN','配置 '+t.name,'这个分身只能阅读右侧勾选的知识。可以随时修改或撤销授权。')+
    `<div class="twin-editor"><div class="twin-settings"><h2>基本信息</h2><div class="field"><label for="twin-edit-name">名称</label><input id="twin-edit-name" value="${esc(t.name)}" maxlength="90"/></div><div class="field"><label for="twin-edit-desc">使用场景</label><textarea id="twin-edit-desc" maxlength="500" rows="4">${esc(t.description)}</textarea></div><button class="btn secondary small" id="save-twin-info">保存基本信息</button>
    <div class="divider"></div><h2>分身试问</h2><p class="soft-caption">仅依据右侧已保存的知识回答，不会读取未授权的原始文件。</p><div class="chat-composer"><input class="text-input" id="twin-question" placeholder="问它一个真实工作问题…"/><button class="btn small" id="twin-ask" ${state.modelReady?'':'disabled'}>${icon('arrow')}</button></div>${state.modelReady?'':'<p class="field-note">企业模型网关尚未配置，暂不能进行问答。</p>'}<div id="twin-answer"></div><div class="divider"></div><button class="btn danger small" id="delete-twin">删除这个分身</button></div>
    <div class="selection-panel"><div class="selection-head"><b>可使用的知识</b><span class="soft-caption" id="selected-count">已选择 ${selected.size} 篇</span></div><div class="selection-search"><div class="search-bar">${icon('search')}<input type="search" id="twin-search" placeholder="搜索并勾选知识…"/></div></div><div class="selection-list" id="selection-list"></div><div class="selection-footer"><span class="soft-caption">更改后请保存授权</span><button class="btn" id="save-selections">${icon('check')} 保存授权</button></div></div></div>`;
  el('twins-back').onclick=()=>{state.twinId=null;go('twins',true)};
  function renderSelection(){
    const q=el('twin-search').value.toLowerCase().trim();
    el('selection-list').innerHTML=eligible.length?groups.map(project=>{
      const list=eligible.filter(k=>entryProject(k)===project &&(k.title+' '+k.body).toLowerCase().includes(q));
      if(!list.length)return '';
      return `<div class="selection-group">${esc(project)} · ${list.length} 篇</div>${list.map(k=>`<label class="selection-row"><input type="checkbox" data-select-entry="${k.id}" ${selected.has(k.id)?'checked':''}/><span>${esc(k.title)}</span></label>`).join('')}`;
    }).join(''):emptyState('book','暂无可分配知识','请先在知识库生成或创建知识。');
    el('selection-list').querySelectorAll('[data-select-entry]').forEach(b=>b.onchange=()=>{const v=Number(b.dataset.selectEntry);if(b.checked)selected.add(v);else selected.delete(v);el('selected-count').textContent=`已选择 ${selected.size} 篇`});
  }
  el('twin-search').oninput=renderSelection;renderSelection();
  el('save-selections').onclick=async()=>{try{await api(`twins/${id}/knowledge`,{method:'PUT',body:{knowledge_ids:[...selected]}});notify(`已保存 ${selected.size} 篇知识授权`);go('twins',true)}catch(e){notify(e.message)}};
  el('save-twin-info').onclick=async()=>{const name=el('twin-edit-name').value.trim();if(!name){notify('名称不能为空');return}await perform(()=>api(`twins/${id}`,{method:'PUT',body:{name,description:el('twin-edit-desc').value}}),'twins');notify('分身信息已保存')};
  el('delete-twin').onclick=async()=>{if(!confirm('确定删除这个数字分身吗？知识库本身不会受到影响。'))return;await perform(()=>api(`twins/${id}`,{method:'DELETE'}),null);state.twinId=null;go('twins',true)};
  el('twin-ask').onclick=async()=>{const question=el('twin-question').value.trim();if(!question)return;el('twin-ask').disabled=true;el('twin-answer').innerHTML='<div class="chat-output">正在依据已授权的知识查找…</div>';try{const r=await api(`twins/${id}/ask`,{method:'POST',body:{question}});el('twin-answer').innerHTML=`<div class="chat-output">${esc(r.answer)}</div><div class="field-note">已提供 ${r.context_count} 篇授权知识 · 回答引用 ${r.citations.length} 篇</div>`}catch(e){el('twin-answer').innerHTML=`<div class="chat-output">${esc(e.message)}</div>`}finally{el('twin-ask').disabled=false}};
}

// First render and lightweight refresh, without tracking/analytics.
document.querySelectorAll('.nav-link').forEach(button=>button.onclick=()=>{if(button.dataset.page!=='twins')state.twinId=null;go(button.dataset.page,true)});
refreshConnection().then(()=>go('knowledge',true));
