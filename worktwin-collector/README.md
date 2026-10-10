# WorkTwin Collector 1.2.0（自动知识维护、项目识别、任务采集集成预览）

在后台采集本人授权的本地工作资料，持续维护连贯的个人知识库，目标是通过 MCP 供其他 AI 使用。产品边界与未完成能力见 [一期边界](docs/PHASE1.md)：当前支持文件夹与 Codex/Claude Code 的公开双向会话；已补充编程工具的执行状态线索，原始工具结果仍待完善；工作单元级跨来源项目归属已进入 1.1.9 开发与验收；提供绑定数字分身授权的本地只读 MCP，完整日志单独授权。个人自行配置模型，企业使用统一分配的模型与 Token。

[已发布安装包与源码](https://github.com/KirenHu/AI-coding/releases) · [MCP 接入](docs/MCP.md) · [部署](docs/DEPLOY.md) · [验收](docs/ACCEPTANCE.md) · [竞品与源码审计](docs/COMPETITOR_AUDIT.md)

## 三个入口

| 入口 | 用户操作 | 后台能力 |
|---|---|---|
| 信息采集 | 选择目录；分别授权采集、AI 整理、分身分享 | 文件监听、定时扫描、增量解析、任务队列 |
| 我的知识库 | 阅读 Markdown、搜索、纠正自动更新、查看证据和历史、浏览双向链接、导出 Wiki | 授权来源的自动提炼、补充和明确结论替代，保留来源与历史 |
| 我的数字分身 | 创建分身、按项目或来源授权一次（或逐条选择）、试问和分享 | 在授权范围内自动继承后续有效知识；本地 MCP 和远程发布遵循同一权限计算器 |

个人版主模型支持 OpenAI Chat Completions；另可选 Jev SystemOne 或轻量 Chat Completions 判断模型。企业员工无需供应商 Key，专用判断模型由企业管理员统一配置。新目录默认没有 AI 或分享授权。新知识和有可靠顺序证据的更新会自动维护本地知识库；无法安全归属的资料保持会话隔离。跨来源冲突和人工编辑仍有更严格的覆盖保护。

## 安装与开始使用

发布提供 macOS Apple Silicon / Intel 的 DMG、Windows x64 ZIP，以及 Python wheel / 源码。Windows 解压后运行 `WorkTwin/WorkTwin.exe`；macOS 将 WorkTwin.app 拖入应用目录。**macOS 包已通过代码签名完整性检测，但仍为未经过 Apple Developer ID 签名、公证的测试包，首次 Finder 打开可能被 Gatekeeper 拦截。请先阅读 [macOS 安装说明与签名排查](docs/MACOS_INSTALL.md)；不能认为“CI 启动成功”意味着下载后可以无提示双击。**

源码安装需要 Python 3.11+：

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows: .venv\Scripts\Activate.ps1
python -m pip install -e .
python -m worktwin serve --open
```

打开 http://127.0.0.1:8765。侧栏底部「设置」在小窗口中也可访问。

- **个人版**：设置 → 个人版 → 模型设置，填写服务商提供的接口根地址、模型名称、API Key，可以点击「获取可用模型」后选择，或手动填写，再点击「测试连接并保存」。成功前保留旧配置，不会发送工作资料作为测试内容。
- **企业员工**：设置 → 企业版 → 连接企业服务，填写管理员提供的地址和企业 Token，然后测试企业模型。不能查看或更改供应商密钥。
- **企业管理员**：用管理员 Token 连接同一个界面，在「管理员设置」配置企业模型和调用限额，生成或撤销员工 Token。后台再次校验管理员权限。

个人版本地整理和问答不依赖企业服务。远程分享是可选功能，需要另行连接持续运行的分享服务；接收者问答使用服务端模型。

1. 在「信息采集」授权文件夹，或 Codex `~/.codex/sessions` / Claude Code `~/.claude/projects`；需要模型整理的来源单独打开 AI 权限。
2. 在「我的知识库」查看 AI 自动生效的知识、原文证据和版本。通常无需逐条确认；如果确需跨来源合并且系统尚不能确定关联，可在来源资料窗口的可选项里关联项目；同名项目默认不会仅凭名称合并。用 `[[K123|标题]]` 链接已有知识；改名后链接仍按 ID 定位。可以查看版本与导出 Markdown Wiki。
3. 创建数字分身，按项目或来源授权一次（也可以继续逐条指定知识）。之后新增、更新且有效的知识自动继承，撤销来源权限后自动失效。试问结果附带引用，不足时拒答。
4. 需要分享时，对涉及来源打开「允许分身分享」，确保分身的知识授权范围符合预期后启用分享，再创建带有效期的访问链接。

访问链接是持有者凭据：接收者名称仅是本人管理标签，不验证对方身份。链接可转发，最长 90 天，可单独撤销。服务端共享快照只包含分身被明确授权且当前有效的知识标题/正文，不上传原始文件或完整会话；偏好条目不参与发布。同一知识在多个分身之间复用，读取范围仍分别授权。

本地编辑、归档、撤销来源和权限会触发同步，后台每 5 秒检查变化。企业服务断开时显示「尚未同步」，远程仍使用上次快照；恢复连接才会撤掉旧内容。紧急撤销应直接在企业服务器完成。退出客户端只停止本地采集，不停止已经发布的分身。

## 企业运行与数据边界

[部署说明](docs/DEPLOY.md)提供 Docker Compose 与 Python 两种启动方式。管理员在服务端设置 OpenAI Chat Completions 兼容的供应商、固定模型和 Key，并为每位员工发独立令牌；员工不能更改付款模型。服务端有持久日调用/Token 限额、每凭据分钟限流、并发上限与不含提示正文的调用记录。这些是调用限额，不能替代供应商账单或金额预算。

授权目录后，本机 SQLite 会保存可检索的完整解析文本。支持 Markdown/TXT/代码、DOCX、可选中文字 PDF；扫描 PDF 不包含 OCR。会话采集保留可见对话及工具名称、返回状态和可识别的退出码；工具参数、原始 stdout/stderr、工具结果正文与内部推理不进入知识分析或 MCP 日志。工具执行记录不能代替成果验收。常见凭据文件会被排除，但未提供企业 DLP。只有允许 AI 的相关文本会发送到当前配置的模型服务。

默认数据库：macOS `~/Library/Application Support/WorkTwin Collector/worktwin.sqlite`；Windows `%LOCALAPPDATA%\WorkTwin Collector\worktwin.sqlite`；Linux `~/.local/share/worktwin/worktwin.sqlite`。`WORKTWIN_DATA_DIR` 可更改目录。macOS 密钥和企业 Token 保存到系统钥匙串，Windows 保存到凭据管理器；Linux 保存到权限受限的本机加密文件。已有数据库中的企业 Token 会迁移并清除。配置读取只返回“是否已保存”，不返回密钥。设置页提供 Wiki 导出和数据库备份；备份不包含配置密钥，恢复说明随备份附带。

## 开发和验证

```bash
python -m pip install -e '.[test]' build playwright
./scripts/verify_release.sh
python -m playwright install --with-deps chromium
PYTHONPATH=. python scripts/ui_acceptance.py
PYTHONPATH=. python scripts/ui_proposals_acceptance.py
PYTHONPATH=. python scripts/ui_sharing_acceptance.py
PYTHONPATH=. python scripts/ui_settings_acceptance.py
python -m build
```

自动验收使用合成资料和确定性测试模型，覆盖真实 HTTP、SQLite、浏览器与打包应用启动。真实付费模型效果、员工历史资料准确率、企业 SSO、系统长期休眠唤醒、签名和公证仍需要组织环境验证，1.1 不宣称已经完成这些验收。

[判断模型与动态知识授权](docs/DECISION_MODEL.md) · [macOS 安装和 Gatekeeper 排查](docs/MACOS_INSTALL.md) · [架构](docs/ARCHITECTURE.md) · [一期边界](docs/PHASE1.md) · [后续工作](docs/ROADMAP.md) · [评测](docs/EVALUATION.md) · [开源组件](docs/OSS.md)

## 许可证

原创代码 MIT；`worktwin/rowboat_markdown.py` 的小范围 Python 移植遵循 Apache-2.0，原始许可与改动说明在 `third_party/rowboat/`，随分发包附带。其他竞品功能参考与实际代码复用分别记录于审计文档。


## 任务触发式浏览器采集（独立能力）

用户在「信息采集」中手动开启后，可安装并配对 Chrome/Edge MV3 扩展。只有已授权流程平台签发任务信号时，才观察该任务打开的首个目标文档，记录结构化 DOM 操作并生成精简、可追溯的自然语言摘要。导航即停止当前文档观察；统一状态接口标记任务完成时停止剩余会话。**不读取流程平台的任务上下文、不生成知识库条目、不加入数字分身**。具体接入协议见 [浏览器采集](docs/BROWSER_CAPTURE.md)。流程平台尚未提供实际域名，部署参数留空，真实联调未完成。

## 版本集成验收

当前集成分支汇总自动生效的知识、跨会话项目匹配、可选轻量判断模型、数字分身按授权策略继承后续知识及任务触发浏览器操作摘要。主干正式发布还需要完整 CI、实际资料项目误合并评估、签名/公证和 Finder 首次启动验收；PR 的 unsigned 安装包不是正式签名包。
