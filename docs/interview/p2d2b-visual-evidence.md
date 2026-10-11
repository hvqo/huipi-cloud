# P2-D2B 面试复盘：VLM 视觉作答证据建议

本阶段提供的是机器建议。它不确认学生已作答，也不评分。模型结果与 `answer_review_decisions` 分开保存。

## 1. 为什么 MinerU OCR 不能直接判断学生是否作答？

**简洁解释：** OCR 把页面内容转成文字。它不能可靠地区分印刷题干和学生笔迹。没有 OCR 文字，也不代表学生没有作答。

**底层原理：** Canonical 和 AnswerAlignment 保留结构与来源，但来源中的文本可能来自印刷字、OCR 或手写识别。`aligned` 只说明内容可能属于某道题。它不说明内容是学生写的。本阶段把原始页面交给 VLM，让它提出可见作答区域候选。

**30 秒回答：** MinerU 的 OCR 主要解决文档结构和文字抽取问题。它没有可靠的笔迹归属标签，所以不能只根据 OCR 文本确认学生作答。P2-D1 的 `aligned` 也只是把来源区域映射到题目。P2-D2B 再看原始页面，让 VLM 判断是否存在疑似笔迹或公式，但结果仍是机器建议，需要人工复核。

**2 分钟回答：** OCR 输出会把印刷题干、页眉、手写内容都变成文本。它可能漏掉浅色笔迹、几何标记和复杂公式，也可能把印刷内容识别成题目来源。文本抽取本身不包含“谁写的”这种可靠语义。P2-D1 可以根据题号把区域映射到作业 Question，但 `aligned` 不等于该区域是答案。P2-D2B 因此读原始 PDF 或图片，给 VLM 题目上下文和对应页面。模型只能输出疑似证据类别和图像框。`candidate_prompt_only` 只表示模型看到印刷题干，不表示学生未作答。人工决定仍保存在独立复核记录中。

**连续追问：** `aligned` 与 `response_present` 有什么区别？如果 OCR 没有提取出公式，系统会得出什么结论？

**代码与验证：** [`protocol.py`](../../src/huipi_cloud/modules/answer_alignment/protocol.py) 中 `AlignedAnswer.matching_status` 的描述；[`service.py`](../../src/huipi_cloud/modules/visual_evidence/service.py) 的 `analyze_question_visual_evidence`；[`test_visual_evidence.py`](../../tests/unit/test_visual_evidence.py) 的 prompt-only 与几何/手写区域协议测试。

## 2. 为什么还要看原始图像？

**简洁解释：** 原始图像保留了像素和版面。OCR 文本不能完整表示手写、几何标记和混合排版。

**底层原理：** 页面图像保留笔画、位置、颜色和相对几何关系。P2-D2B 从私有 S3 下载原始文件，按 P2-D1 已关联的页面渲染，不重新运行 OCR，也不把整份文档无条件发给模型。

**30 秒回答：** OCR 是页面的文字投影。图像里还有笔迹形态、公式布局、表格填写和几何辅助线。P2-D2B 因此从私有 S3 校验原始 PDF 或图片，再只渲染该题已关联的页面。这样模型看到实际视觉证据，而不是只看 OCR 摘要。

**2 分钟回答：** 一份作业同时含印刷题干、学生笔迹和图形。OCR 将它们摊成文本后，空间关系会变弱；浅色笔迹或图形标记也可能没有文本结果。VLM 输入必须包含原始页面图像。服务先校验数据库中的文件大小和 SHA-256，再做格式签名检查和有界页面渲染。渲染只取目标 Question 的来源页，避免处理整份长文档。页图传给模型后，服务不记录 Base64，也不会将原文件放入公开目录。题目和页面内容仍可能含学生敏感信息，因此默认关闭模型，并默认只允许本地 endpoint。

**连续追问：** 为什么不把整份作业全部页面发给模型？局部渲染会损失什么信息？

**代码与验证：** [`render.py`](../../src/huipi_cloud/infrastructure/visual_evidence/render.py) 的 `render_selected_pages`；[`service.py`](../../src/huipi_cloud/modules/visual_evidence/service.py) 的 `_question_pages`、`_download_original`；PDF 只选第 2 页的测试 `test_pdf_renders_only_selected_page_and_records_rotation`。

## 3. VLM 与 OCR 的任务边界是什么？

**简洁解释：** OCR 提取文字和文档结构。VLM 提出页面上的视觉证据区域。二者都不能替代人工确认或评分。

**底层原理：** P2-B MinerU 提供解析输入，P2-C 保存 Canonical 结构，P2-D1 提供题目来源区域。P2-D2B 只增加视觉定位建议。系统不会用 VLM 修改 OCR 结果、Canonical 或 Alignment。

**30 秒回答：** 我把 OCR 与 VLM 分开。OCR/MinerU 建立文字、结构和来源指针。VLM 查看原始页图，返回候选笔迹区域和有限的状态。程序再验证坐标与题目来源是否可能对应。VLM 不改 OCR，不生成答案文本，也不评分。Provider 可选标准 OpenAI-compatible API 或只允许 loopback 的 Ollama 原生接口。

**2 分钟回答：** MinerU 的输出用于可追溯的文档结构化。Canonical 记录来源、页、Block、嵌套节点、BBox 和素材信息。AnswerAlignment 根据 Canonical 将候选来源映射到真实 Question。视觉分析读取这个已验证结果，只把题目编号、题干、页码、BBox/类型摘要和关联页面交给 VLM。VLM 返回严格 JSON；Pydantic 解析器拒绝未知字段和无效坐标。后端只把视觉区域与原有 Canonical 来源进行候选空间匹配，不回写 P2-B、P2-C 或 P2-D1。这样每层职责清楚，出现错误时也能判断来自解析、题号映射还是视觉建议。

**连续追问：** 如果 MinerU 的页方向与渲染方向不同，VLM 的框怎样处理？VLM 能直接写一个 OCR 修正结果吗？

**代码与验证：** [`architecture.md`](../architecture.md) 的 P2-D1/P2-D2B 边界；[`provider.py`](../../src/huipi_cloud/infrastructure/visual_evidence/provider.py) 的 `OpenAICompatibleVisionProvider.analyze`、`OllamaNativeVisionProvider.analyze`；[`service.py`](../../src/huipi_cloud/modules/visual_evidence/service.py) 的 `_attribute_regions`。

## 4. 图像坐标如何映射到 Canonical BBox？

**简洁解释：** 模型只返回图像上的归一化框。程序保存渲染尺寸和变换，再检查它是否与已验证来源框重叠。重叠只生成候选。

**底层原理：** 当前使用完整页面，不做裁剪。均匀缩放不改变归一化坐标。PDF MediaBox/CropBox 和 EXIF 信息会记录。PDF 框不一致、原点偏移、页面旋转或 EXIF 不是 1 时，程序保留视觉框但不生成 Canonical pointer。`source_bbox_iou` 是实际几何比例，不是来源正确率。

**30 秒回答：** VLM 输出的是当前渲染页 0 到 1 的归一化框。Proposal 记录 MediaBox、CropBox、rotation、EXIF、输出尺寸和变换。映射只在 PDF 框相等且全页原点为零、未旋转，或图片 EXIF 为 1 时允许。IoU 0.08 只是候选门槛。即使唯一重叠，结果仍不是已证明的题目归属；印刷题干区域不拿答案 pointer。

**2 分钟回答：** 图像坐标不能直接等同于 Canonical 坐标。PDF 可用 CropBox 改变实际显示区域，MediaBox 或 CropBox 也可带偏移；PDF rotation 和图片 EXIF 会改变方向。系统保存两个 PDF 页面框、旋转、EXIF、输出尺寸、DPI、图像摘要和变换。映射门槛是刻意保守的：PDF 必须是全 MediaBox 可见、原点为零且 rotation 为零；图片必须使用默认 EXIF 朝向。否则 proposal 仍存原始 VLM bbox，但不产生 Canonical pointer。通过门槛后，程序保存原始 IoU 数值，并检查目标来源、跨题区域与共享素材。IoU 0.08 只是历史候选启发式，未经真实作业校准；唯一重叠也不能证明归属。`printed_prompt` 一律保持 unresolved。这样保留视觉意见，同时不会把它升级成人工作答证明。

**连续追问：** 为什么 rotated PDF 页不直接用记录的矩阵映射？IoU 0.08 可以说明什么、不能说明什么？

**代码与验证：** [`render.py`](../../src/huipi_cloud/infrastructure/visual_evidence/render.py) 的 `_render_pdf`、`_render_image`、`transform_bbox_to_source`；[`service.py`](../../src/huipi_cloud/modules/visual_evidence/service.py) 的 `_box_iou`、`_attribute_regions`；测试 `test_pdf_cropbox_difference_or_offset_refuses_source_mapping`、`test_all_rotated_pdf_pages_preserve_rendered_box_but_refuse_canonical_mapping`、`test_exif_orientations_are_recorded_and_nondefault_mapping_is_refused`、`test_printed_prompt_never_gets_an_answer_source_pointer_even_with_perfect_iou`。

## 5. 如何防止 VLM 编造 Question ID 和来源指针？

**简洁解释：** 模型输入目标 Question 由调用方固定。模型输出 schema 不含 Question ID、Canonical pointer 或 URL。后端从已验证数据中生成候选 pointer。

**底层原理：** `extra="forbid"` 的严格 Pydantic 模型拒绝未知字段。page index 必须属于本次输入页。服务通过目标 Question 与 Submission 的数据库关系校验身份，再从已验证 Canonical tree 和 AnswerAlignment 中挑选来源。

**30 秒回答：** 我不让模型决定它正在回答哪道题。Question ID 从 CLI 输入并在当前 Assignment 中验证。模型 JSON 只允许 outcome、page index、bbox、证据类型和枚举原因。额外的 Question ID 或 pointer 会被拒绝。指针由服务根据已校验的 Canonical 节点生成，模型不能提交任意 JSON Pointer。

**2 分钟回答：** 如果模型返回 Question ID 或 JSON Pointer，就会把模型自述当作数据库事实。本实现把这些字段排除在 Provider schema 之外。后端从当前 Alignment 找可能的来源，再用 Canonical atom 的完整 pointer、Block ID 和页码验证存在性。空间关系只给 `source_bbox_iou`，它不代表概率或真实 Question 所有权。单一来源仍只是候选；不同题的框相交、共享素材或来源不唯一时不生成 pointer；`printed_prompt` 不会映射成答案来源。合成真实链路中，几何作答样本的模型返回印刷题干，框与 Canonical 区域 IoU 0.952；新规则保留这个几何证据但拒绝 pointer。

**连续追问：** Question 在模型执行期间被编辑怎么办？模型输出相同框但 Canonical 已变化怎么办？

**代码与验证：** [`protocol.py`](../../src/huipi_cloud/modules/visual_evidence/protocol.py) 的 `VisualEvidenceModelRegion`；[`repository.py`](../../src/huipi_cloud/modules/visual_evidence/repository.py) 的 `register_success`；测试 `test_forged_canonical_pointer_is_rejected_at_provider_boundary`、`test_region_for_another_questions_content_remains_unattributed`。

## 6. 为什么模型建议不能覆盖人工复核？

**简洁解释：** 模型输出未经人工确认，也未校准。系统把它写到独立 proposal 表和 S3 对象，不修改人工决定。

**底层原理：** P2-D2A 是追加式人工审查历史。P2-D2B 结果可能错，数据关系不应把机器推断伪装成人工确认。因此两个流程有不同表、不同 CLI 和不同协议。

**30 秒回答：** 我把机器建议和教师决定分开存。VLM proposal 用自己的 PostgreSQL 索引和私有 S3 JSON；`answer_review_decisions` 不会被调用。Proposal 的状态含 candidate 和 uncertain，不使用人工 `response_present` 或 `response_absent`。这样保留来源，也避免机器覆盖审计历史。

**2 分钟回答：** 人工复核记录包含 reviewer、revision、request id、来源版本和决定。这些字段表达的是一个受控人工判断。模型输出只代表一次推理结果，模型可能漏看笔迹或误把题干当答案。若把它写进人工复核表，读取端就难以区分真实教师确认和模型建议。P2-D2B 因此使用单独 `visual_evidence_artifacts` 轻索引和不可变 proposal JSON。proposal 的 outcome 不包含人工状态 `response_absent`，不写 `reviewer_ref`，也不生成评分就绪状态。未来教师界面可以同时读取两类数据并展示冲突，但由授权人工决定是否新增复核 revision。

**连续追问：** 如果 VLM 与人工 `response_absent` 冲突，系统当前做什么？模型 proposal 是否算复核覆盖率？

**代码与验证：** [`models.py`](../../src/huipi_cloud/modules/visual_evidence/models.py) 的 `VisualEvidenceArtifact`；[`service.py`](../../src/huipi_cloud/modules/visual_evidence/service.py) 的 `analyze_question_visual_evidence`；集成测试 `test_visual_proposal_uses_verified_sources_and_never_writes_human_review`。

## 7. 如何处理模型调用失败和超时？

**简洁解释：** Provider 设置请求超时和最多三次请求。临时网络、429 和 5xx 可以有限重试。非法 JSON 不重试，也不保存成功 proposal。

**底层原理：** 将暂时性故障与确定性合同错误分开，避免重复无效请求。失败只给稳定安全码，不输出请求、图像、Key 或模型原始异常。

**30 秒回答：** HTTPX 有连接和总请求超时。配置最多三次尝试；只对网络、timeout、429 和 5xx 进行退避重试。其他 4xx 和 Pydantic/JSON 合同错误不会重试。Provider 失败后不上传 proposal，也不写成功索引。CLI 输出安全 failure code，日志只记录异常类型。

**2 分钟回答：** VLM 服务区分 Provider 失败和持久化失败。网络、限流或 5xx 只做有界重试。proposal 对象先写私有 S3，再由 PostgreSQL 短事务重新验证来源版本并登记。Repository 在 COMMIT 前显式回滚并给出“确定回滚”结果，服务核验未索引后可以删对象。如果 COMMIT 调用或之后的 Session 清理出错，状态是未知；一次查询返回空或暂时失败都不能证明没有提交，因此绝不删除对象。若查询确认同一 request 已由竞争请求登记，返回既有记录；多出来的不可变对象先保留以待后续对账。当前没有完整垃圾回收器。

**连续追问：** 为何 429 重试而 400 不重试？如果数据库 COMMIT 结果不确定，S3 对象怎样处理？

**代码与验证：** [`provider.py`](../../src/huipi_cloud/infrastructure/visual_evidence/provider.py) 的 `_post_with_bounded_retries`、`_decode_reply`、`_decode_ollama_reply`；测试 `test_provider_uses_bounded_retry_and_reports_provider_usage`、`test_provider_does_not_retry_invalid_model_json`、`test_provider_retries_transient_timeout_only_within_configured_bound`、`test_ollama_native_provider_disables_thinking_and_validates_json_contract`。

## 8. 为什么模型置信度不能直接视为真实概率？

**简洁解释：** 协议不接受模型 confidence。`source_bbox_iou` 是两个框的几何交并比，不是 VLM 正确率或概率。

**底层原理：** 概率校准需要有代表性的标注集、明确评估单位和留出的验证数据。当前没有真实学生作业标签。IoU 只描述图像框与来源框重合多少，不衡量来源是否正确。

**30 秒回答：** 当前协议没有模型 confidence 字段。`source_bbox_iou` 是几何重叠数值，不等于来源归属概率。要报告模型正确率，需要有代表性的真实标注样本和独立测试集；当前只有合成测试，不能报告视觉 Precision/Recall 或评分门槛。

**2 分钟回答：** 生成模型即使输出 confidence，也可能因题型、图像质量、模型版本和提示语变化而失准。若要将它解释为概率，需要定义“正确”的事件，再由多位标注者建立带仲裁的真实样本集，按作业或学生隔离校准和测试，测可靠性曲线与分层表现。本轮协议拒绝模型自报 confidence。我们新增的 `source_bbox_iou` 来自两个矩形的面积公式，是可手算的空间重叠，不是置信度、模型校准分数或 Question 归属正确率。视觉 outcome 仍只是人工复核线索。

**连续追问：** 将来你会选 Platt scaling 还是 isotonic regression？标签分歧如何计入校准？

**代码与验证：** [`protocol.py`](../../src/huipi_cloud/modules/visual_evidence/protocol.py) 中无 confidence 字段；[`scripts/run_visual_evidence_vlm_e2e.py`](../../scripts/run_visual_evidence_vlm_e2e.py) 明确说明样本只用于合同验证。

## 9. 如何控制视觉模型的成本和隐私风险？

**简洁解释：** 默认关闭模型，默认只允许 loopback。服务只渲染题目关联页，并限制原文件、页数、像素和请求字节。

**底层原理：** 页面图像可能包含姓名等信息。降低暴露面需要本地优先、用户显式启用、外部数据额外授权、按需页选择和资源预算。有限重试防止失控调用。

**30 秒回答：** 成本方面，我只渲染目标题的来源页，限制每题页数、像素和上传图像总字节，并设置最大输出 token、超时和重试次数。隐私方面，默认关闭 VLM 和外部 endpoint；访问外部 HTTPS 还需显式批准学生数据。日志不写图像、Base64、Key 或完整错误。当前没有全局推理并发器，所以部署仍需限制并行 CLI 数量。

**2 分钟回答：** 一张作业图片可能包含姓名、班级和其他题目，所以模型配置默认禁用，loopback endpoint 是默认允许范围。远端模型需要两项独立配置：允许远端与授权该学生数据外发。模型 endpoint 不能由请求者提供，HTTP redirect 关闭，也不跟随模型返回的 URL。处理方面，文件先从私有 S3 流式下载并验证 SHA，限制 20 MiB、200 页、每页 12 MP、每题 5 页、JPEG 字节及总输入字节。模型输出 Token、超时和重试也有限。单次运算有硬上限，但多进程并行会叠加内存和推理成本，当前没有全局并发配额或 OS 内存沙箱；部署必须按主机资源做串行限制。

**连续追问：** 为什么只限制每次输入还不够？默认模型 endpoint 为什么仅允许本地？

**代码与验证：** [`config.py`](../../src/huipi_cloud/core/config.py) 中 `visual_evidence_*` 上限；[`provider.py`](../../src/huipi_cloud/infrastructure/visual_evidence/provider.py) 的 `_validate_endpoint`；测试 `test_disabled_provider_and_unapproved_remote_endpoint_fail_closed`；`.env.example` 的 VLM 示例配置。

## 10. 未来如何评价 VLM？

**简洁解释：** 需要获得许可并去标识的真实作业，建立双人区域标注和仲裁，再按学生或作业分组留出测试数据。

**底层原理：** 视觉候选需要同时评价是否找对区域、是否关联正确 Question、是否过度接受题干、是否正确弃权。合成图像只能验证软件合同，不能代表真实分布。

**30 秒回答：** 将来要用有授权的真实样本，由至少两位标注者标出作答、题干、共享图形和不确定区域，再做仲裁。数据集按学生或作业隔离，不能把同一份作业拆到训练和测试。指标包括区域 IoU/边界 F1、Question 归属准确率、弃权率、错误自动接受率和分层表现。现在的五个图像是生成的示例，不报告真实准确率。

**2 分钟回答：** 评测前先和教师明确什么算“存在作答证据”、什么算印刷提示、什么情况应弃权。用许可且去标识的真实作业进行双人独立标注，记录来源页、区域、证据类型、Question 归属和歧义，再由第三方仲裁。按学生、班级或作业模板切分数据，避免相同页面模板泄漏。指标不能只看候选召回率，还要看区域定位、Question 链接、自动建议的精确率、印刷题干误报率、共享图形错误归属、弃权比例和各题型/清晰度分层。自报置信度需要独立校准。任何模型或提示版本都需记录版本、输入摘要、耗时和可获得的 Token 数。当前测试中的 Fake Provider 用于验证事务和持久化，本地真实 VLM 脚本只验证生成样本上的 JSON 合同；二者均不是教学质量评估。

**连续追问：** 如何防止训练/测试数据泄漏？为什么只报告总 Precision 不够？

**代码与验证：** [`tests/visual_samples.py`](../../tests/visual_samples.py) 的五类虚构样本；`VISUAL_EVIDENCE_E2E=1 uv run pytest -q -s tests/integration/test_canonical_documents.py -m visual_evidence_e2e` 运行真实 Ollama + PostgreSQL + MinIO 闭环。2026-10-11 几何样本模型误判为 `printed_prompt`，新规则阻止候选来源指针。Canonical 是合成 MiddleJson fixture，没有运行真实 MinerU。P2-D2A 的 [`evaluate_answer_presence.py`](../../scripts/evaluate_answer_presence.py) 仅评人工标注基线，不应冒充 VLM 评测。

## 11. 为什么 COMMIT 结果未知时不能删除 S3 对象？

**简洁解释：** COMMIT 请求一旦开始，客户端异常不等于数据库回滚。结果未知时保留对象，避免删除已提交索引所指向的数据。

**底层原理：** S3 对象先上传，PostgreSQL 后登记。Repository 把写事务分成提交前工作和 COMMIT 阶段。提交前失败会显式回滚；此时对象可经数据库确认未引用后清理。COMMIT 或会话关闭失败则标成 `VisualEvidenceCommitOutcomeUnknownError`。新连接查询为空也只说明查询当时没有可见行，不能证明服务器不会随后完成 COMMIT。因此服务会在未知状态下保留对象。

**30 秒回答：** 数据库 COMMIT 的确认可能在服务端成功后丢失。客户端看到异常时不能推断事务失败。我的 Repository 只在 COMMIT 尚未开始、并已显式回滚时标为确定失败；COMMIT 阶段错误单独标为未知。遇到未知状态，不管幂等查询是空还是暂时不可用，我都保留 S3 对象。多出的对象以后对账清理，比删掉已提交记录引用的文件安全。

**2 分钟回答：** 写入顺序是先 S3 上传，再由 PostgreSQL 保存对象索引。两个系统没有共同事务。原实现用一次幂等查询判断是否删除；但数据库提交可能正在处理，或成功后确认丢失，查询短暂为空不代表事务最终失败。现在 Repository 在事务体和 COMMIT 之间设了明确边界：事务体内异常执行 rollback，返回 `VisualEvidenceRegistrationRolledBackError`；发出 COMMIT 后任何结果异常都包装为 `VisualEvidenceCommitOutcomeUnknownError`。Service 对前者查询索引并做补偿删除，对后者只尝试幂等读取，从不删除。相同 request ID 已经由竞争请求登记时，返回已有记录并保留不确定的另一个 UUID 对象。我们用事件门闩让测试中的提交稍后完成，验证空查询和暂时不可用都不会产生“数据库有索引但对象已删”。孤儿对象仍可能出现，完整保留期和对账清理留待后续设计。

**连续追问：** 为什么不等待第二次查询确认？并发输家什么时候可以删除？

**代码与验证：** [`repository.py`](../../src/huipi_cloud/modules/visual_evidence/repository.py) 的 `register_success`、`_rollback_before_commit`；[`service.py`](../../src/huipi_cloud/modules/visual_evidence/service.py) 的 `analyze_question_visual_evidence`、`_lookup_request_safely`、`_delete_unindexed_object`；集成测试 `test_unknown_commit_never_deletes_object_before_late_commit_is_resolved`、`test_commit_confirmation_lost_after_commit_returns_index_and_preserves_object`、`test_failed_postgres_registration_removes_only_unindexed_proposal_object`。

## 12. 取消 asyncio 上传是否会终止 boto3 线程？

**简洁解释：** 不会。`asyncio.to_thread` 只取消等待协程，已运行的同步线程可能继续完成 S3 请求。

**底层原理：** boto3 的 `upload_fileobj` 是同步阻塞调用。线程启动后，取消外层 Task 不会安全地中断该线程。取消后 Service 不会提交数据库索引，也不会并发删除同一 Key；上传之后可能成为未索引的孤立对象。当前只使用独立不可变 UUID Key 与 boto3 已有请求超时，没有声称实现 exactly once。

**30 秒回答：** `asyncio.cancel()` 不能杀掉正在运行的 boto3 线程。若取消上传等待，我不触发对象删除，避免线程仍在写时发生危险竞争。线程可能完成上传但不会有 PostgreSQL 成功索引；对象可能孤立。需要后续对账处理，不能说它被强制停止。

**2 分钟回答：** S3 adapter 把 boto3 放到 `asyncio.to_thread`，这防止事件循环被同步网络调用阻塞，但没有给线程增加强制中断能力。Task 被取消时 Python 会让协程停止等待，执行中的 boto3 函数还可能持有文件对象并继续读写。当前视觉 proposal 是内存中的 `BytesIO`，底层线程的 future 会持有必要对象；Service 的 `CancelledError` 不进入正常失败补偿，也不登记数据库。我们用真实 S3 客户端调用加线程 Event 阻塞，取消 Service 后确认没有发出 delete；释放事件后上传完成，MinIO 对象存在、PostgreSQL 请求索引不存在。这个安全边界允许孤立对象，未来应增加按保留期和索引引用对账的清理程序。

**连续追问：** 为什么不在取消后无限等待线程完成？如果网络上传超时，谁负责清理？

**代码与验证：** [`s3.py`](../../src/huipi_cloud/infrastructure/storage/s3.py) 的 `S3ObjectStorage.upload_fileobj`；[`service.py`](../../src/huipi_cloud/modules/visual_evidence/service.py) 的 S3 上传与登记顺序；集成测试 `test_cancelled_boto_upload_is_not_deleted_while_its_thread_is_running`。
