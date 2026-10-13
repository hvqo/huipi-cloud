# DeepSeek Provider 验收与 v2 基线计划

## 验收范围和当前事实

本次复用已有 HTTPX OpenAI-compatible Chat Completions Provider，评分目标名称为 `deepseek-flash`，视觉目标名称为 `deepseek-flash`，Base URL 为 `https://api.deepseek.com`。目标名称是配置值；没有真实回复就没有可确认的实际返回模型ID。

2026-10-13 检查本地配置时，两个 API Key 均缺失，两个模块的远程传输授权也没有启用。本轮没有发出 DeepSeek 请求，没有实际模型Token或费用，没有真实 DeepSeek 成功/失败样本。v2 的25个评分案例全部保持 `not_requested`，另2个案例因参考标签不确定而排除；见[独立未请求清单](../../data/evaluation/grading/deepseek-v2-run-manifest.json)。旧 qwen v1 预测不能充当 DeepSeek 预测，v1/v2 标签和历史报告保持原样。

默认测试使用 MockTransport 验证官方请求形状，并用真实独立 PostgreSQL 测试库、私有S3测试桶完成评分和视觉产物登记、大小/SHA回读、幂等及参数变化冲突。它们验证工程合同，不能证明 DeepSeek 服务已联通或模型质量提高。视觉输入来自合成图片和人工构造的 Canonical/Alignment fixture，未运行真实 MinerU。模型建议和人工作答复核记录保持隔离。

本次本地默认套件为499 passed、9 skipped。新增44项测试通过，其中7项使用真实PostgreSQL/S3和Mock HTTP；2项收费DeepSeek Smoke未执行，另7项为已有按需模型测试。Ruff、锁定依赖、Compose配置、diff及133个文档相对链接检查通过。真实请求成功0、失败0表示没有执行，不能解释成真实服务全部通过；也没有本轮真实模型Token统计。

## 官方接口依据

- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)：JSON object、max_tokens、finish_reason、实际模型ID及usage。
- [Thinking控制](https://api-docs.deepseek.com/guides/thinking_mode/)：默认开启；显式 enabled/disabled；low/high/max effort。
- [Vision](https://api-docs.deepseek.com/guides/vision/)：deepseek-flash 的用户消息图片输入。
- [模型与定价](https://api-docs.deepseek.com/quick_start/pricing/)：运行前核对模型别名和最新单价，不在代码中固化随时间变化的价格。

两模块统一配置Flash、档位max和Thinking关闭。首次验收使用文本4096 tokens/120秒、视觉2048 tokens/90秒，重试均为0。effort本身也能启用思考，故非思考请求只发送thinking=disabled，不发送effort，报告的配置max不等于实际启用了max推理。未来开启Thinking须作为独立实验重新核对预算；项目硬上限16384输出tokens。开启思考时不发送temperature，reasoning_content不记录为评分证据、预测或普通日志。completion_tokens包含思考，不能重复计费。

结束原因必须正常。length 即使附合法JSON也以 `provider_response_truncated` 拒绝；评分离线文件保存该次实际模型ID、finish_reason及可用usage，离线重算也不能把它重新判为有效候选。普通评分/视觉Proposal结构保持原协议；配置摘要和请求指纹区分Thinking与输出预算，新配置不能静默复用旧请求结果。

## 两次合成 Smoke

先在被忽略的 `.env` 填写对应Key，并由操作者显式开启两组远程授权。授权范围先限于合成数据；不要把真实学生数据混入此验收。Key不写入命令行或Git。

在独立的 `_test` PostgreSQL 数据库和专用私有S3测试桶中运行：

```bash
RUN_DEEPSEEK_SMOKE=1 uv run pytest -q -s -m deepseek_e2e
```

最多两次真实请求，无重试：一次“4”合成文本评分，经原有JSON、Decimal、Rubric和Quote门禁后登记/回读Proposal；一次合成图像视觉请求，经页码协议和保守来源映射后登记/回读。输出只含模型ID、状态、用量、耗时、SHA验证和候选/unresolved状态，不含完整答案或理由。视觉映射不一定成功，不因单个空间候选而确认真实Question归属。

没有Key或授权时不要设置opt-in。默认CI只使用MockTransport，不下载模型、不调用收费API；普通测试隔离开发者模型配置。401、404/模型或端点不存在、429、超时、截断、无效JSON、错误Quote及正分无Quote均有回归测试。

## v2 DeepSeek 基线

先完成两次真实Smoke，再对固定 v2 数据集运行A组。默认dry-run不调用模型：

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

P3-D1的未完成Quote实验保留在原分支和stash，未混入本次Provider改动。完成真实DeepSeek Smoke与A组基线、独立审查后，才恢复B组实验：模型选择原文，程序在指定segment内唯一精确定位，并再运行原有严格Quote与评分验证。A/B都使用同一25案例、模型、Thinking/effort、输出预算和生成配置，分开保存原始响应，不修改教师参考标签。

需要分别报告协议通过、有效覆盖、来源有效、总分/Criterion误差、高估风险、实际用量和费用估算；还要报告两组共同成功、仅A成功、仅B成功及都失败的配对案例。引用定位成功不代表引用语义充分，有效候选增加也不代表评分准确率提高。当前既没有DeepSeek基线，也没有实际A/B提升结论。
