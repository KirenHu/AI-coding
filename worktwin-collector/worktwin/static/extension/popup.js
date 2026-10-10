"use strict";
const $ = id => document.getElementById(id);
const state = $("state");
const message = $("message");

function send(payload) {
  return new Promise(resolve => chrome.runtime.sendMessage(payload, result => {
    const error = chrome.runtime.lastError;
    resolve(error ? {ok:false,error:error.message} : (result || {ok:false,error:"没有响应"}));
  }));
}
async function refresh() {
  const data = await send({type:"WT_STATUS"});
  if (!data.ok) {
    state.textContent = "未连接到 WorkTwin";
    message.textContent = data.error || "请先启动桌面应用，在信息采集中开启浏览器采集。";
    return;
  }
  const info = data.data || {};
  state.textContent = info.connected ? "插件已连接 · " + info.browser : "插件尚未完成配对";
  const active = (info.active || []).length;
  message.textContent = active
    ? "正在观察 " + active + " 个授权页面；发生页面跳转后自动停止。"
    : "当前没有正在观察的页面；只有经过签名的流程平台任务才能启动。";
}
$("connect").onclick = async () => {
  const token = $("pair").value.trim();
  if (!token) {message.textContent="请输入 WorkTwin 生成的配对码";return}
  const result = await send({type:"WT_PAIR",token});
  $("pair").value = "";
  message.textContent = result.ok ? "已连接。下次打开浏览器可自动恢复。" : (result.error || "无法连接");
  await refresh();
};
$("stop").onclick = async () => {
  const result = await send({type:"WT_STOP_ACTIVE"});
  message.textContent = result.ok ? "已停止当前页面观察，业务任务仍需在流程平台完成。" : (result.error || "停止失败");
  await refresh();
};
refresh();
