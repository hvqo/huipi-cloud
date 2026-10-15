# Rerank独立留出评测与真实闭环

## 预注册与数据

[独立合成留出集](../../data/evaluation/retrieval/rerank-holdout-v1.json)：60查询，45有相关资料、15无答案/负例，30条来源版本。包含中英改写、定义与因果、公式文字化、相似干扰、同名跨作业、撤销/旧版、三Chunk显微镜资料及多片段问题。

数据SHA：`4d77966f2f0016ab8d2b4b6ca04e3c43f0a056d08b17478adb52ceec7ee04136`。标签为synthetic_curated_reference、human_review_status=unreviewed，不是合格教师双评。正例按可支持所问内容的具体Chunk标注，等级2为直接相关、1为相关补充。负例要求不能提供所请求的具体事实；主题相似不算能回答。这个定义不同于仅判断话题相关，仍需人工审核。

[预注册清单](../../data/evaluation/retrieval/rerank-experiment-v1.json)与数据先在提交8e6dc85冻结，初版评测实现abac072冻结后才进行模型实验。原48查询只作为开发资料，保留原SHA，未重新冒充独立Holdout。[回归清单](../../data/evaluation/retrieval/rerank-regressions-v1.json)区分旧问题与本轮新观察，旧speed/simile案例本轮未重跑。

冻结参数：Qwen3-Embedding-0.6B快照97b0c614…，Qwen3-Reranker-0.6B快照e61197ed…；1024维L2 Embedding、余弦0.55、每路最多20候选、RRF k=60、精排softmax yes/no门槛0.5、batch4、总输入1024 token、最终Top-K=1/3/5。没有看结果后修改标签、门槛、模型或指令。

预设检查包括：跨作用域/过期泄漏0、候选Recall@20至少95%、无答案误命中不高于Dense、首位Recall不低于Dense减2个百分点、Dense已正确首位保留至少95%、nDCG@5不低于Dense减0.02、P95不超过3000ms、观察到的本轮GPU用量不超过10240MiB、模型错误0。来源异常不能降级为成功，由故障测试验证。即使合成量化项全通过，也不允许自动切换默认或声称真实教师问答达标。

## 对照方式与指标定义

[完整逐案例报告](rerank-holdout-v1-results.json)及[SHA](rerank-holdout-v1-results.json.sha256)保留五组候选、原始精排分数、最终返回、错误和延迟。所有组使用同一PG/S3来源快照、稳定UUID5身份、候选20与输出预算16384字节；不变更资料内容。Rerank复用同一查询已经测得的候选快照，验证后重新排序；计时加回该快照的实际候选检索耗时，因此是可解释的组合时延，不是另一次重新召回的独立计时。Agent真实闭环另行验收。

Lexical/Dense/Hybrid基线也从内部20候选取Top5，内部Hybrid每路最多20；这与历史公开Hybrid每路5条的默认路径不同，不能直接将历史48查询指标当本次基线。原有公开检索行为仍保留。

Recall是每案例相关集合覆盖比例的宏平均，只统计45个可回答案例；多Chunk问题使Recall@1不等于首位命中案例率。Precision@K分母为45×请求K，返回不足K不缩分母。MRR统计首个相关名次；nDCG使用gain=2^grade-1及log2位置折扣。负例15条独立计算“是否返回任何Chunk”，不是生成答案是否幻觉。报告提供分子/分母和完整逐案例数据。

## 五组结果

- Lexical：Candidate Recall@10/@20均100%；Recall@1/3/5为68.89%/91.11%/95.56%；MRR 0.8100；nDCG@5 0.8463；Precision@1/3/5为71.11%/32.59%/20.44%；无答案误命中14/15；P50/P95为75.911/92.892ms。
- Dense：Candidate Recall@10/@20均92.22%；Recall@1/3/5为90.00%/92.22%/92.22%；MRR 0.9333；nDCG@5 0.9184；Precision@1/3/5为93.33%/32.59%/19.56%；无答案4/15；P50/P95见原始报告，P95为63.745ms。
- Hybrid：Candidate Recall@10/@20均100%；Recall@1/3/5为86.67%/95.56%/97.78%；MRR 0.9241；nDCG@5 0.9327；Precision@1/3/5为88.89%/34.07%/20.89%；无答案14/15；P95为227.483ms。
- Dense→Rerank：Candidate Recall仍92.22%；Recall@1/3/5为90.00%/92.22%/92.22%；MRR 0.9333；nDCG@5 0.9247；Precision@1/3/5为93.33%/32.59%/19.56%；无答案3/15；P95为224.564ms。
- Hybrid→Rerank：Candidate Recall仍100%；Recall@1/3/5为92.22%/95.56%/95.56%；MRR 0.9556；nDCG@5 0.9556；Precision@1/3/5为95.56%/34.07%/20.44%；无答案3/15；P95为1160.218ms。

五组跨Assignment和过期来源命中均0，精排模型错误0。不能把0泄漏样本统计替代正式教师身份或生产租户隔离。

## 改善、退化与阶段归因

Dense第一阶段完全漏掉pressure-concept、parallel-concept、refraction-concept；另有多Chunk问题只召回部分相关素材。Reranker不能找回这些候选，Dense→Rerank未达到95%候选覆盖准入，不能把漏召回都算成精排失败。

首位包含相关资料的案例数：Dense42/45、Dense→Rerank42/45、Hybrid40/45、Hybrid→Rerank43/45。相对Dense，Hybrid→Rerank新增parallel-concept且保留原42条正确首位；相对Hybrid也改善了其他首位排序，完整配对在报告中，不能只展示单个成功例。

但Hybrid→Rerank过滤掉pressure-concept、refraction-concept的正确Chunk，使Recall@5从Hybrid97.78%降到95.56%。它们在词法候选中存在，属于精排相关性假阴性，不是来源验证失败。Dense→Rerank没有首位新增改善，只减少一条负例误命中并改善部分分级排序；因此没有证据说精排对所有查询都更好。

两种精排仍在old-source、missing-pressure、unknown-code误召回：当前通知与旧编号话题接近、公式不能补出不存在数值、相似编号并不等于所问编号。old-source返回的是当前有效来源，不是旧版泄漏。相关性得分高不证明片段满足具体问题。负例中有主题相关但不提供所求事实的材料，标签口径需人工复核；不能直接称为三次生成幻觉。

Dense→Rerank的量化检查因候选覆盖未通过；Hybrid→Rerank的预注册量化项通过。教学语义审核仍unreviewed，默认保持Lexical，不据此宣布任何生产准确率。

## 本地模型与资源

实际运行使用缓存中完整固定快照，离线加载，无远程Rerank请求。Embedding索引含加载耗时5.322秒，精排模型加载4.491秒，30个当前Chunk的向量净负载122880字节。

两模型在RTX3060 12GB共同常驻、串行计算。逐查询后按本轮两个进程PID轮询，观测最大5452MiB。该值不是瞬时峰值或硬内存限制；没有与无关GPU进程混算，也没有跨CLI调度能力。

Dense→Rerank共处理67个候选（14个空候选查询），本地输入10021 token；Hybrid→Rerank共1115候选（1个空查询），157532 token。分别有7/1051个候选低于0.5，被明确过滤，不是模型错误。单候选组中位模型耗时38ms，Hybrid的20候选组中位733ms；候选文本和长度不同，不能据此推断线性吞吐。

[额外Provider资源观察](rerank-resource-probe-v1.json)只用独立合成短句，不是PG/S3闭环：首次单对调用435ms含CUDA内核/缓存热身，随后4对91ms、20对411ms；同时常驻进程观测2428/2452MiB。不能直接将冷单条与后续批次作性能优劣比较。CPU配置路径保留，但本轮未单独验收CPU性能。模型进程关闭、超时与取消由自动化测试和真实本地测试验证。

## 真实DeepSeek链路与专项测试异常

[原始真实观察](rerank-cloud-v1-observation.json)与[SHA](rerank-cloud-v1-observation.json.sha256)未修改：2026-10-15 12:19:10—12:19:27 UTC（本地20:19），显式授权合成数据外传。模型实际返回deepseek-flash，Thinking disabled，输出上限768，每轮最多3次模型调用、预算0.025美元、零自动重试。

正向链路调用真实Hybrid→本地Rerank→PG/S3来源验证，回答渗透作用、银色雪松、23分钟，并引用同作业、同版本、同SHA素材。其他作业红色鲸鱼/999分钟未出现；恶意资料没有增加execute_sql等工具。无答案查询与资料撤销后均由服务端返回证据不足。

共4次真实模型请求、5条工具尝试，其中实际执行4次：3次检索、1次合法作业概要；另一条在终止后被run_already_terminated拒绝。4736输入/606输出token（缓存命中3328、未命中1408）；四次模型耗时1004/2301/1056/1204ms，三个Agent运行3924/1102/1253ms，另计本地预热。按项目高峰价格，全部未缓存保守估算0.002148美元；考虑返回缓存用量约0.001169568美元，均非账单。

首次付费pytest为1 failed，原因是测试错误要求所有工具只能是检索，模型还合法调用了固定白名单中的get_assignment_summary。修正后离线复核又发现字符串`999`会误命中合法相关性数值`0.99999`，已改为核对外国标本与具体999分钟事实，并补注入这两类真实泄漏文本必失败的回归。没有修改Provider、权限、模型回答或原始记录来迎合测试。

修正后的白名单、来源回执、事实、精排模式、无命中和撤销断言对原4次请求记录离线重放通过。没有为重复证明同一链路新增收费请求，也不把这次重放称为重新付费运行的pytest通过；最初专项失败记录保留。真实本地双模型测试为1 passed。原有MinerU、其他模型opt-in本轮未运行。

## 工程验收与后续

默认CI使用Fake Embedding/Reranker并运行真实PostgreSQL/S3；源码守卫、候选预算、状态变化、损坏源、模型错误、共享门禁、取消、两级降级及正式评分/人工复核记录不变均有测试。新模式未修改GradeInput、GradeProposal或GradeReview协议，没有新Schema或迁移。

保持README项目首页定位。后续优先审核标签口径、Dense候选漏召回和相关性假阴性、具体编号/数值的证据充分性，再开展更大且真正独立的许可资料评测。已观察本Holdout后，不能用它继续调参并声称未见数据；所有参考与预测冻结保留。正式身份/RBAC、ANN及硬GPU资源调度未实现，本轮不提前开发。

## 最终本地质量检查

`uv sync --locked`、Ruff、完整pytest、Compose config与diff检查通过：694 passed、15 skipped（252.99秒），核心PG/S3没有跳过。新增32项默认测试，真实双模型opt-in为1 passed；付费opt-in首次1 failed的测试假设问题及无收费重放已在上文单列。独立空库upgrade head与alembic check通过，head仍为0fd471e9f785，开发库未升级。历史数据/报告原样保留，README、用户.env及已有模型快照未修改，无新增应用依赖或迁移。
