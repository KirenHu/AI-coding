# WorkTwin：基于现有项目的知识维护改造

2026-10-13；目标版本 1.5.2。研究对象均有公开源码，本次直接追踪源码和测试，没有把静态阅读称作二进制逆向。

## 结论与选择

采用 **Rowboat 的知识目录与整理器 + Letta Code 的候选审校与发布流程**。保留 WorkTwin 的 SQLite、授权、精确引用、历史版本和文章界面。Graphiti 用于核对事实更新原则；不引入其图数据库，也不声称已经移植完整事实图谱。

没有发现一个已验证、开箱即用且同时解决文章边界、事实校正、人类可读性的成品。现成项目确实提供了可复用运行机制，但语义判断仍依赖模型。此次交付是明确来源的机制适配，并用新案例验证，不是继续手工整理某一个项目。

| 项目与固定版本 | 已核实的能力 | 采用 / 不采用 |
|---|---|---|
| [Rowboat `68d8d1c`](https://github.com/rowboatlabs/rowboat/tree/68d8d1c8b3bb618df7643dadc00ae4f592a4fb7c)，Apache-2.0 | 现有知识目录识别对象；新资料更新原笔记；另有定期整理器重写和压缩文章，保留历史 | 采用目录先行、稳定文章身份、独立整理。其新增 Topic 默认只是建议卡，不能说它已自动解决文章边界；不照搬联系人模板及邮件准入规则 |
| [Letta Code `145d7cd`](https://github.com/letta-ai/letta-code/tree/145d7cd784f2945673f9023d2bec806e641b9a05)，Apache-2.0 | 反思候选在独立工作区；explicit 模式另开会话审校；确认实际合并后才推进材料游标 | 采用候选与正式内容分离、独立审校、发布与 receipt 同事务。用 SQLite 事务适配 Git 工作树；Letta 默认 auto，不宣称默认自带强制审校 |
| [Graphiti `0a3b171`](https://github.com/getzep/graphiti/tree/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13)，Apache-2.0 | 实体归一；重复与矛盾分开识别；根据事实有效时间失效旧边；来源与历史保留 | 学习“业务生效时间不等于导入时间”和明确替代原则。未移植图谱时间裁决函数；文章级审校记录替代/支持/未决关系，不等同于逐事实有效期引擎 |
| [Microsoft LLM Wiki `e09126f`](https://github.com/microsoft/llmwiki/tree/e09126f257c1fcf8e63234382748a51cf6a5df7f)，MIT | raw/source/topic 层分离，索引、整页编辑、结构 lint | 保留分层思想。不复制中文 slug、同名盲覆盖、结构 lint 充当内容检查；实测相互矛盾页面仍通过 lint |

## Rowboat 的直接源码依据

- [note_creation.ts:124](https://github.com/rowboatlabs/rowboat/blob/68d8d1c8b3bb618df7643dadc00ae4f592a4fb7c/apps/x/packages/core/src/knowledge/note_creation.ts#L124)：先读规范化目录，按身份及上下文匹配，必要时再读全文。
- [note_creation.ts:489](https://github.com/rowboatlabs/rowboat/blob/68d8d1c8b3bb618df7643dadc00ae4f592a4fb7c/apps/x/packages/core/src/knowledge/note_creation.ts#L489)：同名不代表同一对象，同一批次不证明关系。
- [note_creation.ts:804](https://github.com/rowboatlabs/rowboat/blob/68d8d1c8b3bb618df7643dadc00ae4f592a4fb7c/apps/x/packages/core/src/knowledge/note_creation.ts#L804)：新增项目/主题默认建议卡；已有笔记则更新。此边界说明不能把其产品宣传等同于自动文章分类已解决。
- [note_curation.ts](https://github.com/rowboatlabs/rowboat/blob/68d8d1c8b3bb618df7643dadc00ae4f592a4fb7c/apps/x/packages/core/src/knowledge/note_curation.ts)：单篇全文重写，概要反映现状，去重、压缩、降级无依据推断、校正时间措辞。
- [build_graph.ts:883](https://github.com/rowboatlabs/rowboat/blob/68d8d1c8b3bb618df7643dadc00ae4f592a4fb7c/apps/x/packages/core/src/knowledge/build_graph.ts#L883)：每日检查，至少 8 条活动、7 天冷却、一次最多 8 篇，成功后提交历史。这些阈值针对其联系人笔记，不照搬到 WorkTwin。

其整理器主要根据已有笔记编辑，并无独立事实审校器。WorkTwin 的独立审校运行结构取自 Letta explicit integration；审校内容依据原始来源，而非仅凭旧摘要。

其余三个项目的完整调用链、限制与复现见 [Letta](research/LETTA_CODE.md)、[Graphiti](research/GRAPHITI.md)、[LLM Wiki](research/LLMWIKI.md)。Rowboat 和 Letta 本轮只做源码/测试代码阅读；LLM Wiki 运行 144 项相关上游测试及覆盖/矛盾/中文探针；Graphiti 原样隔离运行时间裁决函数，6 项通过。上游测试结果不代表 WorkTwin 内容质量已获保证。

## 融入后的通用处理路径

1. **采集与材料层不变。** 保存来源和上下文、项目身份及权限。零散信息是后台材料，不直接等同于知识库文章。
2. **先找已有文章。** 每次读取同一项目内已授权文章的 ID、标题、概要、适用条件和正文开头。根据完整事项识别新资料的自然归属。已有事项的补充、纠正继续维护原 ID；没有合适归属且能独立解决持续有价值的问题，才建新文章。
3. **允许一项目多篇，也允许一会议一篇。** 不按材料条数、会议标题或固定篇数分割。退款流程和运维部署应分开；同一退款流程中的申请、审核、退回条件应形成连贯说明。确实重复的旧文章可合并；无关文章不参与改写。
4. **重写候选正文。** 编辑器读取选定文章全文、相关材料和原始资料；文章是概要与连贯 Markdown 正文。删除原先按“岗位培训/沟通技巧”等业务类别一刀切的排除规则，按本篇读者需要筛价值。
5. **独立审校。** 另一次调用读取候选、旧文章及原始证据，不继承写作者的推理；检查完整事项、背景、歧义、赘述、遗漏、无依据断言、确认状态和冲突。较晚导入的旧资料、新建议不自动推翻正式规则。记录有证据的 `supports / supersedes / unresolved` 判断。
6. **修订与发布。** 拒绝时携带具体意见重新写一次，再审；仍拒绝则保留正式旧文，记录失败并进入现有延时重试。通过后再检查文章版本、来源内容与权限是否改变；文章、当前引用、版本历史、材料处理回执同事务提交。
7. **继续增量维护。** 每轮最多处理 24 条新材料，后续批次继续；缺上下文材料单独记等待，不能堵住新资料。新增资料或已有文章变化会唤醒等待材料。原有已消费材料不会因升级而全量重跑。

## 实现位置与边界

- `worktwin/article_editor.py`：目录归属协议、独立审校协议、输入 ID 校验和分批证据审核。提示词是针对 WorkTwin 重新表达的适配，没有接入上游运行时。
- `worktwin/article_maintenance.py`：取消项目唯一文章约束；按选定事项写稿；候选审校、一次自动修订、发布及 receipt；等待材料不阻塞队列。
- `project_documents` 迁移为文章 ID 主键，保留原文章 ID、内容与历史；项目可有多篇文章。
- `article_editor_reviews` 保存审校结果；`article_material_deferrals` 保存缺背景材料的等待上下文。不会向用户增加审批步骤。
- 原有当前引用/历史引用与默认检索规则继续使用，退役内容不作为默认当前答案。

本轮改造的是文件与会话的文章维护器。浏览器操作记录仍走独立任务/网站文章管线；没有把它偷偷合进本地文件处理，也没有声称该管线已同步移植所有新机制。项目识别仍使用原有证据归属模型，本轮不重分配历史项目。

## 验证方式

编排回归覆盖：同项目多个完整事项、连续更新原 ID、审校拒绝不改正式稿、不消费失败材料、自动修订一次、错误引用/跨范围 ID、超过 24 条材料持续处理、等待材料不饿死新背景、暂停采集不撤销已有 AI 整理许可、旧版本和授权保留。

另用已配置模型跑全新虚构商城案例：退款流程与部署手册独立成文；正式调整退款审批规则；随后混入晚导入旧会议和未确认的新提议，检查是否保持最新已确认口径。测试脚本 `scripts/evaluate_article_editor.py` 只读取本机连接配置，不读取私人文档，单独生成测试数据库。实际运行结果另记验收报告。
