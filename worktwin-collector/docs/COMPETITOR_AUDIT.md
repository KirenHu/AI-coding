# WorkTwin 竞品源码研究：Viven / Rowboat / remio

> 2026-10-08. Scope: verified official product documentation and selected Rowboat
> source files, not a reproduction of other products.

## 1. 对照结论

| 产品 | 已核查依据 | WorkTwin 可复用的价值 | 当前处理 |
|---|---|---|---|
| Viven（商业闭源） | [Training Your Twin](https://docs.viven.ai/docs/user/working-with-twin/training-your-twin), [Sharing Your Twin](https://docs.viven.ai/docs/user/digital-twins/managing-and-sharing) | 知识草稿/发布闭环、按 Persona 可读范围、问答反馈纠错；组织与员工权限叠加 | 参考设计，不复制闭源代码；当前只具备本地知识分配，未实现真正跨员工的 SSO/RBAC |
| Rowboat（Apache-2.0） | [Knowledge engine](https://github.com/rowboatlabs/rowboat/tree/main/apps/x/packages/core/src/knowledge) | 本地 Markdown、增量构建、实体笔记、`note_creation` 和 `note_curation` 分层，周期 Gardener 养护 | 本轮实现 **Python Gardener**，采用相似的可证据追溯质量约束、冷却期和人审发布；参照其开源源码设计 |
| remio / AK Wiki（商业产品 / 扩展） | [AK Wiki](https://www.remio.ai/aapp-market/ak-wiki), [remio Market](https://www.remio.ai/aapp-market) | 按主题自动编纂 wiki、整理知识集合，并持续演化 | 借鉴产品行为；目前未验证 AK Wiki 有可合法直接引用的源码，也未直接复用代码 |

## 2. Rowboat 的具体源码审计

**主要参考文件：**

- `apps/x/packages/core/src/knowledge/build_graph.ts`: 调度批次、提取和图谱索引、基于 mtime/hash 的增量处理、状态记录。
- `apps/x/packages/core/src/knowledge/note_creation.ts`: 从单份资料构建/更新实体笔记（由知识目录中的 README 指明用途）。
- `apps/x/packages/core/src/knowledge/note_curation.ts`: 智能知识养护，压缩活动、时间校正、明确不新增事实、维护 Wiki 链接。
- `apps/x/packages/core/src/knowledge/knowledge_index.ts`: 人物/项目/主题 Markdown 索引、标题和关系解析。
- `apps/x/packages/core/src/knowledge/README.md`: 设计约束与调度策略：成熟笔记、7 天冷却、单笔记处理及版本历史。

**实际采用：** Rowboat 的“知识生成 / 知识养护”分层与质量约束是本轮功能设计的直接依据。
WorkTwin 新增 `worktwin/gardener.py`，以相同的质量目标独立实现 Python/SQLite
的定期养护、来源授权检查、幂等处理、生成提案、人审更新。

**代码授权与复用说明：** Rowboat 主仓库为 Apache-2.0。我们审查了其
TypeScript 实现，但本轮 **未复制/移植第三方可执行源文件或依赖其运行时**；
核心提示词约束用中文重新撰写，以适配 WorkTwin 的证据与权限模型。未将
Electron 主程序、Node 工具总线或 Harbor 协作后端当成 WorkTwin 运行依赖，
是因为它们的主要职责超过当前纯采集与知识编纂边界，直接嵌入会提高维护成本。
如后续实际复制任何 Apache-2.0 源码，必须保留适用许可证、版权和变更声明。

## 3. 本轮实现：背景知识养护

新增后台流程：

```
授权采集 → 结构化提取 → 关联及冲突建议
                              ↓
                    成熟知识的定时养护
                              ↓
                  原文/时间/链接约束检查
                              ↓
                     已有知识更新建议
                              ↓
             用户核对 → 更新正文及历史记录
```

- **可选条件**：知识至少有 3 条有效证据、正文较长或已有多个历史版本。
- **同意边界**：所依赖的每个来源必须允许 AI 处理，未授权来源不能作为养护上下文。
- **费用与调度**：后台低优先级，待新增知识处理队列空闲后，单次最多处理一篇，每天最多 8 篇。
- **复审**：已有确认知识保持可用。仅在用户确认后更新文章内容；未经核对的整理不会让分身断供。
- **版本**：模型提出整理建议，不自动覆盖已确认知识；人审后沿用现有版本历史逻辑。
- **安全**：模型运行期间如来源被撤销或知识被修改，丢弃过期结果。

## 4. 暂不复用的内容与原因

- Viven 私有的模型训练、企业权限/连接器代码不可公开复用。
- remio AK Wiki 在所查官方材料中是产品扩展，并非已核实的开放源码组件；不能声称复用了它的代码。
- Rowboat 基于 TypeScript/Electron 的完整私有工作空间客户端不能直接嵌入 Python/SQLite；
  应优先复用**明确的工程边界与可移植数据格式**，在技术与许可证适配收益充分时再引入源码。

## 5. 后续验证

1. 对于长期稳定的知识，抽样检查编纂后的正文有无丢失重要旧结论或虚构更新。
2. 正常工作 7 天以上，核查平均每篇的长度、重复率、检索命中率。
3. 试验与 Rowboat/Obsidian 兼容的 Markdown Wiki 导出和双向链接，不复制整套 UI。
4. 接入经过授权的真实企业模型，在受控环境评估跨会话历史结论还原和引用准确性。

与此前设计比较：不再只评价“提取准确”，还要评价“累积若干个月后仍然清晰可用”。
