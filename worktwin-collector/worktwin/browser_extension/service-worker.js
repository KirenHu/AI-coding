/* WorkTwin MV3 worker: signed flow commands, tab/document-bound collection.
 * Service worker is event-driven; persistent pairing token lives in extension
 * storage. We do not inspect or capture unrelated tabs.
 */
const BASE="http://127.0.0.1:8765";
const sessions=new Map(); // sid -> {tabId,documentId,page,flowTab,seq,status}
const recent=new Map();   // tabId -> last navigation metadata, NOT DOM content
const pendingSite=new Map();
const eventQueues=new Map();
let siteSyncRunning=null;
const ready=chrome.storage.session.get("captureSessions").then(({captureSessions})=>{
  for(const item of captureSessions||[]){
    if(item && item.id && item.status!=="completed")sessions.set(item.id,item);
  }
}).catch(()=>{});
function persist(){
  return chrome.storage.session.set({captureSessions:[...sessions.values()]});
}

function page(url){
  try{const u=new URL(url);return u.protocol==="https:"?u.origin+u.pathname:""}catch{return ""}
}
function site(url){
  try{
    const u=new URL(url);
    const local=u.protocol==="http:"&&["localhost","127.0.0.1"].includes(u.hostname);
    return u.protocol==="https:"||local?u.origin:"";
  }catch{return ""}
}
async function token(){return (await chrome.storage.local.get("captureToken")).captureToken||""}
async function call(path,body){
  const credential=await token();
  const r=await fetch(BASE+path,{method:"POST",headers:{
    "Content-Type":"application/json",...(credential?{"Authorization":"Bearer "+credential}:{})
  },body:JSON.stringify(body||{})});
  const data=await r.json().catch(()=>({}));
  if(!r.ok)throw Error(data.detail||("HTTP "+r.status));
  return data;
}
async function stopIfUnused(tabId){
  if(tabId===undefined)return;
  if([...sessions.values()].some(s=>s.tabId===tabId&&s.status==="capturing"))return;
  await chrome.tabs.sendMessage(tabId,{type:"capture:stop"}).catch(()=>{});
}
async function stopRecording(){
  const tabs=new Set();
  for(const s of sessions.values())if(["armed","capturing"].includes(s.status)){
    s.status="paused";
    if(s.tabId!==undefined)tabs.add(s.tabId);
  }
  for(const tabId of tabs)await chrome.tabs.sendMessage(tabId,{type:"capture:stop"}).catch(()=>{});
  pendingSite.clear();
  await persist();
  chrome.action.setBadgeText({text:""});
}
async function heartbeat(){
  try{
    await ready;
    if(!await token())return false;
    const permissions=await chrome.permissions.getAll();
    const connection=await call("/capture/heartbeat",{version:chrome.runtime.getManifest().version,
      origins:permissions.origins||[]});
    if(!connection.enabled){await stopRecording();return true;}
    const watched=[...sessions.values()].filter(s=>
      ["armed","capturing","paused","transport_error"].includes(s.status));
    if(watched.length){
      const result=await call("/capture/sessions/status",{session_ids:watched.map(s=>s.id)});
      for(const s of watched){
        const live=result.sessions?.[s.id];
        s.status=live||"not_found";
        if(!["armed","capturing"].includes(s.status))pendingSite.delete(s.id);
      }
      for(const s of watched)await stopIfUnused(s.tabId);
      await persist();
    }
    await syncSites();
    for(const s of sessions.values())if(s.status==="capturing"&&!s.persistent)await inject(s);
    for(const s of sessions.values())if(s.status==="armed"&&!s.manual){
      for(const info of recent.values())await tryBind(s,info);
    }
    return true;
  }catch{
    await stopRecording();
    return false;
  }
}
async function bindSite(row,tab){
  const frame=await chrome.webNavigation.getFrame({tabId:tab.id,frameId:0}).catch(()=>null);
  if(!frame?.documentId||site(frame.url)!==row.origin)return;
  const granted=await chrome.permissions.contains({origins:[row.origin+"/*"]});
  if(!granted){
    for(const s of sessions.values())if(s.persistent&&s.siteId===row.id&&s.status==="capturing"){
      await closeSite(s,"connection_lost");
    }
    pendingSite.set("site-"+row.id,{origin:row.origin,siteId:row.id});
    chrome.action.setBadgeText({text:"授权"});
    return;
  }
  const existing=[...sessions.values()].find(s=>s.persistent&&s.status==="capturing"&&
    s.siteId===row.id&&s.tabId===tab.id&&s.documentId===frame.documentId);
  if(existing){await inject(existing);return;}
  const result=await call("/capture/sites/bind",{site_id:row.id,tab_id:tab.id,
    document_id:frame.documentId,current_url:frame.url});
  const s={id:result.session_id,page:row.origin,manual:true,persistent:true,siteId:row.id,
    status:"capturing",seq:result.last_seq,tabId:tab.id,documentId:frame.documentId};
  sessions.set(s.id,s);
  pendingSite.delete("site-"+row.id);
  await inject(s);
}
async function closeSite(s,reason){
  await eventQueues.get(s.tabId);
  s.status=reason;
  await call("/capture/sites/close",{session_id:s.id,reason});
  await stopIfUnused(s.tabId);
}
async function syncSites(){
  if(siteSyncRunning)return siteSyncRunning;
  siteSyncRunning=(async()=>{
    if(!await token())return;
    const remote=await call("/capture/sites/current");
    if(!remote.enabled){await stopRecording();return;}
    const allowed=remote.sites.filter(row=>row.manual_enabled);
    const tabs=await chrome.tabs.query({});
    for(const old of sessions.values()){
      if(!old.persistent||old.status!=="capturing")continue;
      const tab=tabs.find(t=>t.id===old.tabId);
      if(!allowed.some(r=>r.id===old.siteId)||!tab||site(tab.url||"")!==old.page){
        await closeSite(old,tab?"navigation_stopped":"tab_closed");
      }
    }
    for(const row of allowed){
      for(const tab of tabs)if(Number.isInteger(tab.id)&&site(tab.url||"")===row.origin){
        await bindSite(row,tab);
      }
    }
    await persist();
  })();
  try{return await siteSyncRunning}finally{siteSyncRunning=null}
}
async function status(){
  const connected=await heartbeat();
  const [active]=await chrome.tabs.query({active:true,currentWindow:true});
  const pending=[...pendingSite.values()];
  const entry=pending.find(item=>item.origin===site(active?.url||""))||pending[0];
  return {connected,pendingOrigin:entry?.origin||""};
}
async function tryBind(session,details){
  if(session.status!=="armed")return;
  // A navigation may finish before its signed command is verified. Read
  // Chrome's current frame identity instead of assuming an early event
  // carried documentId. This still binds only the clicked flow tab/child.
  if(!details.documentId){
    const frame=await chrome.webNavigation.getFrame({
      tabId:details.tabId,frameId:0
    }).catch(()=>null);
    if(!frame?.documentId)return;
    details={...details,url:frame.url,documentId:frame.documentId};
  }
  if((session.manual?site(details.url):page(details.url))!==session.page)return;
  // Only the launcher tab or a new tab with the original launcher's
  // openerTabId is eligible. A different tab showing the same URL is not.
  const tab=await chrome.tabs.get(details.tabId).catch(()=>null);
  if(!tab)return;
  if(session.manual){
    // Only the active tab intentionally visited after enabling manual capture.
    if(!tab.active)return;
  }else if(details.tabId!==session.flowTab &&
           tab.openerTabId!==session.flowTab && details.sourceTabId!==session.flowTab){
    return;
  }
  const origin=site(details.url);
  if(!origin)return;
  const granted=await chrome.permissions.contains({origins:[origin+"/*"]});
  if(!granted){
    pendingSite.set(session.id,{origin,details,session});
    chrome.action.setBadgeText({text:"授权"});
    chrome.action.setBadgeBackgroundColor({color:"#b66b2c"});
    return;
  }
  if(!details.documentId)return;
  await call("/capture/bind",{
    session_id:session.id,tab_id:details.tabId,
    document_id:details.documentId,current_url:details.url
  });
  for(const old of sessions.values())if(!old.manual&&old.id!==session.id&&
      old.tabId===details.tabId&&old.status==="capturing"){
    old.status="navigation_stopped";
  }
  session.status="capturing";
  session.tabId=details.tabId;session.documentId=details.documentId;
  sessions.set(session.id,session);
  await persist();
  pendingSite.delete(session.id);
  await inject(session).catch(()=>{});
}
function captureFrames(s,frames){
  const byId=new Map((frames||[]).map(f=>[f.frameId,f]));
  const top=byId.get(0);
  if(top?.documentId!==s.documentId||
      (s.manual?site(top.url):page(top.url))!==s.page)return [];
  function allowed(f){
    if(!f)return false;
    if(f.frameId===0)return true;
    return (site(f.url)===site(top.url)||['about:blank','about:srcdoc'].includes(f.url))
      && allowed(byId.get(f.parentFrameId));
  }
  return frames.filter(allowed);
}
async function inject(s){
  if(s.status!=="capturing")return;
  const frames=await chrome.webNavigation.getAllFrames({tabId:s.tabId});
  // Dialogs frequently embed a second document. Include only frames within
  // this authorized site; never request access to a third-party iframe.
  const allowed=captureFrames(s,frames);
  await Promise.all(allowed.map(f=>chrome.scripting.executeScript({
    target:{tabId:s.tabId,documentIds:[f.documentId]},
    files:["content.js"],injectImmediately:true
  }).catch(()=>{})));
  chrome.action.setBadgeText({text:"记录"});
  chrome.action.setBadgeBackgroundColor({color:"#196b59"});
}
async function launcherTabId(sender){
  if(!sender?.url)throw Error("无法确认发起操作的流程网页");
  if(Number.isInteger(sender.tab?.id))return sender.tab.id;
  // Chrome may omit sender.tab on external messages from ordinary web pages.
  // A uniquely matching existing tab is acceptable; never guess between tabs.
  const candidates=(await chrome.tabs.query({})).filter(t=>
    Number.isInteger(t.id) && t.url===sender.url);
  if(candidates.length!==1){
    throw Error("无法唯一识别流程平台标签页；请关闭重复的流程标签页后重试");
  }
  return candidates[0].id;
}
async function begin(envelope,sender){
  if(!sender?.url)throw Error("只能由可信流程网页触发");
  const flowTabId=await launcherTabId(sender);
  const senderOrigin=new URL(sender.url).origin;
  const answer=await call("/capture/command",{sender_origin:senderOrigin,envelope});
  if(envelope.action==="complete"){
    for(const s of sessions.values())if((answer.session_ids||[]).includes(s.id)){
      s.status="completed";pendingSite.delete(s.id);
      if(s.tabId!==undefined)stopIfUnused(s.tabId);
    }
    chrome.action.setBadgeText({text:""});
    await persist();
    return answer;
  }
  const s={id:answer.session_id,page:answer.target_page,flowTab:flowTabId,
    status:answer.status,seq:0};
  sessions.set(s.id,s);
  await persist();
  // Navigation may already have committed while WorkTwin verified the signal.
  for(const info of recent.values()){
    if(Date.now()-info.at<20000&&page(info.url)===s.page){
      await tryBind(s,info).catch(()=>{});
      if(s.status==="capturing")break;
    }
  }
  return answer;
}

function enqueueEvent(s,kind,label,currentUrl,documentId){
  if(s.status!=="capturing")return Promise.resolve({ok:false});
  const previous=eventQueues.get(s.tabId)||Promise.resolve();
  const next=previous.catch(()=>{}).then(async()=>{
    if(s.status!=="capturing")return;
    const seq=s.seq+1;
    const tasks=s.persistent?[...sessions.values()].filter(t=>!t.manual&&
      t.status==="capturing"&&t.tabId===s.tabId&&t.documentId===s.documentId).map(t=>t.id):[];
    const answer=await call(s.persistent?"/capture/sites/event":"/capture/event",{
      session_id:s.id,seq,kind,tab_id:s.tabId,
      document_id:documentId||s.documentId,
      current_url:currentUrl,label:label||"",
      ...(s.persistent?{task_session_ids:tasks}:{}),
    });
    for(const [id,seq] of Object.entries(answer.task_sequences||{})){
      const task=sessions.get(id);if(task)task.seq=seq;
    }
    s.seq=answer.ack;
    await persist();
    if(answer.status==="navigation_stopped"||answer.status==="tab_closed"){
      s.status=answer.status;
      await persist();
      stopIfUnused(s.tabId);
      chrome.action.setBadgeText({text:""});
    }
  }).catch(()=>{s.status="transport_error";persist().catch(()=>{});if(s.tabId!==undefined)stopIfUnused(s.tabId);chrome.action.setBadgeText({text:"!"})});
  eventQueues.set(s.tabId,next);
  // A content script is acknowledged only after SQLite accepted the event.
  // Do not report success for an event merely queued in a volatile worker.
  return next.then(()=>({ok:s.status==="capturing"}));
}
chrome.runtime.onMessageExternal.addListener((message,sender,reply)=>{
  if(message?.type!=="worktwin:task"||!message.envelope)return;
  ready.then(()=>begin(message.envelope,sender)).then(data=>reply({ok:true,...data}))
    .catch(e=>reply({ok:false,error:String(e.message||e)}));
  return true;
});
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(message?.type==="capture:status"){
    ready.then(status).then(reply);return true;
  }
  if(message?.type==="capture:pair"){
    call("/capture/pair",{code:message.code}).then(async data=>{
      await chrome.storage.local.set({captureToken:data.token});
      reply({ok:await heartbeat()});
    }).catch(e=>reply({ok:false,error:e.message}));
    return true;
  }
  if(message?.type==="capture:retry"){
    ready.then(async()=>{await syncSites();await Promise.all([...pendingSite.values()].filter(x=>x.session).map(x=>tryBind(x.session,x.details)));})
      .then(()=>reply({ok:true})).catch(e=>reply({ok:false,error:e.message}));
    return true;
  }
  if(message?.type==="capture:event" && sender?.tab?.id){
    ready.then(async()=>{
    const currentFrames=await chrome.webNavigation.getAllFrames({tabId:sender.tab.id});
    const currentTop=currentFrames.find(frame=>frame.frameId===0);
    const candidates=[...sessions.values()].filter(x=>x.status==="capturing"&&
      x.tabId===sender.tab.id&&x.documentId===currentTop?.documentId);
    const s=candidates.find(x=>x.persistent)||candidates[0];
    if(s && ["click","change","submit","feedback"].includes(message.kind)){
      const frames=captureFrames(s,currentFrames);
      const top=frames.find(f=>f.frameId===0);
      const frame=frames.find(f=>f.frameId===sender.frameId);
      if(frame?.documentId!==sender.documentId){reply({ok:false});return;}
      // The session remains bound to the top document. Child navigation must
      // not rebind or stop the whole task. Chrome supplies frame identity.
      enqueueEvent(s,message.kind,message.label,top.url,s.documentId)
        .then(reply).catch(e=>reply({ok:false,error:String(e.message||e)}));
    }else reply({ok:false});
    });return true;
  }
});
chrome.webNavigation.onCreatedNavigationTarget.addListener(details=>{
  // Only retain a short-lived navigation candidate from a trusted workflow
  // page. This is not a collection session and no DOM event is transmitted.
  chrome.tabs.get(details.sourceTabId).then(source=>{
    const siteOrigin=site(source.url||"");
    const trusted=(chrome.runtime.getManifest().externally_connectable?.matches||[])
      .some(pattern=>pattern.startsWith(siteOrigin+"/") && siteOrigin);
    if(trusted)recent.set(details.tabId,{
      tabId:details.tabId,sourceTabId:details.sourceTabId,
      openerTabId:details.sourceTabId,url:details.url,at:Date.now()
    });
  }).catch(()=>{});
});
chrome.tabs.onCreated.addListener(tab=>{
  if(tab.openerTabId===undefined)return;
  // Keep ephemeral navigation metadata only when the opener is the
  // explicitly connected workflow site, not for unrelated browsing.
  chrome.tabs.get(tab.openerTabId).then(parent=>{
    const trusted=(chrome.runtime.getManifest().externally_connectable?.matches||[])
      .some(pattern=>pattern.startsWith(new URL(parent.url||"about:blank").origin+"/"));
    if(trusted)recent.set(tab.id,{
      tabId:tab.id,openerTabId:tab.openerTabId,url:tab.pendingUrl||"",at:Date.now()
    });
  }).catch(()=>{});
});
async function manualNavigation(s,details){
  if(!details.documentId){
    const frame=await chrome.webNavigation.getFrame({
      tabId:details.tabId,frameId:0
    }).catch(()=>null);
    details={...details,documentId:frame?.documentId,url:frame?.url||details.url};
  }
  await enqueueEvent(s,"navigation","",details.url,s.documentId);
  if(s.status!=="capturing"||!details.documentId)return;
  await call("/capture/manual/rebind",{
    session_id:s.id,tab_id:s.tabId,
    document_id:details.documentId,current_url:details.url
  });
  s.documentId=details.documentId;
  await persist();
  await inject(s);
}
chrome.tabs.onActivated.addListener(()=>ready.then(syncSites).catch(()=>{}));
chrome.webNavigation.onCommitted.addListener(details=>{
  if(details.frameId!==0){
    ready.then(()=>{
      for(const s of sessions.values())if(s.status==="capturing"&&s.tabId===details.tabId){
        inject(s).catch(()=>{});
      }
    });
    return;
  }
  ready.then(()=>{
  const previous=recent.get(details.tabId);
  const info={...details,at:Date.now(),
    openerTabId:previous?.openerTabId,sourceTabId:previous?.sourceTabId};
  if(previous||[...sessions.values()].some(x=>x.status==="armed" &&
      (x.flowTab===details.tabId || x.page===page(details.url)))){
    recent.set(details.tabId,info);
  }
  for(const s of sessions.values()){
    if(s.status==="capturing" && s.tabId===details.tabId){
      // Even same URL reload creates a new document: stop at first committed
      // navigation. Never attach to the replacement document.
      if(details.documentId!==s.documentId){
        if(s.persistent){
          closeSite(s,"navigation_stopped").then(syncSites).catch(()=>{});
        }else if(s.manual)manualNavigation(s,info).catch(()=>{});
        else enqueueEvent(s,"navigation","",details.url,s.documentId);
      }
    }else if(s.status==="armed"){
      if(s.manual)syncSites().catch(()=>{});
      else tryBind(s,info).catch(()=>{});
    }
  }
  syncSites().catch(()=>{});
  });
});
chrome.webNavigation.onHistoryStateUpdated.addListener(details=>{
  if(details.frameId!==0)return;
  ready.then(()=>{
  for(const s of sessions.values())if(s.status==="capturing" && s.tabId===details.tabId){
    enqueueEvent(s,"navigation","",details.url,s.documentId);
  }
  });
});
chrome.webNavigation.onCompleted.addListener(details=>{
  ready.then(()=>{
  for(const s of sessions.values())if(s.status==="capturing"&&s.tabId===details.tabId){
    inject(s).catch(()=>{});
  }
  });
});
chrome.webNavigation.onReferenceFragmentUpdated.addListener(details=>{
  if(details.frameId!==0)return;
  ready.then(()=>{
  for(const s of sessions.values())if(s.status==="capturing"&&s.tabId===details.tabId){
    enqueueEvent(s,"navigation","",details.url,s.documentId);
  }
  });
});
chrome.tabs.onRemoved.addListener(tabId=>{
  ready.then(()=>{
  for(const s of sessions.values())if(s.status==="capturing"&&s.tabId===tabId){
    if(s.persistent)closeSite(s,"tab_closed").catch(()=>{});
    else enqueueEvent(s,"tab_closed","",s.page,s.documentId);
  }
  recent.delete(tabId);
  });
});
chrome.alarms.create("capture-heartbeat",{periodInMinutes:0.5});
chrome.alarms.onAlarm.addListener(alarm=>{
  if(alarm.name==="capture-heartbeat")heartbeat().catch(()=>{});
});

chrome.runtime.onStartup.addListener(()=>heartbeat());
chrome.permissions.onRemoved.addListener(()=>stopRecording().then(heartbeat));
