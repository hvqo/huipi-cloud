# 本地Embedding、Dense与混合检索基线

## 冻结与方法

仅使用自建合成教师参考文本，标签来源`synthetic_curated_reference`，人工审核状态`unreviewed`。它们不是教师双评，也不是学生资料。默认CI不下载权重，不进行收费调用；没有调整评分生产协议或标签。

输入：[48个查询与28条来源版本](../../data/evaluation/retrieval/scoped-cases-v1.json)、[实验定义](../../data/evaluation/retrieval/experiment-v1.json)及各自`.sha256`。数据SHA为`675ff665a65abfede6117e3e6b1b84dcb6c67e682cbc746f983299dae64798f4`。在首次质量运行前以提交`7eefac0`登记，报告中的freeze_commit指数据与选型冻结提交；实现当时尚在同一工作树开发，不声称是完整代码发布版本。

共有43个可回答查询、5个无答案探针。包括20个词汇直接匹配、20个中文/中英改写，以及当前/旧版、撤销、跨作业、单字、长查询等8个专项；存在相似干扰资料。每条标签明确document/source_version/chunk_index，与Unicode原文分块对应。此集合内容较短，主要为单Chunk，长文档、多Chunk生命周期另由真实PG/S3集成测试覆盖，不据此证明长文档召回质量。

预先固定：Qwen/Qwen3-Embedding-0.6B官方快照`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`，1024维float32/L2/last-token、固定query指令、余弦门槛0.55、RRF k=60、统一Top-K=1/3/5。门槛未校准，本轮不根据结果调阈值。操作系统与依赖见[开发指南](../development/hybrid-retrieval.md)。

三种模式在相同独立测试PG、私有测试S3和同一来源快照运行，Assignment先过滤再召回，所有返回来源经过SHA与版本检查。先预热索引，再测完整search耗时（包括PG/S3）。报告输出不可覆盖；没有模型生成的相关性标签，也没有LLM参与检索评测。

## 原始结果与分母

[完整逐查询结果](hybrid-retrieval-v1-results.json)与[SHA](hybrid-retrieval-v1-results.json.sha256)保留全部返回标签、分数和延迟。2026-10-15本地RTX3060 12GB实测，26个有效Chunk，净向量106496字节，索引含模型启动5.013秒。模型权重本地约1.2GiB；向量负载不含Python/模型/临时内存，不能当作总内存。

- Lexical：Recall@1为37/43=86.05%，@3与@5均39/43=90.70%；MRR为38/43=0.8837。Precision@1/3/5依次37/43、39/129、39/215。P50/P95为26.625/33.915毫秒。无答案误命中1/5。
- Dense：Recall@1/@3/@5均42/43=97.67%；MRR42/43=0.9767。Precision@1/3/5依次42/43、42/129、42/215。P50/P95为34.383/38.712毫秒。无答案误命中0/5。
- Hybrid：Recall@1/@3/@5均42/43=97.67%；MRR42/43=0.9767。Precision@1/3/5依次42/43、42/129、42/215。P50/P95为73.823/88.755毫秒。无答案误命中1/5。

Recall和MRR只以43个可回答查询为分母，Precision@K按请求的K计算（即使实际不足K也不缩分母）；无答案探针另算。原始相关性标签为等级2，当前指标按相关/不相关二值计算。三种模式跨作业泄漏均0、过期来源命中均0。延迟为一次本地观测，不是性能承诺。

## 收益与失败

20个直接匹配查询三种模式均20/20首位正确。20个改写查询从Lexical14/20提升到Dense/Hybrid19/20。首位改善的五例为photo-paraphrase、oxidation-paraphrase、speed-paraphrase、energy-paraphrase、past-paraphrase；完整配对见原始报告。原先首位正确的案例没有首位退化，但其他返回位置及无关片段不能据此视为全部正确。

`simile-paraphrase`仍失败：问题是“文章将月亮说成玉盘，这是怎样的修辞？”；Dense在0.55门槛下为空，Lexical/Hybrid返回弱相关的机会成本、概率等资料。不能将“有Citation”当作这条问题已正确回答。`weak`只有“的”，Dense拒绝，但Lexical/Hybrid因常见汉字命中；RRF继承词法误召回，不能保证无答案拒答。Dense对另外两条原先仅在较后位命中的查询改善排序，另三条补回原Top5遗漏。

零泄漏只证明本轮作用域样本通过，不代替正式身份授权。标签较小且未经教师复核；尚需更多独立术语、跨段落、歧义查询与真实许可资料评测。默认保持lexical，未启用Rerank或ANN，不据此宣称Hybrid已经适合生产默认。

## 真实云端Agent闭环

[本轮原始合成验收](hybrid-retrieval-cloud-v1.json)与[SHA](hybrid-retrieval-cloud-v1.json.sha256)：2026-10-15 08:49:32—08:49:44 UTC（本地16:49），显式`RUN_HYBRID_CLOUD_E2E=1`和`HYBRID_SYNTHETIC_TRANSFER_AUTHORIZED=1`，使用本地已配Key，只发送合成问题与合成片段。

DeepSeek实际响应模型为`deepseek-flash`，Thinking disabled，输出上限768、每轮最多3次模型调用、每轮保守预留预算0.025美元、零自动重试。Embedding只在本地CUDA运行。两个场景总计3次真实模型请求，均协议有效；共4条工具尝试轨迹，其中3条实际执行检索，另一条在无命中终止后标为`run_already_terminated`且未执行。不能把它计为第四次成功检索。

有源场景：同义问法触发真实Hybrid检索，PG/S3校验通过，回答正确说明热传导与17分钟，Citation指向本作业。其他作业的紫色木星/999分钟未泄漏，资料内execute_sql指令没有进入固定工具注册表。随后撤销唯一有效资料，无命中场景由服务端返回证据不足，无引用、不再请求模型补答。两场景未写正式评分或人工复核。

实际用量4008输入/470输出token，其中1920缓存命中、2088未命中；三次模型调用耗时3302/2092/1081毫秒，两个Agent运行5557/1114毫秒（另计本地模型预热）。沿用项目Flash高峰估价：未缓存全部按输入0.30/输出1.20美元每百万token时上界0.0017664美元；按缓存输入0.006估算约0.00120192美元。仅为保守估算，不是账单。无模型调优、重试筛选或真实学生准确率结论。

## 自动化与后续

默认自动化使用Fake Embedding验证维度/非有限数、Key版本、幂等、并发重建、撤销竞争、故障降级、字节截断、S3/PG故障和Citation。另有显式本地模型测试与收费云端测试，各自独立报告，不混称所有默认测试都使用真实模型。真实MinerU与其他历史opt-in本轮未执行。

没有新增数据库Schema。独立空测试库升级至head `0fd471e9f785`并通过Alembic check后只删除本轮测试库，未升级开发库。README保留正式项目首页，不追加本轮数字。

P4-B2B可通过现有Retriever接口接收有界候选和来源元数据后增加Rerank。仍需预注册独立标签、Recall/MRR与无答案质量标准；排序器不能绕过Assignment、撤销、源SHA或Citation检查。

## 最终本地验证记录

`uv sync --locked`、`uv run ruff check .`、`uv run pytest -q`、`docker compose config --quiet`、`git diff --check`均通过。最终串行默认测试660 passed、13 skipped（233.13秒）；本轮新增32项默认测试，额外两个opt-in默认跳过。独立真实本地Embedding与真实DeepSeek混合闭环各1 passed，默认CI只用Fake模型；原有核心PG/S3未跳过。文档相对链接213项有效，86个历史评测文件与main逐字节相同，冻结输入与报告SHA有效。

早期两个测试进程共享专用测试库，发生fixture互相清理，相关中断结果未作为验收证据；随后全部串行重跑。没有修改开发数据库以解决测试问题。Compose使用desktop-linux原有PostgreSQL/SILO容器与卷，均healthy；临时在default创建的同名服务已停止，没有删除任何数据卷或重启其他服务。
