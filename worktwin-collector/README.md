# WorkTwin Collector

**把日常工作记录整理成可以阅读、更新并授权给数字分身的个人知识库。**

v0.6 是**可在本机运行的试点客户端**。它不是纯设计稿：数据采集、资料变更检测、SQLite 持久化、知识编辑、企业 BYOK 模型网关和分身知识授权都已接入实际后端 API。macOS 原生应用签名、企业 IAM 和远程分身分享尚未完成。

## 新增：成熟知识的后台自动养护

参照 Rowboat 开源知识引擎中的 Gardener 工作方式，采集与提取完成后，
后台会选择**证据充足且需要整理**的文章，调用企业已配置的 BYOK 模型，
生成可以对照旧版的知识整理建议。员工在「我的知识库 → 待核对更新」
中确认或忽略；不另增一级菜单，也不需要员工配置 Prompt 或模型 Key。

- 一篇文章至少具备 3 条有效来源证据、较长正文或多次历史修改，才进入候选。
- 关联的全部来源必须允许 AI 处理；失去授权会使运行中结果失效。
- 相同版本至少间隔 7 天才能再尝试养护；每天最多自动养护 8 篇，处理在新资料任务空闲后进行。
- 整理建议**不自动覆盖正文**，也不会让已授权的数字分身暂停使用原版本。
- 人工接受后记录知识版本历史，方便回看过去的结论和新旧变化。

**[Viven、Rowboat 与 remio 的官方资料及开源源码审计](docs/COMPETITOR_AUDIT.md)**

## 界面只保留三项

| 入口 | 终端用户操作 | 后台工作 |
|---|---|---|
| **信息采集** | 选择允许采集的信息类型和文件夹；独立选择是否允许 AI 整理 | 目录变化监听、定时扫描、格式解析、入队 |
| **我的知识库** | 按项目阅读、搜索、编辑、核对知识更新建议和查阅来源 | 企业 AI 提炼候选、跨文档更新提案、人工核对、版本和原文证据 |
| **我的数字分身** | 创建分身，从已有知识中勾选可用内容，撤销授权 | 使用所选知识生成回答；不读取未分配的资料 |

系统内部的习惯提取、任务调度、原始日志、索引和模型配置**不在终端用户导航栏中**。

## 快速安装和运行

需要 **Python 3.11+**。在项目目录执行：

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m worktwin serve --open
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m worktwin serve --open
```

本地工作台：**http://127.0.0.1:8765**。关闭终端即可退出。

也可以通过 `start-macos.command` / `start-windows.bat` 在源码目录启动。支持使用构建出的标准 Python wheel 分发应用（首次安装第三方依赖仍需可用包源）。

**默认行为**：没有任何文件夹被自动授权；没有任何文本被默认发送至网络；也不会安装、运行或调用 Ollama。你可以先创建一篇手写知识并配置本地数字分身。若要自动生成知识，管理员必须配置企业模型网关，员工必须在对应来源上打开「允许 AI 整理」。

### 使用完整的自动整理流程

1. 企业管理员部署 BYOK 网关（下节），向员工安全分配专属网关地址与短期访问令牌。
2. 员工本机通过环境变量启用网关，然后启动 WorkTwin。**模型供应商 Key 只能保存在企业网关服务器**。
3. 打开「信息采集」，分别授权工作文件夹、Codex `~/.codex/sessions` 或 Claude Code `~/.claude/projects`。
4. 对需要交给企业模型整理的来源开启「允许 AI 整理」。后台定时提取事实、决策和流程知识，并关联原文证据。
5. 在「我的知识库」中直接查看、修订和确认知识。创建数字分身，在知识清单中逐条勾选可访问内容。
6. 企业网关在线时，可在分身页面发起真实问答；未勾选知识不会作为问答上下文发往模型。

> 授权目录后会在本地 SQLite 保存**可检索的完整文本副本**（不是只保存文件路径）。PDF、Word 原文件二进制不作额外云端复制。本地磁盘加密与备份策略应与原始资料安全级别一致。

## 企业统一 BYOK 模型配置

企业网关与员工本地客户端是**两个独立进程**。网关使用 OpenAI Chat Completions 兼容接口，可接支持该接口的模型服务。示例用环境变量演示，本项目不会提供一键生产部署。**不要提交实际凭据到 Git，也不要直接将试点网关暴露公网。**

在企业受控服务的运行环境中配置：

```bash
export WORKTWIN_BYOK_BASE_URL="https://your-model-provider.example/v1"
export WORKTWIN_BYOK_MODEL="your-enterprise-model"
export WORKTWIN_BYOK_API_KEY="<enterprise-provider-key>"
export WORKTWIN_ENTERPRISE_TOKENS="<provisioned-employee-token>"
python -m uvicorn worktwin.gateway:create_gateway --factory --host 127.0.0.1 --port 8789
```

在员工电脑上，管理员通过设备管理分配：

```bash
export WORKTWIN_GATEWAY_URL="https://your-enterprise-gateway.example"
export WORKTWIN_GATEWAY_TOKEN="<provisioned-employee-token>"
python -m worktwin serve --open
```

**重要**：网关进程目前使用简单 Bearer Token 访问限制，只满足内网功能验证。投入企业生产前需要组织 SSO/身份、短期凭据、速率限制、审计和网络 TLS 终止等建设；见 [部署说明](docs/DEPLOY.md)。本地 HTTP `127.0.0.1` 仅供单机测试。员工无法在 UI 中更改模型或供应商费用配置。

## 能做什么

| 能力 | v0.5 状态 |
|---|---|
| 授权目录、暂停采集、撤销授权、清除派生知识 | 已实现并有测试 |
| Codex、Claude Code 可见会话采集；排除工具输出/内部推理 | 已实现；适配器需要跟进上游格式变化 |
| Markdown、TXT、代码、PDF 可选中文字、DOCX 等采集 | 已实现；扫描版 PDF 无 OCR |
| 文件变化自动增量扫描 + 定时复核 | 已实现 |
| SQLite FTS5 中文检索、按项目组织、原文证据 | 已实现；非向量语义检索 |
| 企业 BYOK 模型整理：事实、决策、流程、个人偏好候选 | 已实现，按来源明确授权；需要真实企业模型连接 |
| 知识搜索、文档编辑、历史版本、过期来源隔离 | 已实现 |
| 创建/编辑/删除数字分身；勾选知识并隔离问答范围 | 已实现，本地试问 |
| 分身分发给其他真实员工、云端长期共享 | **未实现** |
| 跨文档同主题的模型关联、补充/替换/冲突更新提案 | **已实现待人工审核链路**；不宣称自动判定必然正确 |
| 长期行为风格、生产级知识准确率评测 | **未实现** |
| 可直接安装的已签名原生 macOS / Windows 应用 | **未实现**（提供各平台构建脚本） |

## 采集与数据安全

- **默认拒绝**：新来源不默认允许 AI 处理；不扫描未授权目录。
- 排除目录中的 `.env`、私钥和常见凭据文件，但**这不是完整的秘密检测器**，员工仍须检查授权范围。
- 当前可为同一数据源分别设置「本地采集」与「企业 AI 整理」权限。只关闭采集会保留已有本地内容，**彻底撤销来源**会删除来源索引及没有其他有效来源的派生知识。
- 知识基于有效来源关联；文件修改会标记过期证据。资料移除导致仅部分证据丢失时，剩余知识仍须人工复核；失效知识不能供分身问答。
- 新资料与已有知识相关时，模型仅生成补充、替换或冲突提案；员工在「我的知识库 → 待核对更新」中核对并接受或忽略。接受后保留版本和历史依据。
- 模型整理任务最多自动尝试四次并指数退避；错误信息不记录上游响应全文或模型密钥。
- 分身只是对知识 ID 的授权，不重复复制知识。问答请求只附带属于该分身的知识正文和有效证据摘录。
- 企业模型收到的是员工允许 AI 处理的**相关文本内容**，可能包含敏感资料。试点不包含企业 DLP/敏感信息脱敏、BYOK 费用限额或完整审计。

## 常用命令

```bash
python -m worktwin serve --open        # 打开本地工作台
python -m worktwin where               # SQLite 文件位置
python -m worktwin scan                # 手动执行一次采集扫描
./scripts/verify_release.sh           # 回归测试 + HTTP 集成测试
CHROMIUM_PATH=/usr/bin/chromium python scripts/ui_acceptance.py  # 可选浏览器主流程
PYTHONPATH=. CHROMIUM_PATH=/usr/bin/chromium python scripts/ui_proposals_acceptance.py  # 可选知识核对交互
```

默认数据库位置：macOS `~/Library/Application Support/WorkTwin Collector/worktwin.sqlite`；Windows `%LOCALAPPDATA%\WorkTwin Collector\worktwin.sqlite`；Linux `~/.local/share/worktwin/worktwin.sqlite`。使用 `WORKTWIN_DATA_DIR` 可修改。数据库含文本副本和知识记录，需妥善保护。

## 开发与验收

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python scripts/acceptance_benchmark.py
python scripts/http_acceptance.py
```

UI 验收额外安装 Playwright 和 Chromium；其他自动化测试不依赖浏览器。测试生成临时的合成资料，不会读取用户主目录。**真实付费模型 API、企业身份平台、macOS 原生安装包和大规模实际知识质量还没有验收**；这不影响本地采集、增量扫描、编辑、授权和企业网关接入代码在测试环境真实运行。

进一步查看：[架构与决策](docs/ARCHITECTURE.md) · [部署](docs/DEPLOY.md) · [验收](docs/ACCEPTANCE.md) · [路线图](docs/ROADMAP.md) · [开源组件](docs/OSS.md)。

## License

原创代码 MIT；未复制第三方产品的界面源代码，界面设计借鉴成熟知识管理产品的导航和文档编辑模型。

## v0.5 知识持续更新机制

当新资料到来时，客户端仍然只做授权范围内的增量采集。企业模型首先生成有原文引文的知识项；如果同一项目已有允许模型处理的知识，它再尝试判断新增、补充、替换或冲突。

- **新增**：可单独形成待确认知识条目。
- **补充 / 替换 / 冲突**：仅创建待核对更新，不自动覆盖已发布内容；相关旧条目先禁止分身引用。
- **人工接受**：检查证据版本与原知识版本，再提交新内容、旧版本历史及新旧证据关系。
- **人工忽略**：移除冲突待处理状态，保留原知识。
- **数据撤销**：若涉及知识失去部分证据，即使仍有其他来源也需要复核，避免衍生知识越权保留。

模型关系分类只是候选建议，不能替代企业知识质量的人工抽样和权限审计。当前尚未对真实生产数据进行知识正确率评测。
