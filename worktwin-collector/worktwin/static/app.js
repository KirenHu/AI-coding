/* WorkTwin: deliberately small, native-feeling, dependency-free client. */
const icon = n => `<svg aria-hidden="true"><use href="#i-${n}"/></svg>`;
const esc = x => String(x ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
const kindNames={fact:'业务知识',decision:'决策记录',process:'流程指引',preference:'个人偏好'};
const titles={knowledge:'知识库',twins:'数字分身',sources:'知识来源',settings:'设置'};
const state={page:'knowledge',knowledge:[],sources:[],twins:[],project:'all',group:'',kind:'all',query:'',twinId:null,modelReady:false,shareReady:false,proposals:[],settings:{},lifecycle:'active',chats:{},dirty:false,overlayDirty:false,scrollPosition:0,sourceTab:'folders',browserTab:'sites',twinTab:'grants'};
const el=id=>document.getElementById(id);
const content=el('page-content');
let toastTimer;
function notify(message){const t=el('toast');t.textContent=message;t.className='show';clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.className='',3500)}
function offerRefresh(message='内容已发生变化，请刷新查看最新结果。'){
  let notice=el('refresh-notice');
  if(!notice){notice=document.createElement('div');notice.id='refresh-notice';notice.setAttribute('role','status');document.body.appendChild(notice);}
  notice.innerHTML=`<span>${esc(message)}</span><button class="btn secondary small" id="refresh-content">刷新内容</button><button class="icon-button" id="dismiss-refresh" aria-label="关闭提示">${icon('close')}</button>`;
  el('dismiss-refresh').onclick=()=>notice.remove();
  el('refresh-content').onclick=async()=>{
    if(!closeOverlay())return;
    await go(state.page,true);
  };
}
function formatTime(value){return value?String(value).replace('T',' ').slice(0,16):'—'}
function localUpdateTime(value){
  if(!value)return '—';
  const utc=String(value).replace(' ','T');
  const parsed=new Date(utc.endsWith('Z')||/[+-]\d\d:\d\d$/.test(utc)?utc:utc+'Z');
  return Number.isNaN(parsed.getTime())?formatTime(value):
    parsed.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',
      hour:'2-digit',minute:'2-digit',hour12:false});
}
function short(value,length=90){const s=String(value||'').replace(/\s+/g,' ').trim();return esc(s.length>length?s.slice(0,length)+'…':s)}
function pageHeader(eyebrow,title,description,action=''){return `<header class="page-title-row"><h1 class="page-title">${esc(title)}</h1><div class="page-actions">${action}</div></header>`}
function emptyState(ico,title,description,action=''){return `<div class="empty-state"><div class="empty-graphic">${icon(ico)}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`}
async function api(path,options={}){
  const opts={method:options.method||'GET',headers:{'X-Worktwin-Token':window.__WORKTWIN_TOKEN__},...options};
  if(options.body && typeof options.body==='object'){
    opts.body=JSON.stringify(options.body);
    opts.headers={...opts.headers,'Content-Type':'application/json'};
  }
  const response=await fetch('/api/'+path,opts);
  if((response.status===404||response.status===409&&path.startsWith('knowledge/'))&&opts.method!=='GET')offerRefresh('内容已变化或不存在，请刷新后重试；未保存的输入仍保留。');
  if(!response.ok){let reason=`请求失败 (${response.status})`;try{const data=await response.json();reason=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail)}catch{}const error=new Error(reason);error.status=response.status;throw error}
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
    const node=el('model-status');const labels={reading_credentials:'等待钥匙串确认',not_configured:'模型未配置',configured:'已配置 · 尚未测试',connected:'模型连接正常',error:'模型调用失败'};
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
  const samePage=state.page===page;
  el('refresh-notice')?.remove();
  state.page=page;
  document.querySelectorAll('.nav-link').forEach(node=>node.classList.toggle('active',node.dataset.page===page));
  el('page-breadcrumb').textContent=titles[page];
  if(!samePage||!content.children.length)content.innerHTML='<div class="loading">正在读取本地数据…</div>';
  try{
    if(page==='knowledge')await renderKnowledge();
    else if(page==='sources')await renderSources();
    else if(page==='twins')await renderTwins();
    else if(page==='settings')await renderSettings();
  }catch(e){content.innerHTML=`<div class="loading-error">无法加载：${esc(e.message)} <button class="btn secondary small" id="retry">重试</button></div>`;el('retry').onclick=()=>go(page,true)}
}

// Information sources: category + explicit capture / enterprise-model permission.
const typeLabel={folder:'本地文件夹',codex:'Codex',claude:'Claude Code',cursor:'Cursor CLI'};
function sourceName(s){const kind=sourceType(s);if(kind==='folder')return s.root.split(/[\\/]/).filter(Boolean).pop()||s.name;const legacy={folder:['工作文件','本地工作文件'],codex:['Codex 对话'],claude:['Claude Code 对话']};return legacy[kind]?.includes(s.name)?typeLabel[kind]:s.name}
function sourceType(s){return s.adapter||s.kind}
async function renderSources(){
  const [sources,stats,jobs,capture]=await Promise.all([api('sources'),api('stats'),api('ai/jobs'),api('browser-capture')]);
  state.sources=sources;
  const actions=state.sourceTab==='folders'?`<button class="btn" id="add-folder">${icon('plus')} 添加文件夹</button>`:state.sourceTab==='browser'?`<button class="btn" id="add-website">${icon('plus')} 添加网站</button>`:'';
  content.innerHTML=pageHeader('','知识来源','',actions)+`<div class="tabs source-tabs" role="tablist">${[['folders','文件夹'],['assistants','编程助手'],['browser','浏览器记录']].map(([key,name])=>`<button role="tab" aria-selected="${state.sourceTab===key}" class="${state.sourceTab===key?'active':''}" data-source-tab="${key}">${name}</button>`).join('')}</div><div id="source-content"></div>`;
  content.querySelectorAll('[data-source-tab]').forEach(b=>b.onclick=()=>{state.sourceTab=b.dataset.sourceTab;go('sources',true)});
  const pane=el('source-content');
  if(state.sourceTab==='browser'){
    pane.innerHTML=`<div class="section-toolbar"><span class="state-label ${capture.extension_connected?'':'grey'}">插件${capture.extension_connected?'已连接':'未连接'}</span><span class="muted">${capture.active_tasks} 个进行中任务</span>${capture.extension_update_required?'<span class="state-label warn">插件需升级，网站持续采集尚不可用</span>':''}<div class="toolbar-fill"></div><button class="btn secondary" id="browser-extension-guide">插件设置</button><button class="btn secondary" id="browser-flow-settings">流程平台设置</button></div><div class="tabs" role="tablist">${[['sites','网站白名单'],['tasks','平台任务'],['history','采集记录']].map(([key,name])=>`<button role="tab" aria-selected="${state.browserTab===key}" class="${state.browserTab===key?'active':''}" data-browser-tab="${key}">${name}</button>`).join('')}</div><div id="browser-sites"></div><div id="browser-records"></div>`;
    content.querySelectorAll('[data-browser-tab]').forEach(b=>b.onclick=()=>{state.browserTab=b.dataset.browserTab;go('sources',true)});
    el('browser-extension-guide').onclick=browserExtensionGuide;
    el('browser-flow-settings').onclick=()=>dialog('流程平台设置',`<section class="settings-group"><h3>连接状态</h3><p>${capture.flow_configured?'已配置可信流程平台':'尚未配置可信流程平台'}</p></section><section class="settings-group"><h3>任务采集</h3><p>平台下发任务和网站范围，开始任务时开启采集，任务结束后停止该任务的记录。</p><p class="field-note">平台连接由部署配置 WORKTWIN_CAPTURE_FLOW_ORIGIN 与签名密钥管理。</p></section>`,`<button class="btn secondary" data-close>关闭</button>`);
    el('add-website').onclick=addWebsite;
    if(state.browserTab==='sites')await renderWebsiteList();
    else await renderBrowserRecords(0);
    return;
  }
  const folders=sources.filter(s=>sourceType(s)==='folder'),processing=processingCounts(stats);
  pane.innerHTML=`<div class="section-toolbar"><span class="muted" id="source-count">${state.sourceTab==='folders'?folders.length+' 个文件夹':'按工具选择本机会话'}</span><div class="toolbar-fill"></div><button class="btn secondary" id="scan">${icon('refresh')} ${state.sourceTab==='assistants'?'检测并检查更新':'检查更新'}</button></div><div class="source-table" id="sources-list"></div><div class="scan-status"><span>最近检查 ${esc(stats.last_scan||'尚未检查')}</span><button class="info-link" id="show-processing">${processingText(processing)}</button></div>`;
  if(state.sourceTab==='folders'){
    el('add-folder').onclick=()=>addSource();
    el('sources-list').innerHTML=folders.length?`<div class="source-table-head"><span>文件夹</span><span>采集</span><span>AI 使用</span><span>状态</span><span></span></div>${folders.map(sourceRow).join('')}`:emptyState('folder','选择工作资料所在的文件夹','',`<button class="btn" id="empty-add-folder">添加文件夹</button>`);
    el('empty-add-folder')?.addEventListener('click',()=>addSource());
  }else{
    const assistants=await api('sources/assistants');
    el('sources-list').innerHTML=`<div class="source-table-head"><span>编程助手</span><span>采集</span><span>AI 使用</span><span>状态</span><span></span></div>${assistants.map(a=>`<div class="source-item"><div class="source-summary"><b>${esc(a.name)}</b><div class="source-path">${a.tool==='antigravity'?'成果与知识文档 · 不含完整对话':a.tool==='cursor'?'CLI 导出会话 · 默认整理新增':'本机会话 · 默认整理新增'}</div></div><label class="permission-cell"><input type="checkbox" data-assistant="${a.tool}" aria-label="采集 ${esc(a.name)}" ${a.selected?'checked':''}></label><label class="permission-cell"><input class="toggle" type="checkbox" aria-label="允许 AI 使用 ${esc(a.name)}" data-ai-toggle="${a.source_id||''}" ${a.allow_ai?'checked':''} ${a.source_id?'':'disabled'}></label><span class="source-state">${a.detected?a.document_count+' 份资料':a.selected?(a.tool==='antigravity'?'等待成果文档':'等待会话记录'):'尚未检测到记录'}${a.waiting_tasks?`<small>${a.waiting_tasks} 个任务等待结束</small>`:''}${a.legacy_sources.length?`<small>保留 ${a.legacy_sources.length} 个原范围</small>`:''}</span><button class="icon-button" data-assistant-detail="${a.tool}" aria-label="${esc(a.name)} 来源设置">${icon('ellipsis')}</button></div>`).join('')}`;
    pane.querySelectorAll('[data-assistant]').forEach(b=>b.onchange=()=>busy(b,async()=>{if(!await perform(()=>api('sources/assistants/'+b.dataset.assistant,{method:'PUT',body:{enabled:b.checked}}),'sources'))b.checked=!b.checked}));
    pane.querySelectorAll('[data-assistant-detail]').forEach(b=>b.onclick=()=>assistantDetail(assistants.find(a=>a.tool===b.dataset.assistantDetail)));
  }
  bindSourceControls(pane);
  el('scan').onclick=()=>busy(el('scan'),async()=>{if(await perform(()=>api('scan',{method:'POST'}),'sources'))notify('已安排检查更新')});
  el('show-processing').onclick=()=>processingDetails(jobs);
  if(processing.queued||processing.running)window.__JOB_POLL__=setInterval(async()=>{
    const current=processingCounts(await api('stats'));
    const footer=el('show-processing');if(!footer)return;
    footer.textContent=processingText(current);
    if(!current.queued&&!current.running){clearInterval(window.__JOB_POLL__);window.__JOB_POLL__=null;offerRefresh(current.error?'整理已结束，部分任务失败。':current.waiting?'本轮整理已结束，部分内容等待补充。':'文章整理已完成，刷新查看结果。')}
  },3500);
}
function bindSourceControls(node){
  bindFileIssues(node);
  node.querySelectorAll('[data-collect-toggle]').forEach(b=>b.onchange=()=>busy(b,async()=>{if(!await perform(()=>api(`sources/${b.dataset.collectToggle}/toggle`,{method:'POST'}),'sources'))b.checked=!b.checked}));
  node.querySelectorAll('[data-ai-toggle]').forEach(b=>b.onchange=()=>busy(b,async()=>{if(!await perform(()=>api(`sources/${b.dataset.aiToggle}/ai`,{method:'PUT',body:{allow_ai:b.checked}}),'sources'))b.checked=!b.checked}));
  node.querySelectorAll('[data-source-detail]').forEach(b=>b.onclick=()=>sourceDetail(state.sources.find(s=>s.id===Number(b.dataset.sourceDetail))));
}
function processingCounts(stats){return {running:stats.ai_jobs.running+stats.article_jobs.running,error:stats.ai_jobs.error+stats.article_jobs.error,queued:stats.ai_jobs.queued+(stats.article_jobs.queued||0),waiting:stats.article_jobs.waiting}}
function processingText(counts){return `${counts.queued?counts.queued+' 项待整理 · ':''}${counts.running} 项整理中 · ${counts.error} 项失败`}
async function processingDetails(jobs,source=null){
  const rows=jobs.filter(j=>j.state!=='done'&&(!source||j.source_id===source.id));
  const [articles,stats]=source?[[],null]:await Promise.all([api('articles/jobs'),api('stats')]);
  const queuedArticles=stats?.article_jobs.queued||0;
  const articleRows=articles.map(j=>`<div class="job-row"><span>${esc(j.project||'文章维护')}${j.task?` · ${esc(j.task)}`:''}<small>${esc(localUpdateTime(j.updated_at))}</small></span><span>${{queued:'等待'+(j.task||'文章维护'),running:j.task?'正在'+j.task:'正在维护文章',waiting:'等待更多材料',error:j.task?j.task+'失败':'文章整理失败'}[j.state]}${j.error?`<small>${esc(j.error)}</small>`:''}</span></div>`).join('');
  dialog(source?source.name+' · 整理任务':'整理任务',`${!state.modelReady?'<p>配置模型后可整理已授权资料。</p>':''}${rows.map(j=>`<div class="job-row"><span>${esc(j.title)}<small>${esc(j.source_name)}</small></span><span>${{queued:'等待提取',running:'正在提取',error:'提取失败'}[j.state]}${j.error?`<small>${esc(j.error)}</small>`:''}</span></div>`).join('')}${articleRows}${queuedArticles?`<p class="field-note" id="queued-article-jobs">${queuedArticles} 个项目等待整理。</p>`:''}${!rows.length&&!articles.length&&!queuedArticles?'<p class="muted">没有待处理任务</p>':''}`,`<button class="btn secondary" data-close>关闭</button>${rows.some(j=>j.state==='error')||articles.some(j=>j.state==='error')?'<button class="btn" id="retry-ai">重试失败任务</button>':''}`,true);
  el('retry-ai')?.addEventListener('click',()=>busy(el('retry-ai'),async()=>{if(await perform(()=>api('ai/jobs/retry'+(source?'?source_id='+source.id:''),{method:'POST'}))){closeOverlay();await go('sources',true)}}));
}
async function articleSourceAccess(id,onDone){
  const sources=await api(`knowledge/${id}/sources`);
  if(dialog('来源授权',`<p class="field-note">允许 AI 使用后，相关文本可用于知识整理和分身回答，并发送到已配置的模型。</p><div class="source-access-list">${sources.map(s=>`<label class="setting-row"><span><b>${esc(sourceName(s))}</b><small class="source-path">${esc(s.root)}</small></span><input class="toggle" type="checkbox" aria-label="允许 AI 使用 ${esc(sourceName(s))}" data-access-source="${s.id}" ${s.allow_ai?'checked':''}></label>`).join('')}</div>`,`<button class="btn" id="source-access-done">完成</button>`)===false)return;
  el('overlay-root').querySelectorAll('[data-access-source]').forEach(b=>b.onchange=()=>busy(b,async()=>{
    try{await api(`sources/${b.dataset.accessSource}/ai`,{method:'PUT',body:{allow_ai:b.checked}});notify('来源授权已更新')}
    catch(e){b.checked=!b.checked;notify(e.message)}
  }));
  el('source-access-done').onclick=async()=>{closeOverlay();await onDone()};
  el('overlay-root').querySelector('[data-close]').onclick=async e=>{e.stopPropagation();closeOverlay();await onDone()};
}
function sourceDetail(s){
  dialog(sourceName(s),`<section class="settings-group"><h3>采集范围</h3><p class="source-path">${s.collection_scope==='assistant'?(sourceType(s)==='antigravity'?'读取本机 brain 和 knowledge 中的 Markdown 成果与知识文档；不读取完整对话、缓存和插件文件':sourceType(s)==='cursor'?'自动识别本机 Cursor 目录中的 CLI JSONL 导出，不读取 IDE 对话':'读取本机公开会话记录，默认仅整理开启 AI 使用之后的新增对话'):esc(s.root)}</p><p>${s.document_count} 份资料</p><p>${s.file_issues?.length?sourceFileStatus(s):esc(s.last_error||'')}</p></section><section class="settings-group"><h3>处理与分享</h3><label class="setting-row"><span>允许 AI 使用<small>用于知识整理和分身回答，相关文本会发送到已配置的模型</small></span><input class="toggle" id="source-detail-ai" type="checkbox" ${s.allow_ai?'checked':''}></label><label class="setting-row"><span>允许分身分享</span><input class="toggle" id="source-detail-share" type="checkbox" ${s.allow_share?'checked':''}></label></section><section class="settings-group"><h3>整理与记录</h3><button class="btn secondary" id="source-jobs">整理任务</button> ${['codex','claude','cursor'].includes(sourceType(s))?'<button class="btn secondary" id="source-history">补充历史记录</button>':''}<button class="btn secondary" id="source-references">规则与记忆文件</button> <button class="btn danger" id="source-remove">移除来源数据</button></section>`,`<button class="btn secondary" data-close>关闭</button>`);
  bindFileIssues(el('overlay-root'));
  el('source-detail-ai').onchange=async e=>{if(await perform(()=>api(`sources/${s.id}/ai`,{method:'PUT',body:{allow_ai:e.target.checked}}))){s.allow_ai=e.target.checked;await go('sources',true)}else e.target.checked=!e.target.checked};
  el('source-detail-share').onchange=async e=>{if(await perform(()=>api(`sources/${s.id}/share`,{method:'PUT',body:{allow_share:e.target.checked}}))){s.allow_share=e.target.checked;notify('分享权限已更新')}else e.target.checked=!e.target.checked};
  el('source-history')?.addEventListener('click',()=>assistantHistory(s));
  el('source-references').onclick=()=>referenceFiles(s);
  el('source-jobs').onclick=async()=>processingDetails(await api('ai/jobs?source_id='+s.id),s);
  el('source-remove').onclick=async()=>{if(!confirm(`移除「${sourceName(s)}」的索引及派生知识？原始文件会保留。`))return;if(await perform(()=>api(`sources/${s.id}`,{method:'DELETE'}),'sources'))closeOverlay()};
}
function assistantHistory(s){
  dialog('补充历史记录',`<section class="settings-group"><h3>整理范围</h3><p class="field-note">选择已采集会话的日期范围。日常新增整理继续保持开启。</p><div class="field"><label for="history-start">开始日期（UTC）</label><input id="history-start" placeholder="2026-10-01" inputmode="numeric"/></div><div class="field"><label for="history-end">结束日期（UTC）</label><input id="history-end" placeholder="2026-10-13" inputmode="numeric"/></div><p id="history-result" role="status"></p></section>`,`<button class="btn secondary" data-close>取消</button><button class="btn" id="history-submit">开始整理</button>`);
  el('history-submit').onclick=()=>busy(el('history-submit'),async()=>{try{const r=await api(`sources/${s.id}/history`,{method:'POST',body:{start:el('history-start').value.trim(),end:el('history-end').value.trim()}});el('history-result').textContent=r.events?`已提交 ${r.documents} 个会话、${r.events} 条记录。`:'此范围内没有已采集的记录。请先检查更新。'}catch(e){el('history-result').textContent=e.message}});
}
async function referenceFiles(s){
  try{const rows=await api(`sources/${s.id}/references`);dialog('规则与记忆文件',`<p class="field-note">这些文件单独保留，不作为项目业务事实或已执行的成果。</p>${rows.length?rows.map(r=>`<details class="settings-group"><summary>${esc(r.relative_path)}</summary><p class="field-note">${esc({project_rules:'项目规则',skill:'技能说明',memory:'记忆与偏好'}[r.role]||r.role)} · ${esc(r.scope_path==='personal'?'个人范围':r.scope_path)}</p><pre style="white-space:pre-wrap">${esc(r.content)}</pre></details>`).join(''):'<p class="muted">此来源暂无规则或记忆文件。</p>'}`,`<button class="btn secondary" data-close>关闭</button>`)}catch(e){notify(e.message)}
}
function assistantDetail(a){
  const legacy=a.legacy_sources.length?`<section class="settings-group"><h3>原范围记录</h3>${a.legacy_sources.map(source=>`<button class="btn secondary" data-legacy-source="${source.id}">${esc(source.name)} · ${source.document_count} 份</button>`).join('')}<p class="field-note">原范围记录仍保留，可分别管理与移除。</p></section>`:'';
  if(a.source_id){sourceDetail(state.sources.find(s=>s.id===a.source_id));el('overlay-root').querySelector('.dialog-body').insertAdjacentHTML('beforeend',legacy)}
  else dialog(a.name,`<section class="settings-group"><h3>采集范围</h3><p>${a.tool==='antigravity'?'读取本机 brain 和 knowledge 中的 Markdown 成果与知识文档，不含完整对话、运行日志和插件缓存。':a.tool==='cursor'?'自动识别本机 Cursor 目录中的 CLI JSONL 导出，不读取 IDE 对话':'此助手在本机的全部可识别会话。'}</p><p class="muted">${a.tool==='antigravity'?(a.detected?'已检测到 '+a.detection.recognized+' 份成果文档，勾选后开始采集':'尚未检测到成果文档，可先勾选等待新文档'):(a.detected?'已检测到会话，勾选后开始采集':'尚未检测到会话，可以先勾选并等待新记录')}</p></section>${legacy}`,`<button class="btn secondary" data-close>关闭</button>`);
  el('overlay-root').querySelectorAll('[data-legacy-source]').forEach(b=>b.onclick=()=>sourceDetail(state.sources.find(s=>s.id===Number(b.dataset.legacySource))));
}
async function renderWebsiteList(){
  const sites=await api('browser-capture/sites');
  const labels={off:'未采集',paused:'采集已暂停',waiting_extension:'等待插件连接',needs_permission:'请在插件中授权此网站',waiting_page:'等待访问网站',capturing:'采集中'};
  el('browser-sites').innerHTML=sites.length?`<div class="website-table"><div class="website-row table-head"><span>允许记录的网站</span><span>采集</span><span>状态</span><span></span></div>${sites.map(s=>`<div class="website-row"><span><b>${esc(s.origin)}</b><small>${s.manual_enabled?'人工开启 · 持续采集':s.task_requests?s.task_requests+' 个平台任务':'已加入白名单'}</small></span><label><input class="toggle" type="checkbox" aria-label="采集 ${esc(s.origin)}" data-website-toggle="${s.id}" ${s.enabled?'checked':''}></label><span>${labels[s.status]}</span><button class="icon-button" data-website-remove="${s.id}" aria-label="移除 ${esc(s.origin)}">${icon('trash')}</button></div>`).join('')}</div>`:emptyState('book','还没有允许记录的网站','添加网站后，可由人工或流程平台开启采集。');
  el('browser-sites').querySelectorAll('[data-website-toggle]').forEach(b=>b.onchange=()=>busy(b,async()=>{if(!await perform(()=>api(`browser-capture/sites/${b.dataset.websiteToggle}`,{method:'PUT',body:{enabled:b.checked}}),'sources'))b.checked=!b.checked}));
  el('browser-sites').querySelectorAll('[data-website-remove]').forEach(b=>b.onclick=async()=>{if(confirm('移除此网站并停止采集？历史记录会保留。'))await perform(()=>api(`browser-capture/sites/${b.dataset.websiteRemove}`,{method:'DELETE'}),'sources')});
}
function addWebsite(){
  dialog('添加网站',`<div class="field"><label for="website-origin">网站地址</label><input id="website-origin" type="url" placeholder="https://example.com"><p class="field-note">仅限此网站，不包含其他子域名。添加后默认不采集。</p></div>`,`<button class="btn secondary" data-close>取消</button><button class="btn" id="save-website">添加</button>`);
  el('save-website').onclick=()=>busy(el('save-website'),async()=>{if(await perform(()=>api('browser-capture/sites',{method:'POST',body:{url:el('website-origin').value.trim()}}))){closeOverlay();state.browserTab='sites';await go('sources',true)}});
}
let browserRecordsPage=0;
const captureStatuses={armed:'等待目标网页',capturing:'记录中',completed:'已结束',
  navigation_stopped:'已离开授权网站',tab_closed:'标签页已关闭',disabled:'已停止',
  expired:'已过期',status_unavailable:'任务连接中断'};
function captureDate(value){return value?new Date(value*1000).toLocaleString():'—'}
async function renderBrowserRecords(page=0){
  browserRecordsPage=page;
  const holder=el('browser-records');
  if(!holder)return;
  const rows=await api('browser-capture/sessions?limit=20&offset='+(page*20)+(state.browserTab==='tasks'?'&mode=task':''));
  const reports=await Promise.all(rows.map(async row=>{
    const report=await api('browser-capture/sessions/'+encodeURIComponent(row.id)+'/summary');
    return {...row,report};
  }));
  holder.innerHTML=reports.length?reports.map(row=>`
    <section class="source-item" style="display:block;padding:14px 0">
      <div style="display:flex;justify-content:space-between;gap:10px;align-items:center">
        <b>${esc(row.mode==='manual'?'手动 · '+row.page_key:'任务 · '+row.task_id)}</b>
        <span class="state-label ${row.status==='capturing'?'':'grey'}">${esc(captureStatuses[row.status]||row.status)}</span>
      </div>
      <div class="soft-caption">${captureDate(row.created_at)} · ${row.event_count} 个事件</div>
      <p style="margin:8px 0">${esc(row.report.summary||'尚未检测到操作')}</p>
      <button class="btn secondary small" data-capture-session="${esc(row.id)}">查看操作详情</button>
    </section>`).join(''):'<p class="field-note">暂无网页操作记录。开始记录指定网站后，这里将展示每次采集的结果。</p>';
  holder.innerHTML+=`<div style="display:flex;gap:10px;margin-top:12px">
    ${page>0?'<button class="btn secondary small" id="browser-record-prev">上一页</button>':''}
    ${rows.length===20?'<button class="btn secondary small" id="browser-record-next">下一页</button>':''}
    <button class="btn secondary small" id="browser-record-refresh">刷新记录</button></div>`;
  holder.querySelectorAll('[data-capture-session]').forEach(b=>b.onclick=()=>browserSessionDetails(b.dataset.captureSession));
  el('browser-record-prev')?.addEventListener('click',()=>renderBrowserRecords(page-1));
  el('browser-record-next')?.addEventListener('click',()=>renderBrowserRecords(page+1));
  el('browser-record-refresh')?.addEventListener('click',()=>renderBrowserRecords(page));
}
async function browserCaptureHistory(){
  state.sourceTab='browser';state.browserTab='history';
  await go('sources',true);
  await renderBrowserRecords(browserRecordsPage);
  el('browser-records')?.scrollIntoView({behavior:'smooth',block:'start'});
}
async function browserSessionDetails(sessionId,offset=0){
  const [summary,events,siteManual]=await Promise.all([
    api('browser-capture/sessions/'+encodeURIComponent(sessionId)+'/summary'),
    api('browser-capture/sessions/'+encodeURIComponent(sessionId)+'/events?limit=100&offset='+offset),
    api('browser-capture/sessions/'+encodeURIComponent(sessionId)+'/site-manual')
  ]);
  const kindNames={click:'点击',change:'修改',submit:'尝试提交',feedback:'页面提示',
    navigation:'页面导航',tab_closed:'关闭标签页'};
  const body=`<p class="soft-caption">采集方式：${summary.mode==='manual'?'手动网站采集':'流程任务采集'} ·
    ${esc(captureStatuses[summary.capture_status]||summary.capture_status)} · ${summary.event_count} 条事件</p>
    <section class="source-reference"><h3>本次操作摘要</h3>
      <p>${esc(summary.summary||'尚未生成操作摘要')}</p>
      <small class="soft-caption">依据事件：${esc((summary.evidence_seq||[]).join('、')||'暂无')} ·
      ${summary.summary_source==='model'?'AI 概括':'本地规则概括'}</small>
      <div style="margin-top:10px"><button class="btn secondary small" id="capture-resummarize">刷新摘要</button>
      ${siteManual.knowledge_id?`<button class="btn secondary small" id="capture-open-guide">查看网站操作手册</button>`:''}</div>
    </section>
    <section class="source-reference"><h3>完整操作时间线</h3>
      ${events.length?events.map(e=>`<div style="padding:10px 0;border-bottom:1px solid #eee">
        <div><b>#${e.seq} · ${esc(kindNames[e.kind]||e.kind)}</b>
        <small class="soft-caption"> · ${captureDate(e.at)}</small></div>
        <p style="margin:5px 0">${esc(e.label||'无控件标签')}</p>
        <small class="soft-caption">${esc(e.location||'')}</small></div>`).join(''):'<p>此页没有操作事件。</p>'}
      <div style="display:flex;gap:10px;margin-top:12px">
        ${offset>0?'<button class="btn secondary small" id="capture-previous-events">上一页</button>':''}
        ${events.length===100?'<button class="btn secondary small" id="capture-more-events">下一页</button>':''}
      </div>
    </section><p class="field-note">仅保留脱敏动作、控件标签及页面反馈；不采集输入值、完整 DOM 或视频。</p>`;
  dialog('网页操作详情',body,'',true);
  el('capture-open-guide')?.addEventListener('click',async()=>{
    closeOverlay(true);
    await go('knowledge',true);
    await openKnowledge(siteManual.knowledge_id);
  });
  el('capture-resummarize')?.addEventListener('click',async()=>{
    await api('browser-capture/sessions/'+encodeURIComponent(sessionId)+'/summarize',{method:'POST'});
    await browserSessionDetails(sessionId,offset);
  });
  el('capture-previous-events')?.addEventListener('click',()=>browserSessionDetails(sessionId,offset-100));
  el('capture-more-events')?.addEventListener('click',()=>browserSessionDetails(sessionId,offset+100));
}

async function browserExtensionGuide(){
  const info=await api('browser-capture');
  dialog('浏览器插件',`<section class="settings-group"><h3>采集控制</h3><label class="setting-row"><span>浏览器采集<small>关闭后暂停记录，保留网站设置和配对</small></span><input class="toggle" id="browser-capture-enabled" type="checkbox" ${info.enabled?'checked':''}></label></section><section class="settings-group"><h3>安装与连接</h3><dl class="detail-list"><dt>连接状态</dt><dd>${info.extension_connected?'已连接':'未连接'}</dd><dt>插件版本</dt><dd>${esc(info.extension_version||'旧版或尚未连接')}${info.extension_update_required?' · 请下载新版并重新加载':''}</dd></dl><div class="button-row"><button class="btn secondary" id="browser-extension-download">下载插件</button><button class="btn secondary" id="browser-extension-pair">生成配对码</button><button class="btn secondary" id="browser-extension-unpair">解除配对</button></div><p id="browser-extension-code" class="field-note"></p><details><summary>安装说明</summary><ol class="setup-steps"><li>在 Chrome 或 Edge 扩展管理页启用开发者模式，加载解压后的插件目录。</li><li>在插件中输入配对码，连接本机 WorkTwin。</li><li>在目标网站打开插件，授予该网站访问权限。</li></ol></details></section><section class="settings-group"><h3>AI 整理</h3><label class="setting-row"><span>允许 AI 使用操作记录<small>仅发送脱敏后的操作记录</small></span><input class="toggle" id="browser-capture-ai" type="checkbox" ${info.allow_ai?'checked':''}></label></section>`,`<button class="btn secondary" data-close>关闭</button>`,true);
  el('browser-capture-enabled').onchange=e=>busy(e.target,async()=>{if(await perform(()=>api('browser-capture',{method:'PUT',body:{enabled:e.target.checked}})))await go('sources',true);else e.target.checked=!e.target.checked});
  el('browser-capture-ai').onchange=e=>busy(e.target,async()=>{if(!await perform(()=>api('browser-capture/ai',{method:'PUT',body:{allow_ai:e.target.checked}})))e.target.checked=!e.target.checked});
  el('browser-extension-download').onclick=()=>downloadFile('browser-capture/extension','WorkTwin-Browser-Extension.zip');
  el('browser-extension-pair').onclick=()=>busy(el('browser-extension-pair'),async()=>{try{const r=await api('browser-capture/pairing',{method:'POST'});el('browser-extension-code').textContent='配对码（5 分钟有效）：'+r.code}catch(e){notify(e.message)}});
  el('browser-extension-unpair').onclick=async()=>{if(confirm('解除插件配对并停止当前采集？网站和历史记录会保留。')&&await perform(()=>api('browser-capture/unpair',{method:'POST'}))){await browserExtensionGuide();await go('sources',true)}};
}

const fileIssueNames={oversized:'超过大小限制',parse_failed:'解析失败',unreadable:'无法读取',no_text:'未提取到文字'};
function sourceFileStatus(s){
  if(s.file_issues?.length)return s.file_issues.map(r=>`<button class="file-issue-link" data-source-issues="${s.id}" data-issue-code="${r.code}">${r.count} 个文件${r.state==='waiting'?fileIssueNames[r.code]:'处理中'}</button>`).join('');
  return s.last_error?`<span class="state-label warn">${esc(s.last_error)}</span>`:s.document_count+' 份资料';
}
function bindFileIssues(node){node.querySelectorAll('[data-source-issues]').forEach(b=>b.onclick=()=>sourceFileIssues(Number(b.dataset.sourceIssues),b.dataset.issueCode))}
async function sourceFileIssues(sourceId,code){
  const data=await api(`sources/${sourceId}/file-issues`);
  const files=data.files.filter(f=>f.code===code);
  const size=n=>n==null?'':(n/1024/1024).toFixed(2)+' MB';
  const rows=items=>items.map(f=>`<div class="file-issue-row"><div><b>${esc(f.name)}</b><small>${esc(f.state==='waiting'?f.message:'正在处理…')}</small></div><span>${size(f.size_bytes)}</span></div>`).join('');
  dialog(fileIssueNames[code]||'采集详情',`<div id="file-issue-list">${files.length?rows(files):'<p>这些文件已处理。</p>'}</div>${code==='oversized'?`<p class="field-note">默认读取上限 ${size(data.limit_bytes)}。本次仅处理以上文件，AI 整理沿用来源设置。</p>`:''}`,`<button class="btn secondary" data-close>关闭</button>${code==='oversized'&&files.some(f=>f.state==='waiting')?'<button class="btn" id="process-large-files">处理这些文件</button>':''}`);
  const list=el('file-issue-list');
  const ids=files.map(f=>f.id);
  const poll=async()=>{
    if(!list.isConnected)return;
    try{
      const current=await api(`sources/${sourceId}/file-issues`);
      if(!list.isConnected)return;
      const remaining=current.files.filter(f=>ids.includes(f.id));
      list.innerHTML=rows(remaining)||'<p>文件已采集。后续是否由 AI 整理，取决于来源设置。</p>';
      if(remaining.some(f=>f.state!=='waiting'))setTimeout(poll,1000);
      else {const button=el('process-large-files');if(button)button.remove();await go('sources',true)}
    }catch(e){if(list.isConnected){list.insertAdjacentHTML('beforeend',`<p class="field-note">${esc(e.message)}，可关闭后重新查看。</p>`);const button=el('process-large-files');if(button)button.remove()}}
  };
  el('process-large-files')?.addEventListener('click',()=>busy(el('process-large-files'),async()=>{
    try{await api(`sources/${sourceId}/process-large-files`,{method:'POST',body:{issue_ids:files.filter(f=>f.state==='waiting').map(f=>f.id)}});el('process-large-files').textContent='处理中…';el('process-large-files').hidden=true;await poll()}catch(e){notify(e.message)}
  }));
  if(files.some(f=>f.state!=='waiting'))setTimeout(poll,1000);
}

function sourceRow(s){const name=sourceName(s);return `<div class="source-item"><div class="source-summary"><b>${esc(name)}</b><div class="source-path" title="${esc(s.root)}">${esc(s.root)}</div></div><label class="permission-cell"><input class="toggle" type="checkbox" aria-label="采集 ${esc(name)}" data-collect-toggle="${s.id}" ${s.enabled?'checked':''}></label><label class="permission-cell"><input class="toggle" type="checkbox" aria-label="允许 AI 使用 ${esc(name)}" data-ai-toggle="${s.id}" ${s.allow_ai?'checked':''}></label><span class="source-state">${sourceFileStatus(s)}</span><button class="icon-button" data-source-detail="${s.id}" aria-label="${esc(name)} 来源设置">${icon('ellipsis')}</button></div>`}
async function addSource(){
  dialog('添加文件夹',`<div class="field"><label>文件夹</label><div class="folder-picker"><span id="new-source-path">尚未选择</span><button class="btn secondary" id="browse-folder">选择文件夹</button></div></div><p class="field-note">添加后开始采集；AI 整理与分享可在来源设置中开启。</p>`,`<button class="btn secondary" data-close>取消</button><button class="btn" id="save-source" disabled>添加</button>`);
  let folder='';
  el('browse-folder').onclick=()=>busy(el('browse-folder'),async()=>{try{const result=await api('pick-folder',{method:'POST'});if(!result.path)return;folder=result.path;el('new-source-path').textContent=folder;el('save-source').disabled=false}catch(e){notify(e.message)}});
  el('save-source').onclick=()=>busy(el('save-source'),async()=>{const name=folder.split(/[\\/]/).filter(Boolean).pop();if(await perform(()=>api('sources',{method:'POST',body:{name,root:folder,kind:'folder',allow_ai:false}}))){closeOverlay();notify('已添加文件夹');await go('sources',true)}});
}

// Knowledge: Notion-like collection sidebar + page list + editable document pane.
function visibleKnowledge(){return state.knowledge.filter(k=>k.organization==='article').filter(k=>state.lifecycle==='archived'?k.status==='archived':state.lifecycle==='confirmed'?k.can_use:state.lifecycle==='review'?!k.can_use&&k.status!=='archived'&&k.quality!=='noise':state.lifecycle==='disabled'?k.quality==='noise':k.status!=='archived'&&k.quality!=='noise')}
function entryProject(k){return k.scope==='global'?'跨项目通用':k.project||k.evidence?.find(x=>x.project)?.project||'范围待核对'}
const scopeNames={project:'项目内',session:'本次讨论',global:'跨项目通用',unknown:'范围待核对'};
const outcomeNames={none:'',reported:'报告完成，尚未核实',supported:'已有验证依据',accepted:'用户已验收'};
function availableToTwin(k){return !!k.can_use}
async function loadKnowledge(organization=''){let rows=[],offset=0;while(true){const batch=await api(`knowledge?status=all&limit=1000&offset=${offset}${organization?'&organization='+organization:''}`);rows.push(...batch);if(batch.length<1000)break;offset+=batch.length}return rows}
async function renderKnowledge(){
  const [articles,proposals]=await Promise.all([loadKnowledge('article'),api('knowledge/proposals')]);state.knowledge=articles;state.proposals=proposals;
  const projects=[...new Set(articles.map(entryProject))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
  if(!projects.includes(state.project))state.project='all';
  content.innerHTML=pageHeader('','知识库','',`<button class="btn" id="create-knowledge">${icon('plus')} 新建知识</button><details class="action-menu"><summary class="icon-button" aria-label="知识库更多操作">${icon('ellipsis')}</summary><div><button id="knowledge-refresh">刷新内容</button><button id="library-jobs">整理任务</button><button id="reprocess-knowledge">重新整理资料</button><button id="library-export">导出 Markdown</button></div></details>`)+`<div class="tabs lifecycle-filters" role="tablist">${[['active','全部'],['confirmed','可用'],['review','待处理'],['archived','已归档'],['disabled','已停用']].map(([value,label])=>`<button role="tab" aria-selected="${state.lifecycle===value}" class="${state.lifecycle===value?'active':''}" data-lifecycle="${value}">${label}</button>`).join('')}</div><div class="library-toolbar"><div class="search-bar">${icon('search')}<input type="search" id="knowledge-search" aria-label="搜索主题文章" placeholder="搜索主题文章" value="${esc(state.query)}"></div><select id="knowledge-project" aria-label="筛选项目"><option value="all">全部项目</option>${projects.map(name=>`<option ${state.project===name?'selected':''}>${esc(name)}</option>`).join('')}</select><select id="knowledge-kind" aria-label="筛选类型">${[['all','全部类型'],['fact','知识'],['decision','决策'],['process','流程'],['preference','个人偏好']].map(([key,label])=>`<option value="${key}" ${state.kind===key?'selected':''}>${label}</option>`).join('')}</select><div class="toolbar-fill"></div><button class="btn secondary small" id="knowledge-proposals">更新建议 ${proposals.length}</button></div><div class="library-location"><nav id="knowledge-breadcrumb" class="library-breadcrumb" aria-label="知识库路径"></nav><button class="library-progress" id="library-progress" aria-live="polite" hidden></button></div><div id="knowledge-list"></div><div class="table-count" id="knowledge-count"></div>`;
  content.querySelectorAll('[data-lifecycle]').forEach(b=>b.onclick=()=>{state.lifecycle=b.dataset.lifecycle;renderKnowledgeList();content.querySelectorAll('[data-lifecycle]').forEach(x=>{x.classList.toggle('active',x===b);x.setAttribute('aria-selected',String(x===b))})});
  el('knowledge-search').oninput=e=>{state.query=e.target.value;renderKnowledgeList()};
  el('knowledge-project').onchange=e=>{state.project=e.target.value;state.group='';renderKnowledgeList()};
  el('knowledge-kind').onchange=e=>{state.kind=e.target.value;renderKnowledgeList()};
  el('library-jobs').onclick=async()=>processingDetails(await api('ai/jobs'));
  el('knowledge-refresh').onclick=()=>go('knowledge',true);
  el('create-knowledge').onclick=()=>openKnowledge(null);
  el('library-export').onclick=()=>downloadFile('export-wiki','WorkTwin-Wiki.zip');
  el('reprocess-knowledge').onclick=()=>busy(el('reprocess-knowledge'),async()=>{if(!confirm('使用当前模型重新整理全部已授权资料？这会产生模型调用费用。'))return;const r=await api('knowledge/reprocess',{method:'POST'});notify(`已安排 ${r.queued} 份资料重新整理`)});
  el('knowledge-proposals').onclick=()=>{
    dialog('更新建议',proposals.length?proposals.map(p=>`<button class="knowledge-row" data-proposal="${p.id}"><span class="knowledge-info"><b>${esc(p.previous_title)}</b><small>${esc(p.reason)}</small></span>${icon('chevron')}</button>`).join(''):'<p class="muted">没有待核对的更新</p>',`<button class="btn secondary" data-close>关闭</button>`,true);
    el('overlay-root').querySelectorAll('[data-proposal]').forEach(b=>b.onclick=()=>openProposal(Number(b.dataset.proposal)));
  };
  renderKnowledgeList();
  await pollLibrary();
  window.__JOB_POLL__=setInterval(()=>pollLibrary().catch(()=>{}),5000);
  if(state.scrollPosition)requestAnimationFrame(()=>document.querySelector('.main-area').scrollTop=state.scrollPosition);
}
let libraryRevision='', libraryPolling=false;
async function pollLibrary(){
  if(libraryPolling||state.page!=='knowledge')return;
  libraryPolling=true;
  try{
    const status=await api('library/status');
    if(state.page!=='knowledge'||!el('library-progress'))return;
    const labels=[];
    if(status.extraction.running)labels.push('资料提取中');
    if(status.grouping.running)labels.push('正在整理分组');
    else if(status.grouping.queued)labels.push('分组待整理');
    if(status.maintenance.running)labels.push('正在维护文章');
    if(status.grouping.error)labels.push(status.grouping.error+' 项分组失败');
    el('library-progress').textContent=labels.join(' · ');
    el('library-progress').hidden=!labels.length;
    el('library-progress').onclick=async()=>processingDetails(await api('ai/jobs'));
    if(libraryRevision&&status.revision!==libraryRevision){
      if(state.dirty||state.overlayDirty||el('overlay-root').children.length){
        offerRefresh('知识库已更新，关闭详情后自动刷新。');
        return;
      }
      const articles=await loadKnowledge('article');
      if(state.page!=='knowledge'||state.dirty||state.overlayDirty||el('overlay-root').children.length)return;
      state.knowledge=articles;
      renderKnowledgeList();
      el('refresh-notice')?.remove();
    }
    libraryRevision=status.revision;
  }finally{libraryPolling=false}
}
function knowledgeStatus(k){return k.status==='archived'?['已归档','grey']:k.quality==='noise'?['已停用','grey']:k.can_use?['可用','']:k.unavailable_reason==='来源未允许 AI 使用'?['AI 未授权','grey']:['待处理','warn']}
function renderKnowledgeList(){
  const selected=state.knowledge.find(k=>k.group_key&&k.group_key===state.group);
  if(state.group&&!selected)state.group='';
  const entries=visibleKnowledge().filter(k=>(!state.group||k.group_key===state.group)&&(state.project==='all'||entryProject(k)===state.project)&&(state.kind==='all'||k.kind===state.kind)).filter(k=>(k.title+' '+k.body+' '+entryProject(k)+' '+(k.group_name||'')).toLowerCase().includes(state.query.toLowerCase()));
  const filtered=state.query||state.project!=='all'||state.kind!=='all'||state.lifecycle!=='active';
  const folders=new Map();
  const direct=entries.filter(k=>{if(state.group||state.query||!k.group_key)return true;let f=folders.get(k.group_key);if(!f){f={key:k.group_key,name:k.group_name,project:entryProject(k),count:0,updated:k.updated_at};folders.set(k.group_key,f)}f.count++;if(k.updated_at>f.updated)f.updated=k.updated_at;return false});
  el('knowledge-breadcrumb').innerHTML=state.group?`<button class="info-link" id="library-root">知识库</button><span aria-hidden="true">/</span><b>${esc(selected.group_name)}</b>`:'';
  el('library-root')?.addEventListener('click',()=>{state.group='';renderKnowledgeList()});
  el('knowledge-count').textContent=`${folders.size?folders.size+' 个分组 · ':''}共 ${entries.length} 篇文章`;
  const folderRows=[...folders.values()].sort((a,b)=>a.name.localeCompare(b.name,'zh-CN')).map(f=>`<button class="knowledge-row folder-row" data-group="${esc(f.key)}"><span class="knowledge-info folder-info">${icon('folder')}<span><b>${esc(f.name)}</b><small>${esc(f.project)} · ${f.count} 篇文章</small></span></span><span class="folder-open">${icon('chevron')}</span><time>${esc(localUpdateTime(f.updated))}</time></button>`).join('');
  const articleRows=direct.map(k=>{const [label,color]=knowledgeStatus(k);return `<button class="knowledge-row" data-entry="${k.id}"><span class="knowledge-info"><b>${esc(k.title)}</b><small>${esc([kindNames[k.kind],...(!state.group?[entryProject(k),k.group_name]:[])].filter(Boolean).join(' · '))}</small></span><span><i class="state-label ${color}">${label}</i></span><time>${esc(localUpdateTime(k.updated_at))}</time></button>`}).join('');
  el('knowledge-list').innerHTML=entries.length?`<div class="knowledge-table"><div class="knowledge-row table-head"><span>${folders.size?'分组与文章':'文章'}</span><span>状态</span><span>更新时间</span></div>${folderRows}${articleRows}</div>`:emptyState('book',filtered||state.group?'没有符合筛选的文章':'还没有整理好的文章',filtered||state.group?'':'连接知识来源后，文章会在这里持续更新。',filtered?'<button class="btn secondary" id="clear-knowledge-filters">清除筛选</button>':state.group?'':'<button class="btn secondary" id="connect-knowledge-source">连接来源</button>');
  el('knowledge-list').querySelectorAll('[data-group]').forEach(b=>b.onclick=()=>{state.group=b.dataset.group;renderKnowledgeList()});
  el('knowledge-list').querySelectorAll('[data-entry]').forEach(b=>b.onclick=()=>openKnowledge(Number(b.dataset.entry)));
  el('clear-knowledge-filters')?.addEventListener('click',()=>{state.query='';state.project='all';state.group='';state.kind='all';state.lifecycle='active';go('knowledge',true)});
  el('connect-knowledge-source')?.addEventListener('click',()=>go('sources',true));
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
  if(id){
    try{
      const fresh=await api(`knowledge-item/${id}`);
      if(k&&fresh.version!==k.version){offerRefresh('这篇知识已更新，请刷新内容后打开。');return;}
      k=fresh;
    }catch(e){offerRefresh('这篇知识已不存在或暂时无法读取，请刷新内容。');return;}
  }
  el('overlay-root').innerHTML=`<div class="drawer-mask" id="drawer-mask"><section class="detail-drawer" role="dialog" aria-modal="true" aria-label="${esc(k?.title||'新建知识')}"><header class="drawer-top"><h2>${esc(k?.title||'新建知识')}</h2><div class="drawer-actions">${k?`<button class="icon-button" id="drawer-history-jump" aria-label="变更日志" title="变更日志">${icon('history')}</button><span id="knowledge-more"></span>`:''}<button class="icon-button" id="drawer-close" aria-label="关闭">${icon('close')}</button></div></header><div class="drawer-inner" id="drawer-inner"></div><div class="drawer-bottom" id="drawer-bottom"></div></section></div>`;
  el('drawer-mask').onclick=e=>{if(e.target.id==='drawer-mask')closeDrawer()};el('drawer-close').onclick=closeDrawer;
  document.addEventListener('keydown',onEscape);
  const details=el('drawer-inner'),footer=el('drawer-bottom');
  const originalTitle=k?.title||'',originalBody=k?.body||'',originalSummary=k?.article_summary||'';
  const sources=k?.evidence||[],browserSources=k?.browser_sources||[];
  const relations=k?await api(`knowledge/${k.id}/relations`):null;
  const versions=k?await api(`knowledge/${k.id}/history`):[];
  function linkedSection(){if(!relations)return '';const groups=[['文中链接',relations.outgoing],['提到这篇的知识',relations.backlinks],['同一份资料的其他知识',relations.same_source]];return `<details class="source-reference"><summary>关联文章</summary>${groups.filter(([_,rows])=>rows.length).map(([name,rows])=>`<h4>${name}</h4>${rows.map(r=>`<button class="info-link relation-link" data-open-knowledge="${r.id}">${esc(r.title)} ${icon('arrow')}</button>`).join('')}`).join('')||'<p class="muted">暂无关联文章</p>'}${relations.unresolved_ids.length?'<p class="field-note">部分链接已失效或需要复核。</p>':''}</details>`}
  function versionRows(){return [{...k,changed_at:k.updated_at,current:true},...versions]}
  function changeTable(heading='更新记录',limit=1){return `<section class="article-updates" aria-label="更新记录"><h3>${heading}</h3><div class="article-updates-scroll"><table><thead><tr><th>时间 / 版本</th><th>更新内容</th><th><span class="sr-only">查看版本</span></th></tr></thead><tbody>${versionRows().slice(0,limit).map(v=>`<tr><td><time>${esc(localUpdateTime(v.changed_at))}</time><span>v${v.version}${v.current?' · 当前':''}</span></td><td>${esc(v.change_summary||v.reason||(v.version===1?'创建文章':'更新文章内容'))}</td><td><button class="info-link" data-view-version="${v.version}" aria-label="查看 v${v.version}">查看</button></td></tr>`).join('')}</tbody></table></div></section>`}
  function articleBody(content=k){
    if(!content.rendered_body)return esc(content.body);
    const template=document.createElement('template');template.innerHTML=content.rendered_body;
    const heading=template.content.firstElementChild;
    if(heading?.matches('h1,h2,h3')&&heading.textContent.trim()===content.title.trim())heading.remove();
    return template.innerHTML;
  }
  function summarySection(content=k){return content.article_summary?`<section class="article-summary" aria-label="文章概要"><h3>概要</h3><p>${esc(content.article_summary)}</p></section>`:''}
  function bindVersionLinks(){details.querySelectorAll('[data-view-version]').forEach(b=>b.onclick=()=>versionView(Number(b.dataset.viewVersion)))}
  function versionView(version){
    const selected=versionRows().find(v=>v.version===version);
    if(selected.current){view();return}
    details.innerHTML=`<div class="version-heading"><h3>v${selected.version} · 历史版本</h3><time>${esc(localUpdateTime(selected.changed_at))}</time></div><h2 class="version-title">${esc(selected.title)}</h2>${summarySection(selected)}<article class="drawer-body ${selected.rendered_body?'markdown-body':''}" aria-label="历史正文">${articleBody(selected)}</article>`;
    footer.innerHTML=`<button class="btn secondary" id="history-back">返回正文</button><button class="btn secondary" id="history-log-back">更新记录</button><button class="btn" id="restore-version">回退至此版本</button>`;
    el('history-back').onclick=view;el('history-log-back').onclick=historyView;
    el('restore-version').onclick=()=>busy(el('restore-version'),async()=>{
      if(!confirm(`回退至 v${version}？该内容将保存为新版本。`))return;
      if(await perform(()=>api(`knowledge/${k.id}/versions/${version}/restore`,{method:'POST'}))){closeOverlay(true);await go('knowledge',true);await openKnowledge(k.id)}
    });
    bindSourceLinks();details.scrollTop=0;
  }
  function historyView(){
    if(state.overlayDirty){notify('请先保存或取消编辑');return}
    details.innerHTML=changeTable('变更日志',Infinity);
    footer.innerHTML='<button class="btn secondary" id="history-back">返回正文</button>';
    el('history-back').onclick=view;bindVersionLinks();details.scrollTop=0;
  }
  function referenceRows(){
    const currentSources=sources.filter(e=>!e.superseded),historicalSources=sources.filter(e=>e.superseded);
    const total=currentSources.length+browserSources.length;
    const evidenceRow=e=>`<div class="reference-row"><b>${esc(e.document_title)}</b><blockquote>${esc(e.quote)}</blockquote>${e.superseded?'<span class="state-label grey">旧版历史引用</span>':!e.is_current?'<span class="state-label warn">来源已变更，需要重新核对</span>':''}${e.is_current||e.superseded?`<button class="info-link" data-read-source="${e.document_id}">查看原始资料 ${icon('arrow')}</button>`:''}</div>`;
    const browserKinds={click:'点击',change:'修改',submit:'尝试提交',feedback:'页面提示',navigation:'页面导航',tab_closed:'关闭标签页'};
    return `<details class="source-reference"><summary>来源依据 <span class="muted">${total} 条</span></summary>${currentSources.map(evidenceRow).join('')}${historicalSources.length?`<details class="historical-evidence"><summary>历史引用 ${historicalSources.length} 条</summary>${historicalSources.map(evidenceRow).join('')}</details>`:''}${browserSources.map(source=>`<div class="reference-row"><b>${esc(source.source_title)}</b>${source.summary?`<p>${esc(source.summary)}</p>`:''}<ol class="browser-evidence-events">${source.events.map(e=>`<li><span>#${e.seq} · ${esc(browserKinds[e.kind]||e.kind)} · ${esc(e.label)}</span>${e.location?`<small>${esc(e.location)}</small>`:''}</li>`).join('')}</ol><button class="info-link" data-read-browser="${esc(source.session_id)}">查看操作详情 ${icon('arrow')}</button></div>`).join('')}${!total?'<p class="muted">人工创建</p>':''}${k?.parts?.length?`<details class="article-parts"><summary>原始材料分段</summary>${k.parts.map(part=>`<div class="reference-row"><b>${esc(part.title)}</b><p>${esc(part.body)}</p><blockquote>${esc(part.quote)}</blockquote>${part.document_id?`<button class="info-link" data-read-source="${part.document_id}">查看原始资料</button>`:''}</div>`).join('')}</details>`:''}</details>`;
  }
  function bindSourceLinks(){
    details.querySelectorAll('[data-read-source]').forEach(x=>x.onclick=()=>showDocument(Number(x.dataset.readSource)));
    details.querySelectorAll('[data-read-browser]').forEach(x=>x.onclick=()=>browserSessionDetails(x.dataset.readBrowser));
    details.querySelectorAll('.markdown-body a[href^="#document-"]').forEach(a=>a.onclick=e=>{e.preventDefault();showDocument(Number(a.getAttribute('href').slice(10)))});
    details.querySelectorAll('.markdown-body a[href^="#knowledge-"]').forEach(a=>a.onclick=e=>{e.preventDefault();openKnowledge(Number(a.getAttribute('href').slice(11)))});
  }
  function view(){const [label,color]=knowledgeStatus(k);details.innerHTML=`<div class="drawer-meta"><span class="state-label ${color}">${label}</span><span>${esc(kindNames[k.kind])}</span><span>${esc([entryProject(k),k.group_name].filter(Boolean).join(' / '))}</span><span>v${k.version}</span><time>${esc(localUpdateTime(k.updated_at))}</time></div>${!k.can_use&&k.unavailable_reason?`<p class="status-note warn">${esc(k.unavailable_reason)}${k.unavailable_reason==='来源未允许 AI 使用'?'<button class="info-link" id="article-source-access">来源授权</button>':''}</p>`:''}${k.scope_detail?`<p class="article-condition">${esc(k.scope_detail)}</p>`:''}${summarySection()}${changeTable()}<article class="drawer-body ${k.rendered_body?'markdown-body':''}" aria-label="文章正文">${articleBody()}</article>${referenceRows()}${linkedSection()}`;
    footer.innerHTML='<button class="btn" id="edit-entry">编辑</button>';bindVersionLinks();
    el('knowledge-more').innerHTML=`<details class="action-menu"><summary class="icon-button" aria-label="知识操作">${icon('ellipsis')}</summary><div>${k.status==='archived'?'<button id="restore-entry">恢复为待确认</button>':'<button id="archive-entry">归档</button>'}${k.kind==='preference'||k.status==='archived'?'<button id="delete-entry">彻底删除</button>':''}${k.status==='draft'&&k.scope!=='unknown'&&k.quality==='useful'?'<button id="confirm-entry">确认内容</button>':''}${k.quality!=='noise'?'<button id="disable-entry">停用</button>':''}</div></details>`;
    details.querySelectorAll('[data-open-knowledge]').forEach(b=>b.onclick=()=>openKnowledge(Number(b.dataset.openKnowledge)));
    el('article-source-access')?.addEventListener('click',()=>articleSourceAccess(k.id,()=>openKnowledge(k.id)));
    el('edit-entry').onclick=edit;
    el('disable-entry')?.addEventListener('click',()=>busy(el('disable-entry'),async()=>{await api('knowledge/disable',{method:'POST',body:{knowledge_ids:[k.id]}});closeOverlay(true);notify('已停用，内容保留但不参与回答');await go('knowledge',true)}));
    el('restore-entry')?.addEventListener('click',()=>busy(el('restore-entry'),async()=>{if(await perform(()=>api(`knowledge/${k.id}/restore`,{method:'POST'}),null)){closeOverlay(true);notify('已恢复为待确认，请核对后再用于分身');await go('knowledge',true)}}));
    el('delete-entry')?.addEventListener('click',()=>busy(el('delete-entry'),async()=>{if(!confirm('彻底删除这篇知识？无法恢复。'))return;if(await perform(()=>api(`knowledge/${k.id}`,{method:'DELETE'}),null)){closeOverlay(true);notify('已删除知识');await go('knowledge',true)}}));
    el('confirm-entry')?.addEventListener('click',()=>save(k.title,k.body,k.kind,'confirmed'));
    el('archive-entry')?.addEventListener('click',async()=>{if(!confirm('将这篇知识归档并从数字分身的可用范围中移除？'))return;await save(k.title,k.body,k.kind,'archived')});
    bindSourceLinks();
  }
  async function save(title,body,kind,status,metadata={}){
    if(!title.trim()||!body.trim()){notify('标题和正文不能为空');return}
    try{const payload={title:title.trim(),body:body.trim(),kind,status,...metadata,...(k?{expected_version:k.version}:{})};await api(k?`knowledge/${k.id}`:'knowledge',{method:k?'PUT':'POST',body:payload});await go('knowledge',true);closeOverlay(true);notify('知识已保存')}catch(e){notify(e.message)}
  }
  function edit(){details.innerHTML=`<div class="drawer-category">编辑知识文档</div><div class="field" style="margin-top:20px"><input id="edit-k-title" class="edit-title" maxlength="130" value="${esc(originalTitle)}" placeholder="知识标题"/></div><div class="field"><label for="edit-k-kind">知识类型</label><select id="edit-k-kind">${[['fact','业务知识'],['decision','决策记录'],['process','流程指引'],['preference','个人偏好']].map(([id,name])=>`<option value="${id}" ${k?.kind===id?'selected':''}>${name}</option>`).join('')}</select></div><div class="field"><label for="edit-k-scope">适用范围</label><select id="edit-k-scope">${Object.entries(scopeNames).map(([id,name])=>`<option value="${id}" ${(k?.scope||'global')===id?'selected':''}>${esc(name)}</option>`).join('')}</select><div class="field-note">局部要求只适用于所属项目或本次讨论。仅明确通用的规则选择“跨项目通用”。</div></div><div class="field"><label for="edit-k-project">所属项目 / 讨论名称</label><input id="edit-k-project" maxlength="200" value="${esc(k?.project||'')}" placeholder="如：Mingo 提示词库"/></div><div class="field"><label for="edit-k-topic">主题</label><input id="edit-k-topic" maxlength="100" value="${esc(k?.topic||k?.title||'')}" placeholder="如：提示词交付规范"/></div><div class="field"><label for="edit-k-scope-detail">具体适用对象与条件</label><input id="edit-k-scope-detail" maxlength="500" value="${esc(k?.scope_detail||(!k?'跨项目适用的人工知识':''))}" placeholder="如：仅用于 Mingo 提示词的正式交付版本"/></div><div class="field"><label for="edit-k-quality">内容是否有可复用价值</label><select id="edit-k-quality"><option value="useful" ${k?.quality==='useful'||!k?'selected':''}>有明确价值，可以使用</option><option value="uncertain" ${k?.quality==='uncertain'?'selected':''}>待核对，暂不使用</option><option value="noise" ${k?.quality==='noise'?'selected':''}>低价值，停用</option></select></div><div class="field"><label for="edit-k-summary">概要</label><textarea id="edit-k-summary" class="edit-summary" maxlength="2000" spellcheck="false" placeholder="这篇文章讲什么、适用于什么场景、核心结论是什么。">${esc(originalSummary)}</textarea></div><div class="field"><label for="edit-k-body">知识正文</label><textarea id="edit-k-body" class="edit-body" spellcheck="false" placeholder="在这里写下知识内容；支持 Markdown；知识链接写作 [[K编号|显示名称]]。">${esc(originalBody)}</textarea></div><label class="check-row"><input id="edit-k-confirmed" type="checkbox" ${k?.status==='confirmed'?'checked':''}/> <span>标记为内容已核对</span></label>${referenceRows()}`;
    const bodyField=el('edit-k-body').closest('.field');
    details.insertBefore(el('edit-k-summary').closest('.field'),el('edit-k-kind').closest('.field'));
    details.insertBefore(bodyField,el('edit-k-kind').closest('.field'));
    const metadata=document.createElement('details');metadata.className='edit-metadata';metadata.innerHTML='<summary>范围与属性</summary>';
    bodyField.after(metadata);
    ['edit-k-kind','edit-k-scope','edit-k-project','edit-k-topic','edit-k-scope-detail','edit-k-quality'].forEach(id=>metadata.append(el(id).closest('.field')));
    el('edit-k-body').maxLength=50000;
    footer.innerHTML=`<button class="btn secondary" id="cancel-edit">取消</button><button class="btn" id="save-entry">保存知识 <span class="kbd-hint">⌘S / Ctrl+S</span></button>`;
    ['edit-k-title','edit-k-summary','edit-k-body','edit-k-kind','edit-k-confirmed','edit-k-scope','edit-k-project','edit-k-topic','edit-k-scope-detail','edit-k-quality'].forEach(id=>{el(id).addEventListener('input',()=>state.overlayDirty=true);el(id).addEventListener('change',()=>state.overlayDirty=true)});
    el('cancel-edit').onclick=()=>{if(state.overlayDirty&&!confirm('放弃尚未保存的修改？'))return;state.overlayDirty=false;k?view():closeDrawer()};
    el('save-entry').onclick=()=>busy(el('save-entry'),()=>save(el('edit-k-title').value,el('edit-k-body').value,el('edit-k-kind').value,el('edit-k-confirmed').checked?'confirmed':'draft',{article_summary:el('edit-k-summary').value,scope:el('edit-k-scope').value,project:el('edit-k-project').value,topic:el('edit-k-topic').value||el('edit-k-title').value,scope_detail:el('edit-k-scope-detail').value,quality:el('edit-k-quality').value}));
    details.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='s'){e.preventDefault();el('save-entry')?.click()}});
    bindSourceLinks();
  }
  if(k)view();else edit();
  el('drawer-history-jump')?.addEventListener('click',historyView);
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
    scopeForm.innerHTML=`<details><summary class="field-note" style="cursor:pointer">优化项目关联（可选）</summary><p class="field-note">${d.project_verified?'已确认项目：'+esc(d.project):'目录只是线索，所属业务项目尚未确认。'}仅当整份资料属于同一项目时确认；同名项目不会自动合并。</p><div class="field"><label>项目关系</label><select id="source-existing-project"><option value="">创建独立项目（允许与已有项目同名）</option>${projectOptions}</select></div><div class="field"><label>项目名称</label><input id="source-project" maxlength="200" value="${esc(d.project_verified?d.project:'')}" placeholder="例如：WorkTwin"/></div><button class="btn secondary small" id="save-source-project">确认项目并重新整理此资料</button></details>`;
    preview.querySelector('.dialog-body').append(scopeForm);
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
  content.innerHTML=pageHeader('DIGITAL TWINS','数字分身','为不同协作者创建分身，按项目或来源授权一次，后续新知识自动继承。',`<button class="btn" id="add-twin">${icon('plus')} 创建数字分身</button>`)+
    (state.twins.length?`<div class="twin-cards">${state.twins.map((t,i)=>`<button class="twin-card" data-twin="${t.id}"><div class="twin-icon">${icon('user')}</div><div class="twin-title">${esc(t.name)}</div><div class="twin-description">${esc(t.description||'只基于你指定的知识提供回答。')}</div><div class="twin-bottom"><span>${t.knowledge_count} 篇可使用知识</span><span>配置分身 ${icon('arrow')}</span></div></button>`).join('')}</div>`:
    emptyState('users','还没有创建数字分身','创建第一个分身，并从你的知识库勾选它能够使用的内容。',`<button class="btn" id="empty-add-twin">创建第一个分身</button>`));
  el('add-twin')?.addEventListener('click',createTwin);
  el('empty-add-twin')?.addEventListener('click',createTwin);
  content.querySelectorAll('[data-twin]').forEach(x=>x.onclick=()=>{state.twinId=Number(x.dataset.twin);go('twins',true)});
}
function createTwin(){
  dialog('创建数字分身',`<div class="field"><label for="twin-name">分身名称</label><input id="twin-name" placeholder="例如：项目交接助手" maxlength="90" autofocus/></div><div class="field"><label for="twin-desc">给它一个用途（可选）</label><textarea id="twin-desc" rows="3" maxlength="500" placeholder="例如：回答关于项目方案、历史决策和操作方法的问题"></textarea></div><div class="permission-note">创建后选择它可以使用的知识。</div>`,
    `<button class="btn secondary" data-close>取消</button><button class="btn" id="submit-twin">创建并选择知识</button>`);
  el('submit-twin').onclick=async()=>{
    const name=el('twin-name').value.trim();if(!name){notify('请输入分身名称');return}
    try{const data=await api('twins',{method:'POST',body:{name,description:el('twin-desc').value.trim()}});closeOverlay();state.twinId=data.id;go('twins',true)}catch(e){notify(e.message)}
  };
}
async function renderTwinEditor(id){
  const [t,knowledge,grantOptions,preview]=await Promise.all([api(`twins/${id}`),loadKnowledge('article'),api('twin-grant-options'),api(`twins/${id}/preview`)]);
  state.knowledge=knowledge;
  const eligible=knowledge.filter(k=>k.organization==='article'&&(k.status!=='archived'||t.knowledge_ids.includes(k.id)));
  const effective=new Set(preview.knowledge.filter(k=>!k.local_reason).map(k=>k.id));
  const selected=new Set(t.knowledge_ids.filter(id=>eligible.some(k=>k.id===id))),rules=t.grants||[];
  const projects=new Set(rules.filter(g=>g.subject_type==='project'&&g.effect==='allow').map(g=>g.subject_key));
  const sources=new Set(rules.filter(g=>g.subject_type==='source'&&g.effect==='allow').map(g=>g.subject_key));
  const explicitRules=rules.filter(g=>g.subject_type==='knowledge'&&g.effect==='allow'&&eligible.some(k=>String(k.id)===g.subject_key));
  let allowGlobal=rules.some(g=>g.subject_type==='global'&&g.effect==='allow'),filter='all';
  const groups=[...new Set(eligible.map(entryProject))];
  const chat=state.chats[id]||={turns:[],scope:'local',project:'',revision:null};
  content.innerHTML=`<button class="info-link back-link" id="twins-back">← 数字分身</button>`+pageHeader('',t.name,'')+`<div class="twin-workspace"><div class="tabs" role="tablist">${[['grants','知识授权'],['chat','试问'],['sharing','分享与访问'],['info','基本信息']].map(([key,name])=>`<button role="tab" data-twin-tab="${key}">${name}</button>`).join('')}</div><section data-twin-panel="grants">${t.grants_migrated?'<p class="status-note">已保留原可用知识，改为逐条选择。</p>':''}<div class="section-toolbar"><select id="twin-mode" aria-label="授权方式"><option value="dynamic" ${t.knowledge_mode==='dynamic'?'selected':''}>自动跟随授权</option><option value="manual" ${t.knowledge_mode==='manual'?'selected':''}>选择文章</option></select><span class="muted">已保存授权 ${preview.selected_count} 篇 · 当前可用 ${preview.usable_count} 篇</span></div><section id="twin-rule-editor"><h3>授权范围</h3><div class="grant-grid"><section><h4>项目</h4>${grantOptions.projects.map(p=>`<label class="check-row"><input type="checkbox" data-grant-project="${esc(p.project_key)}" ${projects.has(p.project_key)?'checked':''}><span>${esc(p.name)}<small>${p.knowledge_count} 篇知识</small></span></label>`).join('')||'<p class="muted">暂无项目</p>'}</section><section><h4>来源</h4>${grantOptions.sources.filter(s=>s.allow_ai).map(s=>`<label class="check-row"><input type="checkbox" data-grant-source="${s.id}" ${sources.has(String(s.id))?'checked':''}>${esc(sourceName(s))}</label>`).join('')||'<p class="muted">暂无可选来源</p>'}</section></div><label class="check-row"><input type="checkbox" id="twin-allow-global" ${allowGlobal?'checked':''}>包含跨项目通用知识</label></section><section class="selection-panel"><div class="selection-head"><b>补充授权文章</b><span class="muted" id="selected-count"></span></div><div class="selection-search"><div class="search-bar">${icon('search')}<input type="search" id="twin-search" placeholder="搜索文章"></div><div class="selection-filter-bar">${[['all','全部'],['selected','已选'],['unselected','未选']].map(([key,name])=>`<button class="selection-filter-btn ${key==='all'?'active':''}" data-twin-filter="${key}">${name}</button>`).join('')}</div></div><div class="selection-list" id="selection-list"></div></section></section><section data-twin-panel="chat" class="chat-panel"><div class="chat-toolbar"><select id="ask-scope" aria-label="试问版本"><option value="local">本地已保存授权</option><option value="published" ${state.shareReady?'':'disabled'}>服务端已发布版本</option></select><select id="ask-project" aria-label="试问范围"><option value="">自动判断范围</option>${[...new Map(eligible.filter(k=>effective.has(k.id)&&k.project_key&&k.scope!=='global').map(k=>[k.project_key,k])).values()].map(k=>`<option value="${esc(k.project_key)}">${esc(entryProject(k))}</option>`).join('')}</select><button class="btn secondary small" id="new-conversation">新会话</button></div><div class="chat-messages" id="twin-answer" role="log" aria-live="polite"></div><button class="new-message-link" id="chat-new-messages" hidden>查看新消息 ↓</button><div class="chat-composer"><textarea class="text-input" id="twin-question" rows="2" maxlength="500" placeholder="继续提问…" aria-label="消息"></textarea><button class="btn" id="twin-ask">发送</button></div><p id="chat-status" class="field-note"></p></section><section data-twin-panel="sharing"><section class="settings-group"><h3>分享链接</h3><div id="sharing-panel">正在读取分享状态…</div></section><section class="settings-group" id="mcp-panel"></section></section><section data-twin-panel="info"><div class="field"><label for="twin-edit-name">名称</label><input id="twin-edit-name" value="${esc(t.name)}" maxlength="90"></div><div class="field"><label for="twin-edit-desc">用途</label><textarea id="twin-edit-desc" maxlength="500" rows="3">${esc(t.description)}</textarea></div><button class="btn danger" id="delete-twin">删除分身</button></section><footer class="twin-footer"><button class="btn secondary" id="twin-done">完成</button><button class="btn" id="save-selections">保存配置</button></footer></div>`;
  const editor=content.querySelector('.twin-workspace');
  function back(){if(!confirmDiscard())return;state.dirty=false;state.twinId=null;go('twins',true)}
  el('twins-back').onclick=back;el('twin-done').onclick=back;
  function showTab(key){state.twinTab=key;content.querySelectorAll('[data-twin-tab]').forEach(b=>{const active=b.dataset.twinTab===key;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active))});content.querySelectorAll('[data-twin-panel]').forEach(p=>p.hidden=p.dataset.twinPanel!==key);content.querySelector('.twin-footer').hidden=key==='chat';if(key==='chat')requestAnimationFrame(scrollChat)}
  content.querySelectorAll('[data-twin-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.twinTab));
  function counts(){el('selected-count').textContent=`已选择 ${selected.size} 篇`}
  function renderSelection(){
    const q=el('twin-search').value.toLowerCase();
    el('selection-list').innerHTML=groups.map(project=>{const rows=eligible.filter(k=>entryProject(k)===project&&(k.title+' '+k.body).toLowerCase().includes(q)&&(filter==='all'||(filter==='selected'?selected.has(k.id):!selected.has(k.id))));return rows.length?`<div class="selection-group"><span>${esc(project)}</span><span class="selection-group-actions"><button data-select-all="${esc(project)}">全选</button><button data-deselect-all="${esc(project)}">取消</button></span></div>${rows.map(k=>`<div class="selection-row"><input type="checkbox" aria-label="授权 ${esc(k.title)}" data-select-entry="${k.id}" ${selected.has(k.id)?'checked':''} ${availableToTwin(k)||selected.has(k.id)?'':'disabled'}><span>${esc(k.title)}${!availableToTwin(k)?`<small>${esc(k.unavailable_reason==='来源未允许 AI 使用'?'来源的 AI 使用已关闭':k.unavailable_reason)}</small>`:''}</span>${k.unavailable_reason==='来源未允许 AI 使用'?`<button class="info-link" data-source-access="${k.id}">来源授权</button>`:''}<button class="info-link" data-inspect-note="${k.id}">查看</button></div>`).join('')}`:''}).join('')||'<p class="empty-copy">暂无符合条件的知识</p>';
    el('selection-list').querySelectorAll('[data-select-entry]').forEach(b=>b.onchange=()=>{b.checked?selected.add(Number(b.dataset.selectEntry)):selected.delete(Number(b.dataset.selectEntry));state.dirty=true;counts()});
    el('selection-list').querySelectorAll('[data-source-access]').forEach(b=>b.onclick=()=>articleSourceAccess(Number(b.dataset.sourceAccess),async()=>{
      const fresh=await loadKnowledge('article');
      for(const k of eligible){const updated=fresh.find(a=>a.id===k.id);if(updated)Object.assign(k,updated)}
      state.knowledge=fresh;renderSelection();
    }));
    el('selection-list').querySelectorAll('[data-inspect-note]').forEach(b=>b.onclick=()=>openKnowledge(Number(b.dataset.inspectNote)));
    for(const [attr,add] of [['selectAll',true],['deselectAll',false]])el('selection-list').querySelectorAll(add?'[data-select-all]':'[data-deselect-all]').forEach(b=>b.onclick=()=>{eligible.filter(k=>entryProject(k)===b.dataset[attr]&&(!add||availableToTwin(k))).forEach(k=>add?selected.add(k.id):selected.delete(k.id));state.dirty=true;renderSelection();counts()});
  }
  el('twin-search').oninput=renderSelection;
  content.querySelectorAll('[data-twin-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.twinFilter;content.querySelectorAll('[data-twin-filter]').forEach(x=>x.classList.toggle('active',x===b));renderSelection()});
  content.querySelectorAll('[data-grant-project]').forEach(b=>b.onchange=()=>{b.checked?projects.add(b.dataset.grantProject):projects.delete(b.dataset.grantProject);state.dirty=true});
  content.querySelectorAll('[data-grant-source]').forEach(b=>b.onchange=()=>{b.checked?sources.add(b.dataset.grantSource):sources.delete(b.dataset.grantSource);state.dirty=true});
  el('twin-allow-global').onchange=e=>{allowGlobal=e.target.checked;state.dirty=true};
  function mode(){const dynamic=el('twin-mode').value==='dynamic';el('twin-rule-editor').hidden=!dynamic;content.querySelector('.selection-head b').textContent=dynamic?'补充授权文章':'授权文章'}
  el('twin-mode').onchange=()=>{if(el('twin-mode').value==='manual'&&t.knowledge_mode==='dynamic'){preview.knowledge.filter(k=>!k.local_reason).forEach(k=>selected.add(k.id));renderSelection();counts()}mode();state.dirty=true};mode();counts();renderSelection();
  watchChanges(el('twin-edit-name'));watchChanges(el('twin-edit-desc'));
  el('save-selections').onclick=()=>busy(el('save-selections'),async()=>{
    const name=el('twin-edit-name').value.trim();if(!name){notify('名称不能为空');return}
    const inputs=[...editor.querySelectorAll('input,textarea,select')];inputs.forEach(input=>input.dataset.wasDisabled=String(input.disabled));inputs.forEach(input=>input.disabled=true);
    const grants=el('twin-mode').value==='dynamic'?[...explicitRules,...[...projects].map(key=>({subject_type:'project',subject_key:key,effect:'allow'})),...[...sources].map(key=>({subject_type:'source',subject_key:key,effect:'allow'})),...(allowGlobal?[{subject_type:'global',subject_key:'*',effect:'allow'}]:[])]:[];
    if(await perform(()=>api(`twins/${id}`,{method:'PUT',body:{name,description:el('twin-edit-desc').value,knowledge_ids:[...selected],knowledge_mode:el('twin-mode').value,grants}}))){state.dirty=false;chat.turns=[];chat.revision=null;notify('配置已保存');await go('twins',true)}
    if(editor.isConnected)inputs.forEach(input=>input.disabled=input.dataset.wasDisabled==='true');
  });
  el('delete-twin').onclick=()=>busy(el('delete-twin'),async()=>{if(confirm('删除分身并停止分享？知识库会保留。')&&await perform(()=>api(`twins/${id}`,{method:'DELETE'}))){state.dirty=false;state.twinId=null;delete state.chats[id];await go('twins',true)}});
  function scrollChat(){const list=el('twin-answer');list.scrollTop=list.scrollHeight;el('chat-new-messages').hidden=true}
  function renderChat(force=false){if(!editor.isConnected)return;updateChatStatus();const pending=chat.turns.some(turn=>turn.loading);['ask-scope','ask-project','new-conversation'].forEach(key=>el(key).disabled=pending);if(chat.retryQuestion&&!el('twin-question').value){el('twin-question').value=chat.retryQuestion;delete chat.retryQuestion}const list=el('twin-answer'),nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<72;list.innerHTML=chat.turns.length?chat.turns.map(turn=>`<div class="chat-message user"><span class="chat-avatar">你</span><div>${esc(turn.question)}</div></div><div class="chat-message assistant"><span class="chat-avatar">W</span><div>${turn.loading?'<span class="chat-loading">正在回答…</span>':`<div class="chat-text">${esc(turn.answer)}</div>${turn.citations.length?`<div class="citation-links">${turn.citations.map(c=>`<button class="info-link" data-citation="${c.knowledge_id}">[K${c.knowledge_id}] ${esc(c.title)}</button>`).join('')}</div>`:''}`}</div></div>`).join(''):'<div class="chat-empty">向分身提问，验证它如何使用已授权知识。</div>';list.querySelectorAll('[data-citation]').forEach(b=>b.onclick=()=>openKnowledge(Number(b.dataset.citation)));if(force||nearBottom)scrollChat();else el('chat-new-messages').hidden=false}
  function resetChat(){chat.turns=[];chat.revision=null;chat.scope=el('ask-scope').value;chat.project=el('ask-project').value;renderChat(true);updateChatStatus()}
  function updateChatStatus(){const ready=chat.scope==='published'?state.shareReady:state.modelReady;el('twin-ask').disabled=!ready||chat.turns.some(turn=>turn.loading);el('chat-status').textContent=ready?'Enter 发送 · Shift+Enter 换行':'请先配置'+(chat.scope==='published'?'分享服务':'模型')}
  el('ask-scope').value=chat.scope;el('ask-project').value=chat.project;
  el('new-conversation').onclick=resetChat;el('ask-scope').onchange=resetChat;el('ask-project').onchange=resetChat;el('chat-new-messages').onclick=scrollChat;
  el('twin-question').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();el('twin-ask').click()}};
  el('twin-ask').onclick=()=>busy(el('twin-ask'),async()=>{
    if(state.dirty){notify('请先保存配置，再开始试问');return}
    const question=el('twin-question').value.trim();if(question.length<2){notify('请至少输入两个字');return}
    const messages=chat.turns.filter(turn=>!turn.error).flatMap(turn=>[{role:'user',content:turn.question},{role:'assistant',content:turn.answer}]).slice(-20);
    const turn={question,answer:'',citations:[],loading:true};chat.turns.push(turn);el('twin-question').value='';renderChat(true);
    try{const result=await api(`twins/${id}/ask`,{method:'POST',body:{question,scope:chat.scope,project_key:chat.project||null,messages,context_revision:chat.revision}});turn.answer=result.answer;turn.citations=result.citations;chat.revision=result.context_revision}catch(e){turn.answer=e.message;turn.error=true;if(e.status===409){chat.turns=[];chat.revision=null;notify('知识或授权已更新，已开启新会话');chat.retryQuestion=question}}finally{turn.loading=false;chat.render()}
  });
  chat.render=renderChat;renderChat();showTab(state.twinTab);
  await Promise.all([renderMcp(id),renderSharing(id)]);
}

async function renderMcp(id){
  const panel=el('mcp-panel');if(!panel)return;
  const info=await api(`twins/${id}/mcp`);
  panel.innerHTML=`<h2>连接其他 AI（MCP）</h2><p class="field-note">其他 AI 可读取此分身的授权知识。连接时需保持 WorkTwin 运行。</p><p>${info.enabled?'已启用':'尚未启用'}</p><button class="btn secondary small" id="generate-mcp">${info.enabled?'更新连接凭据':'启用并获取连接配置'}</button>${info.enabled?'<button class="btn danger small" id="disable-mcp">关闭 MCP</button><div class="field"><label><input type="checkbox" id="allow-mcp-logs" '+(info.allow_logs?'checked':'')+'/> 单独允许读取完整来源日志</label><p class="field-note">默认只提供必要引用。开启后，其他 AI 可读取授权知识引用的整份来源资料或会话，可能包含其他讨论内容。</p></div>':''}`;
  el('generate-mcp').onclick=()=>busy(el('generate-mcp'),async()=>{
    if(state.dirty){notify('请先保存分身的知识授权');return}
    if(info.enabled&&!confirm('更新后，原来的 MCP 连接凭据立即失效，需要重新连接。继续吗？'))return;
    try{
      const r=await api(`twins/${id}/mcp`,{method:'POST'});let client='cursor';
      dialog('连接其他 AI',`<section class="settings-group"><h3>选择客户端</h3><div class="actions">${[['cursor','Cursor'],['vscode','VS Code'],['claude','Claude Desktop']].map(([key,label])=>`<button class="btn secondary" data-mcp-client="${key}" aria-pressed="${key===client}">${label}</button>`).join('')}</div></section><section class="settings-group"><h3>连接配置</h3><button class="btn secondary small" id="test-mcp-config">测试连接</button><p class="field-note">一键配置会保留其他连接，并备份原配置。配置后请重启对应客户端。</p><textarea readonly id="mcp-config" rows="10" aria-label="MCP 连接配置"></textarea><p id="mcp-install-result" role="status"></p></section>`,`<button class="btn secondary" data-close>完成</button><button class="btn secondary" id="copy-mcp-config">复制配置</button><button class="btn" id="install-mcp-config">一键配置</button>`);
      const refresh=()=>{el('mcp-config').value=JSON.stringify(r.clients[client],null,2);el('overlay-root').querySelectorAll('[data-mcp-client]').forEach(b=>{b.setAttribute('aria-pressed',b.dataset.mcpClient===client);b.classList.toggle('secondary',b.dataset.mcpClient!==client)});el('mcp-install-result').textContent=''};refresh();
      el('overlay-root').querySelectorAll('[data-mcp-client]').forEach(b=>b.onclick=()=>{client=b.dataset.mcpClient;refresh()});
      el('copy-mcp-config').onclick=async()=>{try{await navigator.clipboard.writeText(el('mcp-config').value);notify('已复制连接配置')}catch{el('mcp-config').select();notify('请复制选中的配置')}};
      el('test-mcp-config').onclick=()=>busy(el('test-mcp-config'),async()=>{try{const result=await api(`twins/${id}/mcp/test`,{method:'POST',body:{token:r.token}});el('mcp-install-result').textContent=`WorkTwin 连接正常，提供 ${result.tools} 项读取工具。`}catch(e){el('mcp-install-result').textContent=e.message}});
      el('install-mcp-config').onclick=()=>busy(el('install-mcp-config'),async()=>{try{const result=await api(`twins/${id}/mcp/install/${client}`,{method:'POST',body:{token:r.token}});el('mcp-install-result').textContent='已配置：'+result.path+'。请重启客户端。'}catch(e){el('mcp-install-result').textContent=e.message}});
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
  panel.innerHTML=`<p class="field-note">链接持有人可访问已发布知识。</p>${info.error?`<p class="state-label warn">${esc(info.error)}</p><button class="btn secondary small" id="sync-sharing">重试同步</button>`:''}`+
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

// Background collection never replaces an open view. Refresh is explicit.
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
    (info.edition==='personal'?`<section class="settings-section" id="primary-model-settings"><h2>模型配置</h2><p class="soft-caption">支持 OpenAI Chat Completions 兼容服务。地址和模型名称由你的模型服务商提供。</p><div class="field"><label for="personal-url">模型服务地址</label><input id="personal-url" value="${esc(info.personal_base_url||'')}" placeholder="例如 https://api.openai.com/v1"/><div class="field-note">填写服务商提供的接口根地址。</div></div><div class="field"><label for="personal-model">模型名称</label><input list="personal-model-options" id="personal-model" value="${esc(info.personal_model||'')}" placeholder="填写服务商提供的模型名称"/></div><div class="field"><label for="personal-key">API Key</label><input id="personal-key" type="password" autocomplete="off" placeholder="${info.has_personal_key?'已安全保存，留空保留原密钥':'填写你自己的模型密钥'}"/><div class="field-note">保存在${esc(info.secret_storage)}中，不会出现在知识导出里。</div></div><div class="model-discovery"><button class="btn secondary small" id="personal-list-models">获取可用模型</button><span class="field-note" id="personal-list-result">也可以直接填写模型名称。</span><datalist id="personal-model-options"></datalist></div><button class="btn" id="save-personal-model">测试连接并保存</button> <button class="btn secondary" id="test-model" ${info.enterprise_model_ready?'':'disabled'}>测试当前模型</button><p id="settings-result" aria-live="polite">${esc(info.model_error||({connected:'模型已连接',configured:'已保存配置，尚未验证本次启动的模型连接',not_configured:'尚未配置模型',reading_credentials:'正在读取系统钥匙串，请处理 macOS 的权限提示',error:'模型调用失败，请检查设置'}[info.model_status]||''))}</p></section><section class="settings-section"><h2>远程分享服务</h2><p>本地使用不需要配置。远程分享需要持续运行的服务，接收者使用服务端模型。</p><button class="btn secondary" id="personal-share-connect">${info.cloud_sync?'更改分享服务':'连接分享服务'}</button></section>`:
    `<section class="settings-section" id="primary-model-settings"><h2>模型配置</h2><p>向管理员获取企业服务地址和企业 Token。员工不需要模型供应商的 API Key。</p><p>${info.enterprise_url?'当前服务：'+esc(info.enterprise_url):'尚未连接企业服务'}</p><button class="btn" id="settings-enterprise-connect">${info.enterprise_url?'重新连接企业服务':'连接企业服务'}</button> <button class="btn secondary" id="test-model" ${info.enterprise_model_ready?'':'disabled'}>测试企业模型</button><p id="settings-result" aria-live="polite">${esc(info.model_error||({connected:'模型调用已验证',configured:'企业凭据已配置，请测试实际模型调用',not_configured:'请先连接企业服务'}[info.model_status]||''))}</p></section>${info.enterprise_role==='admin'?'<section class="settings-section" id="admin-section"><h2>管理员设置</h2><p>正在读取企业模型与员工授权…</p></section>':''}`)+
    `<section class="settings-group" id="decision-model-settings"><h3>决策与路由模型</h3><p class="field-note">用轻量模型判断项目归属和匹配关系，减少主模型调用成本。知识整理与回答仍使用主模型。</p>`+
    (info.edition==='personal'?`<div class="field"><label for="decision-provider">模型选择</label><select id="decision-provider"><option value="main" ${info.decision_model?.provider==='main'?'selected':''}>沿用主模型（默认）</option><option value="jev" ${info.decision_model?.provider==='jev'?'selected':''}>Jev · SystemOne 决策接口</option><option value="chat" ${info.decision_model?.provider==='chat'?'selected':''}>其他 Chat Completions 模型</option></select></div><div id="decision-specific"><div class="field"><label for="decision-url">服务地址</label><input id="decision-url" value="${esc(info.decision_model?.base_url||'')}" placeholder="例如 https://api.typesafe.ai"/></div><div class="field"><label for="decision-model-name">模型名称</label><input id="decision-model-name" value="${esc(info.decision_model?.model||'')}" placeholder="Jev: jev-latest"/></div><div class="field"><label for="decision-key">API Key</label><input type="password" autocomplete="off" id="decision-key" placeholder="${info.decision_model?.has_key?'已加密保存，留空则保留原值':'填写此模型的 API Key'}"/></div></div><button class="btn secondary" id="save-decision-model">测试并保存</button><p class="field-note" id="decision-status"></p>`:
    `<p class="field-note">${info.enterprise_role==='admin'?'企业管理员可在下方统一配置决策与路由模型；员工只使用企业提供的服务，不接触 API Key。':'决策与路由模型由企业管理员统一配置；当前员工不需要额外设置。'}</p>`)+
    `</section>`+
    `<section class="settings-section"><h2>导出与备份</h2><p>Wiki 导出适合阅读与迁移；数据备份用于恢复本机知识、来源和配置，不包含模型密钥或企业 Token。</p><button class="btn secondary" id="export-wiki">导出 Markdown Wiki</button> <button class="btn secondary" id="backup-data">下载数据备份</button><p class="field-note" id="data-path"></p></section>`;
  content.querySelectorAll('[data-edition]').forEach(b=>b.onclick=async()=>{if(b.dataset.edition===info.edition)return;if(!confirmDiscard())return;await busy(b,async()=>{if(await perform(()=>api('edition',{method:'PUT',body:{edition:b.dataset.edition}}),null)){state.dirty=false;await go('settings',true)}})});
  el('settings-enterprise-connect')?.addEventListener('click',()=>openConnection());
  el('personal-share-connect')?.addEventListener('click',()=>openConnection(true));
  if(el('personal-list-models'))attachModelList('personal','model/personal/models');
  el('primary-model-settings').append(el('decision-model-settings'));
  ['personal-url','personal-model','personal-key'].forEach(n=>{if(el(n))watchChanges(el(n))});
  if(el('decision-provider')){
    const updateDecision=()=>{
      const p=el('decision-provider').value;
      el('decision-specific').style.display=p==='main'?'none':'block';
      if(p==='jev'&&!el('decision-model-name').value.trim()){
        el('decision-model-name').value='jev-latest';
      }
    };
    updateDecision();
    ['decision-provider','decision-url','decision-model-name','decision-key'].forEach(id=>watchChanges(el(id)));
    el('decision-provider').onchange=()=>{updateDecision();state.dirty=true};
    el('save-decision-model').onclick=()=>busy(el('save-decision-model'),async()=>{
      const provider=el('decision-provider').value;
      el('decision-status').textContent=provider==='main'?'正在恢复主模型…':'正在测试决策与路由模型…';
      try{
        await api('model/decision',{method:'PUT',body:{provider,
          base_url:provider==='main'?'':el('decision-url').value.trim(),
          model:provider==='main'?'':el('decision-model-name').value.trim(),
          api_key:provider==='main'?'':el('decision-key').value.trim()}});
        state.dirty=false;await go('settings',true);
        notify(provider==='main'?'项目判断已恢复使用主模型':'决策与路由模型已保存');
      }catch(e){el('decision-status').textContent=e.message}
    });
  }
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
    panel.innerHTML=`<h2>管理员设置</h2><p>只有管理员可以配置企业模型、调用限额及员工 Token。</p><div class="field"><label for="admin-url">企业模型服务地址</label><input id="admin-url" value="${esc(info.base_url)}"/></div><div class="field"><label for="admin-model">默认模型名称</label><input list="admin-model-options" id="admin-model" value="${esc(info.model)}"/></div><div class="field"><label for="admin-key">模型 API Key</label><input id="admin-key" type="password" autocomplete="off" placeholder="${info.has_api_key?'已保存，留空保留原密钥':'填写企业的模型 API Key'}"/><div class="field-note">密钥保存在企业服务端，员工无法读取。</div></div><div class="model-discovery"><button class="btn secondary small" id="admin-list-models">获取企业可用模型</button><span class="field-note" id="admin-list-result">也可以直接填写供应商提供的模型名称。</span><datalist id="admin-model-options"></datalist></div><div class="limit-fields">${[['daily_calls','每天最多调用次数'],['daily_tokens','每天最多 Token 数'],['minute_calls','每人每分钟最多调用次数']].map(([id,label])=>`<div class="field"><label for="admin-${id}">${label}</label><input id="admin-${id}" type="number" min="1" value="${info[id]}"/></div>`).join('')}</div><button class="btn" id="save-admin-model">测试连接并保存企业配置</button><p id="admin-result" aria-live="polite"></p><div class="divider"></div><h3>决策与路由模型</h3><p class="field-note">仅处理项目身份、冲突等快速决策；员工端无需 API Key，主模型仍负责生成正文。</p><div class="field"><label for="admin-decision-provider">模型选择</label><select id="admin-decision-provider"><option value="main" ${info.decision_provider==='main'?'selected':''}>沿用主模型</option><option value="jev" ${info.decision_provider==='jev'?'selected':''}>Jev · SystemOne</option><option value="chat" ${info.decision_provider==='chat'?'selected':''}>Chat Completions</option></select></div><div class="field"><label for="admin-decision-url">服务地址</label><input id="admin-decision-url" value="${esc(info.decision_url||'')}" placeholder="https://api.typesafe.ai"/></div><div class="field"><label for="admin-decision-model">模型</label><input id="admin-decision-model" value="${esc(info.decision_model||'')}" placeholder="jev-latest"/></div><div class="field"><label for="admin-decision-key">API Key</label><input type="password" id="admin-decision-key" autocomplete="off" placeholder="${info.decision_has_key?'已保存，留空保留原密钥':'输入此模型的 API Key'}"/></div><button class="btn secondary" id="save-admin-decision">测试并保存</button><p class="field-note" id="admin-decision-status"></p><div class="divider"></div><h3>员工 Token</h3><p class="field-note">新增或重新分配会生成新的 Token。撤销员工 Token 时，其已有分享链接也会停用。</p><div class="field"><label for="employee-identity">员工标识</label><input id="employee-identity" placeholder="例如 employee-a 或员工邮箱"/></div><button class="btn secondary" id="issue-employee">生成员工 Token</button><div id="issued-token"></div><div class="employee-list">${info.employees.map(e=>`<div class="job-row"><span>${esc(e.identity)}</span><span>${e.enabled?'有效':'已撤销'}</span>${e.enabled?`<button class="btn danger small" data-revoke-employee="${esc(e.identity)}">撤销</button>`:''}</div>`).join('')}</div>`;
    attachModelList('admin','admin/models');
    ['admin-decision-provider','admin-decision-url','admin-decision-model','admin-decision-key'].forEach(id=>watchChanges(el(id)));
    el('save-admin-decision').onclick=()=>busy(el('save-admin-decision'),async()=>{
      const provider=el('admin-decision-provider').value;
      el('admin-decision-status').textContent=provider==='main'?'正在恢复主模型…':'正在连接决策与路由模型…';
      try{await api('admin/decision-model',{method:'PUT',body:{
        provider,base_url:provider==='main'?'':el('admin-decision-url').value.trim(),
        model:provider==='main'?'':el('admin-decision-model').value.trim(),
        api_key:provider==='main'?'':el('admin-decision-key').value.trim()}});
        state.dirty=false;await renderAdminSettings();notify('企业决策与路由模型已保存');
      }catch(e){el('admin-decision-status').textContent=e.message}
    });
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
