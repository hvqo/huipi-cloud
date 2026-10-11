# 可信评分输入准备

P3-A 提供一个本地 CLI，用于检查并保存后续文本评分可消费的输入包。它不调用评分模型，不生成分数，也没有 HTTP 写接口。

## 前置条件

目标作业必须已发布，且目标 Question 有教师录入的 AnswerKey 和完整 Rubric。学生提交还必须完成解析、Canonical 标准化和当前版本的 AnswerAlignment。最后，人工复核必须对该题的最新 revision 作出 `response_present`，并选择有效的 Canonical 来源范围。

`aligned` 只表示来源区域与 Question 的映射证据足够。它不能替代人工复核。`response_absent` 也不会转换成零分。先按[基本作业流程](basic-workflow.md)、[MinerU 与 Worker 指南](mineru.md)和[人工复核 CLI 指南](answer-review.md)完成前置操作。

## 检查输入状态

设置已知的 Submission 和 Question UUID：

```bash
export SUBMISSION_ID='00000000-0000-0000-0000-000000000001'
export QUESTION_ID='00000000-0000-0000-0000-000000000002'

uv run python -m huipi_cloud.workers.prepare_grade_input inspect \
  --submission-id "$SUBMISSION_ID" \
  --question-id "$QUESTION_ID"
```

`inspect` 不显示学生回答原文或完整标准答案。它会显示状态、固定原因码、题目与 Rubric 摘要、来源 artifact/hash、复核 revision、所选区域数、文本字符数和安全素材清单。素材清单不包含对象 Key、外部 URI 或存储凭据。

| 状态 | 含义 |
| --- | --- |
| `ready_text` | 当前来源、人工复核、AnswerKey 和 Rubric 都通过门禁；可以生成文本输入包。 |
| `review_required` | 没有复核，最新决定不是 `response_present`，或作业尚未发布。 |
| `source_stale` | Canonical、Alignment、Question 或复核记录与当前版本不一致，或来源范围无效。 |
| `unsupported_modality` | 已确认的必要作答内容不能安全表示为文本评分输入。 |
| `invalid_rubric` | AnswerKey 缺失/非教师录入，Rubric 缺失或分值总和不等于题目满分。 |
| `source_unavailable` | PostgreSQL、Canonical/Alignment 存储或其他必要来源暂不可读。 |
| `size_limit_exceeded` | 所选文本或最终 JSON 超出配置上限。 |

未准备就绪时，`inspect` 返回退出码 2 和固定原因码。请先修正来源或复核状态，再重新检查。

## 生成输入包

每次逻辑请求使用调用方生成的 UUID。相同请求 ID 和相同输入可以安全重放；相同 ID 对应不同输入时会冲突。

```bash
export REQUEST_ID='00000000-0000-0000-0000-000000000003'

uv run python -m huipi_cloud.workers.prepare_grade_input prepare \
  --submission-id "$SUBMISSION_ID" \
  --question-id "$QUESTION_ID" \
  --request-id "$REQUEST_ID"
```

成功时命令返回 `artifact_id`、`input_sha256`、`object_sha256` 和创建/重放状态，不输出私有 S3 Key、AnswerKey 原文、Rubric 描述或学生回答。完整的 `huipi.grading.input` 1.0 JSON 保存在私有 S3；PostgreSQL 保存来源、版本、哈希、大小和对象定位索引。

## 提取和模态边界

系统只读取最新人工决定中的 `response_regions`。它按完整 Canonical `content_pointer` 定位节点，再使用 Python Unicode 码点的半开范围 `[start, end)` 还原该节点文本。每个片段保留页码、Block ID、Canonical reading order、来源 pointer、字符范围和 SHA-256。不同来源位置即使文字相同也分别保留。`excluded_prompt_regions`、`uncertain_regions`、P2-D1 `text_projection` 和 VLM proposal 都不会作为学生答案输入。

普通文本与公式节点都先检查实际选中的 Canonical 字符范围。带 Canonical asset 引用、`canonical-inline-image:` 占位符、Markdown 图片语法、HTML `img`/`svg`、表格标签、嵌入媒体或 MathML 标签时，inspection 会返回 `unsupported_modality`，并保留节点 pointer、页码、Block、字符范围和固定原因码，例如 `canonical_asset_reference`、`canonical_inline_image_placeholder`、`markdown_image_reference`、`embedded_image_markup`、`embedded_table_markup`、`embedded_media_markup` 和 `structured_math_markup`。服务不会删除标记后只保留其余文本。纯文本与图片标记混合时，整段选择都被阻塞。普通 `x < 5`、`List<T>` 等内容不会仅因尖括号而被拒绝；系统也不会因为任意 HTML 标记就一概阻止文本。

不带素材引用、内容是文本/LaTeX 的公式节点可保留为独立 `formula_text` 片段。只有图片表达的公式、图片/几何、表格、图表、含图片的公式及其他不支持的必要模态会出现在 inspection 证据清单中，并阻止文本包生成。当前没有验证过的表格单元格表示或图像评分表达，因此不会把这些内容猜成普通答案文字。

包内同时快照教师 AnswerKey 与 Rubric Criterion（ID、Question ID、描述、Decimal 分值和顺序）。Rubric 合计必须严格等于题目满分；不使用浮点计算。快照用于复现原始输入，不表示答案或规则经过独立审核。

## 持久化和重新验证

服务先上传每次尝试使用不同 UUID 的不可变对象，再开始短 PostgreSQL 事务。事务按 Submission、Assignment 和 Question 的锁顺序重新核对发布状态、Question digest、Canonical/Alignment、最新 Review revision、原文件 SHA、AnswerKey 与 Rubric。数据库唯一约束保证同一 Submission、Question 和 request ID 只有一个有效索引。

确认事务在 COMMIT 前回滚后，服务会尽力删除该次对象。COMMIT 结果未知时会保留对象，并要求用同一个 request ID 重试。对象存储和 PostgreSQL 没有共享事务；进程崩溃、网络中断或取消同步 S3 上传仍可能留下孤立对象。当前没有自动对账清理任务。

消费者可调用 `huipi_cloud.modules.grading.service.read_grade_input_artifact()` 读取对象。该函数重新检查当前人工复核与评分输入摘要，然后校验对象字节数、SHA-256、协议和 PostgreSQL 索引。新复核修订会使旧包不能作为当前输入读取。P3-B 的 `grade_input_artifact()` 已在模型调用前、调用后和 PostgreSQL 登记事务内重复校验来源；详见[候选评分指南](grade-proposals.md)。

## 配置和隐私

- `GRADE_INPUT_MAX_TEXT_BYTES`：所选回答文本累计 UTF-8 字节上限，默认 1 MiB。
- `GRADE_INPUT_MAX_DOCUMENT_BYTES`：完整 JSON 输入包字节上限，默认 4 MiB。

这些限制用于控制评分输入包大小，不是进程级内存配额。该 CLI 没有认证或 RBAC，只能在本地受控环境中使用。学生文本、标准答案、Rubric 和输入对象都按敏感数据处理。

核心实现见[评分输入服务](../../src/huipi_cloud/modules/grading/service.py)、[评分输入协议](../../src/huipi_cloud/modules/grading/protocol.py)、[PostgreSQL 索引事务](../../src/huipi_cloud/modules/grading/repository.py)和[真实 PostgreSQL/MinIO 集成测试](../../tests/integration/test_grade_input.py)。
