# 开始使用 WorkTwin

先连接一个项目资料目录，读到一篇有来源依据的文章，再尝试数字分身。这条路径可以帮助你了解 WorkTwin 是否适合自己的工作。

## 选择安装方式

- **已发布安装包**：前往 [GitHub Releases](https://github.com/KirenHu/AI-coding/releases)，核对版本号、系统和发布说明。macOS 将 App 拖入「应用程序」；Windows 解压后运行 `WorkTwin/WorkTwin.exe`。
- **体验本分支能力**：按下方源码步骤运行。本文对应 1.5.9 测试版；截至 2026-10-14，最新正式 Release 为 1.1.7，界面和功能可能不同。

macOS 安装包是否经过 Developer ID 签名和 Apple 公证，以对应版本发布记录为准；不要仅凭“构建成功”判断。遇到拦截请查看 [macOS 安装说明](MACOS_INSTALL.md)。

## 从源码运行

需要 Python 3.11+ 和 Git。在仓库中的 `worktwin-collector` 目录执行安装命令。本次 1.5.9 位于研发分支，因此完整命令为：

```bash
git clone --branch feat/worktwin-continuous-evidence https://github.com/KirenHu/AI-coding.git
cd AI-coding/worktwin-collector
python -m venv .venv
```

macOS / Linux 激活环境：

```bash
source .venv/bin/activate
```

Windows PowerShell 激活环境：

```powershell
.venv\Scripts\Activate.ps1
```

然后安装并启动：

```bash
python -m pip install -e .
python -m worktwin serve --open
```

浏览器会打开本机工作台；没有自动打开时访问 <http://127.0.0.1:8765>。采集和 MCP 连接需要 WorkTwin 持续运行。

## 1. 配置模型

在「设置 → 个人版」填写模型服务商提供的接口根地址、模型名称和 API Key。支持 OpenAI Chat Completions 兼容接口；可获取模型列表，也可手动填写。点击「测试连接并保存」，成功后新配置才会启用。

AI 整理和分身问答会使用配置的服务；模型费用由该服务商收取。只查看本机已有文章不需要再次生成回答。

企业用户在「设置 → 企业版」填写管理员提供的企业服务地址和 Token；供应商密钥由管理员配置。详细说明见[模型设置](MODEL_SETUP.md)和[企业部署](DEPLOY.md)。

## 2. 连接一个知识来源

在「知识来源」添加一个包含项目说明、会议结论或操作方法的文件夹，再开启该来源的「AI 使用」。采集与 AI 使用是两个不同开关：只采集不会自动调用模型整理。

也可以连接 Codex、Claude Code、Cursor CLI 或 Antigravity。Codex、Claude Code 和 Cursor CLI 默认从开启 AI 使用之后的新增会话开始；想整理旧记录时，在来源设置选择历史日期范围。界面日期范围按 UTC 标注。

不要因为旧会话没有立即形成文章而反复添加相同目录。先检查来源的 AI 使用开关、历史范围和任务状态。

## 3. 阅读并核对文章

打开「知识库」，等待资料提取和文章维护完成。首次整理的时间取决于资料量和模型响应。

打开文章后，查看概要、正文、最近更新和来源依据。你可以编辑正文、检查历史版本或回退；回退会生成新的当前版本，保留中间历史。知识库也支持搜索、项目筛选、对象分组和 Markdown Wiki 导出。

如果没有文章，先检查资料是否成功解析、来源是否允许 AI 使用、模型是否可用，以及任务是否正在排队或失败。资料缺少足够背景时，也可能暂时保留为后台材料。

## 4. 创建数字分身

在「数字分身」创建一个分身，按项目、来源或文章设置知识范围并保存。用一个资料中确有答案的问题试问，例如“这个项目目前采用什么审批规则？”，检查回答及引用是否对应你的资料。

按项目或来源授权后，后续符合范围的有效知识可自动继承；逐篇授权适合希望精确控制内容的情况。分身无法使用的文章可通过「来源授权」检查原因。

需要给其他 AI 使用时，按 [MCP 指南](MCP.md)配置连接。需要让其他人远程问答时，先配置[分享服务](DEPLOY.md)，再查看[数据与分享说明](DATA_AND_SHARING.md)。

## 升级、退出与备份

升级前在设置中导出 Wiki 或备份数据库，并通过应用「退出」停止旧后台。关闭浏览器页面不会停止 WorkTwin。替换应用后重新打开；模型密钥由系统凭据存储保管。

不同版本可能有数据库迁移，回退应使用对应版本的备份。备份不包含模型密钥，恢复步骤以备份内说明为准。

[返回项目介绍](../README.md) · [全部文档](README.md)
