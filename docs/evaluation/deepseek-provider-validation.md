# DeepSeek Provider 验收与评分基线

## Provider Smoke 历史记录（2026-10-13）

本节记录 PR #18 阶段的 Provider 联通性验收。它是该次运行的历史快照，不代表本分支当前的测试数量或 v2 评测状态；v2 最新 A/B 实验见后文。

当时复用已有HTTPX OpenAI-compatible Chat Completions Provider，两模块配置均为 `deepseek-flash`，Base URL为 `https://api.deepseek.com`。2026-10-13恢复现场后，确认两个Key已能安全加载；用户先前成功调用是现场信息，未找到足以复核其请求内容的历史记录，因此只补做两次获授权的真实合成Smoke，没有执行25案例评测。

本地 `.env` 没有修改。其远程授权仍关闭；本次授权只通过测试进程环境变量临时开启，范围仅限合成数据。密钥没有进入命令行、输出、Git或验收文件。PostgreSQL与S3服务健康；使用独立 `_test` 数据库和专用私有测试桶，测试结束仅清理本轮对象前缀和测试数据。

### 本轮真实结果

验收于2026-10-13 13:39:12 UTC（北京时间21:39:12）结束。该时间来自测试日志完成时间，不是逐请求精确墙钟时间。可复核的脱敏记录见[合成Smoke证据](deepseek-synthetic-smoke-2026-10-13.json)。两次真实请求，无自动重试，2项测试通过，无失败或跳过。

- 文本实际响应 `model=deepseek-flash`、`finish_reason=stop`。688输入Token、122输出Token，Provider耗时907毫秒。JSON及严格评分协议、Rubric/Decimal分数和Quote来源全部通过；状态为 `completed_candidate`；真实PostgreSQL登记和私有S3回读/SHA校验通过。
- 视觉实际响应 `model=deepseek-flash`。1449输入Token、113输出Token、总量1562，Provider耗时1342毫秒。图片来自合成PNG，经实际渲染后传输；Canonical/Alignment为合法测试fixture，不是本轮MinerU结果。严格输出及页码协议通过，建议为 `candidate_prompt_only`，两处来源均为 `unresolved`。真实PostgreSQL登记、S3下载、SHA和Proposal回读通过；没有确认的Question归属。
- 两条链路都验证人工AnswerReview记录数没有变化。工程闭环成功不等于视觉来源匹配成功，更不等于教学评分正确。单个文本样本不能证明候选覆盖率或评分准确率。
- 思考模式显式关闭。配置保留max，但请求未发送 `reasoning_effort`；没有声称max推理生效。文本4096输出Token/120秒、视觉2048输出Token/90秒，均无重试。文本回复没有提供reasoning_tokens；保存的Smoke摘要没有缓存拆分，不把缺失用量解释成0。
- 两次累计2137输入Token和235输出Token。按运行日官方Flash峰时未缓存输入$0.30/百万、输出$1.20/百万估算保守费用为$0.0009231；缓存命中与非峰时可降低费用。这不是平台账单或账户扣费证明。

新安装默认配置和 `.env.example` 均采用关闭状态的云端DeepSeek，不会静默降级至本地模型。Ollama兼容适配器保留，仅可显式选择；CI中的历史本地合同测试使用明确的测试配置，没有业务回退语义。新增清洁配置回归测试先在旧默认值下失败，再通过修复。没有改写Provider HTTP实现、评分协议、数据库模型或历史产物。

默认套件仍以MockTransport验证请求和异常合同，以真实独立PostgreSQL/S3验证持久化。真实Smoke是单独opt-in验收；默认CI不会调用收费API，也不下载模型。本轮没有运行真实MinerU、Ollama或25案例DeepSeek基线。本轮默认完整测试为500 passed、9 skipped；新增清洁云端配置测试通过。独立测试库upgrade head和Alembic check通过（无模型漂移），锁定依赖、Ruff、Compose配置和diff检查通过，135个文档相对链接有效。没有新增迁移。

v2的25个纳入案例全部保持 `not_requested`，另2个参考标签不确定案例排除。SHA为 `0da5948b79a15839d6abb939f134948e8d58dad101b66862ed7d4abaac40b26d`，dry-run确认25个Prompt可构建；[原始未请求清单](../../data/evaluation/grading/deepseek-v2-run-manifest.json)保留，不把两次Smoke伪装为数据集预测。旧qwen v1数据、预测、报告和SHA均未覆盖。

## 官方接口依据

- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)：JSON object、max_tokens、finish_reason、实际模型ID及usage。
- [Thinking控制](https://api-docs.deepseek.com/guides/thinking_mode/)：默认开启；显式 enabled/disabled；low/high/max effort。
- [Vision](https://api-docs.deepseek.com/guides/vision/)：deepseek-flash 的用户消息图片输入。
- [模型与定价](https://api-docs.deepseek.com/quick_start/pricing/)：运行前核对模型别名和最新单价，不在代码中固化随时间变化的价格。

两模块统一配置Flash、档位max和Thinking关闭。首次验收使用文本4096 tokens/120秒、视觉2048 tokens/90秒，重试均为0。effort本身也能启用思考，故非思考请求只发送thinking=disabled，不发送effort，报告的配置max不等于实际启用了max推理。未来开启Thinking须作为独立实验重新核对预算；项目硬上限16384输出tokens。开启思考时不发送temperature，reasoning_content不记录为评分证据、预测或普通日志。completion_tokens包含思考，不能重复计费。

结束原因必须正常。length 即使附合法JSON也以 `provider_response_truncated` 拒绝；评分离线文件保存该次实际模型ID、finish_reason及可用usage，离线重算也不能把它重新判为有效候选。普通评分/视觉Proposal结构保持原协议；配置摘要和请求指纹区分Thinking与输出预算，新配置不能静默复用旧请求结果。

## 两次合成 Smoke

先在被忽略的 `.env` 填写对应Key，并由操作者明确授权合成数据传输。可以只在本次进程中开启两组远程授权，不必修改 `.env`；不要把真实学生数据混入验收。Key不写入命令行或Git。

在独立的 `_test` PostgreSQL 数据库和专用私有S3测试桶中运行：

```bash
RUN_DEEPSEEK_SMOKE=1 \
GRADING_ALLOW_REMOTE=true GRADING_EXTERNAL_DATA_AUTHORIZED=true \
VISUAL_EVIDENCE_ALLOW_REMOTE=true VISUAL_EVIDENCE_EXTERNAL_DATA_AUTHORIZED=true \
uv run pytest -q -s -m deepseek_e2e
```

最多两次真实请求，无重试：一次“4”合成文本评分，经原有JSON、Decimal、Rubric和Quote门禁后登记/回读Proposal；一次合成图像视觉请求，经页码协议和保守来源映射后登记/回读。输出只含模型ID、状态、用量、耗时、SHA验证和候选/unresolved状态，不含完整答案或理由。视觉映射不一定成功，不因单个空间候选而确认真实Question归属。

没有Key或授权时不要设置opt-in。默认CI只使用MockTransport，不下载模型、不调用收费API；普通测试隔离开发者模型配置。401、404/模型或端点不存在、429、超时、截断、无效JSON、错误Quote及正分无Quote均有回归测试。

## v2 DeepSeek 基线与 Quote-selection 实验

2026-10-14 已对固定 v2 数据实际执行 A 组 DeepSeek Flash 严格 Quote 基线，并完成同一模型和生成配置的 B 组确定性定位对照。此前的[deepseek-v2-run-manifest.json](../../data/evaluation/grading/deepseek-v2-run-manifest.json)是实验前的历史 `not_requested` 清单，仍保留作审计；它不代表最新运行状态。真实请求数量、原始文件 SHA、失败分类、成本和配对结果以[P3-D1 实验报告](p3d1-deepseek-quote-selection-2026-10-14.md)为准。

实验脚本默认只验证数据和构造 Prompt，不发送模型请求。报告中的预测文件可离线重新校验；重新运行真实模型属于新的收费实验，必须使用新的输出目录、先检查官方单价并满足合成数据远程传输授权。不要复用历史输出路径。协议细节、受控运行命令及产物链接见[评分开发指南](../development/grade-proposals.md)。

## 结果解释边界

A/B实验产物是不可变文件，使用同一数据集、DeepSeek模型和生成参数；Prompt及响应协议不同。严格偏移组的偏移/长度失败较多，足以说明确定性精确定位值得继续研究。B组覆盖率提高，但24个候选的条件分数比较不等于真实教学准确率；只有独立教师评阅、扩大样本和真实作业双评/仲裁才能回答评分语义质量问题。所有失败候选继续经过原P3-B校验，生产评分入口和历史Proposal协议均未改变。完整结论见[P3-D1实验报告](p3d1-deepseek-quote-selection-2026-10-14.md)。
