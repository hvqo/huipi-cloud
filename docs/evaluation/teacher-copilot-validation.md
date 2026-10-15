# 教师 Copilot 云端与业务闭环验收

## 方法与授权

2026-10-15（Asia/Shanghai）显式启用 `RUN_COPILOT_E2E=1` 和 `COPILOT_SYNTHETIC_TRANSFER_AUTHORIZED=1`。仅发送合成作业名称、题目元数据、Rubric摘要和提交处理计数。Key从本地配置加载，未输出、复制到报告或修改`.env`。所有业务数据位于独立 `_test` 数据库与测试bucket/唯一Key前缀，测试结束只清理测试数据。不使用本地模型，不重跑MinerU，不写GradeProposal、GradeReview或AnswerReview。

Canonical来源采用已有已验证格式的合成ParsedArtifact fixture，标准化、对齐、人工作答确认与查询均走真实服务/数据库。不是本轮真实MinerU解析验收。

真实请求使用原生DeepSeek Tool Calling：实际响应model均为 `deepseek-flash`，Thinking disabled，temperature 0，输出上限1024，单模型30秒，全轮90秒，零自动重试。只验证一个一般说明问题和一个包含四工具的合成业务问题，不推断真实教师任务准确率。

## 两次独立观测

首次完成时间14:01:50。一般说明1次模型、0工具，业务问题2次模型、4工具。模型正确回答作业1题、Rubric上限10、解析succeeded，但虽复述response_present=1，却说无法确认“人工确认数量”。这属于工具字段解释不足，不是数据库查询失败、工具幻觉或模型调用失败。

随后工具增加 `latest_human_answer_review_count` 和明确说明：每题最新人工作答存在性判断数量，不是评分复核或正式成绩。保留原有计数字段，没有改变业务记录。第二次完成时间14:05:22，仍为1次一般说明和2次业务模型调用、4工具。模型正确回答1题、10分上限、succeeded、1条人工作答判断，并保留“索引未重新验证、不是可评分状态、不是正式成绩”的限制。

两次业务链路的四种工具都真实执行；返回结果已回填原call ID，工具状态均ok。首次不足和第二次观测均保留，不把重复请求当成更多独立任务。

## 用量与费用

- 首次一般说明：输入759、输出195 Token，2004ms；业务：输入2242、输出511，3312ms。
- 第二次一般说明：输入759、输出130，1272ms；业务：输入2263、输出469，2783ms。
- 合计6次模型请求、8次工具查询；输入6023、输出1305 Token；缓存命中3840、未命中2183，无缺失用量。
- 四轮累计客户端用时9371ms，非基准性能数据；每轮初始预留均在默认$0.05预算内。
- 按高峰无缓存保守价格估算合计$0.0033729；按当时非高峰、缓存命中分项估算$0.00112197。估算不等于服务商账单，价格可能变化。

[DeepSeek官方Tool Calling合同](https://api-docs.deepseek.com/guides/tool_calls/)和[官方价格](https://api-docs.deepseek.com/quick_start/pricing/)是外部依据。仅固定关闭Thinking；未发送启用思考的reasoning_effort，未使用评分JSON回复合同。

## 可复核证据

[脱敏轨迹及用量](teacher-copilot-validation.json)和[SHA-256](teacher-copilot-validation.json.sha256)保留run ID、调用顺序、状态、参数/结果摘要和实际model。原始本地合成输出的SHA也保留，公开轨迹不保存完整对话、Rubric、模型思考或Key。摘要不能单独恢复正文；测试用断言重新核对数据库结果和result hash。

验收用例：`tests/integration/test_copilot_business.py::test_real_deepseek_tools_and_answer_opt_in`。Fake/Mock自动化与收费测试分开；CI只运行前者。完整指令见[开发指南](../development/teacher-copilot.md)。

## 尚未证明的能力

这些样本证明协议与只读业务链路可运行，不证明任意教师问题、Prompt Injection或所有自然语言解释都正确。人工语义审核状态保持unreviewed。当前没有认证/RBAC、跨作业授权、RAG、持久会话、正式成绩或生产部署验收。P3评分实验质量门槛仍未通过，不能因Copilot可运行就启用Quote Selection v2生产评分。
