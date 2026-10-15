# 本地Embedding与混合检索面试复盘

## 1. 为什么本地Embedding与云端生成分开？

简洁解释：Embedding把文本变为定长向量。生成模型组织回答。两者的输入、成本和权限不同。

30秒：本地Qwen3-Embedding只做1024维特征提取。DeepSeek仍负责规划、工具调用与回答。默认不向第三方Embedding服务发送资料，也不拿本地小模型替代业务生成。模型快照、归一化与query指令进入摘要，升级后不能复用旧Key。

2分钟：主环境不安装Torch，适配器启动独立解释器、仅加载指定本地快照。document不带指令，query带固定检索任务指令；last-token pooling后做L2归一化，余弦可用点积计算。维度、NaN/Infinity、零向量、batch/token上限都在服务端检查。独立进程降低同步推理阻塞事件循环的风险；超时杀进程组，不假称asyncio能中断线程。没有硬GPU隔离，所以仍要限制同机进程数量。模型输出相似度不等于教学评分或语义正确性。

追问：为什么不自动CPU回退？CPU时间与预算不同，应由操作员显式配置。为什么不能只记录模型名？同名模型更新、维度或指令变化都可能改变空间。

代码：`infrastructure/embedding/{protocol,local,worker}.py`。验证：`test_vectors_reject_wrong_dimension_non_finite_and_zero`、`test_embedding_timeout_and_cancellation_kill_owned_subprocess`。

## 2. 为什么暂不用向量数据库？

简洁解释：当前每作用域最多4096 Chunk。精确内存搜索有明确上限，且可以重建。

30秒：采用float32有界LRU缓存，不修改现有PG镜像。pgvector需要扩展迁移，Qdrant增加一个服务。当前没有规模证据要求它们；内存基线可以先验证模型收益。它不是大规模ANN，重启后需要重建。

2分钟：PG先过滤Assignment、active版本和许可，再取当前Chunk。每条向量Key绑定Chunk SHA、source_version、模型配置；索引全部完成且版本重新检查后一次发布。构建失败不修改知识资料表，没有向量库写入成功而PG元数据提交失败的问题。缓存丢失只是可用性影响，不能扩大权限。1024维净向量负载4096×1024×4字节，另计模型、Python和临时构建内存。未来迁移存储仍复用Retriever与来源合同，不能把向量Payload当成授权真相。

追问：旧缓存怎么办？可以保留到淘汰，但当前PG行Key不匹配就不可使用。索引未就绪？明确重建或失败，不伪称检索成功。

代码：`knowledge/vector_index.py`、`DenseRetriever._ensure()`。验证：版本、撤销、并发撤销、缺失索引测试。

## 3. RRF为什么不直接相加两种相似度？

简洁解释：两个分支分数含义不同。RRF只融合名次。

30秒：词法分数是查询词项覆盖，Dense是余弦，两者量纲不同。每条候选在每分支贡献1/(60+rank)，按Chunk去重并稳定排序。分数只是排序信号，不能叫置信概率。

2分钟：每个分支先在同一作用域内返回有界候选；RRF不读标准答案或评分记录。融合后重新验证实际来源版本和SHA，避免两分支之间发生撤销还被引用。单路Embedding故障可明确返回lexical降级；不能隐藏源损坏。RRF继承各分支误召回，例如仅含常用汉字的查询仍可能误命中，不能只看Recall提升。需要比较Precision、无答案误命中率和逐案例退化，阈值也不能看结果后调到刚好好看。

追问：需要Rerank吗？未来可在候选上加独立排序，但必须保留引用和权限检查。Dense成功能绕过S3吗？不能。

代码：`knowledge/hybrid.py:rrf`、`HybridRetriever.search`。验证：手算RRF、输出预算、故障降级与Citation伪造测试。

## 4. 如何解释合成评测？

简洁解释：固定输入和标签，分别记录召回、排序、无答案与延迟。合成成绩不代表真实教学质量。

30秒：本轮先冻结48个Query和资料版本、标签及SHA，再执行真实本地模型。43个有答案、5个无答案；评测用真实测试PG/S3。只检查来源和排序，标签未经教师双评，因此不能宣称教师场景准确率已验证。

2分钟：A/B/C统一Top-K、模型快照与索引，报告Recall@1/3/5、MRR、Precision@K、跨作业泄漏和过期命中。无答案查询单独统计，不把它们当成Recall分母。延迟包括来源验证，预热索引另列。保留每个案例返回的Chunk标签、分数与未命中原因，所有历史文件有SHA且不覆盖。真实DeepSeek端到端另列，只能证明选工具、取源和引用闭环，不证明回答整体语义正确。

追问：为什么Hybrid不自动默认？仍有弱相关误召回和独立标签不足。为什么不能通过调标签提高分数？会破坏比较的可信性。

代码：`knowledge/evaluation.py`、`scripts/evaluate_hybrid_retrieval.py`。验证：数据SHA、手算指标分母、真实本地opt-in与云端opt-in测试。
