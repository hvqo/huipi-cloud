# P2-D1 面试复盘：题号候选与答案区域对齐

> 本文记录 P2-D1 的协议和实现细节。PR #9 的合并及 CI 状态以 GitHub 为准。当前没有 OCR、VLM、自动评分、人工纠正工作流或真实学生作业评测。

## 1. 为什么 OCR 完成后还不能直接批改？

### ASD-STE100 简明解释

OCR 把页面内容变成文字和结构。它不会自动知道哪段答案属于哪道作业题。批改前，系统还要找到题号、切分答案，并把答案关联到真实 Question。

### 底层原理

OCR 输出仍可能包含多个页面、多个 Block、公式、表格和图片。一个答案可以跨 Block 或跨页。P2-D1 读取 Canonical Document，保留来源位置，再把答案候选映射到数据库的 Question。证据不足时，结果进入复核或未分配区域。

### 30 秒面试回答

OCR 解决的是页面内容提取，批改还需要把学生写的内容分配到具体题目。题号可能缺失、误识别或与子题编号冲突，答案也可能跨页。因此我先在 Canonical 结构上生成有来源证据的题号候选和答案区域，再关联数据库里的 Question。规则不能证明时就标记待复核，不会直接送去自动评分。

### 2 分钟深入回答

MinerU 输出的是识别后的文档结构，不是业务上的作答记录。批改系统必须知道这段文字属于 Assignment 的哪一个 Question，才能使用相应 Rubric。P2-D1 的输入是成功生成的 Canonical Document 和 Submission 所属 Assignment 的 Question 列表。识别器只在普通文本行首找题号形式，切分器按 Canonical 阅读顺序创建来源区域，匹配器再用数据库中的真实题号建立关联。

结果保留 `source_block_id`、页码、reading order、JSON pointer 和文本偏移。没有题号的内容不会按顺序猜题；该题状态为 `not_observed`，并不代表学生没作答。重复编号、顺序冲突和不确定的通用数字标签进入复核。这样后续批改有可检查的输入和边界，但本轮并没有实现评分。

### 连续追问

- OCR 把题号识别错时如何区分 OCR 错误和对齐错误？
- 没有题号的手写作业能否按顺序分配？为什么当前不这么做？
- 批改 Agent 如何确认使用的是正确 Rubric？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/service.py::align_submission`
- `src/huipi_cloud/modules/answer_alignment/segmenter.py::align_canonical_document`
- `tests/integration/test_canonical_documents.py::test_explicit_question_answer_completes_synthetic_canonical_to_api_path`
- 演示：查询合成 Canonical 的单题答案 API，展示题号证据和源 Block。

## 2. 什么是题号识别和答案对齐？

### ASD-STE100 简明解释

题号识别找到文档中的题号候选。答案切分把候选之间的内容变成答案区域。对齐把候选题号映射到作业中的真实 Question ID。

### 底层原理

`detector.py` 识别位置和形式；`segmenter.py` 按阅读顺序切分；`matcher.py` 检查数据库题号、重复编号和顺序。识别到题号不等于自动匹配成功。结果分别记录 candidate、answer、unassigned region 和 status。

### 30 秒面试回答

这三步分开处理。识别器只提出候选，切分器依据 Canonical 顺序确定来源区间，匹配器才把候选连到真实 Question。把候选和最终匹配分开，可以保留未知题号或歧义证据，而不会静默覆盖题目。

### 2 分钟深入回答

Canonical 的页面、Block 和嵌套节点提供了统一的阅读顺序与来源身份。识别器扫描普通文本节点的行首，只生成候选及 marker 类型、文本范围。切分器把当前题号末尾到下一个候选之前的区域作为答案内容，并用半开文本偏移切分同一纯文本 Block。匹配器根据 Submission 所属作业中的 `question_number` 找到 `Question.id`，检查候选重复和序列倒置。

输出协议将每个候选的状态和 reason code、每个 Question 的状态和来源区域、未分配内容分别保存。明确的“第 n 题”在没有冲突时可以自动对齐；常见通用数字格式保留候选但需要复核。这是规则基线，不是概率模型，也没有校准过置信度。

### 连续追问

- `candidate` 与 `AlignedAnswer` 有什么区别？
- 一个 Question 可不可以有多个 source region？
- 数据库没有这个题号时如何保存学生内容？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/detector.py::detect_question_candidates`
- `src/huipi_cloud/modules/answer_alignment/matcher.py::assess_candidates`
- `src/huipi_cloud/modules/answer_alignment/segmenter.py::align_canonical_document`
- `tests/unit/test_answer_alignment.py::test_unknown_number_is_unmatched_and_does_not_fill_a_database_question`

## 3. 正则表达式为什么不能直接完成题号识别？

### ASD-STE100 简明解释

正则表达式只能检查字符形式。它不能单独判断这个数字是题号、列表项、页码、公式还是子题号。系统还要检查文档结构和作业题号。

### 底层原理

P2-D1 把正则限制在普通文本节点的行首。检测前先排除公式、表格、图片、代码、列表和布局页码。匹配阶段再检查 Question 集合、重复候选和序列。格式常见但语义不确定的候选不会自动对齐。

### 30 秒面试回答

正则适合找候选，不适合独自决定业务含义。例如 `1.` 可能是第一题，也可能是列表项；`（1）` 可能是小题。我们先按 Canonical 类型和位置筛选，再用真实作业题号和顺序做检查。仍然有歧义的标为复核。

### 2 分钟深入回答

如果对全文搜索数字或编号，公式中的 `x = 1`、分数 `1/2`、页码和列表都会产生误匹配。P2-D1 用 Canonical 的类型语义缩小检测范围，只在普通文本行首检测 `第 n 题`、`n.`、`n、`n)` 和括号数字。列表、公式、表格、图片、代码和页码不参与候选检测。

这仍不能完全区分 `1.` 的题号和编号列表，所以算法对通用数字标记始终要求复核；括号标记也可能是子题。只有显式题号形式并且与 Assignment 题目无重复、顺序一致时才自动匹配。这样减少错误分配，比追求高覆盖率更适合后续批改。

### 连续追问

- `第1题目` 是否会被部分匹配？
- 如果 MinerU 把列表节点识别成普通文本怎么办？
- 为什么不让模型直接判断编号语义？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/detector.py::_LINE_MARKER`
- `src/huipi_cloud/modules/answer_alignment/detector.py::detect_question_candidates`
- `src/huipi_cloud/modules/answer_alignment/matcher.py::assess_candidates`
- 测试：`test_numbers_inside_equations_fractions_and_prose_are_not_question_labels`、`test_list_items_are_not_treated_as_top_level_question_labels`、`test_parenthesized_number_is_review_required_as_possible_subquestion`。

## 4. 跨页答案如何处理？

### ASD-STE100 简明解释

题号候选之间的 Canonical 内容属于当前答案候选。切分器按页面、Block 和节点顺序遍历，所以答案区域可以跨多个 Block 和页面。

### 底层原理

Canonical 每个 Block 有全篇 `reading_order`、`page_index` 和稳定 `block_id`。切分器从当前 marker 末尾到下一个 marker 起点取来源片段。每个片段独立保存页码、Block、类型、JSON pointer 与文本偏移。

### 30 秒面试回答

我没有假定一道题对应一个 Block。Canonical 提供页面和全篇阅读顺序，切分器从当前题号后开始收集来源区域，直到下一个题号候选，所以一个答案可由多个 Block 或页面组成。每一段都保留来源位置，方便检查和回到原文。

### 2 分钟深入回答

MinerU 会按页面和版面输出多个 Block，答案可能由于换页、公式、表格或图片被拆开。P2-D1 先把嵌套 Canonical 内容按深度优先顺序展开成来源原子，同时携带所属顶层 Block 与 JSON pointer。题号候选的位置是某个原子上的文本区间。当前题答案的结束位置是下一个候选开始位置，或文档末尾。

跨 Block 的片段保留每个 Block 的 ID、页码、阅读序号、BBox 和内容指针。相同纯文本 Block 内可以用字符偏移进行无重叠切片。如果题号跨过公式或其他复杂结构，内容仍按源节点保存，但结果标为待复核，不宣称复杂嵌套被精确拆成了独立业务答案。

### 连续追问

- 页码重排或 OCR reading order 错误会造成什么影响？
- 两个 Block 的 BBox 重叠时如何确定顺序？
- 怎样证明区域没有重复归属？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/structure.py::canonical_atoms`
- `src/huipi_cloud/modules/answer_alignment/segmenter.py::_regions_between`
- `tests/unit/test_answer_alignment.py::test_cross_page_answer_keeps_page_block_and_reading_order_trace`
- `tests/unit/test_answer_alignment.py::test_one_text_block_with_two_question_labels_slices_without_overlap`

## 5. 公式、图片如何与答案关联？

### ASD-STE100 简明解释

公式、表格和图片保留为 Canonical 来源区域。答案文本投影只提供文本视图。图片通过 Canonical asset 引用关联，不复制图片二进制到对齐 JSON。

### 底层原理

每个 `SourceRegion` 包含源 Block、页码、阅读顺序、内容节点类型、JSON pointer 和 asset refs。Formula 与 table 的原始内容仍可从 Canonical pointer 追溯。图片区域保存逻辑 `CanonicalAssetReference`，Canonical 不暴露私有对象 Key。

### 30 秒面试回答

答案不能只保存拼接文本，否则公式和图片会丢失。对齐结果同时保存文本投影和源区域。公式、表格保留类型与来源指针，图片保留逻辑素材引用。这样下游可以知道答案还有非文本内容，并能沿来源关系回查 Canonical。

### 2 分钟深入回答

`AlignedAnswer.text_projection` 便于阅读和简单搜索，但不是完整答案。每个 source region 保留 `normalized_type`、`source_type`、来源 pointer 和必要的文字切片。图片逻辑引用使用 Canonical asset id，不暴露对象存储 key 或凭据。这个模块不提供图片预览接口，也不会拉取外部图片 URL。

同一回答可以包含文字、公式、表格和图片节点，因此区域是列表，不是单个字符串。对于复杂结构边界，保留来源结构并要求复核。对于没有文字的图片答案，只要存在逻辑资产引用，Question 就不会因为空文本投影被错误标成无内容。

### 连续追问

- `text_projection` 为什么不能作为唯一存储？
- 外部图片 URL 是否会被服务端请求？
- 如何从逻辑 asset id 找到实际图片？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/protocol.py::SourceRegion`
- `src/huipi_cloud/modules/answer_alignment/segmenter.py::_make_region`、`_unique_asset_refs`
- `tests/unit/test_answer_alignment.py::test_image_only_answer_region_retains_logical_asset_reference`
- `tests/unit/test_answer_alignment.py::test_formula_and_table_content_remain_traceable_in_answer_regions`

## 6. 识别结果有歧义时怎么办？

### ASD-STE100 简明解释

系统保留证据并标记待复核。它不会为了增加自动覆盖率而猜测题号。未知题号和没有题号的内容保存在未分配区域。

### 底层原理

Matcher 检查数据库是否有对应题号、编号是否重复、候选顺序是否与作业顺序冲突。括号和通用数字标签带有具体 reason code。Question 状态分为 `aligned`、`review_required` 和 `not_observed`；未识别内容不会解释为未作答。

### 30 秒面试回答

识别结果有歧义时，我采用 fail closed。系统保存题号候选、来源和 reason code，状态标记 `review_required`；未知编号放进 `unassigned_regions`。没有候选的题目是 `not_observed`，不是“学生没答”。这样后续可以人工判断，而不会把错误输入传给自动批改。

### 2 分钟深入回答

规则只能提供证据强弱，不能证明某些数字一定是大题编号。比如括号编号可能是子题，`1.` 可能是列表。Matcher 对重复候选、题号倒序、未知题号分别记录 reason code。未匹配的候选保留在 candidates 和 unassigned region；不会强行填入某个 Question。

每道 Question 都有结果项。如果没有匹配证据，状态是 `not_observed`，其含义是系统没有足够来源证明学生在该题的答案区域，不是教育意义上的空白作答判定。当前还没有教师纠正界面，所以待复核只作为明确状态暴露。真实使用前还需要身份权限、人工校正流程和真实数据评测。

### 连续追问

- 重复题号的答案如何保留？
- 自动批改是否可以处理 `review_required`？
- 谁能查看学生答案？当前有权限控制吗？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/matcher.py::assess_candidates`
- `src/huipi_cloud/modules/answer_alignment/segmenter.py::align_canonical_document`
- 测试：`test_duplicate_question_number_candidates_are_kept_for_review`、`test_question_order_conflict_marks_candidates_for_review`、`test_unknown_number_is_unmatched_and_does_not_fill_a_database_question`。

## 7. 什么是 Precision 与 Recall？

### ASD-STE100 简明解释

候选 Precision 是系统找到的候选中有多少符合人工标签。候选 Recall 是人工标注的候选中有多少被系统找回。脚本还分开计算候选到 Question 的关联质量、`aligned` 自动接受质量和答案边界字符重叠。所有指标都依赖清楚的标签和评测样本。

### 底层原理

离线脚本分别比较候选位置、数据库 Question 关联和最终自动接受。候选的题号、页码、Block index 和字符区间与人工标记集合比较；候选关联指标还要求关联到预期的 Question ID，即使该候选最终需要复核。自动接受指标只把人工标注可自动接受且系统输出 `aligned` 的 Question 计为正确。答案边界按题目、页面、Block 和 Unicode 码点区间计算重叠字符，能惩罚遗漏、超出和错分。指标会输出分子、分母和定义；分母为 0 时 Precision 为 `null`。当前人工标注集有 12 个合成案例，指标只说明这些案例，不代表真实学生作业。

### 30 秒面试回答

候选 Precision 看预测候选中有多少符合人工标签；Question-link Precision 看候选关联是否指向正确题目；自动接受 Precision 只评价最终 `aligned` 状态是否安全。Recall 用人工标注数量作分母。答案边界另外计算字符交集与并集，所以会同时惩罚漏掉的内容和错分到其他题的内容。现在只有 12 个合成样例，我会报告每个指标的分子、分母和定义，不把它解释成真实数据准确率。

### 2 分钟深入回答

Precision = TP/(TP+FP)，Recall = TP/(TP+FN)。评测要求人工标签先规定什么算一个题号候选和它对应的 Question。当前数据集记录 marker 的题号、页、Block 和文本范围。脚本分别比较候选检测结果和对齐结果；题目映射指标检查候选关联的 Question ID 是否对应人工标注题号。

同一脚本还统计人工确认可自动接受的准确率、对有答案题目的覆盖率、复核候选比例、字符级边界 Precision/Recall/IoU、精确区间命中率和 Canonical 图片逻辑引用保留率。这些指标代表不同问题：候选关联正确不等于答案边界完整，答案边界有重叠也不等于适合自动批改。当前数据集只有 12 个合成案例，包含模拟的手写解题文本但没有真实手写识别；结果不能外推到真实作业。

### 连续追问

- TP、FP、FN 在本项目中分别是什么？
- 为什么没有报告 F1 或置信区间？
- 如何建立代表性的真实评测集？

### 代码与演示

- `scripts/evaluate_answer_alignment.py::main`
- `tests/fixtures/answer_alignment/annotated.json`
- 演示：运行 `uv run python scripts/evaluate_answer_alignment.py`，检查样本数、指标和失败案例。

## 8. 如何评估答案区域切分质量？

### ASD-STE100 简明解释

把系统返回的源区域与人工标注的答案区间比较。检查区域是否落在正确题目、页面和 Block，字符范围有多少重叠，是否遗漏或错分。

### 底层原理

每个 source region 提供 Canonical JSON pointer 与可选半开文本偏移 `[start, end)`。偏移单位是 Unicode 码点，和 Python 字符串切片一致。人工标签提供按 Question、页面和 Block 分组的范围。脚本用区间并集计算字符重叠，因此部分匹配会得到部分分数，遗漏和错分都会降低指标；同时保留精确区间 Precision/Recall。单元测试检查同 Block 多题切分不重叠，跨页答案保留每页来源。

### 30 秒面试回答

我按题目、页面、Block 和字符范围比较人工标签与系统结果。部分重叠会按字符计分，遗漏和错分都会降低 Precision、Recall 或 IoU；精确范围指标则检查边界完全相同。单测还检查同 Block 范围不重叠和跨页来源保留。当前只有 12 个合成样例，不能据此声称真实分割准确率。

### 2 分钟深入回答

答案范围的质量不仅是文本相似度，还包括来源完整性和边界是否安全。P2-D1 的协议使用 Canonical pointer 定位节点，对可切片文本用半开区间保存偏移；一个答案可以有多个区域。离线评测把人工标注的题号、page、block、start、end 与输出比较，计算字符重叠 Precision、Recall 和 IoU，也统计完全相同的区间。字符交并比会惩罚缺失范围、多出的范围和错分到别题的内容。

针对同一文本 Block 中多个题号，单测验证字符片段没有交叠；跨页单测验证按页保留多个区域。复杂嵌套结构的边界只留证据并标记复核，不作为完全自动切分成功。后续需要扩充真实标注样本、报告不同错误类别，并由教师确认复核界面的目标流程。

### 连续追问

- 为什么用精确边界，而不只用文本相似度？
- 同一源区域能否属于多道题？
- 图片区域如何纳入边界评测？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/segmenter.py::_regions_between`
- `scripts/evaluate_answer_alignment.py::main`
- 测试：`test_one_text_block_with_two_question_labels_slices_without_overlap`、`test_cross_page_answer_keeps_page_block_and_reading_order_trace`。

## 9. 为什么先建立规则基线，再引入 VLM？

### ASD-STE100 简明解释

规则基线容易解释，也能给出可检查的来源。它能建立失败案例和测量方法。之后才能判断 VLM 是否提高了实际效果。

### 底层原理

当前任务目标是建立确定性的候选、切分和来源协议。规则不需要额外模型服务，也不会产生未经校准的概率。本轮尚无真实作业标注集，因此没有证据证明 VLM 能改善 Precision、Recall 或复核负担。

### 30 秒面试回答

先做规则基线，可以把支持的版式、失败类型和来源追踪定义清楚，也便于建立离线人工标签。当前没有足够的真实作业评测数据来证明 VLM 更好，所以没有接入模型。后续应在同一标注集上比较规则和 VLM 的准确性、复核比例、成本和延迟。

### 2 分钟深入回答

如果先接入 VLM，错误可能被隐藏在模型输出里，且没有数据判断它是否优于规则。规则基线可以清楚表达限制：哪些文本形态被检测，哪些候选自动匹配，哪些原因会要求复核。同时建立确定的数据协议，让后续模型输出仍必须带证据和来源。

进入下一步之前需要收集经许可和脱敏的代表性作业样本，定义人工标注规范，并按题型、版式、手写质量等分层。然后在固定评测集上比较 Precision、Recall、自动覆盖率、边界质量、人工复核负担、成本与时延。VLM 可以补充复杂布局推理，但不能绕过 Question 校验和人工复核边界。

### 连续追问

- 哪些场景最值得交给 VLM？
- 如果 VLM 与规则结果冲突，使用哪个？
- 如何避免模型产生不存在的 Question ID？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/detector.py::detect_question_candidates`
- `src/huipi_cloud/modules/answer_alignment/matcher.py::assess_candidates`
- `scripts/evaluate_answer_alignment.py` 与人工标签 fixture 用于未来同集对比。

## 10. 如何保证自动对齐结果可追溯与幂等？

### ASD-STE100 简明解释

结果保存 Canonical 版本、题目集合摘要、算法版本和源区域。数据库唯一约束确保相同输入只登记一个有效结果。对象每次使用独立 Key。

### 底层原理

Canonical artifact id、Canonical SHA-256、Question digest、schema/aligner version 共同描述对齐输入。UUIDv5 生成确定性索引 id。PostgreSQL 唯一约束是并发安全边界；S3 对象使用 UUID run Key，避免覆盖。COMMIT 结果不确定时保留对象，防止删除已被引用的文件。

### 30 秒面试回答

结果保留 Canonical ID 和 hash、Question 列表 digest、aligner 版本，以及每个答案的源 pointer、Block 和偏移。索引有数据库唯一约束，并发请求用 PostgreSQL 冲突处理获取同一有效记录。S3 写独立不可变 run key。若提交结果不确定，不删除对象，避免把数据库可能已引用的对象删掉；跨 S3 和数据库并没有 Exactly Once。

### 2 分钟深入回答

单靠进程内锁不能处理多个 Worker 并发，也不能保护崩溃恢复。输入身份由 Canonical artifact、Question 集合 digest、aligner/schema 版本确定，结果 ID 用 UUIDv5 派生。数据库唯一约束 `(canonical_artifact_id, aligner_version, assignment_questions_digest)` 是最终并发不变量。

每次执行向私有 S3 写新的 UUID run key，避免重试之间互相覆盖。随后 Repository 在事务中用 `ON CONFLICT DO NOTHING` 注册，读取胜出的索引。确定性竞争输家可在数据库成功返回后删除自己没有被引用的对象；数据库操作异常可能表示 COMMIT 状态未知，因此必须保留对象。系统仍可能有孤立对象，未来需要按保留期对账。该方案不构成 S3 与 PostgreSQL 的跨系统原子事务，也不承诺 Exactly Once。

### 连续追问

- 为什么唯一约束包含 Question digest？
- 如果算法逻辑变了，旧结果如何处理？
- 如果删除孤立对象时数据库状态发生变化怎么办？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/matcher.py::assignment_questions_digest`
- `src/huipi_cloud/modules/answer_alignment/service.py::align_submission`
- `src/huipi_cloud/modules/answer_alignment/repository.py::register_success`
- `src/huipi_cloud/modules/answer_alignment/models.py::AnswerAlignmentArtifact`
- 集成测试：`test_concurrent_answer_alignment_has_one_effective_index_and_object`、`test_uncertain_index_commit_keeps_uploaded_immutable_object_for_reconciliation`。

## 11. 为什么不可信的后续题号会让前一道题也需要复核？

### ASD-STE100 简明解释

一个题号只能说明可能的开始位置。它不能证明前一道答案在这里结束。如果这个位置是未知题号、列表项或其他待复核候选，前一道题可能被截短。系统把前一道答案标记为 `review_required`，并保留候选后的文字在未分配区域。

### 30 秒面试回答

我把题号识别和答案边界判断分开。即使前一个“第 1 题”是明确题号，如果下一个候选是未知编号或可能的列表项，切分器不能证明第一题答案就在它前面结束。此时答案范围不完整，不能称为 `aligned`。我保留未知部分并用 `answer_end_boundary_is_untrusted` 要求复核。只有边界两侧都可信时，才可能自动接受答案。

### 2 分钟深入回答

旧逻辑按所有候选切分，然后只根据题号匹配状态给每题定状态。这会出现一个问题：作业只有 Question 1，文本先写“第1题”，随后出现“第99题 继续推导”，最后才给出结论。第99题没有数据库映射，内容会放到未分配区域，但第1题却仍显示 `aligned`，好像答案已经完整。

现在切分器在候选评估后检查相邻边界。若当前候选能关联 Question，但紧随其后的候选不是可信 `aligned` 候选，就把当前候选标为 `review_required`，并记录 `answer_end_boundary_is_untrusted`。未匹配候选和它后面的区间仍放在 `unassigned_regions`，不强行拼回前一道答案。后续明确且可信的题号仍可独立对齐。该逻辑降低覆盖率，但避免把局部切片包装成完整答案。

### 连续追问

- 为什么不把未知题号后的文字并入上一题？
- 哪些候选可以作为可信边界？
- 如果 OCR 把一个真实题号识别成未知编号，会发生什么？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/segmenter.py::_review_complex_splits`
- `src/huipi_cloud/modules/answer_alignment/matcher.py::assess_candidates`
- 测试：`test_unknown_number_after_valid_answer_makes_the_previous_boundary_uncertain`、`test_unknown_candidate_between_questions_does_not_make_truncated_answer_aligned`、`test_generic_numbered_list_inside_answer_cannot_truncate_a_trusted_answer`。

## 12. 同一 Canonical 原子带有图片并被多道题切分时怎么办？

### ASD-STE100 简明解释

图片引用没有字符偏移。若同一个节点被切给多个题目，系统无法证明图片只属于其中一道。系统保留同一个 asset ID，并把相关 Question 标为 `review_required`。

### 30 秒面试回答

文本可以按字符偏移切分，但图片只挂在 Canonical 节点上，没有精确到文本片段的位置。若这个节点里有两个题号，我不把图片复制成两份“确定归属”。结果保留原有逻辑图片 ID，并对相关答案标记复核。嵌套父节点上的图片也按同样原则处理，未确定归属时保留在未分配来源里。

### 2 分钟深入回答

Canonical 来源原子表示一个内容节点和它的 asset refs。文本节点可以通过 `[start, end)` 给两个题号生成不重叠片段，但节点的图片引用没有单独的 x/y 或字符位置。旧切分器在同一原子上给每个文本片段复制了完整 asset refs，两个 Question 因而都可能显示为自动对齐，却都引用同一张图。

现在只要候选所在的 Canonical Block 含有无法精确定位的图片引用，相关题目就附带 `asset_attribution_requires_review` 并进入复核；逻辑 asset ID 仍保留在源区域或未分配区域，不删除素材。该策略明确表达“图片存在，但归属不确定”，不是 Exactly Once 的资产分配算法，也没有图片预览或教师纠正功能。

### 连续追问

- 为什么不根据图片在 JSON 中的位置推断属于哪道题？
- 文本区域不重叠是否意味着图片也唯一归属？
- 后续怎样由教师确认素材归属？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/structure.py::canonical_atoms`
- `src/huipi_cloud/modules/answer_alignment/segmenter.py::_review_complex_splits`、`_make_region`
- 测试：`test_one_atom_shared_image_keeps_both_questions_in_review`、`test_nested_parent_image_is_preserved_as_unassigned_and_questions_require_review`。

## 13. 为什么更改边界判定时提升 aligner 版本？

### ASD-STE100 简明解释

同一输入在不同算法规则下可能得到不同状态和范围。新的结果使用新版本索引，不覆盖旧结果。JSON 字段没有改变，所以结果合同版本仍为 1.0。

### 30 秒面试回答

这次修改了“答案边界是否可信”的业务语义，旧的 1.0.0 结果不能和新结果混为同一种算法输出。因此我把 `ALIGNER_VERSION` 从 1.0.0 提高到 1.1.0。持久化唯一键包含算法版本，所以新结果与旧结果并存，不会原地覆盖。协议字段没有变化，因此不增加 schema major 版本。

### 2 分钟深入回答

幂等索引由 Canonical artifact、aligner version 和 Question digest 唯一标识。更新算法后提升版本可让同一 Canonical 与同一题目集合生成新的索引 ID 和新对象 Key。旧的 1.0.0 索引与对象保留，既有数据不会被覆盖；当前 API 查询只选择代码配置中的 1.1.0 版本，因此历史版本不会自动返回给当前读接口。JSON schema 仍然是 `huipi.answer.alignment` 1.0，因为字段形状没有变化，发生变化的是处理语义。

### 连续追问

- 如果希望查询旧版本结果，需要增加什么能力？
- 为什么不是直接更新旧 S3 对象？
- schema version 和算法 version 分别解决什么问题？

### 代码与演示

- `src/huipi_cloud/modules/answer_alignment/protocol.py::ALIGNER_VERSION`
- `src/huipi_cloud/modules/answer_alignment/service.py::align_submission`
- `src/huipi_cloud/modules/answer_alignment/models.py::AnswerAlignmentArtifact`
- 演示：核对数据库唯一键包含算法版本，并检查旧索引未被更新。
