# DeepSeek Provider 验收与 v2 基线计划

## 验收范围和当前事实

本次复用已有HTTPX OpenAI-compatible Chat Completions Provider，两模块配置均为 `deepseek-flash`，Base URL为 `https://api.deepseek.com`。2026-10-13恢复现场后，确认两个Key已能安全加载；用户先前成功调用是现场信息，未找到足以复核其请求内容的历史记录，因此只补做两次获授权的真实合成Smoke，没有执行25案例评测。

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

## v2 DeepSeek 基线

真实合成Smoke已通过，下一阶段对固定v2数据集单独运行A组。默认dry-run不调用模型：

```bash
uv run python scripts/evaluate_grade_proposals.py --dry-run
```

为避免固化过期价格，以下两个单价变量需由操作者从官方页面核对并填写；它们不是API Key：

```bash
uv run python scripts/evaluate_grade_proposals.py \
  --dataset data/evaluation/grading/rubric-cases-v2.json \
  --run-model --model deepseek-flash --max-requests 25 \
  --timeout-seconds 120 --max-output-tokens 4096 \
  --input-usd-per-million "$DEEPSEEK_INPUT_USD_PER_MILLION" \
  --output-usd-per-million "$DEEPSEEK_OUTPUT_USD_PER_MILLION" \
  --max-estimated-cost-usd 0.50 \
  --output data/evaluation/grading/predictions-deepseek-v2-new-run.json \
  --report-output data/evaluation/grading/report-deepseek-v2-new-run.json
```

新输出文件必须不存在。DeepSeek运行缺少单价、单价非有限/非正或估算超过预算时，在请求前拒绝。UTF-8字节与协议开销作为保守输入token预算，输出按总token上限估算，未缓存输入单价用于上界；这是预检和可复算估算，不是平台账单保证或账户级硬限额。网络失败缺少usage时费用标记未知，不当成0。思考配置、输出上限、实际响应模型ID及可用的缓存/思考Token在预测和报告中单独记录。

离线重算不调用API，也不覆盖已有文件：

```bash
uv run python scripts/evaluate_grade_proposals.py \
  --dataset data/evaluation/grading/rubric-cases-v2.json \
  --evaluate-predictions data/evaluation/grading/predictions-deepseek-v2-new-run.json \
  --report-output /tmp/huipi-deepseek-v2-recalculated-new.json
```

## 后续 A/B 的前提

P3-D1的未完成Quote实验保留在原分支和stash，未混入本次Provider改动。真实Smoke已通过；完成A组基线并独立审查后，才恢复B组实验：模型选择原文，程序在指定segment内唯一精确定位，并再运行原有严格Quote与评分验证。A/B都使用同一25案例、模型、Thinking/effort、输出预算和生成配置，分开保存原始响应，不修改教师参考标签。

需要分别报告协议通过、有效覆盖、来源有效、总分/Criterion误差、高估风险、实际用量和费用估算；还要报告两组共同成功、仅A成功、仅B成功及都失败的配对案例。引用定位成功不代表引用语义充分，有效候选增加也不代表评分准确率提高。当前既没有DeepSeek基线，也没有实际A/B提升结论。
