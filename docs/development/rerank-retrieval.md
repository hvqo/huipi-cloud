# 本地Rerank与两阶段检索

先阅读[知识材料](scoped-retrieval.md)、[Embedding/Hybrid](hybrid-retrieval.md)和[Copilot](teacher-copilot.md)指南。这里只支持受控操作员CLI/Service，没有教师认证、RBAC或匿名对话HTTP接口。Assignment参数是数据范围，不是授权凭据。

## 模型与环境

采用Qwen/Qwen3-Reranker-0.6B，固定官方快照`e61197ed45024b0ed8a2d74b80b4d909f1255473`，Apache-2.0。与Embedding使用独立Provider和独立子进程，可使用同一隔离Python解释器（本轮Torch2.14.0+cu130、Transformers4.57.6）。主项目`uv sync --locked`不安装Torch或下载权重；默认CI只用Fake模型。

模型使用官方query/document模板，读取末位yes/no logits并做softmax。虽然Transformers类名是AutoModelForCausalLM，本代码从不调用generate或生成自然语言，只执行相关性分类。分数未校准，不是事实正确率、答案可用概率或学生成绩。生成、规划、Tool Calling仍只调用云端DeepSeek。

本地离线加载，`local_files_only=True`且禁止远程代码。模型原生32K；本适配器限制模板+query+document最多1024 token、每批4对，超限拒绝，不截断来源。单次候选最多20，每条仍是原有最多400 Unicode码点的Chunk。CPU只在显式配置时启用，不自动转CPU或远程Rerank服务。本轮真实验收使用CUDA；CPU性能未单独验收。

## 配置与CLI

不要覆盖已有`.env`或复制API Key。可在当前终端显式设置非敏感运行项：

```bash
export RETRIEVAL_MODE=hybrid_rerank  # 也支持dense_rerank；默认仍为lexical
export EMBEDDING_PYTHON="$HOME/.cache/huipi-cloud/embedding-runtime/bin/python"
export EMBEDDING_MODEL_PATH="$HOME/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B/snapshots/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
export EMBEDDING_DEVICE=cuda
export RERANK_PYTHON="$EMBEDDING_PYTHON"
export RERANK_MODEL_PATH="$HOME/.cache/huggingface/hub/models--Qwen--Qwen3-Reranker-0.6B/snapshots/e61197ed45024b0ed8a2d74b80b4d909f1255473"
export RERANK_DEVICE=cuda
export RERANK_CANDIDATE_LIMIT=20
export RERANK_MIN_SCORE=0.5
export RERANK_ALLOW_FALLBACK=false
uv run python -m huipi_cloud.modules.knowledge search \
  --assignment-id "$ASSIGNMENT_ID" --query '本作业资料如何解释渗透作用？' --top-k 3
uv run python -m huipi_cloud.modules.copilot \
  --assignment-id "$ASSIGNMENT_ID" --require-material-evidence \
  --question '请检索本作业资料，解释渗透作用并引用来源。'
```

准备解释器与DeepSeek外传授权按原有指南进行；如果显式复用评分Key，沿用`--use-grading-key`。所有资料必须事先确认允许使用及外传。代码不自动索引学生作业、私人答案、评分或复核记录，也不枚举整个桶。

CLI先在付费调用与工具短超时前预热模型/索引。直接Service调用者应先`prepare(assignment_id)`，结束时`aclose()`。资料更新后大量索引重建仍可能超过工具期限，需操作员提前预热；不会自动放宽Agent总预算。

## 内部候选与最终结果

`retrieve_candidates(assignment_id, query, candidate_limit)`是内部接口，最多20条/64KiB。PG先过滤Assignment、active版本和许可，保留S3原文SHA、Chunk SHA、版本与Unicode范围验证。不能全局召回后才过滤。Dense与Lexical独立取候选；内部Hybrid每路最多20条，再按Chunk ID去重/RRF融合取20。原有公开Hybrid的每路5条路径保留，避免默默改变已用行为。

`RerankRetriever`支持Dense→Rerank和Hybrid→Rerank。调用本地模型前验证候选唯一身份及来源，精排后、最终回答前重新验证。来源在模型运行中被撤销会失败；重排序不改变引用身份或范围。最高返回5条，仍受工具字节上限限制。内部20条不全部发送给DeepSeek。

同分时按稳定Chunk ID排序；不能用模型输出的任意ID替换候选。模型返回NaN/Infinity、缺失/重复身份、错误模型版本或数量不一致均拒绝。低于配置相关性门槛的候选不返回；空结果由Agent服务端拒答，不再让模型凭空补答。阈值0.5为冻结实验设置，尚未经过真实教师校准。

## 故障、轨迹与资源

默认精排故障直接失败。显式`RERANK_ALLOW_FALLBACK=true`时，仅Reranker已分类故障可恢复原始召回排序，返回requested_mode、actual_mode/retrieval_mode、rerank_degraded及原因。Embedding故障信息独立保留：例如两级都失败时实际为lexical，两个原因都可见；Embedding失败但精排成功时实际为lexical_rerank。PG、权限、S3或SHA错误不允许降级掩盖。

轨迹记录候选数量、模型配置摘要、精排耗时/Token和降级状态，不记录完整工具正文、私有S3 Key或密钥。工具仍是固定白名单，模型不能增加SQL、URL、工具或Assignment。正文内提示注入不能授予新权限；这不保证模型绝不会在语言层面被误导。

Embedding和Rerank进程共享控制器内Semaphore，串行加载/计算、允许共同常驻；不是跨进程GPU调度。建议一张GPU只运行一个Copilot实例。其他CLI或软件仍可能占用显存，代码没有硬显存上限。OOM、超时或取消会终止对应进程组，最多等待回收3秒；操作系统不可中断I/O仍可能延迟实际回收。未捕获的程序错误不能被伪装为成功降级。

本轮两模型在RTX3060 12GB共同运行的查询后轮询显存最大5452MiB，不能解释成瞬时峰值硬保证。真实效果、单条/批量时延和限制见[评测](../evaluation/rerank-retrieval.md)。

## 验证

```bash
uv run python scripts/evaluate_rerank_retrieval.py  # 校验冻结输入，无模型或收费调用
uv run pytest -q tests/unit/test_reranking.py tests/integration/test_rerank_retrieval.py
RUN_LOCAL_RERANK=1 uv run pytest -q -m rerank_e2e
RUN_LOCAL_RERANK=1 uv run python scripts/evaluate_rerank_retrieval.py \
  --run-local --output /tmp/new-rerank-report.json
# 收费，必须仅用合成数据并显式授权：
RUN_RERANK_CLOUD_E2E=1 RERANK_SYNTHETIC_TRANSFER_AUTHORIZED=1 \
  uv run pytest -q -m rerank_cloud_e2e
```

评测只连接独立`_test`库与独立本地测试桶，固定合成作用域不可并行复用；已有作用域时拒绝，不能删除别的运行数据。结束仅清理本轮已提交的Fixture。没有新增数据库Schema或迁移，不自动升级开发库。
