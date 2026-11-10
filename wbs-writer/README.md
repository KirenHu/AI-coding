# wbs-writer：WBS 自动撰写

每周一 10:00，自动把你上周在 Claude Code、Codex、dsh 中的工作记录（来自 claude-mem），以及钉钉的听记、待办、日程和项目群消息，整理成项目组统一格式的《周度计划表》。生成的文件只保存在你自己的电脑上，确认后再由你自己发送。

## 内容

- 一个 skill：`wbs-writer/`，写明了写作规范、信息过滤规则和处理流程。
- 一套提示词：
  - `prompts/setup.md`：首次安装用，只运行一次。
  - `prompts/weekly-wbs.md`：每周定时任务用，setup 会自动配置好。

## 使用方法

1. 打开你常用的 agent（Claude Code、Codex 或 dsh）。
2. 把 [`prompts/setup.md`](prompts/setup.md) 中分隔线以下的全部内容粘贴进去执行。agent 会自动完成以下步骤：
   - 安装本 skill；
   - 安装并启动 claude-mem；
   - 检查钉钉 CLI（dws）的登录状态；
   - 询问你的姓名、项目和现有计划表的位置；
   - 安装 darwin-skill；
   - 创建“每周一 10:00”的定时任务；
   - 试运行一次。
3. 之后每周一，在 `~/WBS/<年>-W<周>/` 下会生成两份文件：
   - `周度计划表-<姓名>-W<周>.xlsx`：在上一版的基础上，回顾上周并追加本周计划；
   - `WBS-<姓名>-W<周>.md`：每一行对应的证据，以及需要你确认的事项。
4. 打开 md 文件，处理“待确认”清单，修改 xlsx 后发送给项目负责人。

想要随时生成，对 agent 说“帮我写本周的 WBS”即可。

## 持续进化

对 agent 说“优化 wbs-writer”，agent 会调用 [darwin-skill](https://github.com/alchaincyf/darwin-skill)。它基于 `evals/test-prompts.json` 给 skill 打分，并做单维度迭代，每一轮都需要你确认。你每周对生成结果的修改，可以作为新的测试用例补充进去。

## 依赖

- macOS，Python 3，openpyxl（setup 会自动检查）。
- [claude-mem](https://github.com/thedotmack/claude-mem)。
- 钉钉 CLI `dws`，需要已登录。
