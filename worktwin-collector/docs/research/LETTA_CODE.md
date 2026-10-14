# Letta Code 知识维护实现核查

核查日期：2026-10-13。官方仓库：https://github.com/letta-ai/letta-code 。固定提交：`145d7cd784f2945673f9023d2bec806e641b9a05`；package version `0.34.13`；Apache-2.0，见仓库 LICENSE。研究范围为源代码及现有测试的静态核查；本机没有 Bun，也未安装该仓库依赖，因此未执行测试或真实模型评估。

## 结论

Letta Code 确实实现了后台反思、现有文档定点更新、隔离候选、另一会话审校并合并、成功后推进消费游标。它适合作为 WorkTwin **文章维护的运行结构**，特别是解决“写完就发布”“失败仍算处理过”的问题。

但它不是开箱即用的人类知识库：文章边界、价值判断、纠错主要由提示词驱动；没有经本次核查确认的独立语义评分器，也没有能证明文章可读性的模型评测。默认 `reflectionMerge=auto`，只有 `explicit` 模式会启动审校会话，不能宣传成默认强制双 Agent 审核。

## 可复核的调用链

以下路径均相对于固定提交，可将路径添加在 `https://github.com/letta-ai/letta-code/blob/145d7cd784f2945673f9023d2bec806e641b9a05/` 后定位。

| 环节 | 实际实现 | 代码证据 |
|---|---|---|
| 定期触发 | 每轮完成后检查累计未反思步骤或上下文压缩事件；只有 MemFS 开启才运行 | `src/cli/helpers/post-turn-reflection.ts:25`；`src/settings-manager.ts:183` 默认 25 步 |
| 选择增量 | 选择尚未处理的 transcript range；跨会话支持 unreflected 与 replay。自动候选上限 30，会话选择上限 5；模型返回未知 conversation ID 会报错 | `src/cli/helpers/reflection-transcript.ts:1062,1212,1511` |
| 构造维护上下文 | 将当前记忆树和常驻记忆作为上下文；需要的其他文件自行按索引读取。跨会话内容综合、消重、修正旧记忆 | `src/cli/helpers/reflection-prompt.ts:9`；`src/agent/subagents/builtin/reflection.md:61` |
| 隔离写候选 | 创建 memory Git worktree，reflection 的 primaryRoot 指向 worktree；父记忆目录作为只读范围 | `src/cli/helpers/reflection-launcher.ts:526`；`src/agent/memory-worktree.ts:191` |
| 内容维护 | 先读现有结构，再筛选，再定点更新；相同主题优先改已有文件，确无归属才新建；矛盾结论在原位置替换，历史移入 ARCHIVE.md | `src/agent/subagents/builtin/reflection.md:61-103` |
| 独立审校 | explicit 模式启动另一 general-purpose 会话，使用父 Agent 身份和独立 conversation；审查准确性、位置、精确性、与已有记忆的一致性，可以修改候选后合并 | `src/cli/helpers/reflection-launcher.ts:458-496,619-645`；`src/cli/helpers/reflection-integration.ts:11` |
| 验证发布 | harness 检查 worktree / parent 干净，并用 `git merge-base --is-ancestor branch HEAD` 判断候选是否真的合并；不是信任 Agent 的“成功”回复 | `src/agent/memory-worktree.ts:403-580` |
| 推进消费游标 | 只有 merged/no_changes 视为消费完成，才更新 reflected_through_message_id；失败保留原游标 | `src/agent/memory-worktree.ts:235`；`src/cli/helpers/reflection-launcher.ts:667`；`src/cli/helpers/reflection-transcript.ts:1870-1927` |
| 重试 | 集成失败从 60 秒开始指数退避，上限 30 分钟；同类错误不反复提醒；手动触发可越过退避 | `src/cli/helpers/reflection-retry.ts:3-62` |

## 内容策略：已有方案实际做了什么

反思 Agent 的五步是 Investigate → Extract → Update → Review → Commit。关键规则可直接学习：

1. **先查已有结构再决定归属。** 先看目录及每篇内容描述，再读邻近主题全文；不会直接为每个提取点新建文档。
2. **允许没有产出。** 优先保留纠错、持续偏好、重要事实和矛盾；临时状态、已记录内容、无法泛化的一次性操作可以不写；没有候选通过筛选就不改、不提交。
3. **已有主题优先维护。** 只有确实独立、且现有文件没有自然归属的新主题才建文件；没有“一项目一篇”的固定约束，也没有“每个点一篇”的默认行为。
4. **旧结论在原处校正。** 不把相互冲突的新旧版本并列塞进当前文档；退役且仍有历史意义的信息缩成日期记录进入历史文件。
5. **将结构检查与内容检查分开。** 内容审校由模型完成；真正是否提交、合并、消费增量由程序检验。

这些是提示词中的产品策略，不能误称成代码硬保证。例如“latest evidence 优先”只是它的通用记忆策略；WorkTwin 有正式决定、草案、AI 建议等不同证据等级，不能照搬为“日期较新就覆盖”。

## 审校失败的真实行为

- reflection 未成功、留下未提交文件：删掉候选 worktree / branch，正式记忆不接收该候选，原 transcript 可以重试。
- 父记忆存在未提交修改：停止集成，保留父修改，清理候选，稍后重试。
- 自动合并发生冲突：abort merge，清理候选，保留消费游标。
- 审校 Agent 说成功但没有真的合并：harness 判 failed，不能消费 transcript。
- 审校 Agent 已真实合并，之后输出流报错：以实际 Git 状态为准判成功，避免重复消费。
- 内容不值得保存：无 commit 判 no_changes，消费 transcript，防止同一无价值内容永远反复处理。

对应现有测试：`src/cli/reflection-worktree-completion.test.ts:105` 审校可修订并提交；`:193` 集成失败；`:220` 已合并但进程失败；`:247` 未提交修改；`:270` 口头成功但没有合并；`:289` 父目录脏；`:332` 合并冲突。`src/agent/memory-worktree.test.ts` 还有真实临时 Git 仓库的合并、冲突和清理测试。测试内容已阅读，未运行。

## 建议迁移到 WorkTwin 的机制

这是从上述实现做适配，不要求把 SQLite 产品改成 Git 记忆仓库。

| Letta 机制 | WorkTwin 对应实现 |
|---|---|
| 当前目录/description → 读取邻近全文 → 决定更新/新建 | 项目内文章目录（文章 ID、标题、摘要、覆盖范围）→ 加载候选文章及证据 → 更新已有文章或创建真正独立事项文章 |
| 隔离 worktree | 候选文章计划与候选正文独立存储/内存对象；不要先改知识库当前版本 |
| 独立 integration conversation | 第二次独立模型调用读取证据、原文、变更稿，判定可发布/需修改/无增量。无需向人类增加审批操作 |
| 实际 Git ancestry 验证 | 审校通过后，在同一 DB 事务内验证原版本与证据摘要未变，再写文章版本、引用状态和材料 receipt |
| no_changes 与失败区别 | 无新增价值可记已处理；模型错误/审校不通过保留为可重试，不污染正式正文 |
| 原位替换 + ARCHIVE | 当前正文只保留现行结论，旧版本保存在现有文章日志与历史证据中，不参与默认当前知识检索 |

## 限制与不应照搬的内容

- 第二会话使用原 Agent 的身份，不保证不同模型或不同知识偏差；只能称独立执行阶段，不能称独立专家共识。
- 审校 harness 验证的是文件/提交状态，不会程序化证明事实正确。WorkTwin 仍须保留精确引用与权限检查，并为内容质量建立实际样本验收。
- Letta 主目标是 Agent 记忆，persona/skill/always-in-context 分层不适合直接呈现在 WorkTwin 人类文章目录中。
- Letta 拒绝保存某些一次性状态是 Agent 记忆取舍；WorkTwin 对会议中的有效决策、阶段结论仍需保留，不应复制它的领域排除词。
- 不必复制 Git 全套租约/推送/工作树代码；WorkTwin 已有 SQLite 事务、版本和证据 SHA，迁移运行契约即可。
- Apache-2.0 允许适配；如复制具体提示词或代码，保留 LICENSE、上游出处、固定提交及修改声明。本次仅写研究记录，未修改 WorkTwin。
