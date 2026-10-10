# WorkTwin · 任务触发式浏览器行为采集（1.2.1 两种采集模式）

## 产品定位

这是 WorkTwin「信息采集」中的独立、手动开启、默认关闭的功能。其结果是**准确、精简、有事件依据的自然语言操作摘要**，而不是知识库条目。不会使用流程平台的 SOP、操作指引或任务目标来猜测用户行为，也不会将记录同步到个人知识库、AI Agent 或数字分身。

用户点击流程平台的业务链接后，浏览器直接打开目标网页。插件并行接收可信平台签发的一次性任务信号，WorkTwin 仅观察该链接打开的**首个目标文档**。同一任务可以多次点击不同业务链接；每个链接都获得独立、短时授权。即使其他标签页也打开相同网址，仍不得观察。

首个文档发生完整导航、History API 路由切换或片段跳转时，记录目标位置并停止观察，不跟随下一个网页。任务继续运行，直到流程平台状态为 completed/cancelled，或用户停用/任务超时/状态接口不可访问。这两个生命周期分别管理。

## 任务结束：平台提供状态接口，WorkTwin 主动查询

WorkTwin 是运行在员工电脑上的本地进程，外部服务器通常无法直接访问它的 loopback 地址，因此不强制让业务系统主动向本机回调。

**统一接入协议**：流程平台在首次发送 start 任务信号时，必须同时提供经签名的 status_path（状态查询路径）。WorkTwin 每约十秒向同一可信平台发起状态查询。检测到 completed/cancelled 后，停止该任务所有采集会话；连续三次查询失败也停止采集并记录 status_unavailable。浏览器插件通过本地心跳读取停止状态，及时移除 DOM 监听。即使插件尚未收到通知，WorkTwin 也立即拒绝已结束会话继续写入事件。额外的签名 complete 信号可作为快速结束路径。

**不接受任意外部域名或内网 URL 作为状态查询地址**。只有部署时配置的流程平台 HTTPS Origin 可被访问；status_path 必须是规范的 /api/ 相对路径，无协议、主机、端口、查询参数或 .. 等路径跳转。

## 流程平台如何接入

部署时需要提供：

- WORKTWIN_CAPTURE_FLOW_ORIGIN：实际可信流程平台 HTTPS Origin，当前没有，留空
- WORKTWIN_CAPTURE_FLOW_SECRET：平台服务端和本地 WorkTwin 共享的高熵签名密钥（长度至少 32 字符），当前没有，留空
- 浏览器插件 externally_connectable.matches：只允许已配置的流程平台站点向插件发消息。仓库中的 flow.example.com 仅用于自动化测试，不代表真实平台域名

密钥只在流程平台后端和本地 WorkTwin 使用，绝不放在浏览器源码、页面脚本或扩展中。

### 开始监控时的信封

以下为协议形状示例，expires_at 必须使用最新有效的 Unix 时间戳，signature 由流程平台服务端计算：

~~~json
{
  "action": "start",
  "task_id": "task-123",
  "target_url": "https://partner.example.com/order/1",
  "status_path": "/api/worktwin/tasks/task-123/status",
  "nonce": "random_nonce_at_least_16_chars",
  "expires_at": 1791700000,
  "signature": "服务器计算的 HMAC-SHA256 hex"
}
~~~

HMAC-SHA256 的输入严格按以下七行拼接（每行之间换行）：

~~~text
action
task_id
target_url
nonce
expires_at
launcher_origin
status_path
~~~

start 和 complete 都使用这七项。nonce 不得复用；expires_at 只能在当前时间之后且最多 120 秒。一个任务的 status_path 必须始终一致。新按钮点击生成新 nonce，已有任务完成后不得重新开启采集。

网页在原始业务链接点击时**并行**通知插件：

~~~javascript
chrome.runtime.sendMessage(extensionId, {
  type: "worktwin:task",
  envelope: preSignedEnvelopeFromFlowBackend
}, (response) => {
  // 仅提示采集状态；原链接照常打开，不经 WorkTwin 中转。
});
~~~

### 任务状态查询接口

WorkTwin 直接请求固定 Origin + 已签名的 status_path，HTTPS 严格验证，禁用重定向，最多读取 4 KB。

~~~http
GET /api/worktwin/tasks/task-123/status HTTP/1.1
X-WorkTwin-Task: task-123
X-WorkTwin-Timestamp: 1791700010
X-WorkTwin-Signature: [HMAC-SHA256 hex]
Accept: application/json
~~~

请求签名的正文是以下四行：

~~~text
GET
task-123
/api/worktwin/tasks/task-123/status
1791700010
~~~

平台需校验请求签名、时间戳和任务范围，返回：

~~~json
{"task_id":"task-123","status":"running"}
~~~

status 只允许 running、completed、cancelled。流程平台**不必提供操作步骤、任务目标或 SOP**。

## 插件安装与权限

用户开启浏览器采集后，如果尚未收到插件握手，WorkTwin 显示引导。可从应用下载插件 ZIP，在 Chrome/Edge 中加载解压的开发者扩展，再用 WorkTwin 生成的五分钟一次性配对码连接。

WorkTwin 无法可靠枚举默认浏览器已安装的插件，因此以带凭据的心跳认定「已连接」；未连接不等于未安装。首次为某目标网页授予浏览器扩展权限，仍可能需要用户确认，不能绕过 Chrome/Edge 的授权机制；无需手动维护业务网站白名单。

本地管理接口：
- GET/PUT /api/browser-capture：采集总开关与状态
- PUT /api/browser-capture/ai：独立授权是否使用已配置模型概括脱敏事件
- POST /api/browser-capture/pairing、GET /api/browser-capture/extension：插件配对/下载
- GET /api/browser-capture/sessions：已建立的观察会话
- GET /api/browser-capture/sessions/{session_id}/summary：当前操作自然语言摘要与依据序号
- POST /api/browser-capture/sessions/{session_id}/summarize：手动刷新摘要

插件独立身份验证的接口：POST /capture/pair、/capture/heartbeat、/capture/sessions/status、/capture/command、/capture/bind、/capture/event。所有事件必须与本任务的 tabId、documentId、URL 范围和递增序号一致。

## 操作摘要的生成

插件只提取互动控件的短标签、动作类型、有限的页面状态提示（成功/失败分类），不保存输入内容、选项实际值、整页 DOM、视频或截图。URL 查询参数、片段不进入事件库。明确的页面成功提示只能表述为「页面提示操作成功」，不能断言后台已完成。

WorkTwin 在本地对事件排序、去重，生成有证据序号的自然语言句子。独立允许 AI 分析后，可以将少量脱敏事件交给已配置模型作更自然的压缩表述；模型失败、输出缺乏引用或虚构结果时使用本地摘要。**不生成知识库条目，不调用知识整理 ai_jobs，也不自动分发给其他数字分身。**

## 当前验收范围及待完成

后端测试包含：授权/签名、接口路径、重复信号、任务完成/断线停止、不同标签页隔离、DOM 事件入库、证据序号、AI 权限、与现有知识库隔离。

Playwright 测试使用实际 Chromium MV3 扩展配合虚拟 HTTPS 流程网页和目标网页，检查原生链接跳转、行为采集、导航停止及其他标签页不被观察。虚拟流程平台**不等于真实业务系统已联调**。

真实流程平台域名、签名服务、商店插件、真实 Chrome/Edge 多网站兼容、iframe、Canvas、初次权限交互、物理设备长时间运行和模型摘要生产准确率，仍需要专门验收。Apple 开发者签名公证仍是独立事项。


## 1.2.1：用户主动指定网站进行手动采集

流程平台尚未开发时，不必伪造平台信号。WorkTwin 在「信息采集」中提供独立「手动记录指定网站」，用户输入完整网址、点击开始并切到该网站的活动浏览器标签页；插件得到本地 WorkTwin 签发的单次会话 ID 后，仅绑定这一标签页。手动模式不依赖流程平台的 HMAC、status_path 或模拟网页。HTTPS 站点可用；本地测试仅允许 http://localhost 或 http://127.0.0.1。

手动模式的页面生命周期与企业任务模式不同：同源页面的导航会产生「页面导航」事件，并在新文档完成绑定后继续观察；离开指定网站、关闭原标签、手动结束或达到八小时时限则停止。浏览器插件仅在已经开启总开关并由用户发起手动会话时才有该权限，首次站点权限由用户在扩展弹窗批准。

手动启动/停止接口：PUT /api/browser-capture/manual（本地面板 Token）。插件仅通过本地已配对凭据调用 POST /capture/manual/current 和 /capture/manual/rebind；不接收任意远程网页下发的手动命令。保留现有 task 模式所有签名检查、导航停止和状态轮询，不改其协议。

用户可在「信息采集 → 网页操作记录」直接查看手动与任务采集的每个历史会话，展开完整事件与发生时间、脱敏路径、操作摘要。对应 API：GET /api/browser-capture/sessions?limit=20&offset=0，以及 GET /api/browser-capture/sessions/{session_id}/events?limit=100&offset=0。此信息保持独立，不写入知识库、MCP 和数字分身。
