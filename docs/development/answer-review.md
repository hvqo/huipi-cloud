# 人工作答复核

当前人工复核入口是本地 CLI，没有 HTTP 写入接口、登录或 RBAC。`reviewer_ref` 是调用者提供的标签，不是验证身份。

要求提交已有可验证的 Canonical 和 AnswerAlignment。先检查来源：

```bash
uv run python -m huipi_cloud.workers.review_answers inspect \
  --submission-id SUBMISSION_UUID
```

根据检查结果选择属于目标题目的 Canonical 内容指针。纯文本范围使用 Unicode 码点半开区间 `CONTENT_POINTER:START:END`。然后使用唯一 UUID 追加一次决定：

```bash
uv run python -m huipi_cloud.workers.review_answers record \
  --submission-id SUBMISSION_UUID \
  --question-id QUESTION_UUID \
  --request-id REQUEST_UUID \
  --reviewer-ref local-reviewer \
  --decision uncertain \
  --reason-code source_quality_insufficient
```

若已确认作答，可用 `--decision response_present` 并按实际区域增加一个或多个 `--response-region CONTENT_POINTER:START:END`。只有排除印刷题干时才添加 `--prompt-region`。不要把仅有题干或无法归属的共享图形标成目标题的确认作答。

查看历史或导出：

```bash
uv run python -m huipi_cloud.workers.review_answers history \
  --submission-id SUBMISSION_UUID --question-id QUESTION_UUID
uv run python -m huipi_cloud.workers.review_answers export \
  --submission-id SUBMISSION_UUID --output /tmp/answer-review-redacted.json
```

导出会去掉 reviewer、自声明学生标识、原始 UUID 和私有对象定位信息，但这只是去标识化，不保证匿名。内容、题号和区域位置仍可能与外部信息组合识别个人；原始记录和导出都应按敏感数据处理。没有复核记录表示 `unreviewed`，不表示学生未作答。`response_present` 也不等于可以评分。
