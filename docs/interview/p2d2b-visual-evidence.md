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

**底层原理：** 当前使用完整页面，不做裁剪。缩放不改变归一化坐标；PDF rotation 和 EXIF 信息会记录。若方向不能证明一致，程序不生成 Canonical pointer。映射还要求目标题目有唯一来源、没有别题区域重叠或共享素材冲突。

**30 秒回答：** VLM 输出 `[x0,y0,x1,y1]`，坐标范围是当前渲染页的 0 到 1 比例。proposal 记录原始页尺寸、旋转、EXIF、栅格尺寸和仿射变换。当前没有裁剪，所以全页缩放仍使用归一化坐标。只有方向可信、与一个已验证的目标题来源区域重叠，且没有跨题冲突时，服务才写候选 pointer；它不是精确匹配声明。

**2 分钟回答：** 图像坐标不能直接等同于 Canonical 坐标。PDF 可以有旋转，图片可以有 EXIF 方向，渲染也会缩放。系统在 `RenderedPageMetadata` 中保留源宽高、页单位、旋转、EXIF、输出宽高、有效 DPI、渲染图摘要和仿射矩阵。当前整页模式没有 crop，缩放前后的归一化坐标相同。服务将模型框用变换函数还原，再与目标题、其他题的已验证 source region 做空间交并比较。匹配采用保守阈值，但阈值未经真实作业校准，因此只标成 candidate。只要方向、页面来源、题目归属或共享图形有疑问，pointer 就为空，视觉框仍保存为 unresolved。

**连续追问：** 为什么 rotated PDF 页不直接用记录的矩阵映射？IoU 0.08 可以说明什么、不能说明什么？

**代码与验证：** [`render.py`](../../src/huipi_cloud/infrastructure/visual_evidence/render.py) 的 `_rotation_display_to_source`、`_exif_display_to_source`、`transform_bbox_to_source`；[`service.py`](../../src/huipi_cloud/modules/visual_evidence/service.py) 的 `_boxes_match`；`test_affine_transform_restores_crop_and_rejects_outside_bbox` 和方向测试。

## 5. 如何防止 VLM 编造 Question ID 和来源指针？

**简洁解释：** 模型输入目标 Question 由调用方固定。模型输出 schema 不含 Question ID、Canonical pointer 或 URL。后端从已验证数据中生成候选 pointer。

**底层原理：** `extra="forbid"` 的严格 Pydantic 模型拒绝未知字段。page index 必须属于本次输入页。服务通过目标 Question 与 Submission 的数据库关系校验身份，再从已验证 Canonical tree 和 AnswerAlignment 中挑选来源。

**30 秒回答：** 我不让模型决定它正在回答哪道题。Question ID 从 CLI 输入并在当前 Assignment 中验证。模型 JSON 只允许 outcome、page index、bbox、证据类型和枚举原因。额外的 Question ID 或 pointer 会被拒绝。指针由服务根据已校验的 Canonical 节点生成，模型不能提交任意 JSON Pointer。

**2 分钟回答：** 如果模型返回 Question ID，我们就会把模型自述当作数据库事实；如果模型返回 JSON Pointer，它也可能指向不存在或另一个题目的内容。本实现把这些字段排除在 Provider schema 之外，并用 `extra="forbid"` 拒绝未知字段。输入页面索引也会验证为本次选中的范围。调用服务会将 `question_id` 与 Submission 的 Assignment Question 集合匹配。完成模型调用后，系统从当前 Alignment source regions 找候选，再通过 Canonical atom 的完整 pointer、Block ID 和页码确认该节点存在。即使匹配成功，状态也只叫 candidate。跨题、共享图片、方向不明或多个来源都重叠时不会给确定归属。

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

**2 分钟回答：** VLM 服务的故障分为可恢复和不可恢复。网络中断、限流或服务端 5xx 可能短暂发生，因此在配置的最大次数内进行退避。HTTP 重定向关闭；OpenAI-compatible endpoint 必须是 loopback，或使用显式允许且有学生数据授权的 HTTPS。Ollama 原生适配器只允许本机 HTTP loopback，并使用 `think=false` 避免思考内容耗尽本地上下文。4xx 不视为临时错误；JSON 缺字段、额外字段、非法 outcome、越界 bbox 等属于模型输出合同错误，重试通常不能修复，因此立即失败。输出大小也有上限。失败不会被改写成 `uncertain` 成功结果，更不会伪造 proposal。若对象写入后数据库登记失败，会先查询幂等记录；无法确认 COMMIT 时保留对象以免删除已被引用数据。

**连续追问：** 为何 429 重试而 400 不重试？如果数据库 COMMIT 结果不确定，S3 对象怎样处理？

**代码与验证：** [`provider.py`](../../src/huipi_cloud/infrastructure/visual_evidence/provider.py) 的 `_post_with_bounded_retries`、`_decode_reply`、`_decode_ollama_reply`；测试 `test_provider_uses_bounded_retry_and_reports_provider_usage`、`test_provider_does_not_retry_invalid_model_json`、`test_provider_retries_transient_timeout_only_within_configured_bound`、`test_ollama_native_provider_disables_thinking_and_validates_json_contract`。

## 8. 为什么模型置信度不能直接视为真实概率？

**简洁解释：** 模型自报分数没有与人工标签校准。0.9 不代表 90% 的真实正确率。

**底层原理：** 概率校准需要有代表性的标注集、明确评估单位和留出的验证数据。当前只有合成图像合同测试，没有真实学生作业标签，也没有模型置信度字段。

**30 秒回答：** 当前没有把模型自报置信度当作真实概率。协议没有 confidence 字段。要解释概率，必须用有代表性的真实标注集做校准和分布外测试。本轮只有虚构样本的模型调用合同检查，不能报告视觉 Precision/Recall，也不能把结果当作评分门槛。

**2 分钟回答：** 生成模型即使输出一个 confidence，也只是模型自我报告的数值，可能因题型、图片质量、模型版本和提示语变化而失准。若要将它解释为概率，需要先定义正例，例如“有可确认的学生回应区域”，再由多位标注者建立带仲裁的真实样本集，按作业/学生隔离训练、校准和测试。之后要测 Brier score、可靠性曲线和不同题型/图片质量分层，同时监控漂移。本轮没有真实标签，也没有统计校准流程，因此协议根本不接受置信度。机器 outcome 只能作为人工复核线索。

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

**代码与验证：** [`tests/visual_samples.py`](../../tests/visual_samples.py) 的五类虚构样本；[`scripts/run_visual_evidence_vlm_e2e.py`](../../scripts/run_visual_evidence_vlm_e2e.py) 的真实调用统计。2026-10-10 本地 Ollama 0.31.2 + `qwen3.5:4b` 五个响应均通过协议校验，但几何标记样本误判为仅印刷题干，来源映射未测。P2-D2A 的 [`evaluate_answer_presence.py`](../../scripts/evaluate_answer_presence.py) 仅评人工标注基线，不应冒充 VLM 评测。
