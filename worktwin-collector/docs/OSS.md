# 开源复用与版权说明

本项目选择“复用可靠开源库”而不是复制一整套可能不匹配且依赖复杂的数字分身产品。

| 第三方项目 | 用途 | 主要许可证 | 官方来源 |
|---|---|---|---|
| FastAPI | 本地 HTTP API 与管理界面 | MIT | https://github.com/fastapi/fastapi |
| Uvicorn | 本地 ASGI 服务 | BSD-3-Clause | https://github.com/encode/uvicorn |
| SQLite / FTS5 | 原始内容、知识条目和中文检索 | Public Domain | https://www.sqlite.org/ |
| watchfiles | 文件变化事件通知 | MIT | https://github.com/samuelcolvin/watchfiles |
| pypdf | PDF 文字解析 | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| python-docx | Word 文档解析 | MIT | https://github.com/python-openxml/python-docx |
| FastAPI/Pydantic | 企业 BYOK 网关请求验证和权限接口 | MIT | https://github.com/pydantic/pydantic |

本仓库通过上述软件的依赖/API 使用功能，没有直接复制这些依赖的源代码，仅通过其公开的依赖/API 使用功能。具体发布时，按各依赖的 LICENSE 原文一并保留许可声明，尤其在做可安装分发包时。

### 产品参考与源码复用

- https://github.com/agentscope-ai/ReMe — 可阅读长期记忆、知识演化。
- https://github.com/rowboatlabs/rowboat — 本地 Markdown 工作知识图谱；实际小范围移植见下文。
- https://github.com/getzep/graphiti — 带时间演化的知识关系。
- https://github.com/onyx-dot-app/onyx — 企业知识检索与权限。
- https://www.notion.com/ — 知识文档侧栏、检索与文档编辑交互参考。
- https://capacities.io/ — 项目与知识对象分类交互参考。

除下述 Rowboat 解析移植外，这些仓库不作为运行依赖，也未直接复制其实现。

### Rowboat 实际代码移植

`worktwin/rowboat_markdown.py` 将 Rowboat `apps/x/packages/core/src/knowledge/knowledge_index.ts` 的字段、列表、标题提取方式移植为 Python，固定源提交 `f07c3fcd7793e99d850ee61363a91eafa16d6afb`。运行时用于文档标题和知识别名/关键词检索；其余索引、版本存储与养护实现使用 WorkTwin 的 Python/SQLite 架构。没有移植 Node 服务或 isomorphic-git 运行时。

原始 Apache-2.0 LICENSE 与本次改动说明在 `third_party/rowboat/`；wheel 与桌面包随附。新增 `markdown-it-py` 依赖（MIT）用于禁用原始 HTML 的 Markdown 展示。设计参考、已移植范围和尚未完成部分分别记录于 [竞品审计](COMPETITOR_AUDIT.md)。

### 1.1 设置能力参考

实际阅读 AnythingLLM 的模型设置和可用模型选择代码、Open WebUI 的管理员连接与后端权限实现；设计参考及文件版本见竞品审计第 7 节。本轮独立实现个人/企业设置与模型列表，没有新增两者运行依赖或复制源文件。密钥存储使用 `keyring`（MIT）与 `cryptography`（Apache-2.0/BSD），按其依赖方式分发。

### 1.3 持续工作证据

参考 memU 的会话适配边界，独立实现按字节位置读取的 Codex、Claude Code 和 Cursor CLI 适配器；未复制 memU 源码或引入其数据库。Hindsight 仅通过标准 HTTP 在独立研发脚本中验证，桌面程序没有新增 Hindsight SDK、PostgreSQL、Docker 或本地模型依赖。OpenViking 仅作设计参考，未复制 AGPL 主项目或 Apache 示例代码。具体版本、许可证核对与候选引擎采用条件见 [知识引擎验证](KNOWLEDGE_ENGINE_PROBE.md)。
