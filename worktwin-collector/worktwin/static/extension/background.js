/* WorkTwin browser-extension controller (Chrome / Edge Manifest V3).
 * Never opens target links. The ordinary workflow hyperlink and this
 * best-effort observation handshake happen independently.
 *
 * A workflow ticket is verified by the local WorkTwin app, not by the page.
 * Only a matched NEW navigation from the clicked workflow tab may bind.
 */
"use strict";
const BASE = "http://127.0.0.1:8765";
const SIGNAL_WINDOW_MS = 15000;
let pending = {};
let active = {};
let children = {};
let recent = [];
let deliveryQueues = {};
let binding = new Set();
const loaded = chrome.storage.session.get(["wtPending","wtActive","wtChildren"]).then(s => {
  pending = s.wtPending || {};
  active = s.wtActive || {};
  children = s.wtChildren || {};
}).catch(() => {});
const store = () => chrome.storage.session.set({
  wtPending: pending, wtActive: active, wtChildren: children
});
const now = () => Date.now();
const pathOnly = url => {
  try {const parsed = new URL(url);return parsed.protocol === "https:"
    ? parsed.origin + parsed.pathname : "";}
  catch {return ""}
};
async function extRequest(route, payload, pairingToken = null) {
  const token = pairingToken || (await chrome.storage.local.get("wtPairing")).wtPairing;
  if (!token) throw new Error("插件尚未配对");
  let response;
  try {
    response = await fetch(BASE + "/api/capture/ext/" + route, {
      method:"POST",
      headers:{"Content-Type":"application/json","X-Worktwin-Capture-Key":token},
      body:JSON.stringify(payload)
    });
  } catch {throw new Error("本地 WorkTwin 未运行或连接不可用")}
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "本地服务拒绝采集");
    error.status = response.status;
    throw error;
  }
  return data;
}
const browserName = () =>
  navigator.userAgent.includes("Edg/") ? "Edge" :
  navigator.userAgent.includes("Chrome/") ? "Chrome" : "Chromium";

async function heartbeat(token = null) {
  return extRequest("heartbeat", {browser:browserName(),
    version:chrome.runtime.getManifest().version}, token);
}
const prune = () => {
  recent = recent.filter(x => now() - x.time < SIGNAL_WINDOW_MS);
  for (const [id,p] of Object.entries(pending))
    if (now() - p.time > SIGNAL_WINDOW_MS) delete pending[id];
  for (const [tab,entry] of Object.entries(children))
    if (now() - entry.time > SIGNAL_WINDOW_MS) delete children[tab];
};
const badge = (tab, text = "", color = "#405d7a") => {
  chrome.action.setBadgeBackgroundColor({tabId:Number(tab),color}).catch(() => {});
  chrome.action.setBadgeText({tabId:Number(tab),text}).catch(() => {});
};

async function disableLocal(tabId, documentId) {
  try {await chrome.tabs.sendMessage(Number(tabId),{type:"WT_DISABLE"},{documentId});}
  catch {}
  badge(tabId);
}
async function stopActive(tabId, reason = "manual") {
  await loaded;
  const key = String(tabId);
  const record = active[key];
  if (!record) return;
  delete active[key];
  delete deliveryQueues[key];
  await store();
  await disableLocal(tabId, record.documentId);
  try {await extRequest("stop",{session_id:record.sessionId,
    tab_id:Number(tabId),document_id:record.documentId,reason});}
  catch {} // Best effort: no further events leave the extension in any case.
}
async function navigated(tabId, toUrl, reason) {
  await loaded;
  const key = String(tabId);
  const record = active[key];
  if (!record) return;
  delete active[key];
  delete deliveryQueues[key];
  await store();
  await disableLocal(tabId, record.documentId);
  try {await extRequest("navigation",{
    session_id:record.sessionId,tab_id:Number(tabId),
    document_id:record.documentId,to_url:toUrl || "",reason});}
  catch {} // Event stream stops even if backend cannot be contacted.
}
async function haltAll() {
  await loaded;
  const previous = {...active};
  pending = {};active = {};children = {};deliveryQueues = {};
  await store();
  await Promise.all(Object.entries(previous).map(([tab,record]) =>
    disableLocal(tab,record.documentId)));
}
async function sendEnable(tab,record) {
  for (let retry=0;retry<7;retry++) {
    try {
      const reply = await chrome.tabs.sendMessage(Number(tab),{
        type:"WT_ENABLE",session_id:record.sessionId,target_path:record.targetPath
      },{documentId:record.documentId});
      if (reply?.ok) {badge(tab,"REC","#bf3d38");return true;}
    } catch {}
    await new Promise(resolve => setTimeout(resolve,220));
  }
  await stopActive(tab,"observer_unavailable");
  return false;
}

async function attach(tabId,documentId,url) {
  await loaded;
  prune();
  const page = pathOnly(url);
  if (!page || !documentId || active[String(tabId)] || binding.has(tabId)) return;
  const item = Object.entries(pending).find(([,p]) =>
    p.targetPath === page &&
    now() - p.time < SIGNAL_WINDOW_MS &&
    (p.sourceTabId === tabId || children[String(tabId)]?.sourceTabId === p.sourceTabId));
  if (!item) return;
  const [id,p] = item;
  binding.add(tabId);
  try {
    await extRequest("bind",{
      session_id:id,tab_id:tabId,document_id:documentId,page_url:url});
    delete pending[id];
    const record = {sessionId:id,tabId,documentId,targetPath:p.targetPath};
    active[String(tabId)] = record;
    await store();
    await sendEnable(tabId,record);
  } catch (error) {
    delete pending[id];
    await store();
  } finally {
    binding.delete(tabId);
  }
}
async function acceptWorkflow(message,sender) {
  await loaded;
  if (!sender.tab || sender.frameId !== 0) throw new Error("只接收流程平台主页面触发");
  const senderOrigin = new URL(sender.url).origin;
  if (senderOrigin !== message.source_origin) throw new Error("流程消息来源不匹配");
  const result = await extRequest("signal",{
    ticket:message.ticket,source_origin:senderOrigin
  });
  if (result.action === "complete") {
    for (const sid of result.closed_sessions || []) {
      delete pending[sid];
      for (const [tab,record] of Object.entries({...active})) {
        if (record.sessionId === sid) {
          delete active[tab];
          await disableLocal(tab,record.documentId);
        }
      }
    }
    await store();
    return result;
  }
  pending[result.session_id] = {
    targetPath:result.target_path,sourceTabId:sender.tab.id,
    taskId:result.task_id,time:now()
  };
  prune();
  await store();
  // The native click may have opened the tab before the local signature
  // verification returned. We inspect only recently *created* navigations,
  // never arbitrary previously open tabs on the same domain.
  for (const item of recent) {
    if (item.tabId === sender.tab.id ||
        (item.sourceTabId === sender.tab.id && item.tabId !== sender.tab.id)) {
      await attach(item.tabId,item.documentId,item.url);
    }
  }
  return result;
}
async function sendEvents(message,sender) {
  await loaded;
  const tab = sender.tab?.id;
  const doc = sender.documentId;
  const record = active[String(tab)];
  if (!record || sender.frameId !== 0 || record.sessionId !== message.session_id ||
      record.documentId !== doc || record.targetPath !== pathOnly(sender.url)) {
    return {ok:false,stop:true,error:"不是当前授权的任务页面"};
  }
  const key = String(tab);
  const earlier = deliveryQueues[key] || Promise.resolve();
  const action = earlier.catch(() => {}).then(() => extRequest("events",{
    session_id:record.sessionId,tab_id:tab,document_id:doc,
    page_url:sender.url,events:message.events
  }));
  deliveryQueues[key] = action;
  try {
    await action;
    return {ok:true};
  } catch (e) {
    const refused = e.status === 401 || e.status === 403 || e.status === 409;
    if (refused) await haltAll();
    return {ok:false,stop:refused,error:e.message};
  }
}

chrome.runtime.onMessage.addListener((message,sender,reply) => {
  (async () => {
    await loaded;
    if (message?.type === "WT_FLOW_SIGNAL")
      return {ok:true,data:await acceptWorkflow(message,sender)};
    if (message?.type === "WT_PAGE_READY") {
      if (sender.frameId !== 0 || !sender.tab) return {ok:true};
      const record = active[String(sender.tab.id)];
      if (record && record.documentId === sender.documentId)
        await sendEnable(sender.tab.id,record);
      else await attach(sender.tab.id,sender.documentId,sender.url);
      return {ok:true};
    }
    if (message?.type === "WT_EVENTS") return await sendEvents(message,sender);
    if (message?.type === "WT_PAIR") {
      if (typeof message.token !== "string" || message.token.length < 32)
        throw new Error("配对码格式无效");
      await heartbeat(message.token);
      await chrome.storage.local.set({wtPairing:message.token});
      return {ok:true};
    }
    if (message?.type === "WT_STATUS") {
      const connected = await heartbeat().then(() => true).catch(() => false);
      return {ok:true,data:{connected,browser:browserName(),
        active:Object.values(active).map(r => ({tab:r.tabId,session:r.sessionId}))}};
    }
    if (message?.type === "WT_STOP_ACTIVE") {
      for (const tab of Object.keys({...active})) await stopActive(Number(tab));
      return {ok:true};
    }
    return {ok:false,error:"未知指令"};
  })().then(reply).catch(error => reply({ok:false,error:error.message}));
  return true;
});

chrome.webNavigation.onCreatedNavigationTarget.addListener(async details => {
  await loaded;
  if (active[String(details.sourceTabId)]) {
    await navigated(details.sourceTabId,details.url,"opened_next_tab");
  } else {
    children[String(details.tabId)] = {sourceTabId:details.sourceTabId,time:now()};
    await store();
  }
});
chrome.webNavigation.onCommitted.addListener(async details => {
  if (details.frameId !== 0) return;
  await loaded;
  recent.push({...details,time:now(),sourceTabId:children[String(details.tabId)]?.sourceTabId});
  prune();
  const record = active[String(details.tabId)];
  if (record && (record.documentId !== details.documentId ||
      record.targetPath !== pathOnly(details.url))) {
    await navigated(details.tabId,details.url,"document_navigation");
    return;
  }
  await attach(details.tabId,details.documentId,details.url);
});
for (const event of [chrome.webNavigation.onHistoryStateUpdated,
                     chrome.webNavigation.onReferenceFragmentUpdated]) {
  event.addListener(details => {
    if (details.frameId === 0 && active[String(details.tabId)]) {
      void navigated(details.tabId,details.url,"same_document_navigation");
    }
  });
}
chrome.tabs.onRemoved.addListener(tabId => {
  void stopActive(tabId,"tab_closed");
});
chrome.alarms.onAlarm.addListener(async event => {
  if (event.name !== "wt-heartbeat") return;
  await loaded;
  try {await heartbeat();}
  catch {await haltAll();}
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("wt-heartbeat",{periodInMinutes:1});
});
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("wt-heartbeat",{periodInMinutes:1});
});
chrome.alarms.create("wt-heartbeat",{periodInMinutes:1});
