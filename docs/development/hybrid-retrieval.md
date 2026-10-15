# 本地Embedding与混合检索

先阅读[教师材料指南](scoped-retrieval.md)与[Copilot指南](teacher-copilot.md)。本功能仅用于受控本地CLI，没有认证/RBAC。Assignment是数据作用域，不是可信教师授权。默认仍为lexical，新模式需显式选择。

## 运行条件

主项目继续`uv sync --locked`，不安装Torch/Transformers，不下载权重。Embedding运行在独立Python环境；默认CI只用Fake Embedding，真实PG/S3。可按以下方式准备隔离环境（模型需操作员另外下载指定快照）：

```bash
uv venv --python 3.12 "$HOME/.cache/huipi-cloud/embedding-runtime"
uv pip install --python "$HOME/.cache/huipi-cloud/embedding-runtime/bin/python" \
  'torch>=2.6,<3' 'transformers>=4.51,<5'
```

选择与设备兼容的官方Torch构建；GPU需可用CUDA，CPU运行必须显式设`cpu`。本轮实测Torch2.14.0+cu130、Transformers4.57.6、RTX3060 12GB；不同运行库/设备可能产生数值差异，升级应重新构建并评测。代码只加载本地快照、`trust_remote_code=False`，禁止Hub自动下载。Python与模型路径均来自受控操作员配置，不接受模型工具参数。

在自己的本地环境设置这些非敏感项，不要覆盖已有`.env`：

```bash
export RETRIEVAL_MODE=hybrid  # lexical / dense / hybrid
export EMBEDDING_PYTHON="$HOME/.cache/huipi-cloud/embedding-runtime/bin/python"
export EMBEDDING_MODEL_PATH="$HOME/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B/snapshots/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
export EMBEDDING_DEVICE=cuda
export EMBEDDING_MIN_COSINE=0.55
export RETRIEVAL_ALLOW_LEXICAL_FALLBACK=true
```

主要模型为Qwen/Qwen3-Embedding-0.6B（Apache-2.0）；仅做特征提取，不进行生成、规划或评分。query使用固定英文检索指令，document不加指令；last-token pooling、float32、L2归一化、1024维。原生32K上下文，本服务上限512 token，每批最多8条、每条最多1000字符；超长拒绝而非静默截断。每个资料Chunk最多400 Unicode码点，token超限仍会安全失败。

## 使用

沿用原有导入、撤销和版本命令，不自动索引现有桶、学生作业或评分产物。

```bash
uv run python -m huipi_cloud.modules.knowledge search \
  --assignment-id "$ASSIGNMENT_ID" --query '物体直接接触时如何传热？' --top-k 3
uv run python -m huipi_cloud.modules.copilot \
  --assignment-id "$ASSIGNMENT_ID" --require-material-evidence \
  --question '请根据本作业资料解释接触传热并引用来源。'
```

CopilotCLI先在付费模型调用前预热本地索引；Agent工具仍有原来的短超时和总预算。直接Service调用者可先`await retriever.prepare(assignment_id)`，结束后`await retriever.aclose()`。主配置不会静默复用云端Embedding或本地生成模型。DeepSeek Key/外传许可仍按原有Copilot指南显式配置。

## 索引与失败

索引是进程内有界缓存，总计最多4096 Chunk；满时LRU淘汰。1024维float32向量净负载上限16MiB，Python元数据、构建期间的临时数组与模型显存另计。没有持久化向量表，没有跨数据库Exactly Once。重启后缓存为空；当前来源版本新建、文本SHA变化或模型配置摘要变化将生成新Key。缓存Key包括Assignment、Chunk、模型ID/快照/维度/配置摘要、Chunk SHA、source_version。

默认查询按需重建缺失向量；`auto_index=False`可要求显式prepare，否则返回`embedding_index_not_ready`。构建先读取当前许可/active来源并验证S3和字符范围，批次全部成功且数据库版本重新检查通过后才发布缓存。取消、OOM、超时、模型缺失、维度或非有限数错误不会写知识表。状态包括not_built/not_ready/ready；返回值区分rebuilt/ready。旧缓存即使尚未淘汰，也只能按当前PG行身份取用。

独立Embedding进程每实例串行处理，一次请求默认30秒、启动60秒；超时或取消发送进程组SIGKILL，等待回收最多3秒。没有阻塞线程被误称为可强制取消。操作系统不可中断I/O仍可能延迟实际回收；没有硬内存/显存隔离。不要同时启动多个大模型CLI占满同一GPU，建议每张卡仅一个Embedding进程。OOM有安全错误码，禁止自动远程回退。

hybrid只对明确的Embedding故障降级lexical，返回`requested_mode=hybrid`、实际`retrieval_mode=lexical`、`degraded=true`和稳定原因码；轨迹也记录实际模式。预热失败不会在同轮工具调用再次启动模型，需显式prepare恢复。源SHA不符、S3/PG故障或版本竞争不以降级掩盖。dense模式故障直接失败。

## 检索与来源合同

Dense在PG预先限定作用域/active/许可后的行上进行精确余弦搜索，不是全局Top-K后过滤，也不是ANN。阈值0.55是冻结实验配置，未完成生产校准；返回分数为`(cosine+1)/2`，不是置信度。Hybrid采用RRF：每分支最多5条、从1开始计rank，`score=sum(1/(60+rank))`，按Chunk ID去重，分数相同按ID排序。

词法与Dense分支共享PG/S3来源验证；合并后、最终回答前再次验证实际引用的版本、Unicode范围和SHA。跨分支版本变化则拒绝，不能混用。输出字节上限与工具预算一致，任何上游截断都会保留truncated。Citation协议不变，模型无法选择Assignment、SQL、URL或扩大权限。资料正文仍是不可信输入。

RRF可能继承词法误召回，不会自动拒绝所有无答案查询。引用合法仅证明身份与来源，不证明回答语义正确。效果与残余失败见[评测记录](../evaluation/hybrid-retrieval.md)。

## 验证

```bash
uv run python scripts/evaluate_hybrid_retrieval.py  # 只校验冻结数据，不启动模型
uv run pytest -q tests/unit/test_embedding_retrieval.py tests/integration/test_hybrid_retrieval.py
RUN_LOCAL_EMBEDDING=1 uv run pytest -q -m embedding_e2e
RUN_LOCAL_EMBEDDING=1 uv run python scripts/evaluate_hybrid_retrieval.py \
  --run-local --output /tmp/new-hybrid-report.json
# 会收费；仅合成文本、显式外传授权，默认CI不运行：
RUN_HYBRID_CLOUD_E2E=1 HYBRID_SYNTHETIC_TRANSFER_AUTHORIZED=1 \
  uv run pytest -q -m hybrid_cloud_e2e
```

评测脚本只使用不同于开发库且以`_test`结尾的测试PG、本地独立测试桶和本次UUID作用域，结束仅删除自身资料与对象。历史报告不可覆盖。没有新增迁移，不自动升级开发库。可选精排见[Rerank指南](rerank-retrieval.md)，不得替代当前作用域/许可与Citation验证。
