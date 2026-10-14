# Graphiti：可借鉴的事实更新机制，不是完整文章维护器

研究日期：2026-10-13。官方仓库 https://github.com/getzep/graphiti 。固定 commit `0a3b1711b018e5590cf5c44a3ee3e67d6733ee13`（2026-10-11），Apache-2.0。直接阅读完整公开源码，因此无需对同一代码再做二进制逆向。未读用户私人资料，未调用模型、未改 WorkTwin。

## 结论

采用 Graphiti 的“实体先归一、重复与冲突分开判断、按事实有效时间更新、保留来源和历史”机制，适合作为 WorkTwin 文章更新之前的证据整理层。不要把它的实体摘要或社区聚类直接当成人类知识文章：其核心产物是关系事实；社区摘要最多 1000 字符，没有完整文章的章节维护、读者目标、独立内容质量审核。

它不是“新资料覆盖旧资料”。先由模型判断两条事实无法同时成立，程序才比较事实的有效时间。后导入的旧会议不会因为导入较晚而推翻新决策。业务有效时间与系统收录时间必须区分。

## 实际调用链与源码证据

以下链接全部锁定上述 commit。

1. `Graphiti.add_episode` 收录原始消息/文本/JSON，携带 `reference_time`、分区 `group_id`，读取相关前序 episode。先抽取实体，再对实体归一，然后抽取关系、去重、失效旧关系。新旧关系一并写回，来源 episode 得以保留。[入口及处理链](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/graphiti.py#L1430)
2. 实体候选综合身份属性、规范化精确名称、语义近邻，不是仅凭文件夹或同名匹配。[节点候选](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/utils/maintenance/node_operations.py#L468)
3. 对新事实先在同分区、同实体对内检索重复候选；再在同分区更广泛检索矛盾候选。两个集合去重后送入裁决。[候选召回](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/utils/maintenance/edge_operations.py#L520)
4. 完全相同的事实及端点走确定性去重，复用原 ID、追加来源 episode。其他事实由小模型输出重复和矛盾索引，代码检查索引范围。[去重与结构化裁决](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/utils/maintenance/edge_operations.py#L937)
5. 语义矛盾判断是提示词与模型能力，不是确定性逻辑。提示词明确要求不能仅凭记录更新、日期较新、同项目有新进展就宣告旧事实失效；只有明确替代、终止、转移或不能同时成立才算。[矛盾提示词](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/prompts/dedupe_edges.py#L44)
6. 程序依据 `valid_at` / `invalid_at` 处理时间区间；后收到的旧事实遇到已知的新事实，会终止旧事实自身；重叠区间内新事实可以终止旧事实，写 `invalid_at` 和系统 `expired_at`，不删原内容。[时间裁决](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/utils/maintenance/edge_operations.py#L715)
7. 事实保留来源 episode IDs 及有效/失效时间。[数据字段](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/edges.py#L263)；新旧事实一起持久化。[保存](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/graphiti.py#L1026)
8. 社区是按实体连接强度标签传播分组，然后成对合并摘要。[社区聚类](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/utils/maintenance/community_operations.py#L96)；[成对摘要](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/utils/maintenance/community_operations.py#L198)。这不等价于完整事项的文章边界。
9. 去掉“某某提到/讨论了”等转述废话、直接表达信息，是摘要提示词已有做法；但仅是提示词要求，没有独立审稿器。[摘要提示词](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/prompts/summarize_nodes.py#L55)

## 隔离复现：6 项通过

`graphiti-temporal-probe.py` 用 Python AST 原样加载上游两个时间处理函数和 UTC 函数，替换数据容器为最小 dataclass。无图数据库、embedding、模型调用。运行：

```sh
../AI-coding/worktwin-collector/.venv/bin/python graphiti-temporal-probe.py
```

结果见 `graphiti-temporal-probe-result.json`：

| 情况 | 实测结果 |
|---|---|
| 明确冲突，新事实生效较晚 | 旧事实失效时间改为新事实生效时间 |
| 旧事实后到达，已有更新事实 | 后到达的旧事实结束，不推翻新事实 |
| 两条冲突事实有效时间相同 | 都不自动失效，仍需上层处理 |
| 旧事实有效时间缺失 | 不自动结束旧事实 |
| 有效区间不重叠 | 不改变旧区间 |
| 无语义冲突候选 | 不执行失效 |

这是上游时间机制的独立复现，不是完整 Graphiti 集成或模型准确率验证。语义矛盾识别及实体归一准确率未知。

## 融入 WorkTwin 的最小机制

建议学习机制而非引入完整图数据库栈：保留 SQLite 和现有文章/引用结构。

- 把“新材料与既有文章/事实的关系”结构化成 `补充 / 重复 / 明确替代 / 冲突未决`，记录被作用的旧材料或文章段落 ID、证据 ID、有效时间与理由。模型不直接在散文里含混地覆盖旧结论。
- 同一项目归属先稳定，再找相同事项；Graphiti 的分区与实体归一可借鉴，但它没有自动替 WorkTwin 发现完整文章边界。
- 明确替代需要引用证据与相同适用条件。导入时间不能当决策生效时间。未决冲突保持可见，不能仅因“最新”选择一边。
- 发布新文章版本时，同时更新当前证据集合、被替代关系和版本记录。旧证据留历史，当前检索只读当前版本有效证据。文章正文不并列旧、新两个都标“当前”的结论。
- 重复信息只追加来源，不生成新文章、不无意义改写正文。
- 由完整文章编辑器消费整理后的当前事实，再由独立审核步骤检查是否仍含冲突/无证据结论及阅读质量。后两项需要从文章/wiki 项目寻找实现；Graphiti 本身没有可直接复刻的完整文章维护器。

以上为移植建议，尚未改代码。若直接移植其函数/提示词，需在 third_party 保留 Apache-2.0 与 Zep 版权、固定上游版本和改动说明。

## 不应照搬的部分

- Graphiti 的“相关实体社区”不是“面向读者的完整事项”。按连接强度聚类可能把项目所有人/事聚在一起，也可能把完整任务拆开。
- 不能声称装了 Graphiti 即默认只检索最新事实。其基础 `search` 没给 filter 时传入空 `SearchFilters()`；`invalid_at` / `expired_at` 默认均为空。应用需明确当前有效过滤或在答案组装阶段处理时间。[入口](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/graphiti.py#L1946)，[过滤模型](https://github.com/getzep/graphiti/blob/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13/graphiti_core/search/search_filters.py#L55)。
- 时间相同/缺失的冲突无法由该确定性函数决定；来源权威性也未纳入此时间裁决函数。需保留未决状态。
- 单次 LLM 判断出错仍会影响失效；结构化输出与时间代码没有证明语义事实正确。
