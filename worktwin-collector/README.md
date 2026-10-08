# WorkTwin Collector 1.0

把日常工作记录整理成可以阅读、修改、持续维护并授权给数字分身的个人知识库。客户端本地采集，企业服务器统一支付模型费用，并保存明确允许分享的知识快照。

[下载 1.0 安装包与源码](https://github.com/KirenHu/AI-coding/releases/tag/worktwin-v1.0.0) · [部署](docs/DEPLOY.md) · [验收](docs/ACCEPTANCE.md) · [竞品与源码审计](docs/COMPETITOR_AUDIT.md)

## 三个入口

| 入口 | 用户操作 | 后台能力 |
|---|---|---|
| 信息采集 | 选择目录；分别授权采集、AI 整理、分身分享 | 文件监听、定时扫描、增量解析、任务队列 |
| 我的知识库 | 阅读 Markdown、搜索、编辑、确认更新、查看来源和历史、浏览双向链接、导出 Wiki | 企业模型提炼与同主题更新提案、空闲期知识养护 |
| 我的数字分身 | 创建分身、勾选知识、试问、启用分享、建立到期链接、撤销访问 | 只使用授权且有效的知识回答；企业服务器支持本人电脑离线后的查询 |

不需要 Ollama，也不需要员工持有模型供应商 Key。新目录默认没有 AI 或分享授权。后台养护建议与冲突更新均由本人确认后生效，不自动覆盖正文。

## 安装与开始使用

发布提供 macOS Apple Silicon / Intel 的 DMG、Windows x64 ZIP，以及 Python wheel / 源码。Windows 解压后运行 `WorkTwin/WorkTwin.exe`；macOS 将 WorkTwin.app 拖入应用目录。桌面包未签名、未公证，组织可自行签名并分发。

源码安装需要 Python 3.11+：

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows: .venv\Scripts\Activate.ps1
python -m pip install -e .
python -m worktwin serve --open
```

打开 http://127.0.0.1:8765。可以直接创建手写知识和分身。自动整理、模型问答和远程分享需要管理员先部署企业服务，然后点击侧栏「连接企业知识服务」，输入企业地址与个人访问令牌。连接检查验证员工凭据，不证明模型供应商已成功响应。

1. 在「信息采集」授权文件夹，或 Codex `~/.codex/sessions` / Claude Code `~/.claude/projects`；需要模型整理的来源单独打开 AI 权限。
2. 在「我的知识库」核对提炼结果、原文证据和更新建议。用 `[[K123|标题]]` 链接已有知识；改名后链接仍按 ID 定位。可以查看版本与导出 Markdown Wiki。
3. 创建数字分身并逐条勾选知识；试问结果附带知识引用，不足时拒答。
4. 需要分享时，对涉及来源打开「允许分身分享」，确认知识后在分身中启用分享，再创建带有效期的访问链接。

访问链接是持有者凭据：接收者名称仅是本人管理标签，不验证对方身份。链接可转发，最长 90 天，可单独撤销。服务端共享快照只包含已确认的知识标题/正文，不上传原始文件或完整会话；偏好条目不参与发布。同一知识在多个分身之间复用，读取范围仍分别授权。

本地编辑、归档、撤销来源和权限会触发同步，后台每 5 秒检查变化。企业服务断开时显示「尚未同步」，远程仍使用上次快照；恢复连接才会撤掉旧内容。紧急撤销应直接在企业服务器完成。退出客户端只停止本地采集，不停止已经发布的分身。

## 企业运行与数据边界

[部署说明](docs/DEPLOY.md)提供 Docker Compose 与 Python 两种启动方式。管理员在服务端设置 OpenAI Chat Completions 兼容的供应商、固定模型和 Key，并为每位员工发独立令牌；员工不能更改付款模型。服务端有持久日调用/Token 限额、每凭据分钟限流、并发上限与不含提示正文的调用记录。这些是调用限额，不能替代供应商账单或金额预算。

授权目录后，本机 SQLite 会保存可检索的完整解析文本。支持 Markdown/TXT/代码、DOCX、可选中文字 PDF；扫描 PDF 不包含 OCR。会话采集只保留可见内容，排除工具输出和内部推理。常见凭据文件会被排除，但未提供企业 DLP。只有允许 AI 的相关文本会发送到企业模型。

默认数据库：macOS `~/Library/Application Support/WorkTwin Collector/worktwin.sqlite`；Windows `%LOCALAPPDATA%\WorkTwin Collector\worktwin.sqlite`；Linux `~/.local/share/worktwin/worktwin.sqlite`。`WORKTWIN_DATA_DIR` 可更改目录。个人服务令牌保存在本机配置数据库，供应商 Key 仅在服务端；备份与设备访问权限由组织管理。

## 开发和验证

```bash
python -m pip install -e '.[test]' build playwright
./scripts/verify_release.sh
python -m playwright install --with-deps chromium
PYTHONPATH=. python scripts/ui_acceptance.py
PYTHONPATH=. python scripts/ui_proposals_acceptance.py
PYTHONPATH=. python scripts/ui_sharing_acceptance.py
python -m build
```

自动验收使用合成资料和确定性测试模型，覆盖真实 HTTP、SQLite、浏览器与打包应用启动。真实付费模型效果、员工历史资料准确率、企业 SSO、系统长期休眠唤醒、签名和公证仍需要组织环境验证，1.0 不宣称已经完成这些验收。

[架构](docs/ARCHITECTURE.md) · [后续工作](docs/ROADMAP.md) · [评测](docs/EVALUATION.md) · [开源组件](docs/OSS.md)

## 许可证

原创代码 MIT；`worktwin/rowboat_markdown.py` 的小范围 Python 移植遵循 Apache-2.0，原始许可与改动说明在 `third_party/rowboat/`，随分发包附带。其他竞品功能参考与实际代码复用分别记录于审计文档。
