# Rubric语义一致性实验

本实验只测试独立的 `rubric-quote-selection-v2`。生产评分仍使用既有严格协议，Quote Selection没有进入业务默认入口。新提示词不改JSON Schema，不改Decimal上限、正分Quote、唯一原文定位和来源版本校验，也不写GradeProposal或GradeReview。

## 提示词与指纹

保留v1为默认版本。v2增加逐Criterion条件识别、实际内容/正确性/完整性判断、部分分规则，以及输出前理由/引用/分值一致性检查。它明确区分相关文字与语义关系、学生实际换算与模型补算、错误步骤与正确计算；同时保留Rubric允许的等值、同义、独立结果分和部分分。规则是通用描述，没有列出四个v3案例的答案。

回复仍只包含既有Criterion分值、理由和证据。内部检查不增加回复字段，不要求输出思考过程。Prompt版本和完整system/user内容参与每例SHA；实验指纹由Provider generation_config_digest及Prompt版本生成。Provider配置摘要在A/B之间保持一致，实验指纹则随Prompt版本变化。未知版本或不匹配的输入、Prompt、配置及数据SHA会拒绝复算。历史v1对象可继续校验。

## 请求前锁定

- [v3开发回归切片](../../data/evaluation/grading/rubric-semantics-regression-v1.json)：20条，4条已知问题、15条历史正确对照、1条重复Quote案例。v3已用于调试，这组不再是新Holdout。排除两条标签争议案例。
- [独立v4合成留出集](../../data/evaluation/grading/rubric-cases-v4-semantics.json)：20条计分案例和2条来源不确定探针。所有正分参考逐项提供原文证据，Decimal总和经数据协议校验。包含正确/错误计算、独立结果分、明确部分分、单位换算、同义关系、关键词、化学、跨页和注入内容。标签是synthetic_curated_reference，未经教师双评。
- [预注册条件](../../data/evaluation/grading/rubric-semantics-acceptance-v1.json)：Provider与协议至少95%，候选覆盖至少90%，全部原始Quote定位至少95%；四条已知风险需全部分项正确，15条普通对照不允许新退化。独立留出总分MAE≤0.50、Criterion MAE≤0.25、总分/逐项一致率≥90%，不允许零分误给正分、错误/缺失步骤误给分，两条不确定探针都须弃权。合格人工语义审核是另一个必需条件。
- [冻结与费用预检](../../data/evaluation/grading/rubric-semantics-2026-10-14-run-01/experiment-freeze.json)：64次新请求，合计保守上界USD 0.4135242，预算USD 0.50。高峰价格预检来源为[DeepSeek官方价格表](https://api-docs.deepseek.com/quick_start/pricing/)。价格会变化，实际账单与估算不同。

回归A来自不可变v3历史响应，B实际请求v2。独立留出A/B均各请求一次。固定deepseek-flash、Thinking disabled、4096输出Token、120秒、零重试。两组顺序运行，回归历史A更早；单次输出和云端服务可能波动，不能将差异全部归因于Prompt。未运行Thinking enabled实验。模型只收到合成题目、标准答案、Rubric和学生片段，参考分/理由不进Prompt。

## 已验证的来源准备边界

真实PostgreSQL独立测试库和S3测试桶中，Canonical对象丢失/篡改及Alignment对象丢失均导致source_stale并阻止生成GradeInput。教师确认的含图片、表格内容会被unsupported_modality阻止；图片底层对象删除后也不能转换成文本输入。纯图片同样被既有提取门禁阻止。缺少Canonical资产清单引用在协议解析时拒绝，空白文本与找不到节点有不同错误。response_absent/uncertain/unreviewed保持人工复核状态，不产生学生零分。

需要注意两种当前元数据边界：

1. Question没有可信的required_materials声明。已确认纯文本可以准备，即使题干提到附图；系统不能可靠推断该题图像是否必需。对此不能仅由合成探针文本断言门禁绕过。
2. GradeInput校验消费的是不可变Canonical/Alignment快照和来源元数据。管理员替换原始S3对象而未更新元数据时，检查不会重新下载原文件，已验证的Canonical文本仍可准备。Canonical自身的实际字节变更则立即被哈希检查阻止。此限制需要对象不可变权限/版本控制，或在确需消费原图时再次核验原始对象SHA。

未发现可把已知不支持的确认素材伪装为ready_text的新增P0，本轮不改GradeInput 1.0。生产集成前，建议教师显式声明本题必须的模态及必要素材，绑定Question版本、Canonical版本、素材标识与SHA；声明不完整、素材未就绪或模态不支持时拒绝调用模型或转人工。该Teacher Requirement提议不依据题干关键词猜测，不自动给0分，也没有在本轮实现。

## 操作与结果记录

使用 `scripts/evaluate_quote_selection.py --prompt-version rubric-quote-selection-v2`，模型运行需要`--standalone`、新输出目录和预注册manifest。先执行dry-run，审核预检后才用合成数据授权开关执行。`.env`不修改。每例最多一次收费调用，失败保存原回复、状态和缺失Token；默认CI只用Fake/Mock Provider。

逐例配对、语义待复核清单、原始回复、用量、耗时和冻结SHA保存在上述运行目录。以下数值来自冻结原始回复离线重算；人工审核状态一律unreviewed，不冒充教师审核，不作为发布成绩或进入生产集成的授权。

## 冻结实验实际结果（2026-10-14）

模型请求前的冻结提交为 `3dd11cd`。64次新请求均返回deepseek-flash，Provider响应/JSON解析/严格Quote Selection协议均为64/64。没有自动重试、超时、截断或缺失输入/输出Token。v3历史A的20次响应仅复用，未再次计入本轮收费调用。请求发生在同一天的非高峰时段，精确观察窗口和原始文件SHA见运行完整性清单；该窗口是客户端观察，不是服务端时间戳。

### 固定v3回归：仅修复两例，出现一例正常对照退化

- A（历史v1）：候选19/20；总分一致15/19，MAE15/19=0.7895，4高估、0低估；Criterion一致40/44，MAE15/44=0.3409。原始Quote唯一定位38/39，被拒绝候选保留quote_ambiguous。
- B（v2）：候选19/20；总分一致16/19，MAE13/19=0.6842，2高估、1低估；Criterion一致41/44，MAE13/44=0.2955。原始Quote唯一定位35/36；被接受候选内34/34。重复Quote案例仍被拒绝，没有修补或删除失败引用。
- `v3-linear-arithmetic-slip`：A=8、B=8、参考=5。除法Criterion仍给3/3；v2理由甚至把21÷7得到3.5称为“计算正确”，但最终值项又给0分。属于算术正确性误判及逐项判断不一致。
- `v3-mixed-right-wrong`：A=8、B=5、参考=5。错误除法项由3分降为0分，保留正确移项5分。
- `v3-partial-quarter`：A=10、B=10、参考=5。换算项仍给5/5，理由将可由75000cm换算出的750m当作学生已完成；该学生未写换算结果。
- `v3-english-keyword`：A=4、B=0、参考=0。oxygen关键词不再被当作产物关系解释。
- 15条历史正确普通对照中14条保持逐项正确，`v3-chinese-paraphrase`由10分降到5分；学习/思考结合项被判0，局限项保留5分。它是相对冻结参考的低估与语义解释分歧，尚未教师仲裁，不能将字面省略直接当作语义缺失。
- 三条零分参考案例的有效候选正分由A的1/3降为B的0/3，且没有额外弃权。总体MAE稍降并不表示新版更可靠：未达到四例全部修复且普通对照不退化的预注册要求。

### 独立v4：v1已到样本上限，v2增加一例低估

- A（v1）：计分候选20/20，总分20/20完全一致，MAE0；Criterion39/39一致，MAE0。原始Quote31/31唯一定位。
- B（v2）：计分候选20/20，总分19/20完全一致，MAE5/20=0.2500，0高估、1低估；Criterion38/39一致，MAE5/39=0.1282。原始Quote32/32唯一定位。
- 两组来源不确定探针均2/2弃权，不进入计分分母。零分参考误给正分均0/3，预注册错误/缺失步骤切片没有正向误给分。
- v2唯一偏差为`h4-osmosis-paraphrase`：参考10，v1=10，v2=5。Rubric明确允许外界盐浓度驱动力的通俗同义表达；模型把“外面盐更多，吸水能力强”说成与向外失水方向相反，拒绝驱动力项，保留向外流动项。按冻结合成参考属于低估，但教学语义仍待人工审读。
- 7条满分参考案例中v1没有扣分，v2有1条扣分，没有用大量弃权或全部零分规避高估。v1在这个小样本组上已全部正确，因此该组没有显示v2改进，反而观察到一个低估；不能把它描述为新方案整体提升。

v2的预注册数值门槛在独立组上通过，但开发回归的“四条已知问题全部修复”和“普通对照无退化”失败；人工语义审核也未完成。**不建议进入P3-D2B，不切换生产默认Prompt。**未追加Thinking enabled运行，也没有依据本轮结果调整标签或再次请求直到得分正确。

### 用量、费用与语义待审核

本轮实际只有三个新运行：回归B20次（prompt26578/completion5219，客户端耗时合计34603ms）、留出A22次（18429/4655，33972ms）、留出B22次（29011/4868，33475ms）。合计输入74018、输出14742，缓存命中32000、未命中42018；耗时合计102050ms，64次平均1594.53ms。输入/输出/缓存用量均有返回，未将缓存量再次加到prompt总量。

按[官方非高峰价格](https://api-docs.deepseek.com/quick_start/pricing/)命中$0.003/M、未命中$0.15/M、输出$0.60/M估算约 **USD0.0152439**；按高峰全部输入不缓存保守估算 **USD0.0398958**。这是用量估算，不是账户账单。请求前64次合计最坏Token预检为USD0.4135242，低于USD0.50。本地.env及API Key未修改，远程与合成数据授权只在调用进程中临时启用，没有传输真实学生作业。

v2两组有效候选共有39条、51个正分Criterion需要人工语义审核。检查清单保留84条组内记录（20条历史A、64条新请求），全部状态unreviewed，包含被拒绝候选和不确定探针。它没有写入GradeReviewRevision。自动定位只证明来源字面存在，不能证明Quote充分支持评分。

[逐例对照摘要](../../data/evaluation/grading/rubric-semantics-2026-10-14-run-01/semantics-summary.json)、[受控合成人工清单](../../data/evaluation/grading/rubric-semantics-2026-10-14-run-01/semantic-human-review.json)和[完整性清单](../../data/evaluation/grading/rubric-semantics-2026-10-14-run-01/run-integrity.json)绑定数据/Prompt/配置及原始预测SHA。原始模型回复保存在三个独立子目录，不覆盖v1/v2/v3历史运行。

下一轮应优先解决算术事实校核、学生已写步骤与模型可推导步骤的区别，以及同义表达是否覆盖Criterion的人工审定。任何后续Prompt调整需另立版本、保留本轮失败并在新独立样本验证；不能只针对四条题目强化提示，也不能用减少给分取代评分准确性。
