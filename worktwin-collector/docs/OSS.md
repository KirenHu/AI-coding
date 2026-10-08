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

本仓库没有直接复制上述软件的源代码，仅通过其公开的依赖/API 使用功能。具体发布时，按各依赖的 LICENSE 原文一并保留许可声明，尤其在做可安装分发包时。

### 参考但未直接复制的产品思路

- https://github.com/agentscope-ai/ReMe — 可阅读长期记忆、知识演化。
- https://github.com/rowboatlabs/rowboat — 本地 Markdown 工作知识图谱。
- https://github.com/getzep/graphiti — 带时间演化的知识关系。
- https://github.com/onyx-dot-app/onyx — 企业知识检索与权限。
- https://www.notion.com/ — 知识文档侧栏、检索与文档编辑交互参考。
- https://capacities.io/ — 项目与知识对象分类交互参考。

这些仓库不作为依赖，不宣称使用其内部代码。后续若复用实际实现，应先审查各自许可证、NOTICE、商标和第三方依赖。
