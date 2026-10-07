# 测试与评测口径

## 1. 原则

- 自动化测试证明确定性合同，真实模型评测衡量概率性表现；
- 不把 fixture 结果当真实模型能力；
- public 与 seed-blind 分开报告；
- 失败报告保留，不覆盖、不修改 scorer 迎合已知案例；
- 不降低门槛包装成功；
- Tool Calling 和 Extraction 评测必须证明零业务写入、零资源创建和零重复资源。

## 2. RAG

### 49 条基线

- 33 train + 16 holdout；
- 回答正确率 89.80%；
- 引用支持率 96.77%；
- 正确拒答率 94.44%；
- E2E P95 5,338ms。

### 具体业务问题回归

corrected public 12 条结果：终态 12/12、可回答项 7/7、澄清 precision/recall 3/3、引用支持/验证 10/10、拒答 2/2。

Retrieval P95 为 3,246ms，未达到原 500ms 严格门槛；E2E P95 为 4,918ms，达到 8,000ms 门槛。quality gate 因 retrieval 指标为 false。这是已接受但仍保留的性能债务，不应改写为“全部门禁通过”。

## 3. Candidate Slot Extraction

| 指标 | Public 60 | Seed-blind 20 |
|---|---:|---:|
| Envelope valid | 60/60 | 20/20 |
| Field precision | 99/99 | 33/33 |
| Field recall | 99/99 | 33/33 |
| Ambiguity handling | 19/19 | 4/4 |
| Source verification | 99/99 | 33/33 |
| Clarification | 25/25 | 6/6 |
| Terminal | 60/60 | 20/20 |
| Must-not-execute | 60/60 | 20/20 |
| Total-turn P95 | 1,835ms | 1,849ms |

两组均为 `deepseek-v4-flash` 真实模型；blind 组在冻结后生成并只运行一次。

## 4. HR Tool Calling

公开 48 条回归前六次仍有不同失败，证明 `temperature=0` 不等于确定性。第七次达到门槛：工具 48/48、完整参数 24/26、澄清 3/3、终态 48/48、Must-not-execute 33/33、P95 5,053ms。

seed-blind 20 条首次单次运行：工具 19/20、完整参数 14/15、澄清 2/2、终态 20/20、Must-not-execute 14/14、P95 3,915ms。

两组均为 write executed 0、created resources 0、duplicate resources 0。

## 5. 采购 Tool Calling 与审批

采购 public 20：工具 19/20、完整参数 14/15、澄清 2/2、终态 19/20、Must-not-execute 20/20、P95 2,952ms。

采购 seed-blind 20：工具 20/20、完整参数 15/15、澄清 2/2、终态 20/20、Must-not-execute 20/20、P95 2,684ms。

冻结阶段另有真实 PostgreSQL 后端全量 1,891 passed、前端 240/240、浏览器组合 8 passed。100k 合成审批数据下，20-sample P95 为：pending list 401.50ms、completed list 104.57ms、detail 20.50ms、deterministic write 50.18ms。

这些数字属于当时冻结提交和环境，不自动代表未来提交；公开候选的实际验证结果单独记录在 `docs/release-verification.md`。

## 6. 为什么不提交原始报告

原始 `output/` 报告属于本地验收资产，可能包含运行环境、Provider 配置摘要和大量中间 trace。公开仓库保留数据集、manifest、evaluator、评分代码和经核实的聚合证据，不复制整个输出目录。
