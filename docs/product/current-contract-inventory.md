# 当前持久化与 HTTP 合同清单

证据日期：2026-10-18；后端基线 main `680421cb7502c8eed208c9b0a79a96ec6a087a8f`。由本地 ORM 元数据和 `app.openapi()` **只读加载**生成，未连接数据库、未启动 lifespan/Worker、未执行模型调用。部署数据库实际版本/索引存在性不在本轮验证范围。

源码根：[后端](../../src/huipi_cloud/)、[迁移](../../migrations/versions/)、[统一授权](../../src/huipi_cloud/modules/identity/dependencies.py)。PR #31/视觉分支未改后端模型或迁移。

## 1. 20 个持久化表

### admin_operations

- 列：`id` (CHAR(32)), `actor_user_id` (CHAR(32)), `target_user_id` (CHAR(32)), `assignment_id` (CHAR(32), nullable), `request_id` (CHAR(32)), `action` (VARCHAR(48)), `payload_sha256` (VARCHAR(64)), `state` (JSONB), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：无显式 FK（如部分审计 UUID 为历史事实，不能据此推断实时关联存在）。
- 唯一键：`(actor_user_id,request_id)`。
- 索引：`ix_admin_operations_assignment_created`(assignment_id,created_at)；`ix_admin_operations_target_created`(target_user_id,created_at)。
- CHECK：`action IN ('teacher_registered', 'teacher_activation_changed', 'assignment_membership_granted', 'assignment_membership_revoked')`；`jsonb_typeof(state) = 'object'`；`length(payload_sha256) = 64`。

### assignments

- 列：`id` (CHAR(32)), `title` (VARCHAR(200)), `subject` (VARCHAR(100)), `grade_level` (VARCHAR(50)), `description` (TEXT, nullable), `status` (VARCHAR(20)), `created_at` (DATETIME), `updated_at` (DATETIME)。
- 主键：`id`。
- 外键：无显式 FK（如部分审计 UUID 为历史事实，不能据此推断实时关联存在）。
- 唯一键：除主键外无。
- 索引：`ix_assignments_status_created_at`(status,created_at)。
- CHECK：`length(trim(grade_level)) > 0`；`length(trim(subject)) > 0`；`length(trim(title)) > 0`；`status IN ('draft', 'published')`。

### users

- 列：`id` (CHAR(32)), `issuer` (VARCHAR(512)), `subject` (VARCHAR(255)), `is_active` (BOOLEAN), `is_admin` (BOOLEAN), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：无显式 FK（如部分审计 UUID 为历史事实，不能据此推断实时关联存在）。
- 唯一键：`(issuer,subject)`。
- 索引：无额外显式索引。
- CHECK：`length(trim(issuer)) > 0`；`length(trim(subject)) > 0`。

### assignment_memberships

- 列：`id` (CHAR(32)), `assignment_id` (CHAR(32)), `user_id` (CHAR(32)), `can_write` (BOOLEAN), `created_at` (DATETIME), `revoked_at` (DATETIME, nullable)。
- 主键：`id`。
- 外键：assignment_id → assignments.id (CASCADE)；user_id → users.id (default restrict/no action)。
- 唯一键：`(assignment_id,user_id)`。
- 索引：`ix_assignment_memberships_user_assignment`(user_id,assignment_id)。
- CHECK：无额外 CHECK。

### knowledge_documents

- 列：`id` (CHAR(32)), `document_id` (CHAR(32)), `assignment_id` (CHAR(32)), `source_version` (INTEGER), `source_sha256` (VARCHAR(64)), `input_sha256` (VARCHAR(64)), `title` (VARCHAR(200)), `source_type` (VARCHAR(16)), `permission` (VARCHAR(16)), `chunker_version` (VARCHAR(40)), `status` (VARCHAR(16)), `bucket` (VARCHAR(63)), `object_key` (VARCHAR(1024)), `size_bytes` (INTEGER), `chunk_count` (INTEGER), `created_at` (DATETIME), `revoked_at` (DATETIME, nullable)。
- 主键：`id`。
- 外键：assignment_id → assignments.id (CASCADE)。
- 唯一键：`(document_id,input_sha256)`；`(document_id,source_version)`。
- 索引：`ix_knowledge_assignment_status`(assignment_id,status)；`uq_knowledge_active_document`(document_id)。
- CHECK：`(status='revoked') = (revoked_at IS NOT NULL)`；`length(source_sha256) = 64 AND length(input_sha256) = 64`；`length(trim(title)) > 0 AND length(trim(bucket)) > 0 AND length(trim(object_key)) > 0`；`source_type IN ('markdown','text') AND permission IN ('synthetic','authorized')`；`source_version > 0 AND size_bytes > 0 AND size_bytes <= 65536 AND chunk_count > 0 AND chunk_count <= 256`；`status IN ('active','superseded','revoked')`。

### questions

- 列：`id` (CHAR(32)), `assignment_id` (CHAR(32)), `question_number` (INTEGER), `question_type` (VARCHAR(30)), `stem` (TEXT), `max_score` (NUMERIC(8, 2)), `created_at` (DATETIME), `updated_at` (DATETIME)。
- 主键：`id`。
- 外键：assignment_id → assignments.id (CASCADE)。
- 唯一键：`(assignment_id,question_number)`。
- 索引：`ix_questions_assignment_id`(assignment_id)。
- CHECK：`length(trim(stem)) > 0`；`max_score > 0`；`question_number > 0`；`question_type IN ('single_choice', 'multiple_choice', 'fill_blank', 'short_answer', 'essay')`。

### submissions

- 列：`id` (CHAR(32)), `assignment_id` (CHAR(32)), `student_ref` (VARCHAR(64)), `status` (VARCHAR(20)), `created_at` (DATETIME), `updated_at` (DATETIME), `submitted_at` (DATETIME)。
- 主键：`id`。
- 外键：assignment_id → assignments.id (CASCADE)。
- 唯一键：`(assignment_id,student_ref)`。
- 索引：`ix_submissions_assignment_created_at`(assignment_id,created_at)。
- CHECK：`length(trim(student_ref)) > 0`；`status = 'submitted'`。

### answer_keys

- 列：`id` (CHAR(32)), `question_id` (CHAR(32)), `answer_content` (TEXT), `source` (VARCHAR(30)), `created_at` (DATETIME), `updated_at` (DATETIME)。
- 主键：`id`。
- 外键：question_id → questions.id (CASCADE)。
- 唯一键：`(question_id)`。
- 索引：无额外显式索引。
- CHECK：`length(trim(answer_content)) > 0`；`source IN ('teacher', 'model_generated')`。

### knowledge_chunks

- 列：`id` (CHAR(32)), `document_version_id` (CHAR(32)), `chunk_index` (INTEGER), `chunk_text` (TEXT), `chunk_text_sha256` (VARCHAR(64)), `text_start` (INTEGER), `text_end` (INTEGER), `content_pointer` (VARCHAR(16)), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：document_version_id → knowledge_documents.id (CASCADE)。
- 唯一键：`(document_version_id,chunk_index)`。
- 索引：`ix_knowledge_chunks_document_version_id`(document_version_id)。
- CHECK：`chunk_index >= 0 AND text_start >= 0 AND text_end > text_start AND text_end-text_start = char_length(chunk_text) AND char_length(chunk_text) <= 400`；`length(chunk_text_sha256) = 64 AND content_pointer = '/text'`。

### parsing_tasks

- 列：`id` (CHAR(32)), `submission_id` (CHAR(32)), `status` (VARCHAR(20)), `attempt_count` (INTEGER), `max_attempts` (INTEGER), `next_run_at` (DATETIME, nullable), `lease_token` (CHAR(32), nullable), `lease_expires_at` (DATETIME, nullable), `started_at` (DATETIME, nullable), `finished_at` (DATETIME, nullable), `last_error_code` (VARCHAR(64), nullable), `last_error_message` (VARCHAR(240), nullable), `created_at` (DATETIME), `updated_at` (DATETIME)。
- 主键：`id`。
- 外键：submission_id → submissions.id (CASCADE)。
- 唯一键：`(submission_id)`。
- 索引：`ix_parsing_tasks_status_lease_expires_at`(status,lease_expires_at)；`ix_parsing_tasks_status_next_run_created_at`(status,next_run_at,created_at)。
- CHECK：`(status = 'retry_wait' AND next_run_at IS NOT NULL) OR (status <> 'retry_wait' AND next_run_at IS NULL)`；`(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)`；`(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL) OR (status NOT IN ('succeeded', 'failed') AND finished_at IS NULL)`；`attempt_count >= 0`；`max_attempts > 0 AND attempt_count <= max_attempts`；`status IN ('pending', 'running', 'retry_wait', 'succeeded', 'failed')`。

### rubric_criteria

- 列：`id` (CHAR(32)), `question_id` (CHAR(32)), `description` (TEXT), `points` (NUMERIC(8, 2)), `sort_order` (INTEGER)。
- 主键：`id`。
- 外键：question_id → questions.id (CASCADE)。
- 唯一键：`(question_id,sort_order)`。
- 索引：`ix_rubric_criteria_question_id`(question_id)。
- CHECK：`length(trim(description)) > 0`；`points > 0`；`sort_order > 0`。

### submission_files

- 列：`id` (CHAR(32)), `submission_id` (CHAR(32)), `bucket` (VARCHAR(63)), `object_key` (VARCHAR(512)), `original_filename` (VARCHAR(255)), `content_type` (VARCHAR(100)), `size_bytes` (INTEGER), `sha256` (VARCHAR(64)), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：submission_id → submissions.id (CASCADE)。
- 唯一键：`(bucket,object_key)`；`(submission_id)`。
- 索引：`ix_submission_files_created_at`(created_at)。
- CHECK：`content_type IN ('application/pdf', 'image/jpeg', 'image/png')`；`length(sha256) = 64`；`length(trim(bucket)) > 0`；`length(trim(object_key)) > 0`；`length(trim(original_filename)) > 0`；`size_bytes > 0`。

### parsed_artifacts

- 列：`id` (CHAR(32)), `parsing_task_id` (CHAR(32)), `submission_id` (CHAR(32)), `bucket` (VARCHAR(63)), `original_sha256` (VARCHAR(64)), `parser_name` (VARCHAR(32)), `parser_version` (VARCHAR(64)), `tier` (VARCHAR(16)), `schema_name` (VARCHAR(64)), `schema_version` (VARCHAR(16)), `page_count` (INTEGER), `asset_count` (INTEGER), `archive_key` (VARCHAR(1024)), `archive_sha256` (VARCHAR(64)), `archive_size_bytes` (BIGINT), `markdown_key` (VARCHAR(1024)), `markdown_sha256` (VARCHAR(64)), `markdown_size_bytes` (BIGINT), `middle_json_key` (VARCHAR(1024)), `middle_json_sha256` (VARCHAR(64)), `middle_json_size_bytes` (BIGINT), `structured_content_key` (VARCHAR(1024)), `structured_content_sha256` (VARCHAR(64)), `structured_content_size_bytes` (BIGINT), `assets_manifest_key` (VARCHAR(1024)), `assets_manifest_sha256` (VARCHAR(64)), `assets_manifest_size_bytes` (BIGINT), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：parsing_task_id → parsing_tasks.id (CASCADE)；submission_id → submissions.id (CASCADE)。
- 唯一键：`(parsing_task_id)`；`(submission_id)`。
- 索引：`ix_parsed_artifacts_created_at`(created_at)。
- CHECK：`archive_size_bytes >= 0 AND markdown_size_bytes >= 0 AND middle_json_size_bytes >= 0 AND structured_content_size_bytes >= 0 AND assets_manifest_size_bytes >= 0`；`asset_count >= 0`；`length(original_sha256) = 64 AND length(archive_sha256) = 64 AND length(markdown_sha256) = 64 AND length(middle_json_sha256) = 64 AND length(structured_content_sha256) = 64 AND length(assets_manifest_sha256) = 64`；`page_count > 0`；`schema_name = 'docvortex.middle' AND schema_version = '2.0'`。

### canonical_artifacts

- 列：`id` (CHAR(32)), `parsed_artifact_id` (CHAR(32)), `submission_id` (CHAR(32)), `bucket` (VARCHAR(63)), `schema_version` (VARCHAR(16)), `normalizer_version` (VARCHAR(32)), `status` (VARCHAR(16)), `canonical_object_key` (VARCHAR(1024), nullable), `canonical_sha256` (VARCHAR(64), nullable), `canonical_size_bytes` (BIGINT, nullable), `page_count` (INTEGER, nullable), `block_count` (INTEGER, nullable), `source_parser` (VARCHAR(32)), `source_parser_version` (VARCHAR(64)), `source_sha256` (VARCHAR(64)), `failure_code` (VARCHAR(64), nullable), `created_at` (DATETIME), `updated_at` (DATETIME)。
- 主键：`id`。
- 外键：parsed_artifact_id → parsed_artifacts.id (CASCADE)；submission_id → submissions.id (CASCADE)。
- 唯一键：`(parsed_artifact_id,normalizer_version)`。
- 索引：`ix_canonical_artifacts_submission_created_at`(submission_id,created_at)。
- CHECK：`(status = 'available' AND canonical_object_key IS NOT NULL AND length(trim(canonical_object_key)) > 0 AND length(canonical_sha256) = 64 AND canonical_size_bytes >= 0 AND page_count > 0 AND block_count >= 0 AND failure_code IS NULL) OR (status = 'failed' AND canonical_object_key IS NULL AND canonical_sha256 IS NULL AND canonical_size_bytes IS NULL AND page_count IS NULL AND block_count IS NULL AND failure_code IS NOT NULL)`；`length(trim(bucket)) > 0 AND length(trim(normalizer_version)) > 0`；`status IN ('available', 'failed')`。

### answer_alignment_artifacts

- 列：`id` (CHAR(32)), `submission_id` (CHAR(32)), `assignment_id` (CHAR(32)), `canonical_artifact_id` (CHAR(32)), `canonical_sha256` (VARCHAR(64)), `aligner_version` (VARCHAR(32)), `assignment_questions_digest` (VARCHAR(64)), `status` (VARCHAR(24)), `bucket` (VARCHAR(63)), `object_key` (VARCHAR(1024)), `alignment_sha256` (VARCHAR(64)), `alignment_size_bytes` (BIGINT), `question_count` (INTEGER), `aligned_count` (INTEGER), `review_required_count` (INTEGER), `unmatched_count` (INTEGER), `not_observed_count` (INTEGER), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：submission_id → submissions.id (CASCADE)；assignment_id → assignments.id (CASCADE)；canonical_artifact_id → canonical_artifacts.id (CASCADE)。
- 唯一键：`(canonical_artifact_id,aligner_version,assignment_questions_digest)`。
- 索引：`ix_answer_alignment_submission_created_at`(submission_id,created_at)。
- CHECK：`alignment_size_bytes > 0 AND question_count >= 0 AND aligned_count >= 0 AND review_required_count >= 0 AND unmatched_count >= 0 AND not_observed_count >= 0`；`length(canonical_sha256) = 64 AND length(alignment_sha256) = 64 AND length(assignment_questions_digest) = 64`；`length(trim(bucket)) > 0 AND length(trim(object_key)) > 0 AND length(trim(aligner_version)) > 0`；`status IN ('complete', 'review_required')`。

### answer_review_decisions

- 列：`id` (CHAR(32)), `submission_id` (CHAR(32)), `assignment_id` (CHAR(32)), `question_id` (CHAR(32)), `question_number` (INTEGER), `alignment_status` (VARCHAR(24)), `alignment_artifact_id` (CHAR(32)), `alignment_sha256` (VARCHAR(64)), `aligner_version` (VARCHAR(32)), `assignment_questions_digest` (VARCHAR(64)), `canonical_document_id` (CHAR(32)), `canonical_sha256` (VARCHAR(64)), `review_schema_version` (VARCHAR(16)), `decision` (VARCHAR(24)), `response_regions` (JSONB), `excluded_prompt_regions` (JSONB), `uncertain_regions` (JSONB), `reason_codes` (JSONB), `reviewer_user_id` (CHAR(32), nullable), `reviewer_ref` (VARCHAR(64)), `request_id` (CHAR(32)), `request_sha256` (VARCHAR(64)), `revision` (INTEGER), `supersedes_decision_id` (CHAR(32), nullable), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：submission_id → submissions.id (CASCADE)；assignment_id → assignments.id (CASCADE)；question_id → questions.id (CASCADE)；alignment_artifact_id → answer_alignment_artifacts.id (RESTRICT)；canonical_document_id → canonical_artifacts.id (RESTRICT)；reviewer_user_id → users.id (RESTRICT)；supersedes_decision_id → answer_review_decisions.id (RESTRICT)。
- 唯一键：`(submission_id,question_id,request_id)`；`(submission_id,question_id,revision)`；`(supersedes_decision_id)`。
- 索引：`ix_answer_review_reviewer_user`(reviewer_user_id)。
- CHECK：`(decision = 'response_present' AND jsonb_array_length(response_regions) > 0 AND jsonb_array_length(uncertain_regions) = 0) OR (decision = 'response_absent' AND jsonb_array_length(response_regions) = 0 AND jsonb_array_length(uncertain_regions) = 0 AND jsonb_array_length(excluded_prompt_regions) > 0) OR (decision = 'uncertain' AND jsonb_array_length(response_regions) = 0 AND (jsonb_array_length(uncertain_regions) > 0 OR reason_codes @> '["source_quality_insufficient"]'::jsonb OR reason_codes @> '["response_not_linked_to_question"]'::jsonb))`；`alignment_status IN ('aligned', 'review_required', 'not_observed')`；`decision IN ('response_present', 'response_absent', 'uncertain')`；`jsonb_typeof(response_regions) = 'array' AND jsonb_typeof(excluded_prompt_regions) = 'array' AND jsonb_typeof(uncertain_regions) = 'array' AND jsonb_typeof(reason_codes) = 'array'`；`length(alignment_sha256) = 64 AND length(canonical_sha256) = 64 AND length(assignment_questions_digest) = 64 AND length(request_sha256) = 64`；`length(trim(aligner_version)) > 0 AND review_schema_version = '1.0' AND length(trim(reviewer_ref)) > 0`；`question_number > 0 AND revision > 0`；`reviewer_user_id IS NULL OR reviewer_ref = 'teacher_' || replace(reviewer_user_id::text, '-', '')`。

### visual_evidence_artifacts

- 列：`id` (CHAR(32)), `request_id` (CHAR(32)), `submission_id` (CHAR(32)), `assignment_id` (CHAR(32)), `question_id` (CHAR(32)), `question_number` (INTEGER), `canonical_artifact_id` (CHAR(32)), `canonical_sha256` (VARCHAR(64)), `alignment_artifact_id` (CHAR(32)), `alignment_sha256` (VARCHAR(64)), `original_file_sha256` (VARCHAR(64)), `model_id` (VARCHAR(200)), `prompt_version` (VARCHAR(32)), `preprocessing_version` (VARCHAR(32)), `model_input_digest` (VARCHAR(64)), `outcome` (VARCHAR(40)), `proposal_bucket` (VARCHAR(63)), `proposal_object_key` (VARCHAR(1024)), `proposal_sha256` (VARCHAR(64)), `proposal_size_bytes` (BIGINT), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：submission_id → submissions.id (CASCADE)；assignment_id → assignments.id (CASCADE)；question_id → questions.id (CASCADE)；canonical_artifact_id → canonical_artifacts.id (RESTRICT)；alignment_artifact_id → answer_alignment_artifacts.id (RESTRICT)。
- 唯一键：`(proposal_bucket,proposal_object_key)`；`(submission_id,question_id,request_id)`。
- 索引：`ix_visual_evidence_question_created_at`(question_id,created_at)；`ix_visual_evidence_submission_created_at`(submission_id,created_at)。
- CHECK：`length(canonical_sha256) = 64 AND length(alignment_sha256) = 64 AND length(original_file_sha256) = 64 AND length(model_input_digest) = 64 AND length(proposal_sha256) = 64`；`outcome IN ('candidate_response_present', 'candidate_prompt_only', 'uncertain')`；`proposal_size_bytes > 0 AND length(trim(proposal_bucket)) > 0 AND length(trim(proposal_object_key)) > 0 AND length(trim(model_id)) > 0`；`question_number > 0`。

### grade_input_artifacts

- 列：`id` (CHAR(32)), `request_id` (CHAR(32)), `submission_id` (CHAR(32)), `assignment_id` (CHAR(32)), `question_id` (CHAR(32)), `question_number` (INTEGER), `canonical_artifact_id` (CHAR(32)), `canonical_sha256` (VARCHAR(64)), `alignment_artifact_id` (CHAR(32)), `alignment_sha256` (VARCHAR(64)), `assignment_questions_digest` (VARCHAR(64)), `review_decision_id` (CHAR(32)), `review_revision` (INTEGER), `review_request_sha256` (VARCHAR(64)), `source_file_sha256` (VARCHAR(64)), `answer_key_id` (CHAR(32)), `answer_key_sha256` (VARCHAR(64)), `rubric_sha256` (VARCHAR(64)), `input_sha256` (VARCHAR(64)), `schema_version` (VARCHAR(16)), `bucket` (VARCHAR(63)), `object_key` (VARCHAR(1024)), `object_sha256` (VARCHAR(64)), `object_size_bytes` (BIGINT), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：submission_id → submissions.id (RESTRICT)；assignment_id → assignments.id (RESTRICT)；question_id → questions.id (RESTRICT)；canonical_artifact_id → canonical_artifacts.id (RESTRICT)；alignment_artifact_id → answer_alignment_artifacts.id (RESTRICT)；review_decision_id → answer_review_decisions.id (RESTRICT)；answer_key_id → answer_keys.id (RESTRICT)。
- 唯一键：`(bucket,object_key)`；`(submission_id,question_id,request_id)`。
- 索引：`ix_grade_inputs_question_created`(question_id,created_at)；`ix_grade_inputs_submission_created`(submission_id,created_at)。
- CHECK：`length(canonical_sha256) = 64 AND length(alignment_sha256) = 64 AND length(assignment_questions_digest) = 64 AND length(review_request_sha256) = 64 AND length(source_file_sha256) = 64 AND length(answer_key_sha256) = 64 AND length(rubric_sha256) = 64 AND length(input_sha256) = 64 AND length(object_sha256) = 64`；`length(trim(schema_version)) > 0 AND length(trim(bucket)) > 0 AND length(trim(object_key)) > 0 AND object_size_bytes > 0`；`question_number > 0 AND review_revision > 0`。

### grade_proposal_artifacts

- 列：`id` (CHAR(32)), `request_id` (CHAR(32)), `grade_input_artifact_id` (CHAR(32)), `grade_input_sha256` (VARCHAR(64)), `submission_id` (CHAR(32)), `question_id` (CHAR(32)), `provider` (VARCHAR(32)), `model_id` (VARCHAR(200)), `prompt_version` (VARCHAR(64)), `generation_config_digest` (VARCHAR(64)), `request_fingerprint` (VARCHAR(64)), `grading_status` (VARCHAR(32)), `bucket` (VARCHAR(63)), `object_key` (VARCHAR(1024)), `object_sha256` (VARCHAR(64)), `object_size_bytes` (BIGINT), `prompt_tokens` (BIGINT, nullable), `completion_tokens` (BIGINT, nullable), `model_elapsed_ms` (BIGINT), `created_at` (DATETIME)。
- 主键：`id`。
- 外键：grade_input_artifact_id → grade_input_artifacts.id (RESTRICT)；submission_id → submissions.id (RESTRICT)；question_id → questions.id (RESTRICT)。
- 唯一键：`(bucket,object_key)`；`(request_id)`。
- 索引：`ix_grade_proposals_input_created`(grade_input_artifact_id,created_at)。
- CHECK：`(prompt_tokens IS NULL OR prompt_tokens >= 0) AND (completion_tokens IS NULL OR completion_tokens >= 0)`；`grading_status IN ('completed_candidate', 'grading_abstained') AND length(trim(provider)) > 0 AND length(trim(model_id)) > 0 AND length(trim(prompt_version)) > 0 AND length(trim(bucket)) > 0 AND length(trim(object_key)) > 0 AND object_size_bytes > 0 AND model_elapsed_ms >= 0`；`length(grade_input_sha256) = 64 AND length(request_fingerprint) = 64 AND length(generation_config_digest) = 64 AND length(object_sha256) = 64`。

### grade_review_revisions

- 列：`review_id` (CHAR(32)), `proposal_id` (CHAR(32)), `grade_input_artifact_id` (CHAR(32)), `proposal_sha256` (VARCHAR(64)), `grade_input_sha256` (VARCHAR(64)), `submission_id` (CHAR(32)), `question_id` (CHAR(32)), `review_schema_version` (VARCHAR(16)), `review_revision` (INTEGER), `supersedes_review_id` (CHAR(32), nullable), `reviewer_ref` (VARCHAR(64)), `reviewer_user_id` (CHAR(32), nullable), `request_id` (CHAR(32)), `request_sha256` (VARCHAR(64)), `review_status` (VARCHAR(24)), `criterion_reviews` (JSONB), `review_reason_codes` (JSONB), `review_comment` (TEXT, nullable), `proposed_total_score` (NUMERIC(8, 2), nullable), `reviewed_total_score` (NUMERIC(8, 2), nullable), `created_at` (DATETIME)。
- 主键：`review_id`。
- 外键：proposal_id → grade_proposal_artifacts.id (RESTRICT)；grade_input_artifact_id → grade_input_artifacts.id (RESTRICT)；submission_id → submissions.id (RESTRICT)；question_id → questions.id (RESTRICT)；supersedes_review_id → grade_review_revisions.review_id (RESTRICT)；reviewer_user_id → users.id (RESTRICT)。
- 唯一键：`(proposal_id,request_id)`；`(proposal_id,review_revision)`；`(supersedes_review_id)`。
- 索引：`ix_grade_review_proposal_created`(proposal_id,created_at)；`ix_grade_review_reviewer_user`(reviewer_user_id)。
- CHECK：`((review_status IN ('accepted', 'revised')) AND jsonb_array_length(criterion_reviews) > 0 AND proposed_total_score IS NOT NULL AND reviewed_total_score IS NOT NULL) OR ((review_status IN ('rejected', 'regrading_required')) AND jsonb_array_length(criterion_reviews) = 0 AND reviewed_total_score IS NULL)`；`jsonb_typeof(criterion_reviews) = 'array' AND jsonb_typeof(review_reason_codes) = 'array'`；`review_revision > 0`；`review_schema_version = '1.0' AND length(trim(reviewer_ref)) > 0 AND length(proposal_sha256) = 64 AND length(grade_input_sha256) = 64 AND length(request_sha256) = 64`；`review_status IN ('accepted', 'revised', 'rejected', 'regrading_required')`；`reviewer_user_id IS NULL OR reviewer_ref = 'teacher_' || replace(reviewer_user_id::text, '-', '')`。

## 2. 34 个 OpenAPI 操作

除 `GET /health` 存活探针外，业务操作均注册在 protected_router，使用 OIDC/本地 User/精确策略白名单。GET 通常要求资源所属 Assignment 可读，写要求 can_write；管理操作要求数据库 admin。GradeReview 还验证 proposal/input/submission/question 绑定。当前 admin 兼容性全局访问不是未来匿名阅卷权限。

| 方法 | 完整路径 | 当前权限/性质 |
|---|---|---|
| GET | `/api/v1/health` | 匿名存活；不是DB/S3就绪 |
| POST | `/api/v1/assignments` | 可信User；创建绑定成员/列表按权限过滤 |
| GET | `/api/v1/assignments` | 可信User；创建绑定成员/列表按权限过滤 |
| GET | `/api/v1/assignments/{assignment_id}` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| POST | `/api/v1/assignments/{assignment_id}/questions` | 所属作业可写；写事务仍有自身业务门禁 |
| POST | `/api/v1/assignments/{assignment_id}/publish` | 所属作业可写；写事务仍有自身业务门禁 |
| PUT | `/api/v1/questions/{question_id}/answer-key` | 所属作业可写；写事务仍有自身业务门禁 |
| PUT | `/api/v1/questions/{question_id}/rubric` | 所属作业可写；写事务仍有自身业务门禁 |
| POST | `/api/v1/assignments/{assignment_id}/submissions` | 所属作业可写；写事务仍有自身业务门禁 |
| GET | `/api/v1/assignments/{assignment_id}/submissions` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/file` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/parsing-task` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/parsed-document` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/parsed-document/markdown` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/canonical-document` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/canonical-document/pages/{page_number}` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/answer-alignment` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/answer-alignment/questions/{question_id}` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| PUT | `/api/v1/assignments/{assignment_id}/members/{user_id}` | 数据库admin；管理审计 |
| DELETE | `/api/v1/assignments/{assignment_id}/members/{user_id}` | 数据库admin；管理审计 |
| GET | `/api/v1/me` | 当前数据库User |
| GET | `/api/v1/admin/teachers` | 数据库admin；管理审计 |
| POST | `/api/v1/admin/teachers` | 数据库admin；管理审计 |
| PATCH | `/api/v1/admin/teachers/{user_id}` | 数据库admin；管理审计 |
| GET | `/api/v1/admin/teachers/{user_id}/memberships` | 数据库admin；管理审计 |
| GET | `/api/v1/admin/audit-events` | 数据库admin；管理审计 |
| GET | `/api/v1/submissions/{submission_id}/answer-reviews` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/answer-reviews/questions/{question_id}` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| POST | `/api/v1/submissions/{submission_id}/answer-reviews/questions/{question_id}` | 所属作业可写；写事务仍有自身业务门禁 |
| GET | `/api/v1/submissions/{submission_id}/questions/{question_id}/grade-proposals/{proposal_id}/review` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| GET | `/api/v1/submissions/{submission_id}/questions/{question_id}/grade-proposals/{proposal_id}/reviews` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |
| POST | `/api/v1/submissions/{submission_id}/questions/{question_id}/grade-proposals/{proposal_id}/reviews` | 所属作业可写；写事务仍有自身业务门禁 |
| GET | `/api/v1/assignments/{assignment_id}/workbench` | 所属作业可读；文件/原文敏感；不适用匿名题级授权 |

没有考试/扫描批次/阅卷派单/成绩发布/设备/自定义岗位 HTTP。知识库、Copilot、视觉建议和评分生成有受控服务/CLI，不等于匿名公开HTTP。现有HTTP只暴露已登记的候选人工审核，不自动生成并发布成绩。

## 3. 迁移与可验证性

16 个 revision，唯一代码 head `c72d9e14a510`。`alembic heads/history` 不验证部署库；没有运行 `alembic current/check/upgrade` 或开发数据库查询。新迁移的真实PG/S3及升级/回退验收在下一阶段独立测试库完成。
