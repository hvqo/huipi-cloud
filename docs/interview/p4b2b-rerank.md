# 两阶段检索与Rerank面试复盘

## 1. Embedding和Reranker解决什么不同问题？

简洁解释：Embedding独立编码文本，适合先召回。Reranker同时看查询和候选，适合对小集合精排。

30秒：我们先在作业范围内最多召回20条，再让本地Qwen3-Reranker判断每一对query/document相关性，最后最多返回5条。Embedding快但可能漏同义表达，Rerank不能找回根本没有被召回的Chunk。DeepSeek只读取最终有来源的片段。

2分钟：Dense使用归一化向量余弦，Lexical使用词项覆盖，Hybrid用RRF合并名次。Reranker使用官方模板及末位yes/no logits，不生成自然语言。两阶段必须分开测Candidate Recall和最终MRR/nDCG，否则会把第一阶段缺失误算成精排失败。当前模型分数只用于相关性排序和过滤，不是事实概率、学生得分或答复正确率。原公开top_k仍最多5，内部CandidateResult允许20，预算与权限不随候选增加而放开。

追问：为什么不能只精排5条？候选集太小会限制提升。为什么不直接全库CrossEncoder？延迟与GPU成本不合适，且仍必须限定作用域。

代码：`knowledge/retriever.py:retrieve_candidates`、`knowledge/rerank.py:RerankRetriever`。测试：内部20条提升第16候选、原Top5上限、重复Candidate拒绝。

## 2. 同分排序和评测身份为什么重要？

简洁解释：固定输入还不够。固定来源身份才能稳定打破并列。

30秒：生产按稳定Chunk ID打破同分。评测若每次随机生成作业UUID，派生Chunk也变化，指标可能因同分排序漂移。我们用数据SHA与作用域别名推导固定Fixture，冻结标签、模型和准入门槛，再比较五组结果。

2分钟：留出集有60查询，含15负例及多个相关Chunk。Recall按每题相关集合的比例计算，不能把多Chunk的Recall@1直接叫首位正确案例率；MRR看首个相关位置，nDCG使用分级相关性与位置折扣。Candidate Recall在精排前统计。日志完整保留无答案误召回、漏召回、退化与模型错误，不通过改标签提高数字。已用来分析的集合后续只能作为开发回归，不再叫全新Holdout。

追问：没有教师双评怎么办？标记synthetic/unreviewed，不能当真实教学质量。指标不同是否必然是模型随机？先查身份、参数、过滤和分母。

代码：`knowledge/evaluation.py:evaluation_scope_ids`、`rerank_evaluation.py`、评测脚本。验证：SHA、手算两相关Chunk的Recall/nDCG及原始报告。

## 3. 如何保证重排序不突破权限？

简洁解释：模型只能排序服务端给定候选。权限和来源由程序检查。

30秒：PG先限定Assignment、active版本和许可，候选进入Reranker前验证S3 SHA及字符范围，返回和引用回答前再次验证。模型不能传SQL、URL或新作用域；资料内恶意指令只是数据。引用合法不代表回答语义正确。

2分钟：RerankReply必须覆盖且只覆盖本次Candidate ID，缺失/重复/非有限分数或模型版本不符都拒绝。模型只产生分数，不更改Chunk文本、source_version或引用范围。精排期间发生撤销，后置验证返回citation_source_stale；不能当普通模型故障降级。相同短文本来自不同资料，身份也不能合并。读取采用已有只读事务，Rerank不改Rubric、候选分数或人工复核记录。

追问：S3坏了但Dense有缓存能返回吗？不行，缓存不是来源真相。为什么模型白名单不能由Prompt控制？模型文本不等于授权，必须服务端强制注册与参数校验。

代码：`RerankRetriever.rank_candidates`、`validate_reply`、`BusinessTools`。验证：撤销竞争、S3损坏、跨作用域、PG不可用及真实评分记录不变测试。

## 4. 两模型怎样共享GPU并处理故障？

简洁解释：各自独立进程，共享串行计算门禁。故障显式记录，不假装精排成功。

30秒：Embedding和Rerank可共同常驻，但同一控制器用Semaphore串行运行。每次批量和Token都有上限；超时/取消杀进程组并有界等待。它不是跨CLI调度器或显存硬配额，所以部署仍限制一张卡一个实例。

2分钟：精排失败默认报错，显式开启才恢复原始排序。Embedding失败、Rerank失败分别记录，实际模式可能是lexical、hybrid或lexical_rerank，不能只写hybrid_rerank成功。来源异常不能降级掩盖。我们观察到两模型共用GPU约5.3GiB，但采样不保证捕获瞬时峰值。OS不可中断I/O也可能延迟进程回收，Python不能保证立即释放任意底层资源。真实云端调用与Mock测试分开，默认CI没有权重和付费Key。

追问：为什么不用to_thread？取消等待不会强制结束同步推理。为什么不把两个模型放同一Provider？它们的输入输出、生命周期和故障合同不同。

代码：`infrastructure/{embedding,reranking}/local.py`、`knowledge/dependencies.py`。验证：共享门禁、超时、主动取消和显式两级降级测试。
