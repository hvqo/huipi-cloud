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

逐例配对、语义待复核清单、原始回复、用量、耗时和冻结SHA保存在上述运行目录。最终数值记录将在运行结束后补充；人工审核状态一律unreviewed，不冒充教师审核，不作为发布成绩或进入生产集成的授权。
