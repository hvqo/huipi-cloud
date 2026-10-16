# 教师候选评分审核 HTTP

此工作流复用原有候选与人工复核服务。它追加审核修订，不发布正式成绩，不调用评分模型，不启用实验 Quote Selection 或新 Prompt。部署前先按[教师认证指南](teacher-auth.md)配置 OIDC/JWKS、内部 User 和作业 Membership。

## 资源与权限

统一前缀：

```text
/api/v1/submissions/{submission_id}/questions/{question_id}/grade-proposals/{proposal_id}
```

- `GET /review`：检查当前候选、GradeInput 来源状态、逐项分数、最新修订和重新评分标记。
- `GET /reviews?limit=5&offset=0`：按修订号倒序分页；limit 为 1–10，offset 为 0–100000，返回 has_next/next_offset。历史 source_validity=not_checked 不表示仍可评分。
- `POST /reviews`：追加决定；首次成功 201，同请求幂等恢复 200。

全部接口要求 Bearer Token。读取需作业读权限，写入需 can_write 或内部管理员。服务端先查询 Proposal、GradeInput、Submission、Question 的真实关系和绑定摘要；即使管理员也不能拼接不同题目或作业的 ID。不存在或无权访问统一 404。授权在 S3、历史和 request_id 重放读取之前完成。

HTTP 请求不接受 reviewer_ref、reviewer_user_id、reviewer_id、teacher_id、role 或 assignment_id。服务端从 Principal.user_id 派生 `teacher_<UUID去连字符>`，同时写入可信 User 外键。历史 CLI 行仍为 NULL；前缀相同不证明认证身份。

## 最小请求

先读取 `/review` 的 latest_review_revision，再构造请求；Token 应从安全的本地身份会话加载，不写入文件或提交：

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "expected_revision": 0,
  "review_status": "accepted"
}
```

示例 UUID 仅表示字段格式，每个新逻辑决定须生成新 request_id。丢失确认时，用原 ID、原 expected_revision、原正文和同一教师重试。新 request_id 不能沿用旧修订号。相同 ID 不同正文或不同教师返回 409。

- `accepted`：确认**原始模型候选**分数；不接受 criterion_reviews，也不表示接受上一次人工修订。
- `revised`：提交完整 Rubric 快照，每个 Criterion 都必须有 reviewed_points；用十进制字符串，不用二进制浮点数。changed 相对原始 Proposal。改变的项目必须有理由和有效证据，正分至少有一条原文 Quote；未变项目保留原候选理由和证据。具体字段和完整快照规则见[原 CLI 合同](grade-review.md)。
- `rejected`：必须有 review_reason_codes，没有人工总分。
- `regrading_required`：必须有原因码，只记录后续处理要求，不触发收费调用或生成新候选。

模型弃权候选不能被 accepted/revised。Criterion 集合、Decimal、满分、总分、Unicode 半开字符范围、精确 Quote、最新人工确认与来源 SHA 校验全部沿用已有服务。缺失材料不是学生零分。P3-D2B 质量门禁没有获准，教师接受也不绕过它。

## 输出与来源核对

默认检查与历史不返回学生全文、标准答案、完整 Quote、模型长理由、人工评论或私有 S3 Key。历史只投影分数与绑定摘要，返回原因码及评论是否存在；每条最多 100 个评分项。`reviewer_kind` 区分 authenticated_teacher 与 legacy_cli_claim，不能根据字符串推断身份。

需要核对候选引用时，使用 `/review?evidence_limit=10&evidence_offset=0`，limit 最多 25；只返回经过原文验证的 Quote 坐标，不返回 Quote 正文。`text_start/end` 相对 GradeInput segment，`canonical_text_start/end` 相对 Canonical ContentNode，均为 Unicode 码点半开范围。canonical_page_path 指向同样需要作业授权的页面接口。该部分有 48KiB 上限，遵循 evidence_truncated/next_evidence_offset 翻页。默认 evidence_limit=0 不读取额外的证据投影。

所有响应 no-store。`grade_published` 恒为 false；source_stale 时仍可审计旧修订，但不能追加新决定。已提交的相同请求允许在来源后来变化后恢复，前提是当前教师仍有写权限。

## 事务、撤权与未知提交

S3 来源验证不持有成员锁。追加事务锁定 Proposal，重新验证所有来源、expected_revision、用户启用状态及可写成员。User/Membership SHARE 锁持有到提交；撤权先提交则拒绝，复核先取得锁则撤权等待。锁等待 3 秒，SQL 5 秒。请求前的重放查找也在短事务中复查授权，不可利用旧 request_id 绕过撤权。

两个教师同时提交相同 expected_revision 仅一条成功。相同 request_id/正文/教师并发则只追加一条，其余返回同一结果。请求摘要加入可信 user_id；旧 CLI 摘要与 1.0 文档不变。

写入前失败会回滚；COMMIT 已尝试但确认丢失时返回 503 `grade_review_commit_outcome_unknown`，不声称成功或确定回滚。使用原请求重试恢复。复核不上传新对象，不删除 Proposal/GradeInput。存储或数据库故障返回安全 503；实际来源版本失效返回 409，非法分值/证据为 422，身份无效为 401，停用用户等为 403。

## 迁移与留存

迁移 `b61c8e72a490` 仅添加可空 reviewer_user_id、RESTRICT 外键、索引及身份一致性 CHECK，不回填历史。原有 UPDATE/DELETE 拒绝触发器保持。普通删除与级联删除受限制，应独立设计留存/归档策略，不能临时关闭触发器清历史。

downgrade 保留旧行与原评分内容，但丢弃新增的认证外键，重新 upgrade 不会重建已经丢失的身份来源；执行前需保留授权审计备份。迁移只在独立测试库验证，不会由应用启动自动升级用户开发库。

## 合成验收与前端边界

```bash
uv run pytest -q tests/integration/test_grade_review_http.py tests/unit/test_grade_review_http_protocol.py
```

运行时 RSA 签名、Mock JWKS、真实独立 PostgreSQL 与私有 S3；合成 ParsedArtifact/Canonical/Alignment 经现有校验流程建立 GradeInput，严格 Fake Provider 建立候选。没有真实学生资料、MinerU/VLM 或收费模型调用。

后续教师界面可据上述检查、分页、来源坐标、身份与修订合同编辑完整评分。完整理由/评论的受限审计读取、正式成绩发布、教师界面和真实 IdP 部署验收仍需单独设计；不能把摘要当成已经完成的教学语义审核。
