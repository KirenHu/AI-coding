# Hindsight 影子验证与开源复用决策

复核日期：2026-10-10。WorkTwin 开发基线为 1.2.1 候选；核对的 Hindsight 源码版本为 0.10.3。

## 本轮决定

继续增强现有采集、工作证据与项目持续纠错。Hindsight 保留为候选知识整理引擎，通过独立脚本验证；不把其数据库、模型或 SDK 加入桌面包，不写入 WorkTwin 正式知识。只有实际质量和维护收益优于当前实现，才决定接管具体环节。

这不是对 Hindsight 能力的否定：它已经提供事实提取、跨记录整理、引用、动态页面及多个删除维护机制。当前关键问题是这些机制与本产品的低门槛桌面分发是否匹配，以及中文工作资料的实际整理效果。

## 本次核对对上一轮结论的修正

| 问题 | 当前官方事实 | 对 WorkTwin 的选择 |
| --- | --- | --- |
| 追加重试 | async retain 支持调用方提供 `operation_id`；同 ID 重交复用原任务，避免重复追加。同步 retain 忽略该字段。 | 直接复用这一机制，稳定批次生成 UUID，不另建一套复杂分布式事务框架。 |
| 来源删除 | 删除文档会删除其事实和派生 observations，把仍存在的其他来源重新排队整理；图维护还会修补关系与孤立实体。 | 验证这些已有能力，不重复实现引擎内部维护。 |
| 页面删除传播 | Knowledge Pages 的 `is_stale` 仍不观察删除；已生成页面可能保留失效结论。官方支持 `clear` 后 `refresh` 完整重建。 | 分别测试自动传播和显式重建；后一项成功不能掩盖前一项失败。正式接管前必须确定 WorkTwin 如何限制失效候选的使用。 |
| 桌面依赖 | PostgreSQL 14+；默认 pg0 面向开发。Slim API 最低约 512 MB 内存，另需数据库和 embedding/reranker；完整镜像约 9 GB AMD64 / 3.7 GB ARM64。 | 先走可选 HTTP 研究入口，不给每个桌面用户新增部署任务。 |
| memU 增量 | `TranscriptSource.read_incremental()` 默认调用 `read_records()` 全量读取，再按行数标记新增部分。 | 借鉴多工具统一适配边界；真正按游标读取由 WorkTwin 实现。 |
| OpenViking 复用 | 主项目 AGPLv3，`examples` 为 Apache 2.0。`examples/compile` 是编译示例及 skill，不是可直接替换的轻量独立引擎。 | 参考知识编译结构；具体复制源码时再随对应文件保留许可。本次未复制其源码。 |

## 执行入口

脚本仅使用 Python 标准库连接 Hindsight。所有输入是仓库中的三个合成中文会话，包含同名不同项目、上下文省略、已有决策更新、AI 声称完成但测试失败、来源删除。不会读取已安装的 WorkTwin 数据、用户目录、Keychain 或真实会话。

```bash
cd worktwin-collector
python scripts/knowledge_engine_probe.py --report /tmp/hindsight-probe.json
```

未提供服务地址时，输出 `PENDING`、退出码 2，不发起网络请求；这是环境待配置，不是引擎通过。

已有 Hindsight 服务时：

```bash
python scripts/knowledge_engine_probe.py \
  --endpoint http://localhost:8888 \
  --report /tmp/hindsight-probe.json
```

服务需要鉴权时，使用环境变量 `WORKTWIN_HINDSIGHT_API_KEY`，不要把 Key 写入命令行 URL。脚本新建随机 `worktwin-probe-*` bank，结束后删除该 bank；加 `--keep-bank` 可保留合成结果供查看。服务自身须已配置 LLM、embedding 与 reranker；试验产生该服务对应的模型调用费用。

同时比较现有提炼器时，显式设置 `WORKTWIN_PROBE_MODEL_URL`、`WORKTWIN_PROBE_MODEL_KEY`、`WORKTWIN_PROBE_MODEL`，再增加 `--baseline`。它直接调用现有 `extract_knowledge`，使用相同合成会话与前一轮候选知识；需要 WorkTwin 开发依赖已安装。此比较覆盖提炼结果，不等于完整知识维护管线的端到端比较。

报告记录：

- 数据集 SHA-256、独立 bank ID、检查结果和请求耗时。
- 稳定 operation ID 重试、一次性追加结果、项目范围与源文档追溯。
- 删除前后 recall、页面自动更新、显式重建的原始结果。
- 可选的现有提炼器初始/更新结果与耗时。
- 质量复核保持 `PENDING`；依据 fixture 中的 `expected_outcomes` 查看中文结论和引用。自动检查通过不能证明优于现有引擎。

异步 retain 不返回 token usage，现有提炼接口也不回传 usage，因此报告的 `model_cost` 为 `null`，不通过字符数臆算费用。需要比较成本时，使用两侧同一运行期间的供应商或服务使用记录。

退出码：0 表示本脚本自动检查通过，1 表示检查失败或服务错误，2 表示没有配置 Hindsight。脚本会保留失败报告，不跳过失败检查，也不自动将候选结果写回 WorkTwin。

## 当前验收状态与采用标准

本轮环境没有 Hindsight 服务、PostgreSQL/Docker 或显式提供的模型测试凭据，因此**真实引擎效果、删除传播和成本比较尚待执行**。离线测试只验证请求与状态处理契约；不代表 Hindsight 的实际能力验收。

进入正式接管应满足：同一批资料下中文结论正确且有来源；项目范围不混用；上下文增量没有丢失；删除和撤权后旧候选不可用；维护成本或整理质量有明确收益。先替换有收益的一处，不同时运行多个正式知识库。

## 官方依据

- [Hindsight retain：append 与 async operation_id](https://hindsight.vectorize.io/developer/api/retain)
- [Hindsight observations：删除及重新整理](https://hindsight.vectorize.io/developer/observations)
- [Hindsight Knowledge Pages：页面与 is_stale](https://hindsight.vectorize.io/developer/api/knowledge-pages)
- [Hindsight mental models：clear + refresh](https://hindsight.vectorize.io/developer/api/mental-models)
- [Hindsight graph maintenance](https://hindsight.vectorize.io/developer/api/operations)
- [Hindsight 部署](https://hindsight.vectorize.io/developer/installation)
- [Hindsight 0.10.3 API 包](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api/pyproject.toml)：复核 blob `f44758a27894e8f4fb269182eacbc51dcea33dc5`，MIT。
- [memU TranscriptSource](https://github.com/NevaMind-AI/memU/blob/main/src/memu/hosts/base.py)：复核 blob `2f8fe12de41b53a062e3121ff6ed001391f5b0cf`。
- [OpenViking examples 许可](https://github.com/volcengine/OpenViking/blob/main/examples/LICENSE)：复核 blob `b65287786d2ed55db2a33e2f121959dab3693e50`。
- [OpenViking compile 示例](https://github.com/volcengine/OpenViking/tree/main/examples/compile)。
