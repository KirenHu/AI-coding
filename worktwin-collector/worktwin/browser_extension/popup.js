const status=document.getElementById("status");
const pairing=document.getElementById("pairing");
const permission=document.getElementById("permission");
async function refresh(){
  const state=await chrome.runtime.sendMessage({type:"capture:status"});
  status.textContent=state.connected?"已连接本地 WorkTwin":"未连接 WorkTwin";
  pairing.hidden=!!state.connected;
  permission.hidden=!state.pendingOrigin;
  if(state.pendingOrigin)document.getElementById("site").textContent="请仅为当前任务授权："+state.pendingOrigin;
}
document.getElementById("pair").onclick=async()=>{
  const code=document.getElementById("pair-code").value.trim();
  if(!code)return;
  const r=await chrome.runtime.sendMessage({type:"capture:pair",code});
  status.textContent=r.ok?"连接成功":("连接失败："+(r.error||"请重新生成配对码"));
  await refresh();
};
document.getElementById("grant").onclick=async()=>{
  const state=await chrome.runtime.sendMessage({type:"capture:status"});
  if(!state.pendingOrigin)return;
  const granted=await chrome.permissions.request({origins:[state.pendingOrigin+"/*"]});
  if(granted)await chrome.runtime.sendMessage({type:"capture:retry"});
  await refresh();
};
refresh().catch(()=>status.textContent="插件后台暂时不可用");