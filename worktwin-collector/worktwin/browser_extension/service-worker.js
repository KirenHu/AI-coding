/* WorkTwin MV3 worker: signed flow commands, tab/document-bound collection.
 * Service worker is event-driven; persistent pairing token lives in extension
 * storage. We do not inspect or capture unrelated tabs.
 */
const BASE="http://127.0.0.1:8765";
const sessions=new Map(); // sid -> {tabId,documentId,page,flowTab,seq,status}
const recent=new Map();   // tabId -> last navigation metadata, NOT DOM content
const pendingSite=new Map();
const eventQueues=new Map();
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
  try{const u=new URL(url);return u.protocol==="https:"?u.origin:""}catch{return ""}
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
async function heartbeat(){
  try{if(await token())await call("/capture/heartbeat");return true}
  catch{return false}
}
async function status(){
  const connected=await heartbeat();
  const entry=[...pendingSite.values()][0];
  return {connected,pendingOrigin:entry?.origin||""};
}
async function tryBind(session,details){
  if(session.status!=="armed"||page(details.url)!==session.page)return;
  // Only the launcher tab or a new tab with the original launcher's
  // openerTabId is eligible. A different tab showing the same URL is not.
  const tab=await chrome.tabs.get(details.tabId).catch(()=>null);
  if(!tab || (details.tabId!==session.flowTab && tab.openerTabId!==session.flowTab && details.sourceTabId!==session.flowTab))return;
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
  session.status="capturing";
  session.tabId=details.tabId;session.documentId=details.documentId;
  sessions.set(session.id,session);
  await persist();
  pendingSite.delete(session.id);
  await inject(session).catch(()=>{});
}
async function inject(s){
  if(s.status!=="capturing")return;
  await chrome.scripting.executeScript({
    target:{tabId:s.tabId,documentIds:[s.documentId]},
    files:["content.js"],injectImmediately:true
  });
  chrome.action.setBadgeText({text:"记录"});
  chrome.action.setBadgeBackgroundColor({color:"#196b59"});
}
async function begin(envelope,sender){
  if(!sender?.url||!sender?.tab?.id)throw Error("只能由可信流程网页的实际标签页触发");
  const senderOrigin=new URL(sender.url).origin;
  const answer=await call("/capture/command",{sender_origin:senderOrigin,envelope});
  if(envelope.action==="complete"){
    for(const s of sessions.values())if(s.id===answer.session_id){
      s.status="completed";pendingSite.delete(s.id);
      if(s.tabId!==undefined)chrome.tabs.sendMessage(s.tabId,{type:"capture:stop"}).catch(()=>{});
    }
    chrome.action.setBadgeText({text:""});
    await persist();
    return answer;
  }
  const s={id:answer.session_id,page:answer.target_page,flowTab:sender.tab.id,
    status:"armed",seq:0};
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
  if(s.status!=="capturing")return;
  const previous=eventQueues.get(s.id)||Promise.resolve();
  const next=previous.catch(()=>{}).then(async()=>{
    if(s.status!=="capturing")return;
    const seq=s.seq+1;
    const answer=await call("/capture/event",{
      session_id:s.id,seq,kind,tab_id:s.tabId,
      document_id:documentId||s.documentId,
      current_url:currentUrl,label:label||""
    });
    s.seq=answer.ack;
    await persist();
    if(answer.status==="navigation_stopped"||answer.status==="tab_closed"){
      s.status=answer.status;
      await persist();
      chrome.tabs.sendMessage(s.tabId,{type:"capture:stop"}).catch(()=>{});
      chrome.action.setBadgeText({text:""});
    }
  }).catch(()=>{s.status="transport_error";persist().catch(()=>{});chrome.action.setBadgeText({text:"!"})});
  eventQueues.set(s.id,next);
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
    ready.then(()=>Promise.all([...pendingSite.values()].map(x=>tryBind(x.session,x.details))))
      .then(()=>reply({ok:true})).catch(e=>reply({ok:false,error:e.message}));
    return true;
  }
  if(message?.type==="capture:event" && sender?.tab?.id){
    ready.then(()=>{
    const s=[...sessions.values()].find(x=>
      x.status==="capturing" && x.tabId===sender.tab.id &&
      (!sender.documentId||x.documentId===sender.documentId));
    if(s && ["click","change","submit","feedback"].includes(message.kind)){
      enqueueEvent(s,message.kind,message.label,s.page,s.documentId);
      reply({ok:true});
    }else reply({ok:false});
    });return true;
  }
});
chrome.webNavigation.onCreatedNavigationTarget.addListener(details=>{
  // Browser-provided sourceTabId is stronger than tab.openerTabId, which may
  // be absent for links opened with noopener.
  recent.set(details.tabId,{
    tabId:details.tabId,sourceTabId:details.sourceTabId,
    openerTabId:details.sourceTabId,url:details.url,at:Date.now()
  });
});
chrome.tabs.onCreated.addListener(tab=>{
  if(tab.openerTabId!==undefined)recent.set(tab.id,{
    tabId:tab.id,openerTabId:tab.openerTabId,url:tab.pendingUrl||"",at:Date.now()
  });
});
chrome.webNavigation.onCommitted.addListener(details=>{
  if(details.frameId!==0)return;
  ready.then(()=>{
  const previous=recent.get(details.tabId);
  const info={...details,at:Date.now(),
    openerTabId:previous?.openerTabId,sourceTabId:previous?.sourceTabId};
  recent.set(details.tabId,info);
  for(const s of sessions.values()){
    if(s.status==="capturing" && s.tabId===details.tabId){
      // Even same URL reload creates a new document: stop at first committed
      // navigation. Never attach to the replacement document.
      if(details.documentId!==s.documentId){
        enqueueEvent(s,"navigation","",details.url,s.documentId);
      }
    }else if(s.status==="armed"){
      tryBind(s,info).catch(()=>{});
    }
  }
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
  if(details.frameId!==0)return;
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
    enqueueEvent(s,"tab_closed","",s.page,s.documentId);
  }
  recent.delete(tabId);
  });
});
chrome.alarms.create("capture-heartbeat",{periodInMinutes:1});
chrome.alarms.onAlarm.addListener(alarm=>{
  if(alarm.name==="capture-heartbeat")heartbeat().catch(()=>{});
});
