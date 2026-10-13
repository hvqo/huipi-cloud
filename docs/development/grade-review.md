# Rubric 候选评分人工复核

本地 CLI 为已有的 `GradeProposalArtifact` 追加人工复核记录。它不调用模型，不建立正式成绩，也不修改 P2-D2A 的作答存在性决定。`reviewer_ref` 由操作者填写，目前没有身份认证或 RBAC；只能在受控本地环境使用。

## 前置条件

- PostgreSQL 已迁移到当前 Alembic head。
- `.env` 已配置 PostgreSQL 与私有 S3 兼容存储。
- 目标 GradeProposal 与 GradeInput 对象、索引和当前来源都可读取。
- 操作者已从受控渠道核对候选评分和原文证据。CLI 输出不会显示学生答案、AnswerKey、完整模型理由、对象 Key 或评论。

## 检查候选

```bash
uv run python -m huipi_cloud.workers.review_grade_proposal inspect \
  --proposal-id <proposal-uuid>
```

结果提供候选状态、逐项候选分数、当前来源状态和最新复核修订号。`source_status` 不是 `current` 时，不能接受或修订旧候选；应先检查来源，并基于当前输入重新准备评分流程。

## 记录决定

建立一个 UTF-8 JSON 文件。`accepted` 的最小示例：

```json
{
  "schema_name": "huipi.grading.review",
  "schema_version": "1.0",
  "review_status": "accepted",
  "reviewer_ref": "teacher.local"
}
```

接受会逐项复制并保留 Proposal 原分数及证据，不能在接受请求中提交新分数。记录时为每次逻辑请求生成 UUID，并提供读到的当前 revision：

```bash
uv run python -m huipi_cloud.workers.review_grade_proposal record \
  --proposal-id <proposal-uuid> \
  --request-id <request-uuid> \
  --expected-revision 0 \
  --file review.json
```

若因网络或数据库错误无法确认结果，使用相同 `request_id` 和相同 JSON 重试。相同请求返回已提交记录；同 ID 不同内容会冲突。若其他操作者先写入新 revision，旧 `expected_revision` 会返回冲突，先运行 `inspect` 和 `history` 再决定下一步。

`revised` 必须为每个 Rubric Criterion 提供且仅提供一个分数，分数使用十进制字符串。总分由服务计算。分数变化的项目必须附理由和证据引用；引用的 segment、Unicode 码点半开范围和原文必须属于当前 GradeInput。正分至少需要一个有效 Quote。未变化的项目不允许覆盖原候选的理由和证据。请从已授权的原始材料和 GradeInput 中人工取得真实引用，不能使用占位引用。

`rejected` 与 `regrading_required` 必须提供原因码，但没有 reviewed score。`regrading_required` 只记录后续动作要求，不会自动调用模型、创建新 Proposal 或修改旧记录。模型弃权 Proposal 只能被拒绝或标记需要重新评分。

## 查看历史

```bash
uv run python -m huipi_cloud.workers.review_grade_proposal history \
  --proposal-id <proposal-uuid>
```

历史显示状态、revision、前序记录、分数差异摘要、原因码和来源状态；不会输出学生原文、quote、Criterion 理由、教师评论或 S3 Key。原始复核记录含敏感审计信息，应按受限数据库数据保护。

## 状态和数据边界

| 状态 | 复核行为 | 复核分数 |
| --- | --- | --- |
| `accepted` | 确认候选逐项分数不变 | 与候选相同 |
| `revised` | 人工修订逐项分数并给变化项提供理由及证据 | 服务计算 |
| `rejected` | 拒绝当前候选并记录原因 | 无 |
| `regrading_required` | 记录需要新证据或后续评分 | 无 |

记录绑定 Proposal、GradeInput、Question、来源版本和哈希。写事务锁定 Proposal 行、重新验证当前 GradeInput 来源并检查 `expected_revision`。PostgreSQL 唯一约束保证 request/revision 不重复；数据库触发器禁止修改或删除已追加的行。复核记录不是高可信参考标签；后续质量评测仍需要授权数据、独立教师评分和仲裁。`accepted` 与 `revised` 都不代表成绩已经发布。
