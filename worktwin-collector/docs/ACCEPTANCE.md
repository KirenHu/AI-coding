# WorkTwin Collector 验收范围

macOS v1.1.1 增补：在 macOS ARM/Intel Runner 验证实际 **DMG 容器完整性、挂载 App 的 codesign --verify --deep --strict、复制 App 后代码签名、打包程序的 HTTP/SQLite 启停**，并明确记录 Gatekeeper 对 ad-hoc 签名的拒绝。该组检查不能替代 Apple Developer ID 签名、公证或用户实际首次下载双击测试；见 [安装排查](MACOS_INSTALL.md)。

自动回归使用临时合成资料与确定性模型或本机模拟 HTTP 供应商，不调用真实付费模型；文末单独记录用户授权的真实付费模型有限样本验收。测试运行真实 SQLite、FastAPI/uvicorn 与浏览器脚本，不以静态页面替代后端。

| 验收 | 实现与检查 |
|---|---|
| 后端回归 | 当前修订为 100 项 pytest：采集/适配器、证据、更新/养护、任务恢复、来源权限、项目范围与质量、问答范围、共享与撤销、预算、链接和 Markdown 安全 |
| 合成回答评测 | 8 项：知识引用、无依据拒答、撤权、隐私隔离；结果不代表生产准确率 |
| 批量与 HTTP | 80 份合成资料增量/幂等扫描；独立 HTTP 网关模拟供应商调用 |
| 浏览器主流程 | 授权目录、编辑知识、创建分身、勾选授权、来源撤销 |
| 浏览器知识更新 | 新旧对照、接受/忽略、版本和失效提案 |
| 浏览器共享闭环 | UI 连接企业服务、Markdown 显示、发布/创建链接、接收者问答、本人进程退出后继续问答、刷新保留访问、点击引用、远程撤销后拒绝访问 |
| 浏览器设置 | 个人真实模拟模型测试、失败保留配置、未保存保护、分身一并保存、小窗口设置、管理员模型与员工 Token |
| 浏览器布局 | 260 篇知识下，三种窗口尺寸的设置可见、可点击，正文独立滚动 |
| 分发 | wheel/sdist/source；macOS arm64/x86_64、Windows x64 打包启动、知识写入、Markdown 渲染、打包后的 MCP 初始化/读取/撤销与正常退出 smoke |

发布必须通过仓库 `.github/workflows/worktwin-build.yml` 的 Python、六个浏览器脚本和三个平台打包启动任务。成功后 main 的工作流生成 Release 与 SHA256 文件。运行状态以对应 GitHub Actions 记录为准；这里记录验收定义，不预先宣称未运行任务成功。

```bash
python -m pip install -e '.[test]' build playwright
./scripts/verify_release.sh
python -m playwright install --with-deps chromium
PYTHONPATH=. python scripts/ui_acceptance.py
PYTHONPATH=. python scripts/ui_proposals_acceptance.py
PYTHONPATH=. python scripts/ui_sharing_acceptance.py
PYTHONPATH=. python scripts/ui_settings_acceptance.py
PYTHONPATH=. python scripts/ui_layout_acceptance.py
PYTHONPATH=. python scripts/ui_mcp_acceptance.py
```

没有完成充分验收：真实付费模型在完整本地资料库上的提炼/养护质量、真实员工资料的准确率和归因率、组织 SSO、DLP、物理 Mac/Windows 长期运行/睡眠/唤醒与系统弹窗、已签名/公证安装包、自动升级。打包程序 smoke 检查启动 API、写入知识、渲染 Markdown、MCP 初始化/读取/撤销及正常退出，不证明上述系统行为。

## 1.1 本地运行记录（2026-10-09）

67 项后端回归通过，8/8 合成回答断言通过，80 份增量扫描与真实 HTTP 验收通过。四个浏览器脚本已在 Chrome Headless Shell 中实际执行并通过，未发现 JavaScript 错误；受本地进程限制，浏览器用单进程模式，主流程/更新流通过本地 HTTP 桥接，设置/接收者页面同时验证实际 origin、刷新和 sessionStorage。Python wheel/sdist 构建通过。

用户已授权上传及生成测试安装包，改动已提交至 [PR #13](https://github.com/KirenHu/AI-coding/pull/13)。CI 执行四组浏览器验收和三个平台的原生打包检查；原生启动检查还确认设置接口和系统凭据后端能加载。实际运行状态见该 PR 及 main 的 GitHub Actions 记录。物理系统钥匙串交互提示和 Apple 公证仍未验收。

## 1.1.1 升级兼容修复

新增资源版本地址与禁止旧资源缓存的回归检查；浏览器验收模拟旧后台版本和备份目录接口失败，分别验证明确的重新启动指引及模型表单保留。67 项后端测试与四组浏览器脚本本地通过。原生 smoke 还检查 HTML 界面版本与打包服务版本一致。安装包发布状态以 GitHub Actions 的实际记录为准。

## 范围、质量和 Obsidian 导出修订（2026-10-09，提交时的验收记录）

本轮本地后端回归为 93 项，全部通过；8/8 合成回答评测、80 份增量扫描和 HTTP 验收通过，原有四组浏览器脚本通过。新增真实浏览器布局验收：260 篇知识，在 1280×768、1024×600、900×480 窗口中直接点击设置，滚动正文不改变设置按钮位置，无 JavaScript 错误。工作流增加第五组浏览器验收。

新增回归覆盖：相同目录不自动等于同一业务项目；AI 方案与用户确认的发言顺序；无关的后续“同意”不作为确认；低价值内容及无原文依据的内容拒绝提取；项目提示词要求不跨范围使用；无相关知识时不发送任意笔记；人工更改项目改变真实检索边界并保留历史；停用立即生效且可恢复；旧知识迁移暂停使用且重启不重复覆盖；不同主题不合并；事件时间统一比较；Markdown 导出保留范围、可用状态和停用提示。

上述测试使用合成资料和模拟模型，不证明真实资料的提炼质量。完整工具执行日志、跨会话业务项目识别、本地 MCP 及 Markdown 文件夹自动同步仍未完成。源码修订、平台打包和已安装版本分别验收；此记录不代表新安装包已发布。

## 1.2.0：任务触发式浏览器行为采集

增加独立的浏览器会话与事件表、工作流 Ed25519 票据验证、nonce 去重、用户开关、插件配对心跳、指定 tab/document/URL 事件隔离、跳转后停止但任务维持 open、流程平台完成通知，以及只在独立 AI 授权下运行的阶段分析。后端测试使用真实 SQLite + FastAPI TestClient 与现场签名密钥生成；扩展 JS 使用 Node 语法检查、manifest JSON 校验和打包静态资产检测。

**尚不能视为已完成**：真实 Chrome/Edge 安装后的跨进程信号与脚本注入验收、第三方流程平台后端签名联调、macOS/Windows 用户物理机器上的真实默认浏览器检测，以及将浏览器证据合并进可引用的有效知识。此能力默认关闭；扩展 ZIP 是供开发者模式加载的测试包，不是已通过 Chrome/Edge 商店审核的上架版本。GitHub CI 结果须以对应 PR/Actions 实时记录为准。

## 1.1.5：用户确认的生效规则与分身 MCP

新增自动生效验收门槛、明确项目确认、只接受原文保留的纯补充、替代正文退出当前知识库。官方 MCP SDK 客户端验证真实 ASGI 应用，覆盖当前授权、停用、来源撤权、日志独立授权、关闭连接、凭据更新及 2025-11-25 兼容。新增浏览器 MCP 配置、日志开关和未保存授权保护。

实际 DeepSeek 调用使用本项目用户的真实讨论（来源为当前聊天记录，重构为会话格式，不是用户 Mac 原始日志）。首次测试发现模型把方案批准误标为成果验收，导致知识为空；修复后连接、原文引用、用户归因、自动生效前验收要求、分身 MCP 权限、当前结论、仅借鉴 Obsidian 及结论替换需核对、实际存储、分身授权和 MCP 读取共 10 项检查通过。报告不写入已安装用户的验收记录，自动生效保持关闭。该有限样本不代表完整用户资料的准确率，也不代表跨会话项目识别已经完成。

有限样本的实际运行记录：[真实模型链路报告](reports/real-model-sample-20261009.json)。该报告不包含模型 Key，也不是启用自动生效的验收凭据。

1.1.5 原生修订 `be59d6702f0af681183fae042df766ec172bf33e` 的 [CI 验收](https://github.com/KirenHu/AI-coding/actions/runs/37918958787) 已实际通过：100 项后端回归、六组浏览器验收、Mac ARM/Intel 与 Windows 的打包及启动检查。三个平台都验证了打包程序内的 MCP 初始化、当前授权知识读取和凭据撤销。该记录不代表用户物理设备、Apple 公证或 Codex/Claude Code 实际接入已完成验收。
