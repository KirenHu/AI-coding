# microsoft/llmwiki 源码核验

研究日期：2026-10-13。上游：https://github.com/microsoft/llmwiki 。固定提交：`e09126f257c1fcf8e63234382748a51cf6a5df7f`，package 版本 1.0.1，MIT（Microsoft Corporation，仓库 LICENSE）。本文仅含公开源码和合成测试数据。

## 结论

可以借鉴“不可变来源与可维护主题页分离、目录先行、读取相关完整页后更新、反链与结构检查”的工具链。不能把它当作已解决文章边界、事实冲突和阅读质量的成品编辑引擎：这些判断主要委托外部 Agent；内置 VS Code 导入甚至会直接覆盖同名主题页。比 WorkTwin 当前“每项目一篇”更灵活，但它自动生成实体/概念页的方式也可能造成用户反感的碎片化。

## 实际执行路径

| 环节 | 实际机制及证据 | 与宣传/设计的区别 |
| --- | --- | --- |
| 来源入库 | `packages/core/src/ingest.ts:88-145` 读文件，按 basename 的 slug 生成 `sources/{slug}-summary.md`，重复文件摘要存在则跳过，force 则覆盖；无内容哈希增量判断。 | 机械 ingest 是前 500 字预览，不是语义总结。 |
| 查找相关页 | `ingest-context.ts:74-91,180-203` 提取英语关键词，取前 20 个词调用 query；`query.ts:73-108` 标题权重3、摘要2，只有标题/摘要命中的页才读正文增加分数。 | 无语义聚类/项目归属；中文在正则中过滤掉，相关页发现失效。 |
| 内置生成 | `packages/vscode/src/llmIngest.ts:60-78,173-199` 新来源最多 60000 字符 + 全索引的标题/摘要，单次模型生成 summary、entities、concepts、crosslinks。 | 未把已有相关页完整正文交给生成模型，不能可靠维护旧事实。 |
| 页面边界 | `llmIngest.ts:179-193` 提示词要求只为实质讨论的实体/概念建页、1-3段，信息少就少建或不建。 | 属于提示词约定，不是独立边界规划器，没有跨源同事项身份验证。 |
| 更新已有页 | `packages/core/src/wiki.ts:104-175` createEntity/createConcept 以名称slug作路径，直接 writePage。`llmIngest.ts:100-131` 用新来源数组覆盖 page.frontmatter.sources。 | 同名并不是可靠合并：旧正文和来源可能丢失。 |
| 外部 Agent 维护 | `packages/core/src/mcp/write-tools.ts:403-471` 提供 read+bodyReplace/bodyAppend；`mcp/prompts.ts:47-80` 导入工作流要求跳过已存在实体/概念并建立反链、lint。 | 工具能执行整页维护，但“理解后合并/校正”靠外部 Agent；内置 prompt 不等于已实现调度和冲突算法。 |
| 健康检查 | `packages/core/src/lint.ts:101-230` 检查断链、孤立页、索引漏项/失效项、缺失目标、frontmatter。 | 无事实冲突、依据充分性、低价值内容、可读性检测。 |
| AI检查/修复 | `packages/vscode/src/chatParticipant.ts:288-292` lint 无结构异常直接返回；`:448-475` 修复允许 remove-link/update-link/delete-page/skip。 | /lint 的 LLM 只是解释结构检查，/fix 不是独立编辑审稿。 |
| 测试 | `tests/shared/mcp/smart-ingest-workflow.test.ts:162-296` 固定输入依次调用 ingest/read/create/crosslink/update/lint。 | 测试内容和动作人工写死，无真实模型决策质量评测；不能据其通过证明自然语言可读性。 |

稳定证据链接（固定 commit）：

- [导入上下文/英文关键词](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/ingest-context.ts#L74-L91)
- [内置生成提示词与上下文](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/vscode/src/llmIngest.ts#L173-L199)
- [同名概念页覆盖](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/wiki.ts#L143-L175)
- [整页更新工具](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/mcp/write-tools.ts#L403-L471)
- [结构 lint](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/core/src/lint.ts#L101-L230)
- [结构无异常就停止检查](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/packages/vscode/src/chatParticipant.ts#L288-L292)
- [固定动作集成测试](https://github.com/microsoft/llmwiki/blob/e09126f257c1fcf8e63234382748a51cf6a5df7f/tests/shared/mcp/smart-ingest-workflow.test.ts#L162-L296)

## 本机验证

Node 24.19.0，TypeScript core 编译通过。安装 npm 依赖最初漏掉 darwin-arm64 可选 native binding，补齐同版本 `@rolldown/binding-darwin-arm64@1.2.8` 后执行上游测试，未修改上游代码/依赖清单或 lockfile。

```
node node_modules/typescript/bin/tsc -p packages/core/tsconfig.json
node node_modules/vitest/vitest.mjs run tests/shared/ingest-context.test.ts tests/shared/mcp/smart-ingest-workflow.test.ts tests/shared/mcp/write-tools.test.ts tests/shared/lint.test.ts tests/shared/wiki-create-pages.test.ts
```

结果：5 个文件、144 个测试全部通过，日志 `test-result.txt`。只证明相关确定性 API 行为，不证明模型写作质量。

额外最小探针 `probe.mjs`，运行 `node probe.mjs`（先编译 core），结果 `probe-result.json`：

1. 同名 Release Process 调用两次 createConcept，第二次仅保留“生产需一人审批”，第一次“两人审批”消失。
2. 再建 Approval Rules 写“两人审批”，两个页互相矛盾，lint findings 仍为空。
3. 纯中文会议文本提取关键词为空；两个不同中文标题 slug 都为空。

探针使用临时目录并自动清理，未读写 WorkTwin 数据。未连接 Copilot，没有跑真人会话/付费模型。

## 对 WorkTwin 的迁移取舍

可采用的机制（来自上游）：

1. 原始来源/材料作为证据层，维护过的主题文章为读者入口，二者不混成同一列表。
2. 先看目录标题+摘要，再读取相关现有文章，最后更新原文章的稳定身份；新来源不等于新文章。
3. 页面维护结束同步摘要目录、来源链接、反链、记录操作，再做结构检查。

必须标为 WorkTwin 适配、不能归功于上游的部分：

1. 将项目作为归属范围，范围内按完整事项维护文章；同项目可以多篇，不能从“每项目一篇”直接照搬为“每实体一篇”。
2. 文章 identity 使用已有 ID/规范目录记录，不用中文 slug 或标题相等推断；一个来源可以支持多篇，多个来源可以归入一篇。
3. 合并必须读旧正文和有效证据，明确失效/替代关系；不能复制上游同名盲覆盖。
4. 内容价值/可读性须另选真实可借鉴的编辑审校方案或如实标为适配新增，不能把 lint 改名为质量审查。

授权：仅移植构思可在研究记录注明借鉴；若复制/改写大段代码或提示词，随发行保留 MIT LICENSE 和 Microsoft Copyright。此研究本身没有修改 WorkTwin。
