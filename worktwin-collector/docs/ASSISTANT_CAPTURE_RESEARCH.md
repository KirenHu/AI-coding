# 编程助手数据接入：证据与取舍

2026-10-13。本轮添加 Antigravity 成果文档适配，并研究现有 Codex 管线；未改变既有助手的采集或模型授权。

## 本机核验事实

- Codex sessions 目录有 776 个 JSONL，原始文件共 5,996,505,452 字节。WorkTwin 的 776 份 Codex 文档及 transcript_events 证明已实际读取；规范化消息 31,352 条，共 17,551,323 字符。另有工具调用 90,695 条、工具结果 90,691 条，text 均为空，只保存事件元数据。原始字节、消息字符不能直接当作模型 token 或账单。
- `worktwin/ingest/adapters.py` 识别 session_meta、response_item、event_msg 等结构，保留用户/助手可见文本及工具结果状态，不保存工具输出正文和独立 reasoning 记录。`ingest/__init__.py` 按文件偏移增量读取、去重，并按 processed_seq 提取新批次。默认批次为 80 个消息事件、约 20,000 字符，附带前序 8 个事件；单个完整事件可能超过字符目标。首次历史仍会进入待处理范围，因此并未解决历史规模、低价值轮次和原始消息保留周期的问题。
- 以上来自本地文件与 SQLite `mode=ro` 核验，未调用网页或绕过访问控制；只能说明本机当前格式可读，不能保证所有版本、所有助手或云端记录都可读。
- 本机 Antigravity summary SQLite 有 21 条会话元信息，FTS 表 35 个块，user_body / agent_body 为可读文本。它是搜索索引，不足以证明完整对话覆盖。brain 下有 11 份 Markdown、共约 72 KB，knowledge 当前无 Markdown。
- REA 对本机 app.asar 静态分析：SHA256 `259c83ffa266088dda6ede520a601bbf06cc5c34985cdf56c5d68549c98dbb0e`；parent evidence `ev_d29a03fd0d73b99a3becdb52dc01b101a474ee519f2ed14e2c10f7342fe8cdb6`。确认 Electron 制品，分析覆盖 partial，不能据此声称完整恢复私有对话格式。接入不依赖该私有格式。

## 可复用的公开实现

### Claude-Mem

仓库 https://github.com/thedotmack/claude-mem ，审阅提交 `a2150b2c09c6da01dfdc1296f8d2c3f3975a4418`。

- `src/services/transcripts/config.ts`：原生 Codex hook 已覆盖主会话时，默认抑制重复的完整 transcript watch；子代理采集及完整历史采集为显式选项。
- `src/services/transcripts/watcher.ts`：偏移检查点、文件身份和 startAtEnd；不是每轮重新读取并发给模型全部日志。
- `src/services/transcripts/processor.ts`：把用户请求与工具调用/结果关联为观察和摘要的输入。
- `src/services/worker/codex-observation-batch.ts`：观察按请求、代理和工作目录隔开；控制批次，对超长单项显式省略。这是该项目的取舍，不代表 WorkTwin 应复制其截断策略。

它解决的是 Agent 跨会话记忆，不能直接等同于面向人的完整知识文章。

### Letta Code

仓库 https://github.com/letta-ai/letta-code ，本地审阅提交 `145d7cd784f2945673f9023d2bec806e641b9a05`。

`src/agent/memory-worktree.ts` 将反思产生的记忆变更隔离在工作树，整合后才消费对应 transcript；冲突可重试。说明“对话作为输入、维护后的记忆作为输出”，而非只读取现有记忆文件或无限累加对话。

### Antigravity 官方文档

https://www.antigravity.google/docs/artifacts ：规划、成果及反馈通过 artifacts 承载。
https://www.antigravity.google/docs/rules-workflows?tab=ide ：规则文件用于指导 Agent。规则不是已完成工作的事实证明。

## 本轮实施边界

Google Antigravity 出现在编程助手列表。用户勾选后只索引 `~/.gemini/antigravity/brain/**/*.md` 与 `knowledge/**/*.md`，通过既有文件哈希进行增量更新；排除对话数据库、运行日志、插件缓存、内置 skill 和符号链接。界面显示“成果与知识文档 · 不含完整对话”。不自动打开采集、AI 或分享授权。

## 建议的下一步（尚未实施）

1. 成果层：项目文档、决策记录、经过验证的总结作为文章主来源，按内容哈希去重更新。
2. 规则层：用户自己维护的偏好、记忆、AGENTS.md、GEMINI.md、SKILL.md 作为带作用域的规范/方法；区分第三方内置说明，不把所有安装的 skill 当个人知识，不把规则当工作事实。
3. 协作层：以一次任务为单位，提取用户明确纠正、最后决策、选择理由和验证结果，附有限上下文与成果引用；缺证据保留不确定，不把 AI 自述完成当事实。
4. 历史只在用户选择项目/时间范围后补充处理。默认自启用后的新增任务开始，不默认回溯所有旧会话。已有资料不自动删除。
5. 搜索与分身优先用维护后的文章，原对话仅用于追溯和补证据。原始消息保留周期和清理能力另行明确，不以盲目截短上下文代替筛选。

结论：对话有价值，但应是有选择的证据输入；不是所有聊天都值得成为知识，也不是只读偏好文件就足够。
