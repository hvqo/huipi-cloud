# 基本作业处理流程

本教程通过本地 API 创建作业、提交合成 PDF，并运行解析、标准化和题目对齐。业务 HTTP 接口要求[可信教师身份和作业授权](teacher-auth.md)；先配置发行方、建立用户映射并安全载入 `HUIPI_ACCESS_TOKEN`。CLI 仍限本地受控操作员使用。

## 启动依赖

按[快速开始](quickstart.md)配置 `.env`、启动 PostgreSQL 和对象存储、执行迁移，并在终端启动 API。解析步骤还需要配置并启动[MinerU Worker](mineru.md)。本仓库的演示 PDF 为程序生成内容，不是真实学生作业。

## 创建并发布作业

需要 curl 和 jq。在仓库根目录执行：

```bash
set -euo pipefail
: "${HUIPI_ACCESS_TOKEN:?请安全载入可信教师access token}"
API=http://127.0.0.1:8000/api/v1

assignment_id=$(curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" -X POST "$API/assignments" \
  -H 'Content-Type: application/json' \
  -d '{"title":"本地流程演示","subject":"数学","grade_level":"高一"}' \
  | jq -r .id)

question_id=$(curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" -X POST "$API/assignments/$assignment_id/questions" \
  -H 'Content-Type: application/json' \
  -d '{"question_number":1,"question_type":"short_answer","stem":"计算 2+2，并写出过程。","max_score":"10.00"}' \
  | jq -r .id)

curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" -X PUT "$API/questions/$question_id/answer-key" \
  -H 'Content-Type: application/json' \
  -d '{"answer_content":"4"}' >/dev/null

curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" -X PUT "$API/questions/$question_id/rubric" \
  -H 'Content-Type: application/json' \
  -d '{"criteria":[{"description":"答案正确","points":"10.00","sort_order":1}]}' >/dev/null

curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" -X POST "$API/assignments/$assignment_id/publish" >/dev/null
printf 'assignment_id=%s\nquestion_id=%s\n' "$assignment_id" "$question_id"
```

## 上传合成 PDF

```bash
submission_id=$(curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" -X POST "$API/assignments/$assignment_id/submissions" \
  -F 'student_ref=local-demo-student' \
  -F 'file=@tests/fixtures/mineru/visual-evidence-chinese-questions.pdf;type=application/pdf' \
  | jq -r .id)

curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" "$API/submissions/$submission_id"
curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" "$API/submissions/$submission_id/parsing-task"
```

`student_ref` 只是模拟身份，不是认证凭据。真实解析需要已配置的 MinerU 运行时；单独打开一个终端运行：

```bash
uv run python -m huipi_cloud.workers.parsing_worker
```

等待解析状态变为 `succeeded` 后，在仓库根目录执行：

```bash
uv run python -m huipi_cloud.workers.normalize_document --submission-id "$submission_id"
uv run python -m huipi_cloud.workers.align_answers --submission-id "$submission_id"
curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" "$API/submissions/$submission_id/canonical-document"
curl -fsS -H "Authorization: Bearer ${HUIPI_ACCESS_TOKEN}" "$API/submissions/$submission_id/answer-alignment"
```

完成这些步骤后，系统已有可供检查的 Canonical 文档和 Question 来源对齐。`aligned` 不代表学生已作答，也不代表答案可以评分。

## 可选：视觉建议与人工复核

视觉建议需要配置经授权的云端 DeepSeek API。先执行 dry-run 检查文件和来源页，再显式允许本次模型调用：

```bash
VISUAL_EVIDENCE_ENABLED=true uv run python -m huipi_cloud.workers.analyze_visual_evidence \
  --submission-id "$submission_id" --question-id "$question_id" --dry-run
```

实际生成建议和记录人工决定的完整操作见[视觉建议指南](visual-evidence.md)和[人工复核指南](answer-review.md)。两者是独立数据路径；建议不会自动成为人工复核决定。
