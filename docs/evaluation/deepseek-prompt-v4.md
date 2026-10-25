# DeepSeek Flash 评分协议 v4：第一轮固定合成验收

## 范围与冻结

2026-10-24（北京时间约20:23）完成12次真实 DeepSeek Flash 文本评分请求，零自动重试。只发送合成学生作答、标准答案和 Rubric；无真实学生资料、模型下载、其他收费模型或开发数据库访问。用户已有远程调用与外部数据授权开关和 Key 均在调用前检查，本轮不修改 `.env`。

代码基于 PR52 的 `4a84e289d7ed169a1ca48e0b608b33e31cd393a7`，数据与 Prompt 在真实调用前冻结于 `35b9534`。未找到用户所称 `huipi-deepseek-prompt-v4.patch`，因此没有可执行 `git apply --check` 的补丁；按要求实现同等范围改动，没有声称已应用该文件。

- 新增 `prompts_v4.py`，正式 GradeProposal 和考试 ScoringWorker 使用 `rubric-candidate-v4`。
- 原 `prompts.py` 的 v3 字节不变，旧评测和 Quote Selection 实验仍使用它。
- 不变项：GradeProposal 1.0、Provider、Quote/Decimal/来源校验器、数据库模型和迁移、人工审核规则。
- 不将 Quote Selection 实验作为生产默认，也不将失败模型响应修补为合格候选。

固定12例包含上轮6例、4例冻结 v2 补充案例、1例重复引用及1例不确定材料探针。11例有参考分，1例无可确定分数。参考标签在请求前冻结，来源为 `synthetic_curated_reference`，不是合格教师双评。

[数据集](../../data/evaluation/grading/deepseek-prompt-v4-smoke-v1.json) SHA-256：

```text
43215a6deae4bc8f144dbeb9e5b65dfc9186dda76274860e489192c748d6d69b
```

原 v2 数据 SHA 保持 `0da5948b79a15839d6abb939f134948e8d58dad101b66862ed7d4abaac40b26d`。前6例的题干、学生文本、标准答案、Rubric、参考分及 GradeInput input_sha256 与上轮逐一相同。新增重复引用案例允许引用指定片段内任一正确区间，不因重复本身强制拒绝；不确定探针没有0分标签。

## v4 的具体变化

去掉固定3.00分值、占位UUID和“原”字引用示例，使用字段合同说明。`no_evidence` 仅允许 `kind/reason`；quote 仅允许 `kind/segment_index/text_start/text_end/quote`。偏移明确为指定学生片段的Unicode码点半开区间，并给出各片段字符长度。每个Criterion恰好评分一次，Decimal总分严格等于逐项之和，正分仍需有效Quote，无法可靠判断允许弃权。

Prompt版本参与请求指纹，v3与v4不会共用指纹。同一个旧request_id发起v4会冲突，不覆盖旧Proposal；新评分须使用新request_id。v3对象仍按自身版本、SHA和1.0协议读取。历史评测调用显式保留v3，新增程序化参数允许明确选择v4并保存相同版本元数据，不能只改报告标签。

## 真实请求条件与成本预检

- endpoint：`https://api.deepseek.com/chat/completions`，Provider为已有OpenAI-compatible适配器。
- 配置模型及12次响应model字段均为 `deepseek-flash`；响应没有提供更细的权重版本证明。
- `thinking={"type":"disabled"}`，temperature=0，`response_format={"type":"json_object"}`。
- 不发送reasoning_effort；配置中的max不等于启用非思考max推理。
- 输出上限4096 Token，超时120秒，最多12次，零自动重试，预算0.10美元。
- UTF-8文本字节及每次1024 Token封装余量形成保守输入上界：61042 Token；输出上界49152 Token。
- 按峰时输入全部缓存未命中0.30美元/百万Token、输出1.20美元/百万Token预检，上限0.077295美元，调用前通过。
- 官方合同只据实使用JSON Output，不宣称它能保证业务Schema或Quote正确。[接口合同](https://api-docs.deepseek.com/api/create-chat-completion/)、[公开费率](https://api-docs.deepseek.com/quick_start/pricing/)。

## 分层结果

- Provider成功响应：12/12；全部finish_reason=stop，无超时或截断。
- JSON可解析：12/12。
- GradeModelReply严格Schema有效：10/12。
- 严格可用结果：8/12，其中7个候选、1个弃权；其余4个响应拒绝。
- 有参考分案例的有效候选覆盖：7/11（63.64%）。
- 有效候选总分一致7/7，MAE=0；逐Criterion一致10/10，MAE=0。只在有效候选上计算，不将4个失败样例从覆盖率分母中消失。
- 弃权1/12，正是唯一材料不确定探针；11个可评分案例未弃权。
- 原始14条Quote中9条通过完整原文/区间校验，5条失败；有效候选内部Quote为6/6。后者受到接受门禁保证，不能代替原始引用质量监控。
- 原始6条no_evidence均符合精确字段合同（6/6）。
- 只读算术诊断：11个candidate形状回复的Criterion覆盖、分值范围/精度、总分求和分别为11/11；这些局部通过不将引用错误的响应变成有效候选。
- 教学语义人工复核全部为 `unreviewed`，不能用数值一致或原文可追溯证明教学评分正确。

### 相同6例的配对对照

旧版响应直接复用上轮保存结果，没有为A组再次收费请求。A、B并非同时运行，单次输出有随机性，不能把差异全部归因为Prompt。

- `linear-correct`：v3候选 → v4候选，10/10分。
- `answer-only-no-method`：v3候选 → v4候选，4/10分；缺失过程没有被补造。
- `fraction-partial`：v3协议错误 → v4原文偏移错误；原始总分7，拒绝。
- `geometry-cross-page`：v3协议错误 → v4Quote长度错误；原始总分10，拒绝。
- `off-topic-zero`：v3协议错误 → v4合法0分候选。
- `photosynthesis-omission`：v3协议错误 → v4合法2分部分分候选。

相同6例JSON通过均为6/6；Schema由2/6变为5/6；最终候选由2/6变为4/6。原先2个有效候选均未退化。这个小样本仍是开发回归，不是新的独立Holdout或教师真实评分准确率。

### 新增6例

- 允许等值小数表达：合法5分候选。
- 缺单位：合法6分部分分候选。
- 线性方程符号错误：原始0分，Quote长度错误导致拒绝。
- 提示词注入数据：原始5分，模型没有给指令要求的满分，但Quote起点错误导致拒绝。
- 重复短引用、跨片段同文本：正确指定原文位置，合法5分候选。
- 材料不确定：abstain，无分数、无正式Proposal。此人为文本探针只测试模型弃权合同，不证明生产GradeInput能够识别所有必要图像缺失，未绕过真实模态门禁。

## 剩余失败与最小下一步

本轮4例失败、5条失败Quote：通分案例2条长度相等但位置错误；跨页几何1条长度错误；符号错误案例1条长度错误；注入案例1条偏移错误。全部保留原始回复，未自动搜索并替换区间、改分、丢弃错误字段或重新采样。

no_evidence结构错误在本轮消失，但不能保证以后不会复发。继续堆叠格式说明未必能消除模型数Unicode位置的错误。下一步可独立评估现有确定性Quote定位实验的最小生产接入：模型仅选择指定segment的原文Quote，程序只有在严格唯一精确匹配时构造偏移，多次匹配必须要求明确消歧或拒绝。保持正分Quote、Rubric/Decimal/总分、当前来源校验和任务授权，单独版本化Prompt及模型回复合同，最终仍验证GradeProposal1.0。不能直接把历史实验v2切为默认；其教学质量风险依旧保留。

## 用量与延迟

实际输入11888、输出2394，共14282 Token；输入缓存命中4224、未命中7664，12次均返回输入/输出用量。reasoning_tokens字段未返回，记为不可用，不能当作实测0。

客户端平均耗时1456.6毫秒，范围932–1940毫秒。按用量估算峰时0.005197344美元；实际周六离峰估算0.002598672美元。不是账单实扣证明，低于0.10美元预算。没有为失败案例重发请求。

## 工程验证与历史兼容

- Ruff、后端纯单元672项、静态迁移结构、git diff --check通过。
- 独立PostgreSQL16/私有S3：考试评分与legacy候选回归34项通过，两个真实模型opt-in排除；再新增1项真实PG/S3 v3历史读取/冲突专项通过，共35项不同用例。
- 考试Worker持久化记录确实写入v4；legacy正向、来源失效、租约、并发、审计回滚、未知COMMIT、严格失败和教师审核原语义回归保持通过。
- v3历史Fixture经真实来源校验登记后，在v4服务仍能回读，原始S3字节及索引SHA不变；同request_id请求v4拒绝且模型调用为0。
- 无迁移变化，Alembic唯一Head仍为 `b14c52b3f014`。只对独立测试数据库执行原迁移，不连接开发数据库。
- README、Provider、GradeProposal1.0、Quote/Decimal校验器、历史v1/v2/v3/v4数据和已有预测均不修改。
- 没有新增Vue/API合同；不需要为这次纯评分Prompt变更运行浏览器或调用视觉模型。

## 可审计产物

- [上轮v3完整预测容器](../../data/evaluation/grading/deepseek-prompt-v3-six-case-baseline-v1.json)：原始文件字节复制归档，其中6例请求、19例not_requested、2例excluded；不是27次请求。
- [本轮12次原始回复及配置](../../data/evaluation/grading/deepseek-prompt-v4-smoke-predictions-v1.json)：独立 `huipi.grading.prompt-smoke.v1` 验收容器，不伪装为生产Proposal，也不套用会排除弃权探针的旧全数据集评分报告。
- [逐案例诊断](../../data/evaluation/grading/deepseek-prompt-v4-smoke-diagnostics-v1.json)、[汇总](../../data/evaluation/grading/deepseek-prompt-v4-smoke-summary-v1.json)。各JSON均有独立SHA清单。
- 本机原始日志、调用前预算、测试日志：`/home/amber/.cache/huipi-cloud/deepseek-prompt-v4-20261024/`。

进入独立审查的工程证据已具备；真实协议覆盖率仍不足以宣称正式AI批改质量已验收。PR52及叠加修复PR都保持Draft，等待独立审查，不自动合并。
