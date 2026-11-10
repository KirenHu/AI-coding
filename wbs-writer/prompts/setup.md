# 首次安装提示词（复制下面整段，粘贴给你常用的 Claude Code / Codex / dsh，只需运行一次）

---

请帮我安装并配置“WBS 自动撰写”工具。按下列步骤依次执行，每一步都要验证结果；某一步失败时记下原因，继续执行后面不依赖它的步骤。全部结束后，按第 8 步输出检查表。重复运行这段提示词时，已完成的步骤只做验证，不重复安装，也不覆盖已有配置。

## 1. 安装 wbs-writer skill
- 仓库地址：`https://github.com/KirenHu/AI-coding`，skill 在仓库的 `wbs-writer/` 目录。
- 克隆或更新仓库到 `~/.wbs-writer/AI-coding`：目录已存在就执行 `git -C ~/.wbs-writer/AI-coding pull --ff-only`。
- 在本机每个已安装的 agent 的 skills 目录下，建立指向 `~/.wbs-writer/AI-coding/wbs-writer` 的软链接，名字叫 `wbs-writer`：
  - Claude Code：`~/.claude/skills/wbs-writer`
  - Codex：`~/.codex/skills/wbs-writer`
  - dsh：`~/.dsh/skills/wbs-writer`
- 某个 agent 没有安装（`which claude`、`which codex`、`which dsh` 查不到），就跳过它，不要替我安装这个 agent。

## 2. 安装并启动 claude-mem（后台记录我每天的 AI 工作）
- 先检查是否已经运行：读 `~/.claude-mem/settings.json` 中的 `CLAUDE_MEM_WORKER_PORT`；没有这个字段，就用 `lsof -iTCP -sTCP:LISTEN -nP | grep bun` 找端口。然后执行 `curl -s http://127.0.0.1:<端口>/api/health`，返回 `"status":"ok"` 即为已运行。
- 没有运行就安装。要求非交互安装，不弹浏览器登录：
  - Claude Code：`npx claude-mem install --provider claude`；如果 Claude Code 里已经能用 `/plugin`，也可以用 thedotmack marketplace 的插件 `claude-mem`。
  - dsh：`npx claude-mem install --ide dsh --dsh-profile tui`
  - Codex：用 claude-mem 自带的 Codex hooks 接入。具体以 `https://docs.claude-mem.ai` 的官方说明为准，先读文档再执行。
- 装完后再检查一次 health。如果是 `/plugin` 方式安装，可能需要重启 agent 才生效，这种情况在检查表里注明“需重启”。
- 只把 claude-mem 接到第 1 步检测到的 agent 上。

## 3. 检查 Python 依赖和钉钉 CLI（dws）
- 执行 `python3 -c 'import openpyxl'`。导入失败时，执行 `python3 -m pip install --user openpyxl`，然后再导入一次确认。
- 执行 `dws contact +me --format json`，能返回我的姓名就算通过。
- 没有安装或没有登录时，停下来告诉我该执行的登录命令，等我登录完成后再继续。不要替我输入任何账号或密码。

## 4. 写配置 `~/WBS/config.json`
- 一次性问我以下几项。已有配置时，显示当前值并问我是否修改：
  1. 姓名，默认用第 3 步 dws 返回的姓名；
  2. 负责的项目，可以填多个，例如“智能问数”；
  3. 常用分类标签，例如“智能问数一期开发、智能决策”；
  4. 项目 Week1 的周一日期，例如 2026-08-17；
  5. 现有周度计划表 xlsx 的路径（团队统一格式的累计表），没有就留空；
  5a. 把我已有的 WBS 归集到 `~/WBS/`：用 `mdfind -name WBS`、`mdfind -name 计划表`、`mdfind -name 周度计划` 搜索本机近 90 天修改过的 xlsx，列出候选（路径、修改时间、能看出的周次），让我确认哪些是我的 WBS。对确认的文件，按它覆盖的周（看表内日期，看不出就用修改时间所在的 ISO 周）**复制**到 `~/WBS/<YYYY>-W<ww>/`，保留原文件名，不移动、不改动原文件；目标已有同名文件时跳过。完成后列出归集结果。这个文件夹以后就是我所有 WBS 的唯一存放处；
  5b. 项目对应关系：读 `~/.claude-mem/claude-mem.db`（只读）近 30 天 observations 的 project 列表，列出给我，请我说明每个文件夹对应哪个业务项目，写入 `project_map`，无关的不写；
  6. 要读取的项目群名称，可以留空。填了的话，用 `dws chat +search` 或 `dws chat +chat-list` 找到对应的 `openConversationId`，并和我确认是这个群。
- 按 `wbs-writer/SKILL.md` 的“前置条件”一节写入 JSON。`mem_projects` 先留空。

## 5. 安装 darwin-skill（让 wbs-writer 持续进化）
- 执行 `npx skills add alchaincyf/darwin-skill`。如果无法访问 GitHub，就下载仓库 zip，把 SKILL.md 放到 `~/.claude/skills/darwin-skill/`。

## 6. 创建每周定时任务
- 时间：**每周一 10:00（本地时间）**。任务名：`每周WBS撰写`。
- 任务内容：读取 `~/.wbs-writer/AI-coding/wbs-writer/prompts/weekly-wbs.md` 的全文，作为提示词执行。
- 用你（当前 agent）自带的定时任务能力创建：
  - Claude 桌面版：scheduled task；
  - Codex：automations；
  - 都不支持时，用 macOS launchd 创建 `~/Library/LaunchAgents/com.wbs-writer.weekly.plist`，调用本机可用的无交互命令。按 `claude -p`、`codex exec`、`dsh headless` 的顺序，用第一个可用的。日志写到 `~/WBS/logs/`。
- 设置“错过时间时，在下次开机或唤醒后补跑”。
- 创建后读回任务列表，确认任务存在，且下次运行时间是最近的一个周一 10:00。
- 已存在同名任务时，不新建，只核对时间和内容是否正确。

## 7. 试运行一次
- 立即按 `prompts/weekly-wbs.md` 执行一次，确认 `~/WBS/<本周>/` 下生成了 xlsx 和 md 两个文件。
- 打开 md，给我看：一共写了几行、待确认几条、有没有证据缺口。

## 8. 输出检查表
用表格输出：每一步的结果（✅/⚠️/❌）、关键证据（端口、任务下次运行时间、文件路径），以及失败原因和需要我手动做的事。没有实际验证过的步骤，不要标 ✅。

## 全程禁止
- 不发送任何钉钉消息、邮件或待办，不修改我原有的周度计划表文件。
- 不执行 `rm -rf`、`git reset --hard`、`git push`，不修改仓库内容。
- 不替我安装 agent 本身，也不替我输入登录凭据。
