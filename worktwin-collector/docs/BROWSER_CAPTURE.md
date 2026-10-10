# WorkTwin：任务触发式浏览器行为采集（技术预览）

## 正式产品边界

本能力与本地文件夹及 Codex/Claude Code 并列，需要用户手动开启，默认关闭。用户直接点击业务链接，原生跳转不被插件拦截或重开；同时平台向插件发送一次任务信号。插件通知 WorkTwin，由 WorkTwin 判断是否允许采集。

流程平台临时指定目标页面，无需用户维护业务网址白名单。WorkTwin 验证任务签名，绑定从流程平台链接打开的指定标签页和首个文档，不观察其他同域名、同 URL 标签页。首次网站权限仍可能需要浏览器弹窗授权，这种浏览器授权不能由 WorkTwin 绕过。

用户在首个目标网页内发生新的文档导航、SPA History API 导航、片段跳转时，WorkTwin 记录导航目的地（排除 URL query 和 fragment），立即停止原文档采集。**不会观察跳转后的网页。** 关闭标签页也停止观察。同一任务可通过多次有效按钮点击建立多个独立目标文档的采集会话，每次点击必须有新 nonce 和签名；任务完成信号统一关闭这些会话。会话元数据持续到流程平台正式发送任务完成信号、用户停止采集或本地上限超时。导航停止的任务不允许再次启动或扩大采集范围。

只记录点击、修改动作、提交、有限的反馈类型、导航和时间顺序。原始输入值、页面正文、DOM HTML、工具参数、截图和视频均不采集。事件存独立的 browser_capture_events 表，不写入原有文件扫描 documents/ai_jobs，默认不调用 AI 或发布给分身。

## 插件安装、连接状态

WorkTwin 信息采集页开启该功能后，如果尚未收到有效插件握手，弹出安装引导。WorkTwin 内提供插件 ZIP 下载与 5 分钟一次性配对码。使用者解压 ZIP，在 Chrome/Edge 扩展管理页开启开发者模式，选择“加载已解压的扩展程序”，再于插件弹窗输入配对码。

目前**没有浏览器商店版本**。应用无法通用而可靠地枚举默认浏览器是否安装扩展，实际显示“已连接／未连接”，依据插件带凭据的心跳（90 秒内有效）；未连接不意味着一定未安装。后续商店分发时改为正式安装链接。

## 流程平台身份与签名

服务端环境必须配置：

- WORKTWIN_CAPTURE_FLOW_ORIGIN = https://flow.example.com （改成实际可信平台）
- WORKTWIN_CAPTURE_FLOW_SECRET = 长度至少 32 字符的服务端共享密钥

浏览器插件的 manifest.json 内 externally_connectable.matches 也要由部署构建时替换为实际流程平台域名，不可使用全 HTTPS 通配。这个可信平台身份允许名单与“每次任务临时指定目标网页”是两件事。

平台服务器为用户点击生成短期信封，包含 action=start、task_id、target_url、nonce、expires_at、signature。expires_at 为 Unix 秒且距当前不超过 120 秒，nonce 是至少 16 字符的随机字符串，单次使用。签名为 HMAC-SHA256 hex。签名正文严格采用六行，以换行符连接：

1. action
2. task_id
3. target_url（完整原始 URL）
4. nonce
5. expires_at
6. launcher_origin（与配置的可信流程平台 Origin 一致）

插件通过 Chrome runtime.onMessageExternal 接收签名信封，工作流网页使用 chrome.runtime.sendMessage(extensionId, {type:"worktwin:task", envelope:signedEnvelope})。这条信号与原本的链接点击并行发生；建议在页面渲染时就预签发短期信封，避免等待签名接口影响跳转。链接无需经 WorkTwin 页面中转。

任务标记“完成”时平台签发新的 action=complete 信封，必须包含相同 task_id 和 target_url，但采用新的 nonce、expires_at、signature。WorkTwin 验证后关闭会话。未签名、伪造签名、重放、过期、错误来源、未配对或已关闭采集时拒绝信号。

签名密钥应通过企业受控配置下发，只进入可信服务端和员工本地 WorkTwin，**不得进入网页源码或浏览器扩展**。真实部署可考虑改为非对称签名，避免本地存在共享签名密钥。

## 本地端点

仪表盘需本地管理令牌：GET/PUT /api/browser-capture、POST /api/browser-capture/pairing、GET /api/browser-capture/extension、GET /api/browser-capture/sessions。

插件使用独立 Bearer 凭据：POST /capture/pair、/capture/heartbeat、/capture/command、/capture/bind、/capture/event。事件按 session_id + sequence 顺序写入，tabId/documentId 不匹配或文档导航后继续上报会被拒绝。关闭 WorkTwin 开关后，已配对凭据立即撤销。

## 技术预览与尚未验收范围

此次提供后端会话管理、数据持久化、扩展基础操作采集、手动权限、下载、配对及测试。**真实流程平台接入、浏览器商店发布、用户设备浏览器验收、事件聚合成业务步骤以及 AI 实时语义分析尚未完成**，不能称为完整交付。

当前仅处理顶层网页事件，无法保证观察跨域 iframe、Canvas 等复杂内容。需要对真实浏览器中的新标签页、同标签页导航、SPA、回退、关闭、插件 Worker 休眠/恢复、首次授权、未安装插件和失联逐项验收。

官方技术参考：
- Chrome 扩展消息：https://developer.chrome.com/docs/extensions/develop/concepts/messaging
- Chrome 导航事件：https://developer.chrome.com/docs/extensions/reference/api/webNavigation
- Native Messaging：https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging
