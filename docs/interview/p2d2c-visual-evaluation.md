# P2-D2C：视觉作答证据质量评测面试复盘

## 本轮事实

- 数据集：`datasets/visual-evidence/synthetic-v1/dataset.json`，26 个合成案例、27 张图像；14 个 `response_present`、7 个 `response_absent`、5 个 `uncertain`。
- 来源：程序绘制图像，区域框由生成配方标注并经图片核对。模拟蓝色笔画是程序绘制，不是真人手写。公开仓库中不放真实学生作业。
- qwen3.5:4b：26/26 结构协议通过。21 个确定标签样本的存在性 TP=8、FP=0、FN=6、TN=7；Precision=8/8，Recall=8/14。IoU 0.5 的目标作答区域定位 TP=0、FP=8、FN=14。定位结果很差，不能靠调阈值隐藏。
- Candidate Question Link 与 Automatic Accept 指标因数据没有可信 Canonical/Alignment 指针标签而为 `not_evaluated`。
- 真实 MinerU 4.0.10 → Canonical → AnswerAlignment → qwen3.5:4b → Proposal S3/PG 专项测试通过。MinerU 和 Canonical 来源验证真实；VLM 输出的区域最终 `unresolved`，来源框 IoU 为 0.0。Proposal SHA/大小正确，人工复核行数没有变化。
- 相关实现：`src/huipi_cloud/modules/visual_evaluation/protocol.py`、`metrics.py`、`scripts/build_visual_evaluation_dataset.py`、`scripts/evaluate_visual_evidence.py`、`tests/integration/test_mineru_e2e.py`、`src/huipi_cloud/modules/visual_evidence/service.py`。

## 1. 如何评价 VLM 识别学生作答的能力？

**简洁技术解释：**分开测作答存在性、区域定位和 Question 来源映射。只有对应的人工真值存在时才计算指标，并同时报告原始计数与分母。

**底层原理：**案例级存在性用 TP/FP/FN/TN。区域定位在同一案例和页内使用 IoU 阈值及确定性一对一匹配。来源映射需要 Canonical/Alignment 的可信标注；没有时输出 `not_evaluated`。一次 JSON 协议通过只说明输出结构合法。

**30 秒回答：**

我把识别任务拆成三层：有没有作答证据、模型框有没有覆盖人工标注区域、这个框能否关联到正确题目来源。每层使用不同的标签和指标。比如这轮 26 个合成案例中，JSON 全部合法，但区域 IoU 定位没有一次超过 0.5，所以我不会把协议通过率说成识别准确率。来源映射因为没有可信 pointer 真值，明确记为未评估。

**2 分钟回答：**

数据集是版本化的，每张图片都有相对路径、实际格式、尺寸和 SHA-256。人工标签独立保存，按 `response_present`、`response_absent` 和 `uncertain` 区分。确定标签案例计算案例级混淆矩阵，并报告 precision、recall、FPR、FNR、覆盖率和弃权率。区域任务只把目标 Question 的人工 response 区域与非印刷题干的预测框比较，同一页内按 IoU 阈值做一对一匹配，避免一个预测框重复命中多个标注。Question 来源则是另一条链：模型自身不能给 Question ID 或 Canonical pointer，服务用当前 Canonical/Alignment 做候选映射；评测必须有可信来源指针标签才可算 P/R。当前合成视觉集没有这些标签，所以这项不评估。三个层次分别报告，不能让 JSON 有效或候选状态当成整条业务正确。

**连续追问：**为什么不只报一个总准确率？为什么 uncertain 不并到负类？协议有效率能代表什么？

**代码与演示：**`metrics.py::evaluate_visual_predictions`；运行 `uv run python scripts/evaluate_visual_evidence.py --evaluate-predictions datasets/visual-evidence/runs/qwen3.5-4b-synthetic-v1.json`。

## 2. Precision 和 Recall 为什么可能相互影响？

**简洁技术解释：**收紧自动接受条件通常会减少错误接受，也会让更多真实作答漏掉或转人工，因此 Precision 可能提高而 Recall 下降。

**底层原理：**Precision 的分母是预测正例，Recall 的分母是真实正例。阈值或拒绝策略改变了 TP、FP、FN 的比例。没有概率校准或真实分布时，不能承诺某一阈值达到某个业务质量。

**30 秒回答：**

Precision 看被系统报为有作答的区域中有多少是真的；Recall 看真实有作答的区域中找回多少。更保守的判定可能减少误报，但会漏掉模糊或小字迹。本轮没有为了提高一个数字去改模型提示或人工标签，而是保留固定 IoU=0.5，把 TP、FP、FN 和分母一起报告。

**2 分钟回答：**

假设视觉模型只接受边界很清楚的框，误报会减少，precision 有机会提升；但小公式、浅色笔迹和几何标记也可能被拒绝，FN 增加，recall 会下降。相反，放宽匹配阈值可能让更多框被计为 TP，但也可能把只擦到边的框算作正确。因此阈值必须先定义并固定，最好在开发集上选择，再用独立留出集评估。当前 26 例数据只做合成基线，IoU 0.5 是显式报告设置，没有据它调模型。真实部署前还要有授权的真实样本、双人标注、分歧仲裁和按学科/版式分层分析。

**连续追问：**IoU 阈值从 0.5 改到 0.3 会怎样？能否通过调阈值让结果变好？如何避免在测试集上反复调参？

**代码与演示：**`metrics.py::_ratio`、`_candidate_detection_metrics`、`_region_metrics`；单测 `test_response_region_candidate_detection_has_separate_hand_calculable_counts`。

## 3. 为什么弃权率重要？

**简洁技术解释：**弃权不是正确负例。系统可以把难例全部交给人工，同时只在少量容易样本上报高 Precision，所以必须一起报告覆盖率和弃权率。

**底层原理：**已接受子集质量只适用于模型实际给出确定判断的样本。覆盖率衡量被自动判断比例，弃权率衡量不确定/失败比例。单看 accepted subset 会隐藏大量未处理样本。

**30 秒回答：**

如果模型对大多数案例都回答“不确定”，剩下少量简单案例可能有很高 Precision，但系统实际帮到的样本很少。因此我同时报告确定判断覆盖率、模型弃权率和已接受子集的混淆矩阵。人工标签本身为 uncertain 的案例会从存在性主分母排除，不被模型的候选负类吞掉。

**2 分钟回答：**

在实际教师工作流里，弃权一般会转人工复核，不等于无作答。若只在成功输出中算分类质量，模型可以通过不回答难例来优化数字。当前报告包含 21 个确定标签案例和 5 个 uncertain 标签案例；模型对全部 26 个都给了候选输出，确定标签覆盖率 21/21，模型弃权率 0/26。这里的 100% 覆盖只意味着有输出，不代表准确；Recall 仍只有 8/14。请求失败、协议失败和显式 uncertain 都不能算 TN。

**连续追问：**覆盖率和可用率有何区别？模型调用失败如何计数？人工 uncertain 如何处理？

**代码与演示：**`metrics.py::_presence_metrics`；手算测试 `test_presence_confusion_matrix_and_abstention_are_hand_calculable`。

## 4. BBox IoU 衡量什么，不能衡量什么？

**简洁技术解释：**IoU 是两个矩形交集面积除以并集面积，衡量位置重合，不衡量区域语义、题目归属或答案正确性。

**底层原理：**框的归一化坐标按页面宽高比例解释，区域匹配还要求页索引相同。一对一匹配避免预测框和人工区域重复计数。错位坐标、框类型错误或框住整段印刷内容都可能有高或低 IoU，但这本身不说明模型理解正确。

**30 秒回答：**

IoU 只回答“两个框重合多少”。它不说明框里面是不是学生答案，也不说明来源属于哪道题。我们固定按页面比较，报告阈值和未匹配数。本轮模型的 response 区域没有一个框达到 0.5，所以定位 TP 是 0；存在性 P/R 较高并不能掩盖定位失败。

**2 分钟回答：**

设预测框和人工框面积分别为 A、B，交集为 I，IoU=I/(A+B-I)。坐标是页面归一化的 `[x0,y0,x1,y1]` 半开框。我们不跨页面匹配，也不把一个预测框匹配到多个人工框。一个宽框可能覆盖真实笔迹却包含大量无关印刷区，因此 IoU 会低；反过来，框几何重合也无法证明内容类型正确或 Question 归属可信。本轮使用 IoU=0.5 作为明确的测量门槛，另外报告 best IoU 的均值，方便看到“没有达阈值但离得多远”。来源映射由后端与已验证 Canonical 区域另行判断，不能用评测 BBox 代替。

**连续追问：**多框怎么匹配？为什么不是只选模型第一框？什么是半开框？

**代码与演示：**`metrics.py::box_iou`、`_match_regions`；手算 IoU 单测 `test_bbox_iou_and_all_region_matching_are_hand_calculable`。

## 5. 如何区分 OCR 错误、来源对齐错误和 VLM 错误？

**简洁技术解释：**按数据流逐层核对原页、MinerU MiddleJson、Canonical、AnswerAlignment 和 VLM Proposal 的摘要与结果，不把某一层的问题归给另一层。

**底层原理：**原始页图像是视觉参照；MinerU 决定解析文本/布局；Canonical 保存结构；AnswerAlignment 决定题号候选及来源区域；VLM 只对指定页面返回候选框和证据类型。每层分别记录 SHA、状态和安全错误码。

**30 秒回答：**

我会先看真实页面是否有内容，再看 MinerU 输出有没有识别题号和区域；之后检查 Canonical 是否保留来源节点，再看 AnswerAlignment 是否按真实规则关联到 Question；最后才看 VLM 的标签和框。P2-D2C 的真实专项里 MinerU 解析与 alignment 都通过，Proposal 也持久化成功，但视觉框和 Canonical 对齐文本框 IoU 为 0，服务正确保留 unresolved。这不是矿工识别失败，也不是映射成功。

**2 分钟回答：**

我用每层的输入输出做边界。MinerU 阶段核对 ParsedArtifact 的解析器版本、源文件 SHA、MiddleJson/Markdown 是否含有题号文本。Canonical 阶段核对它确实指向本次 ParsedArtifact，结构和 SHA 验证通过。AnswerAlignment 阶段查看 Question candidate、matching_status、pointer 和切片；状态必须来自真实算法而非测试注入。VLM 阶段检查 provider 响应协议、模型输出框和标签。服务映射阶段再检查 PDF/EXIF 坐标资格、来源框 IoU、目标 Question 是否唯一；无证据时应该 unresolved。最后看 S3/PG proposal 摘要与原始数据一致。每一层有独立证据，不因为一个最终状态失败就模糊归因。

**连续追问：**如果 MinerU Markdown 有题号但 Canonical 对齐找不到，问题在哪？图像 caption 内嵌在 image 节点是否能对齐？

**代码与演示：**`tests/integration/test_mineru_e2e.py::test_real_mineru_to_canonical_alignment_ollama_proposal_e2e`、`canonical_documents/service.py`、`answer_alignment/{detector,segmenter,service}.py`、`visual_evidence/service.py`。

## 6. 如何避免数据泄露和数据集污染？

**简洁技术解释：**公开评测集只放合成图；真实数据须经授权、去标识化、受控存储和访问审查。图片 hash、尺寸和固定版本可发现文件变化，但不能代替隐私保护。

**底层原理：**模型输入可能含姓名、学校、笔迹和其他敏感信息。将真实数据发送至远程 Provider 需要明确授权。训练/开发/测试应按学生、作业来源或版式隔离，避免近重复样本造成泄漏。

**30 秒回答：**

公开仓库只包含程序生成的合成页，模型输出也不写 Base64 或原图。真实作业进入远程模型前需要授权，默认 Provider 限制到 loopback。数据集图片以 hash 和尺寸固定，预测记录与人工标签分开。真正做真实评测还需要独立划分样本，避免同一作业或几乎相同的模板同时出现在训练和测试里。

**2 分钟回答：**

保护分三个方面：来源、传输和评测隔离。来源上本轮只公开无真实学生资料的程序合成 PDF/PNG；授权真实样本应留在受控存储，并执行必要的去标识化。传输上默认只允许本地 Ollama；使用远程 HTTPS 时必须显式启用 endpoint 和数据授权开关。记录中只保存文件 hash、输入摘要、标签和模型结构化输出，不放图像字节、Base64、Key 或完整个人身份。评测上固定 dataset manifest 和 digest，人工标签与预测分离；将来应按学生/作业批次/模板分组划分，审计重复页和相同原图，避免测试集污染。去标识化不等于匿名，仍要限制访问和保留周期。

**连续追问：**SHA-256 会不会泄露图片身份？仅遮掉姓名是否够？怎样划分重复模板？

**代码与演示：**`protocol.py::load_dataset`、`scripts/evaluate_visual_evidence.py::_input_digest`；仓库中的 `datasets/visual-evidence/synthetic-v1/`。

## 7. 如何比较不同 VLM 的质量与延迟？

**简洁技术解释：**同一数据集版本、Prompt 语义、预处理、预算和指标下比较，并分开报告协议通过、识别、定位、来源映射、延迟和 token。

**底层原理：**模型间输出格式能力、采样参数和思考 token 可能不同。必须固定或记录这些条件。平均值不能代替分位数；token 也不是可直接换算的真实成本。

**30 秒回答：**

我会锁定相同图片 hash、模型可用的等价 Prompt、页面预处理、请求上限和 IoU 指标，再分别比较作答存在性、区域定位、协议失败、P50/P95 延迟和 token 用量。如果某个接口不支持相同 JSON 限制，记录为实验条件差异，不直接把它说成模型优劣。当前只完成 qwen3.5:4b 基线，没有做模型排行或成本估算。

**2 分钟回答：**

每个预测记录包含模型 ID、Prompt 和 preprocessing 版本、每页输入 SHA/尺寸、请求延迟及 provider 返回的 token 计数。对照模型时先固定数据集 manifest digest、问题文字、渲染 DPI/页面数、输出 token 预算和采样参数。报告每个任务的分子分母，而不是单一综合分；失败和弃权另计。延迟至少报平均、P50 和 P95，并说明机器与并发条件。API 的 token 费用需要实际价格和账单来源，当前只有 token 计数，没有成本数据。模型数增加不会自动提高证据质量。

**连续追问：**硬件不同如何比较？失败请求纳入延迟分布吗？token 数能代表成本吗？

**代码与演示：**预测合同 `scripts/evaluate_visual_evidence.py::_run_model`；统计 `metrics.py::_quality_metrics`。

## 8. 为什么合成样本准确率不代表真实学生作业？

**简洁技术解释：**程序图形与真人笔迹在书写、扫描、压缩、污渍、拍摄和版式分布上不同，因此合成表现只验证工具链和受控边界案例。

**底层原理：**合成样本与模型训练分布可能高度相似，且标签通常由绘图规则直接确定。没有真实数据分布和人工仲裁，就不能推断泛化准确率。

**30 秒回答：**

这些 26 张页是程序绘制的，蓝色线条不是真人手写。它们可以验证数据合同、区域计数和已知边界场景，也能复现当前模型把几何标记误判为印刷内容的问题，但不能预测真实高中作业中的笔迹和扫描条件。真实准确率需要有授权的真实作业和双人标签。

**2 分钟回答：**

合成图的字体、线条、颜色、几何位置和噪声都是人工控制的，样本分布和真实学生作业不同。模型可能识别合成蓝线而不认识铅笔字，也可能因为 watermark 或整齐画布学到捷径。标签来源是生成程序和人工核对，不等于对真实作业进行独立判断。合成集适合做协议回归、资源预算、安全拒绝和机制覆盖，也能提供固定 baseline；它不适合对外声称教学识别率、不同学校表现或模型可上线。下一步须获取经许可样本，按学科、纸张、笔迹、扫描质量分层，留出独立测试集并进行人工仲裁。

**连续追问：**合成集还有什么价值？怎样减少分布偏差？可以用它调参吗？

**代码与演示：**`scripts/build_visual_evaluation_dataset.py`；数据协议将 `dataset_kind` 和 `annotation_provenance` 固定写入 manifest。

## 9. 如何进行人工标注与分歧仲裁？

**简洁技术解释：**标注指南先定义作答存在、模糊和 Question 归属；至少两位标注者独立标注关键真实样本，分歧由仲裁者复核并保留理由和版本。

**底层原理：**同一页面可能有印刷解答、草稿、涂改、共享图形或模糊笔迹。单人标签会把主观判断混进指标。记录区域类型、题目归属、不确定和标注依据可追溯争议。

**30 秒回答：**

真实数据集不能只让一个人快速画框。我会先写标注手册，区分确认作答、确认没有作答、无法判断，以及题目归属和区域类型。两名标注者独立标注，按类别和边界统计分歧；仲裁者查看原页与规则，记录决策和版本。无法确认的案例保留 uncertain，不为了覆盖率硬标。

**2 分钟回答：**

先确定样本纳入和隐私流程，然后定义 `response_present`、`response_absent`、`uncertain` 的判定边界，说明涂改、草稿、印刷解答、表格填写和共享图形怎么标。Question 归属与作答存在性分开标；区域框规则统一使用页面坐标、页索引和半开框。两位标注者在不知道模型输出的情况下独立完成，记录案例级一致性和区域框 IoU 差异。仲裁者对高分歧样本复核原页，给出依据；无法确认的内容保留 uncertain。最后冻结标签版本、图片 hash 和仲裁记录，测试集不再用于调 Prompt。当前仓库只有合成标签，还没有真实标注平台、双人仲裁或真实作业数据。

**连续追问：**为什么标注前要隐藏模型输出？不同标注者框略有偏差怎么办？不确定样本要不要删？

**代码与演示：**`protocol.py::VisualEvaluationCase`、`GroundTruthRegion` 和 `load_dataset`；`dataset.json` 中的 `annotation_provenance`。

## 10. 未来怎样把可信视觉证据提供给批改系统？

**简洁技术解释：**先由视觉模型生成有版本、有来源和不确定原因的候选，再由人工复核确认；只有通过明确业务规则的结构化证据才能进入评分流程。

**底层原理：**`aligned` 只表示来源与 Question 关联，VLM proposal 只表示机器建议，人工 `response_present` 是另一条记录。需要把 Canonical/Alignment/原图/模型输入/人工决定用 hash 绑定，拒绝过期或不可追溯证据。

**30 秒回答：**

不会把模型的 candidate 直接当成答案交给评分。视觉候选、AnswerAlignment 和人工复核是不同数据层。后续要确认题目归属、来源版本、作答存在性和 OCR 完整性；模糊或共享图形继续人工复核。评分输入应包含可追溯来源和人工决定，并校验 Canonical、Alignment 和原文件版本没有变化。现在还没有自动批改功能。

**2 分钟回答：**

将来评分输入需要是受约束的证据对象，而不是一段模型自由生成文本。它应绑定 Submission 原文件 SHA、Canonical ID/SHA、Alignment ID/SHA、Question digest、视觉 proposal 模型与 preprocessing 版本，以及人工复核决定 revision。视觉模型只提供候选框和类别，服务映射到现有 Canonical pointer；若变换不可逆、Question 来源冲突或共享素材无法归属，则保持 unresolved。教师确认后，评分 Agent 才能在对应 Rubric 版本下读取已确认区域。任何来源更新都使旧证据失效并要求重审。还要记录评分理由和人工覆盖，建立真实数据离线评测及审计。P2-D2C 仅做评测和链路验证，不实现这条评分路径。

**连续追问：**模型建议什么时候过期？人工修改 Question 后怎么办？什么条件能叫 `ready_for_grading`？

**代码与演示：**`visual_evidence/service.py::analyze_question_visual_evidence`、`answer_alignment/service.py::load_verified_alignment_bundle`、`answer_review` 模块。当前系统没有 `ready_for_grading` 状态。
