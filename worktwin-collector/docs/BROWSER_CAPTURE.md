# WorkTwin 1.2.0 — 任务触发式浏览器行为采集

## 适用场景与固定决策

这项能力与本地文件夹、Codex 和 Claude Code 并列。用户在「信息采集」中手动开启，默认关闭。开关关闭时不接受扩展发来的任务信号、绑定请求或 DOM 事件。

正常业务链接由浏览器直接打开，不经过 WorkTwin 中转，也不能由插件代替用户打开。流程平台在同一个点击事件中并行通知扩展。目标网页是流程平台通过任务签名动态指定的，无需员工维护目标网站白名单。

只观察本次流程按钮打开的**首个顶层文档**，绑定浏览器 tabId、documentId、HTTPS origin 和 pathname。无论跨域、同域、刷新、单页应用 History 路由还是片段路由变化，首次页面导航后的页面均不继续观察；记录导航行为和去向的去参数 URL。新开子标签页不会自动纳入监控。

**页面观察结束不等于任务结束。** 业务任务保持 open，直到相同平台、任务及员工身份的有效签名完成信号到达。平台可在用户点击「完成」后或接收服务端状态变更后发送完成信号；不要求用户为了停止 WorkTwin 进行额外跳转。如果浏览器/电脑断开且收不到完成信号，最迟 8 小时强制到期，避免无界监控。

## 三条独立权限

1. **桌面产品功能开关**：用户授权 WorkTwin 接受浏览器任务；关闭立即终止所有正在观察的会话。
2. **浏览器扩展网站权限**：Chrome/Edge 必须允许扩展在相关页面运行。由于目标网站来自外部任务，初版使用安装时授权的广泛 http(s) content script，未收到有效任务时仅监听协议消息，不创建 DOM 观察器，也不上传页面事件。
3. **AI 处理权限**：用户另行勾选「允许 AI 分析已归并的操作步骤」，才会把任务目标与脱敏步骤发给已配置的模型。模型回复明确标为未验收的阶段分析，不自动写入当前有效知识，更不自动判定任务完成。

重要区别：流程平台动态指定的是**本次任务的采集范围**，不能绕开 Chrome/Edge 的安装权限。真正有资格发起任务的平台，由企业在部署时配置一次 Ed25519 公钥；这不是员工维护的目标网站白名单。

## 扩展安装与检测

在 WorkTwin → 信息采集，开启「任务触发式浏览器行为采集」。若没有收到插件握手，应用会弹出安装和连接指南：

1. 点击「下载插件 ZIP」（直接从运行中的 WorkTwin 应用提供，不依赖尚未上架的商店页面）。
2. 解压到一个不会删除的本地目录。
3. 在 Chrome 的扩展程序页或 Edge 的扩展程序页启用开发者模式，选择「加载已解压的扩展程序」，并按浏览器要求授予网站访问权限。
4. 回到 WorkTwin「插件安装与连接」，生成配对码（重新生成会使旧配对失效）。
5. 点击浏览器工具栏中的 WorkTwin 插件，粘贴配对码并「连接 WorkTwin」；返回应用点击「重新检测」。

插件主动向本地 http://127.0.0.1:8765 的专用 API 发送心跳。WorkTwin 只保存配对 Token 的 SHA-256 摘要，并以最近 90 秒的有效心跳显示连接状态。

**限制：** 操作系统没有统一可靠的接口枚举「默认浏览器安装并启用了某扩展」。上述状态只能证明当前运行的 Chrome/Edge 扩展与 WorkTwin 成功握手；不证明它必然是系统默认浏览器。如果用户默认使用 Safari/Firefox，初版不支持自动采集。

没有 Chrome Web Store / Edge Add-ons 的正式上架版本之前，以上是开发者模式测试安装路径；不能把它宣传成已发布的浏览器商店插件。

## 流程平台接入协议

平台服务端保管 Ed25519 私钥，WorkTwin 端只保管对应的**公开验证密钥**。部署时设置：

~~~bash
export WORKTWIN_CAPTURE_TRUSTED_PLATFORMS='{"https://workflow.example.com":"<32-byte-ed25519-public-key-base64url>"}'
~~~

默认为空：即使用户开启本地功能，也不会接受任何外部平台的启动信号。请替换为企业真实 HTTPS 业务域名，不允许本地网页或普通 HTTP 模拟生产业务平台。

签名对象为 JSON UTF-8 原始字节；票据格式：

~~~text
base64url(payload_json_bytes) + "." + base64url(ed25519_signature_bytes)
~~~

启动信号的数据字段示例：

~~~json
{
  "iss": "https://workflow.example.com",
  "action": "capture.start",
  "iat": 1791610000,
  "exp": 1791610060,
  "nonce": "random_unguessable_123456",
  "task_id": "task_123",
  "request_id": "click_456",
  "subject": "staff_789",
  "project_id": "project_321",
  "goal": "修改审批配置",
  "target_url": "https://third-party.example.com/settings?id=123"
}
~~~

iat 与 exp 必须是实时生成的 Unix 秒数（上面的时间纯属协议示意，不是可直接使用的票据）。有效期最多 120 秒；每个 issuer 的 nonce 只能使用一次。项目 ID、目标、员工身份均由可信流程平台签署，其他网页无法通过伪造 JS 消息扩充监控范围。启动信号只能用于指定的 HTTPS 页面，不能指向系统内置页面或任意 HTTP 页面。

**网页按钮保持原有 href 和 target 行为。** 如果平台已经准备好有效启动票据，在原本的按钮 click 处理器中、浏览器执行默认导航之前，同步发送：

~~~js
businessLink.addEventListener("click", () => {
  window.postMessage({
    channel: "worktwin.workflow",
    type: "capture.start",
    ticket: signedStartTicket
  }, window.location.origin);
  // 不调用 preventDefault、不调用 WorkTwin 页面，也不替换 href
});
~~~

注意：启动信号需要真实点击，不能用程序化 click 伪造。由于点击之后页面可能立即卸载，业务平台应在链接可点击前从其后端获取有效签名票据，并在过期前刷新。不能等用户点击后才异步向服务器申请签名并依赖原页面继续存在；若信号迟到，最初的操作可能遗漏且不能补造。

完成动作的数据使用相同的签名格式、issuer、task_id 和 subject，action 改为 capture.complete（target_url 非必需）。在流程平台确认任务确实已经被标记完成后发送：

~~~js
window.postMessage({
  channel: "worktwin.workflow",
  type: "capture.complete",
  ticket: signedCompleteTicket
}, window.location.origin);
~~~

完成事件不要求再次点击；支持平台前端在得到服务器任务状态更新时主动推送。如果流程平台对应页面已经关闭、本机离线或其他浏览器无法传递完成票据，该会话只能等安全到期；如需跨设备可靠完成通知，后续应增加有认证、可恢复的服务器到本地消息中继，而不是开放局域网监听端口。

## 采集结构与 AI

事件链路：

~~~text
流程平台的原生按钮点击
  ├── 正常浏览器链接导航（WorkTwin 不接管）
  └── postMessage 签名票据
        -> 扩展 Content Script
        -> 扩展 Service Worker
        -> WorkTwin localhost：校验签名和一次性 nonce
        -> 新标签页导航匹配
        -> 绑定指定 tabId + documentId + HTTPS 页面路径
        -> 被绑定的 Content Script 才开始观察 DOM
        -> 结构化事件（点击、表单修改发生、提交、页面反馈、跳转意图）
        -> SQLite browser_capture_events / browser_capture_steps
        -> 开启 AI 时按多步骤合并生成未验收的阶段总结
~~~

初版保留事件时间、脱敏元素类型/标签、页面状态提示、URL 去查询参数后的路径、事件序号和操作摘要。不读取或发送键盘逐字输入、表单值、密码、令牌、文件内容、完整 DOM、屏幕录像或浏览器历史。普通反馈提示只能证明「页面显示此信息」，不能证明服务端配置已生效。

浏览器采集数据存放在独立的 browser_capture_sessions / browser_capture_events / browser_capture_steps，不注入文件扫描的 SHA 队列。收尾时查看操作轨迹和可选的 AI 阶段总结，后续需要与统一来源证据、业务项目确认和知识更新审批衔接，不应把任何一次临时操作直接变成全局有效知识。

## 现场调试与验收要求

- 功能默认关闭；没有配对/没有企业可信签名/签名错误/信号过期/nonce 重放均拒绝。
- 已配对但没有平台按钮，浏览其他网站、已有同域页面、其他标签页，不得产生浏览器事件。
- 正常 href 点击仍直接打开，WorkTwin 只观察与该点击关联的新导航；插件未连接也不得阻塞用户。
- 页面进入后，对 DOM 的可观察操作依次入库；非授权标签页/文档/URL 的事件拒绝。
- 页面跳转后仅记录跳转，不观察新页面；即使新页面同域亦不接收事件；任务不自动完成。
- 流程平台的完成票据对应同一 issuer、task_id 和 subject 才能关闭任务；注销功能或到期也能停止。
- 断网与插件休眠可能造成事件空缺，序号缺口可见；不能称为完整录屏或绝对无损事件记录。
- 在同一任务内归并操作摘要，不凭「保存成功」页面字样宣称已验收。
- 测试包含实际 SQLite 和 FastAPI 请求、浏览器插件文件校验、原页面跳转不接管；仍需真实 Chrome/Edge 手工安装和流程平台服务端发票据的全链路现场验收。

## 现有产品边界不变

数字分身仍只读取已确认有效且经过授权的知识，完整源日志单独授权。本期浏览器事件及 AI 临时总结不会自动进入现有 MCP，防止跳过原有证据和审核规则。后续必须先设计统一来源关联、撤权和有出处的知识更新再接入。
