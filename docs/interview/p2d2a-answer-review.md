# P2-D2A 面试复盘：学生作答证据与人工复核

> 本文描述当前功能分支中的实现。当前没有自动作答检测器、VLM、自动评分、教师账号或 RBAC。13 个标注范围全部为合成数据，不代表真实教学作业准确率。

## 1. 为什么 `aligned` 不代表学生已经作答？

### ASD-STE100 简明解释

`aligned` 表示来源区域与某个 Question 的映射通过当前规则。它不表示区域中有学生作答。区域也可能只有印刷题干。

### 底层原理

P2-D1 的 `matching_status` 描述来源与作业题目的对应关系。P2-D2A 的 `decision` 单独描述人工确认的作答存在性。`unreviewed` 是没有复核记录，不是 `response_absent`。

### 30 秒面试回答

`aligned` 只说明算法把某个 Canonical 来源区域关联到真实 Question。它不能判断文字来自印刷题干还是学生笔迹，也不能证明 OCR 完整。因此我保留 P2-D1 的 alignment status，并新增独立的人工作答决定。没有人工记录时就是未复核，不能按未作答处理；即使人工确认有作答，也没有表示可以自动批改的状态。

### 2 分钟深入回答

题号识别和作答存在性是两个不同的问题。P2-D1 根据 Canonical 页面和题号候选建立来源到 Question 的映射，输出 `aligned`、`review_required` 或 `not_observed`。当扫描件只显示印刷题干时，算法仍可能正确识别题号并给出 `aligned`。因此把 `aligned` 当作“学生已作答”会产生业务误判。

P2-D2A 用独立的 `AnswerReviewDecision` 保存 `response_present`、`response_absent` 或 `uncertain`，并在每条记录中保存原始 `alignment_status`。没有一行决定表示 `unreviewed`。系统目前没有 `ready_for_grading`，也不允许通过人工的 presence decision 自动进入批改。

### 连续追问

- `not_observed` 能否证明学生没有作答？不能，它只表示没有安全的自动 Question 关联。
- 如果 OCR 只识别到题干，能否用题干关键词推断空白？不能，代码没有这种启发式。
- `response_present` 能否进入评分？不能，评分就绪概念尚未实现。

### 代码与测试

- `src/huipi_cloud/modules/answer_alignment/protocol.py::AlignedAnswer`
- `src/huipi_cloud/modules/answer_review/protocol.py::AnswerReviewDecisionRead`
- `tests/unit/test_answer_review_protocol.py::test_presence_decisions_are_distinct_from_unreviewed`
- 演示：读取合成基线的 `synthetic-printed-prompt-only` 与 `synthetic-unreviewed` 两例，比较 `response_absent` 和空决定。

## 2. 印刷题干与学生手写作答有什么区别？

### ASD-STE100 简明解释

OCR 文本本身不说明文字是谁写的。Canonical 可以提供页面、节点和素材引用，但当前系统没有可靠的笔迹分类器。

### 底层原理

打印字体和手写笔迹可能都被转换成文本。位置、图像、文字样式可能提供线索，但本轮没有实现这类模型识别。人工复核可以分别标注印刷区域、学生回应和不确定区域。

### 30 秒面试回答

我们不能因为 Canonical 有文本就认为这是学生的答案。OCR 把内容转成文字后，文本可能是题干、页眉或手写作答。当前没有可靠的笔迹来源分类器，所以本轮由人工在真实 Canonical 节点上选择印刷题干、学生作答或不确定区域，并把决定和对齐状态分开保存。

### 2 分钟深入回答

文本抽取和作者归属是两个任务。MinerU 的 MiddleJson 和 Canonical 结构保留了文本、公式、表格、图片、Block、页码、BBox 和素材引用，但不提供“这个字由学生手写”的可信标签。扫描 PDF 中的印刷内容可能与笔迹同在一个 Block；OCR 也可能遗漏笔画或把图形解释成文字。

因此人工协议允许在同一节点上选择不同的半开字符范围，分别标记印刷题干和作答；对于图像节点可使用整节点引用。来源无法判断时记录 `uncertain`，不采用“答案”“解”等文字关键词直接断定作答。未来可能用经过验证的视觉模型提供候选，但需要人工标注和真实扫描件评估。

### 连续追问

- 为什么不基于字体或位置直接分类？当前没有被验证的分类规则。
- 手写与印刷内容混排怎么办？用不重叠区域标注；无法稳定切分时选 uncertain。
- 纯文本合成集能验证笔迹区分吗？不能。

### 代码与测试

- `src/huipi_cloud/modules/answer_review/annotations.py::SyntheticAnnotationRegion`
- `tests/fixtures/answer_presence_synthetic_v1.json::synthetic-prompt-and-steps`
- `tests/fixtures/answer_presence_synthetic_v1.json::synthetic-geometry-marks`
- 演示：查看两种标签都指向 source node，且范围不重叠；这只验证协议。

## 3. 为什么不能只用 OCR 文本判断是否作答？

### ASD-STE100 简明解释

OCR 可能漏掉、误读或合并内容。它也不能单独区分印刷题干、学生笔迹、公式、表格和图形。

### 底层原理

Canonical 包含带有类型和位置的内容节点。只读 `text_projection` 会丢失非文本内容和具体来源身份。当前规则保留这些证据，但不会声称它们足以自动确认作答。

### 30 秒面试回答

OCR 是来源内容的一种识别结果，不是学生已经作答的业务结论。它可能漏掉手写内容，也可能把印刷题干作为答案文本返回。公式、表格和图形还可能没有可用文本。因此复核记录引用 Canonical 节点和偏移，而不是单独依据拼接文本或关键词判断。

### 2 分钟深入回答

仅用 OCR 文本会造成两类错误：文本里有题干但没有学生作答时产生假阳性；学生只画图、填写表格或写公式时又可能产生假阴性。OCR 也可能把不同内容块拼在一起，失去页码、Block 和字符偏移。

当前 `record_decision` 先加载并校验当前 Canonical 和 Answer Alignment JSON 的 SHA-256，再通过 JSON Pointer 找到真实 `CanonicalContentNode`。文本范围按 Python Unicode 码点检查；图片或空布局节点只有在有资产证据时才能整节点引用。这里实现的是可追溯人工协议，不是自动理解图像或保证 OCR 结果正确。

### 连续追问

- 是否把 `text_projection` 写进复核表？没有，复核记录存引用和范围，不复制源文本。
- OCR 错了以后旧标签会自动改吗？不会；新源版本要重新复核。
- 是否会读取 AnswerKey？不会。

### 代码与测试

- `src/huipi_cloud/modules/answer_review/service.py::record_decision`
- `src/huipi_cloud/modules/answer_review/service.py::_materialize_regions`
- `tests/integration/test_answer_review.py::test_invalid_pointer_and_unicode_range_fail_before_insert`

## 4. 如何保存人工复核结果？

### ASD-STE100 简明解释

PostgreSQL 保存决定、题目身份、来源版本、所选区域和时间。区域引用不可变 Canonical，不复制作业正文，也不写回 P2-D1 结果。

### 底层原理

`answer_review_decisions` 保存 Canonical ID/SHA、Alignment ID/SHA、aligner 版本、Question 集合摘要、revision 和来源 JSON。Canonical 与 Alignment 在私有 S3 中由其现有模块管理；人工复核记录不写 S3。

### 30 秒面试回答

人工决定单独存入 PostgreSQL，每一条都绑定当时的 Canonical 和 Answer Alignment 哈希、Question 集合摘要与 schema 版本。区域只存 JSON Pointer、偏移和从 Canonical 校验后复制的页码、Block、BBox、类型及安全资产元数据。这样能回溯人工依据，同时不会修改自动对齐结果或重复保存学生全文。

### 2 分钟深入回答

服务首先从现有 Answer Alignment 模块取得并验证最新 Canonical 和 alignment 文档。它检查 S3 内容摘要与数据库索引一致、Question 属于该 Submission 的 Assignment、pointer 存在、字符范围不越界。之后 PostgreSQL repository 在事务中再次检查 source version，创建 append-only 的决定记录。

这轮没有为人工复核创建额外 S3 JSON，因为保存的数据量很小，且区域和来源可以直接用不可变 Canonical 的指针重建。原始 Canonical 和自动 Alignment 继续由原模块以私有 S3 保存。若读取 S3 失败，数据库写入不会发生；如果 PostgreSQL 返回结果不确定，操作者重用相同 request_id，服务通过幂等摘要返回已经提交的记录。

### 连续追问

- 为什么不把 Canonical 文本复制进记录？避免重复和保存错误版本。
- 复核记录会上传 S3 吗？不会。
- COMMIT 回应丢失怎么办？复用相同 request_id 重试。

### 代码与测试

- `src/huipi_cloud/modules/answer_review/models.py::AnswerReviewDecision`
- `src/huipi_cloud/modules/answer_review/repository.py::append_decision`
- `tests/integration/test_answer_review.py::test_review_is_source_bound_append_only_and_does_not_write_s3`
- `tests/integration/test_answer_review.py::test_unknown_commit_outcome_can_be_replayed_without_duplicate`

## 5. 为什么修订需要版本历史？

### ASD-STE100 简明解释

复核者可能更正先前决定。系统新增修订，不覆盖旧行。revision 数字决定顺序，不依赖时间戳。

### 底层原理

每次追加先锁定 Submission，再按该 Question 找到最新 revision，生成下一个 revision 并保存 `supersedes_decision_id`。唯一约束阻止重复 revision 和多条直接后继。request_id 与 payload 摘要实现幂等重试。

### 30 秒面试回答

人工标签是审计记录，不能用 UPDATE 覆盖旧判断。我们保留每个决定，用 revision 和 supersedes 关系表达修订顺序。同一 Submission 上的写入由 PostgreSQL 行锁串行化；唯一约束做最后保护。request_id 可安全重试：相同请求返回原记录，不同内容复用同一 ID 会报冲突。

### 2 分钟深入回答

如果只保存最新值，就无法解释为什么同一题的判断变化，也无法识别写入竞争导致的覆盖。Repository 在 PostgreSQL 事务内对 Submission 行执行 `FOR UPDATE`，使同一提交的决定顺序串行。然后读取该 Question 当前最高的 revision，插入 `revision + 1`，并指向前一条记录。唯一约束限制 `(submission_id, question_id, revision)` 和 `(submission_id, question_id, request_id)`，后继 ID 也唯一。

request_sha256 包含 Submission、Question、Canonical 和 Alignment 来源版本及用户输入。相同 request_id、相同摘要时返回已存在记录；request_id 相同但内容不同则失败。并发不同请求不会丢修订，而形成明确链。Question 集合或 Canonical 变化时，旧修订保留历史，但 `is_current_for_bundle` 返回 false，不能自动套用到新源。

### 连续追问

- 为什么不按 `created_at` 取最新？并发时间戳无法清楚表达事务顺序。
- 两个不同 request_id 并发会丢一条吗？Submission 行锁串行并建立相邻 revisions。
- 审计记录能否删除？应用没有删除用例；父业务记录级联删除仍属于数据保留策略限制。

### 代码与测试

- `src/huipi_cloud/modules/answer_review/repository.py::append_decision`
- `migrations/versions/3c9a6f12d8e4_answer_review_decisions.py`
- `tests/integration/test_answer_review.py::test_concurrent_revisions_are_serialized_and_request_id_is_idempotent`

## 6. SourceRegion 如何追溯到原始文档？

### ASD-STE100 简明解释

区域指向 Canonical 文档中的节点。系统验证文档哈希、节点路径和文本范围，再复制必要的位置元数据。

### 底层原理

Canonical JSON Pointer 由 page、block 和嵌套 child 组成。节点偏移以 Python Unicode 码点计数，起始包含、结束不包含。Canonical 同时保存页码、Block UUID、BBox、节点类型和逻辑素材引用。

### 30 秒面试回答

复核区域存的是 Canonical 的 JSON Pointer 和可选字符半开区间。服务会先读取并校验当前 Canonical 的 SHA，再确认节点存在。文本端点用 Python 字符串长度验证，所以偏移是 Unicode 码点而非 UTF-8 字节。记录还保留页码、Block ID、BBox、类型和经过去 URI 的资产元数据。

### 2 分钟深入回答

`content_pointer` 指向 `/pages/{page}/blocks/{index}/content/children/...` 中的具体 `CanonicalContentNode`。在 `_index_canonical_nodes` 中，服务按 Canonical 树生成 pointer、父节点和所属 Page/Block 的映射。之后 `_materialize_regions` 对不存在的路径拒绝，对文本节点要求明确 start/end，并检查 `end <= len(value)`。这遵循 Python Unicode 字符串语义和半开区间。

Question 归属校验也遍历这棵树。同一节点内按半开字符范围比较；父容器和后代被视为内容覆盖关系，不能因为 pointer 字符串不同就当成独立区域。若父节点覆盖另一 Question 的来源，当前题不能把整个父节点确认为答案。不同兄弟节点使用相同 `[start, end)` 数字时仍是不同来源，不会误认为相同文字。

没有文本范围时，服务只接受有有效资产引用的 `image` 节点。Block 的图片引用不会让任意空文本或布局节点变成图像证据。如果某个 Block 级图片同时落在多道题的来源范围内，系统不接受专属 `response_present`；复核者可以把对应图片标成 `uncertain` 并注明 `shared_figure_attribution_unclear`。Block 与子节点重复引用相同资产时按资产 ID/内容摘要去重。

选中节点后，复核记录补上 page index/number、Block UUID、reading order、BBox、source/normalized type 和安全逻辑资产引用。外部 URL 和对象 Key不写入复核结果。Canonical 原始对象和 pointer 仍保存在现有私有存储体系中，人工决定不会反向修改它。

### 连续追问

- 为什么不是 UTF-8 字节偏移？源码节点使用 Python 字符串，采用码点偏移可直接切片。
- pointer 过期怎么办？当前源校验失败并要求重新检查。
- 一段答案跨页怎么办？可以记录多个节点区域。

### 代码与测试

- `src/huipi_cloud/modules/answer_review/service.py::_index_canonical_nodes`
- `src/huipi_cloud/modules/answer_review/service.py::_materialize_regions`
- `tests/integration/test_answer_review.py::test_invalid_pointer_and_unicode_range_fail_before_insert`
- `tests/fixtures/answer_presence_synthetic_v1.json::synthetic-cross-page`
- `tests/unit/test_answer_review_ownership.py::test_parent_container_cannot_claim_a_sibling_questions_child`
- `tests/unit/test_answer_review_ownership.py::test_shared_image_is_uncertain_not_question_specific_response`

## 7. 为什么人工标签与模型预测必须分开？

### ASD-STE100 简明解释

人工标签提供评测标准。模型预测是待检查结果。把二者当成同一数据会掩盖错误，无法计算质量指标。

### 底层原理

评测需要明确真值与预测的来源。当前数据只有人工合成标签，没有检测器。报告统计人工决定分布和覆盖率，但模型性能指标为 null / `not_evaluated`。

### 30 秒面试回答

Precision 和 Recall 要比较模型预测与人工真值。如果把人工标签同时当成预测，就会人为制造完美结果。本轮只有人工标注基线，没有自动作答检测器，因此脚本只报告标注覆盖率、不确定比例和结构一致性。作答检测的 Precision、Recall、FPR 明确返回 `not_evaluated`。

### 2 分钟深入回答

评测数据至少要有独立的人工标签和模型输出。人工标签定义正例、负例与 uncertain；自动系统另行输出预测。对于二分类指标，当前定义 `response_present` 为正类，`response_absent` 为负类，uncertain 和 unreviewed 不进入该二分类分母。脚本可读单独的 predictions JSON；没有预测文件时不会复用 `decision` 计算模型指标。

当前 13 个合成范围中，12 个已复核，其中 8 个 response_present、1 个 response_absent、3 个 uncertain；1 个未复核。该分布说明当前测试数据覆盖什么，而不是检测器表现。合成文本也没有验证真实手写内容。

### 连续追问

- 目前输出多少模型 Precision？没有检测器，所以是 null。
- uncertain 是否算 FN？当前它们不进入二分类评测分母。
- 合成数据上的人工标签可以代表真实分布吗？不可以。

### 代码与测试

- `src/huipi_cloud/modules/answer_review/evaluation.py::evaluate_dataset`
- `src/huipi_cloud/modules/answer_review/evaluation.py::evaluate_predictions`
- `tests/unit/test_answer_review_protocol.py::test_synthetic_baseline_is_valid_and_reports_manual_only_metrics`

## 8. False Positive 和 False Negative 是什么？

### ASD-STE100 简明解释

False Positive 表示检测器说“有作答”，但人工标注为没有。False Negative 表示检测器说“没有作答”，但人工标注为有。

### 底层原理

本项目把 `response_present` 作为正类、`response_absent` 作为负类。Precision 是 TP/(TP+FP)，Recall 是 TP/(TP+FN)，FPR 是 FP/(FP+TN)。零分母时指标标记 `not_evaluated`。

### 30 秒面试回答

在作答存在性任务里，FP 是系统把只有题干或空白作业报成有作答；FN 是系统漏掉了实际作答。Precision 看报出来的正例有多少正确，Recall 看人工确认的作答被找回多少，FPR 看无作答样本中有多少被误报。当前没有自动检测器，所以这些值还不能报告。

### 2 分钟深入回答

设 `response_present` 为正类。TP 是人工确认有作答且模型也预测有；FP 是人工确认 `response_absent`、模型预测有；FN 是人工确认有作答、模型预测没有；TN 是人工确认无作答且模型预测没有。Precision 对误报敏感，Recall 对漏检敏感，FPR 专门看负类上的误报率。

不同误差可能有不同后果。把题干误认为答案会让下游评分无依据；漏掉手写答案可能导致学生得不到评分。uncertain 不应偷偷归入负类。当前 eval 函数会报告 TP/FP/FN/TN 与分母，并在没有真实预测或某项分母为零时返回 `not_evaluated`。

### 连续追问

- 只有一个负例的合成集能说明真实 FPR 吗？不能。
- Precision 高但 Recall 低意味着什么？系统保守，误报较少但漏掉不少正例。
- 如何处理 uncertain？本定义将其排除并单独报告比例。

### 代码与测试

- `src/huipi_cloud/modules/answer_review/evaluation.py::_ratio`
- `src/huipi_cloud/modules/answer_review/evaluation.py::evaluate_predictions`
- `tests/unit/test_answer_review_protocol.py::test_detector_metrics_use_separate_predictions_and_hand_computable_counts`
- `tests/unit/test_answer_review_protocol.py::test_zero_denominator_metrics_are_not_evaluated`

## 9. 为什么“没有看到答案”不能直接判定未作答？

### ASD-STE100 简明解释

OCR 或区域匹配可能遗漏答案。缺少识别结果只表示系统没有证据，不证明学生没有写。

### 底层原理

手写可能很浅、被遮挡、在页边或只表现为公式和图形。自动识别失败与真实空白不是同一事件。因此系统保留 uncertain 和 unreviewed，并要求人工引用来源证据后才可标记 absent。

### 30 秒面试回答

没有识别到内容可能是学生没写，也可能是扫描质量、OCR、图像或题号关联失败。若把“没有系统输出”当作 `response_absent`，就会误判学生。当前明确区分 `unreviewed`、`uncertain` 和人工确认的 `response_absent`。无作答决定还要求引用印刷题干或作答区域来源。

### 2 分钟深入回答

这是证据缺失和负例之间的区别。`not_observed` 属于 P2-D1 算法状态，说明没有安全的 Question 关联；`unreviewed` 是没有人工决定；`uncertain` 表示人工仍不能确定；`response_absent` 才是人工明确判断没有回应。它们不能合并成一类。

标注协议把 uncertain 和 unreviewed 排除在二分类指标分母之外，单独报告比例。Schema 对 `response_absent` 要求引用排除掉的印刷题干或作答区域，防止没有任何来源时直接创建负例。该限制只能改善记录质量，不代表人类观察绝对正确；真实项目还需标注指南、双人复核和仲裁。

### 连续追问

- 页面空白可以证明没有作答吗？需要看清完整作答区域；当前 Canonical 没有空白区域视觉证明机制。
- OCR 未返回文本是否是负例？不是。
- 如何衡量人工一致性？后续需多标注者数据和一致性统计，当前未实现。

### 代码与测试

- `src/huipi_cloud/modules/answer_review/protocol.py::AnswerReviewDecisionCreate`
- `tests/unit/test_answer_review_protocol.py::test_absent_requires_a_cited_prompt_region`
- `tests/fixtures/answer_presence_synthetic_v1.json::synthetic-unreviewed`

## 10. 未来如何使用 VLM 辅助识别？

### ASD-STE100 简明解释

未来模型可以提出作答区域和不确定度。它不能替代经过验证的标签、来源追踪和教师复核。

### 底层原理

VLM 需要读取受控的 Canonical 或页面图像并输出候选节点/区域。预测必须带模型版本、来源摘要和可验证范围；离线评测须使用独立真实标签。模型输出不能直接覆盖人工决定。

### 30 秒面试回答

后续可以让 VLM 根据页面图像提出“可能有作答”的区域、Question 归属和不确定状态，但这些是预测，不是人工真值。预测要绑定 Canonical 版本并保留页、Block 或图像坐标。之后用独立标注集评估 Precision、Recall 和 FPR，低置信或复杂公式、表格、共享图形仍进入教师复核。本轮没有接入 VLM。

### 2 分钟深入回答

现有协议先解决标签可追踪和版本绑定。未来的模型适配层可以输入当前 Canonical 来源及必要的本地页面图像，输出候选 presence、candidate Question 和 source location；每条预测必须绑定模型版本、输入 Canonical SHA 和 Question 集合 digest。预测对象与 `AnswerReviewDecision` 分开持久化，避免模型覆盖人工修订。

评测时按 Submission/Question 维度隔离训练和验证样本，保留 `response_present`、`response_absent`、`uncertain` 与 unreviewed 规则。用真实且获得许可的脱敏作业测量 TP、FP、FN、TN，并检查跨年级、扫描质量、公式/图形和页面布局差异。模型无法定位可信来源或置信度不足时应回到人工复核。当前没有模型、推理服务、性能数据或真实准确率。

### 连续追问

- VLM 输出如何避免被直接当真？与人工决策分表、存模型和输入版本、评测后再定义自动接受阈值。
- 是否把学生原图发第三方？当前不发送；未来部署需要明确数据权限和本地/受控推理策略。
- 自动评分什么时候开始？需另行完成标准答案/Rubric、复核状态和质量门槛设计。

### 代码与测试

- 当前接口边界：`src/huipi_cloud/modules/answer_review/protocol.py::AnswerReviewDecisionRead`
- 当前评测基线：`src/huipi_cloud/modules/answer_review/evaluation.py::evaluate_predictions`
- `docs/roadmap.md` 的 P2-D2A 与 P3 项
- 演示：运行 `uv run python scripts/evaluate_answer_presence.py`，确认自动检测器指标仍为 `not_evaluated`。

## 去标识化不是匿名化

`export` 会移除 `reviewer_ref` 和原始 UUID，并用本次导出专用随机盐替换 ID 与摘要引用；它还会移除对象 Key 和外部 URI。这降低直接识别风险，但导出仍保留题号、Canonical pointer、页码、区域标签、BBox 等结构信息，关联到外部作业或其他资料时仍可能识别学生。导出和数据库原始复核数据都属于敏感数据；本地受控 CLI 也不等于访问控制系统。
