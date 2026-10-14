# WorkTwin 1.5.9：更新处理、编程助手与客户端接入

## 本次决策

质量优先。正文完全相同或仅换行编码、文末空行变化时跳过提取；来源、权限和元数据仍同步。新增独立 Markdown 章节可提交变化及上下文。修改、删除、数字变化、否定反转、段落移动和不能可靠判断的情况走完整处理。文章维护仍核对完整当前来源。不开启语义相似度跳过，不引入 Embeddings。

同一研究对象的技术说明与商业评估允许分篇，但属于同一个对象组；文章阅读目标不能替代对象身份。

## 已落地的入口

- Codex、Claude Code、Cursor CLI：首次开启 AI 使用时从当前进度开始，之后按新增记录整理。补录历史必须在来源设置选择日期范围；范围是已采集、带时间戳的记录，日期明确标为 UTC。无时间戳记录不会被猜测归入历史日期。
- 补录与日常水位分开，补录不倒退日常进度；现有任务执行时不替换其范围。关闭 AI 使用会停止排队，重新开启从当时进度开始。
- Codex 原生 task_started/task_complete 存在时优先按完整任务选取；旧格式和其他适配器保留有限批次与前序上下文。典型任务最多 120,000 字符一起读取；超过后保留 24,000 字符重叠上下文分批。超长任务仍由提取器分批，不能宣称任意长对话始终一次完整输入。
- 项目成果文件仍通过用户选择的文件夹采集。对话只提供决定、纠正及背景；工具日志只保留动作摘要，不把“AI 说完成”当成已验证成果。
- 明确选中文件夹内的 AGENTS.md、CLAUDE.md、GEMINI.md、SKILL.md、MEMORY.md、preferences.md 单独登记为规则、技能或记忆，记录目录作用域，不提取成项目事实。来源设置可查看。插件缓存与内置 .system 技能排除。
- 本版不擅自扩大到用户未选中的隐藏配置目录，也不自动把历史文章重写成个人偏好。规则文件是可查的参考资料，不自动执行其中指令。

## 适配器能力

| 入口 | 实际读取 | 不宣称支持 |
| --- | --- | --- |
| Codex | 本机可识别 JSONL 的公开用户/助手消息、任务边界、工具动作摘要 | 云端未落盘会话、隐藏推理 |
| Claude Code | 本地项目 JSONL 可见消息及工具摘要 | Claude Desktop 云会话 |
| Cursor CLI | stream-json 导出 | Cursor IDE 私有数据库中的全部聊天 |
| Google Antigravity | brain、knowledge 的 Markdown 成果文档 | 完整对话、缓存和插件内容 |

## 模型实测

采用隔离 SQLite，生产库只以 mode=ro 读取连接设置，通过钥匙串取凭据；没有上传未获 AI 授权的生产来源。

1. 文章维护：退款与部署分别成篇。已确认的新规则把普通退款审批从两人改为一人，晚导入的旧会议及未确认的免审批建议没有覆盖现行规则。退款文章更新至 v2，部署文章保持 v1。经历一次 JSON 解析失败，补充结构化输出约束后复跑通过；一次通过不能证明所有输出始终有效。
2. 对象分组：发现模型把同一对象的商业与技术内容分组，明确对象与阅读目标边界后复跑；再次核对发现同一对象的能力与部署仍为两条短文，补充“互补章节也应合并”的通用判断后，最终为两个对象组、两篇连贯文章。以验收目录中的 verified-result.json 为最终结果，保留初次失败记录。
3. 增量提取：使用本次用户提出的 WorkTwin 需求改写为测试文档，并扩展背景以检验选择范围。完整输入 1,971 Token、增量 880 Token；均提取到手动开关持续采集、平台任务结束不得关闭手动采集。两次均调用一次模型。供应商报告费用分别为 0.00016877、0.00008025 美元。不是生产平均收益，也不包含后续文章编辑费用。
4. 20 个短判断样例对照：首轮发现任务类型与判分说明不够明确，保留原结果后补充统一规则复测。复测 GLM-5.3-Flash 严格标签命中 20/20，固定 Nemotron-3-Super 免费端点 17/20；费用分别为供应商报告的 0.00067724 美元及 0 美元，累计响应约 26.1 秒与 21.0 秒。这是短分类试验，不等于整体知识质量评分。免费模型误判较晚导入的旧材料、模糊确认和同一对象的不同章节，不能承担静默跳过或替代主模型。生产模型保持原设置。

NVIDIA 免费端点还明确要求不上传机密与个人数据，因此本轮仅用虚构样本。依据：[OpenRouter 模型页面](https://openrouter.ai/nvidia/nemotron-3-super-120b-a12b:free)。不使用随机 free router 作为生产默认。

## MCP 客户端

数字分身的连接页面提供 Cursor、VS Code、Claude Desktop 配置切换、复制和一键写入。写入会保留其他配置项并备份旧文件。Claude Desktop 使用安装包内的 stdio 桥接，不要求安装 Node.js。HTTP 和桥接都调用同一分身授权接口；撤销或换密钥后旧连接失效。

验证覆盖协议初始化、工具列表、授权文章读取和现有授权回收测试。客户端本身的全版本组合不在本次验证范围。客户端未安装时可保存配置，但不能宣称该客户端已经完成运行验收。

参考：[MCP 本地客户端](https://modelcontextprotocol.io/docs/develop/connect-local-servers)、[VS Code MCP](https://code.visualstudio.com/docs/copilot/customization/mcp-servers)、[Cursor MCP](https://cursor.com/docs/context/mcp)。

## 统计与回退

`GET /api/processing/stats` 按模型和处理阶段返回调用数、输入输出 Token、耗时和供应商报告费用。没有可靠费用字段的调用记为未知，不用字符数冒充 Token 或费用。旧记录的阶段为 unclassified，无法追溯补造。

`WORKTWIN_DOCUMENT_INCREMENTAL=0` 或数据库现有 `document_incremental_enabled=0` 回到完整文档处理，确定性去重仍启用。筛选错误自动走完整处理。语义筛选未启用，不存在需要用户管理的向量依赖。

签名新增 signed_validation 手动参数：可对当前研发分支签名、公证并生成验收产物，不自动发布。正式 Release 仍要求 main、双架构、signed_release=true。本机更新前保存一致 SQLite 备份与原 App；本次安装和签名验收结果另见交付记录。

## 本机验收记录

2026-10-13 已安装并运行 1.5.9。344 项 Python 回归通过；十组浏览器验收通过；冻结 App 冒烟与包内 stdio MCP 初始化、列工具、读取授权文章通过。运行版本和安装包版本一致，模型凭据成功加载，SQLite quick_check 为 ok；安装前后的来源采集、AI 使用、分享权限逐项一致。签名 CI 结果独立记录，不以本机 ad-hoc 签名冒充正式公证。


## 最终签名安装结果

2026-10-13 本机已替换为 Developer ID 签名、Apple 公证的 1.5.9（应用源码提交 04ba011）。下载的 ARM DMG 和挂载后的 App 均通过 stapler 与 Gatekeeper；同一包完成冻结应用冒烟、MCP 协议初始化、读取及撤销验证，再安装到 /Applications/WorkTwin.app。运行版本 1.5.9，模型凭据可用，SQLite quick_check 为 ok，采集、AI、分享授权与安装前备份一致。

[构建记录](https://github.com/KirenHu/AI-coding/actions/runs/38378402772)：Python 与浏览器验收通过；Intel macOS 的签名、公证、Gatekeeper、冻结应用及 MCP 验收通过；Windows 冻结应用及 MCP 验收通过。该轮 ARM CI 在公证成功后的 CloudKit 网络核验失败；同一签名包随后在本机完整核验通过，不将该轮 CI 描述成全绿。b793ef2 仅把已验证的网络重试复用到安装包验收，未改变应用逻辑；[流水线复跑](https://github.com/KirenHu/AI-coding/actions/runs/38379523806)单独保留。

Windows 产物仍未签名，未宣称完成 Windows 可信发行；正式签名需要相应证书或签名服务。未执行正式 Release 发布、PR 合并或生产历史批量重处理。

本机证据位于 release/local-install-signed-1.5.9.json 与 release/local-verification-signed-1.5.9.json；安装记录包含数据库快照与原 App 的完整路径。保留两者用于回退，不能只降级程序却混用不兼容的数据库。真实生产资料的整体知识质量与月度成本尚未测量，前述小样本结果不代替长期效果。
