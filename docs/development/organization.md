# 单校组织、学生身份与两阶段名册导入（B-1.1b）

本文说明 B-1.1b「可信组织、学年学期与学生导入」切片的真实实现范围、数据库约束、HTTP/CLI 合同、安装级权限边界与后续缺口。证据来自当前分支 `feat/b11b-school-organization` 的源码、迁移 [b11b0a7c4e21](../../migrations/versions/b11b0a7c4e21_organization_students.py)、由 `app.openapi()` 生成的合同与本切片测试定义；**没有连接或升级开发数据库，没有运行 pytest 集成测试，没有调用模型**。

## 0. 状态标签与证据边界

全文只用以下五个标签：

| 标签 | 含义 |
|---|---|
| **[已实现]** | 当前分支工作树包含真实源码/迁移/HTTP 合同（非 Mock），由本文阅读代码与导出的 OpenAPI 证实。 |
| **[已测试]** | 本轮在真实环境中执行通过、并有可复现结果的自动化测试/检查（执行范围与证据见[第 9 节](#9-验证证据与测试状态)）。测试定义存在但尚未执行通过的不使用此标签。 |
| **[仅设计]** | 只有冻结的接口/表结构/文档约定，服务实现尚未落地，或已明确列为后续实现；不能据此调用或宣称可用。 |
| **[暂未实现]** | 本切片明确不做、也没有可执行入口的能力。 |
| **[需学校确认]** | 学校政策/责任人/数据范围问题；确认前按安全默认关闭，不自行猜测补齐。 |

边界提醒：

- 本切片是**单校**组织的后端基础，不是多校租户或完整学校 RBAC。
- 组织/学生数据不属于 `legacy_homework` 资源图，旧 `User.is_admin`、`AssignmentMembership`、JWT 自声明角色都不会授予组织权限。
- 本文不把「表已建、接口已登记」写成「考试业务已实现」：考试场次、试卷、参试名单、扫描、分卷、阅卷、成绩仍全部暂未实现，见[第 8 节](#8-明确不包含范围)。
- 开发数据库迁移状态未知；破坏性降级会被迁移主动拒绝（见 [2.6](#26-迁移升级前置与破坏性降级拒绝)）。

### 能力状态总览

| 能力 | 状态 | 证据位置 |
|---|---|---|
| 学年 `AcademicYear`、学期 `AcademicTerm` 数据模型与只读接口 | **[已实现]** | `academic_years` / `academic_terms` 表与迁移；`GET /organization/academic-years`、`/academic-terms` |
| 入学届别 `GradeCohort` 与派生年级（不是学生永久属性） | **[已实现]** | `grade_cohorts` 表；`service.grade_label()` |
| 学科 `Subject` | **[已实现]** | `subjects` 表；`GET /organization/subjects` |
| 行政班 `AdministrativeClass`（≠教学班/考场） | **[已实现]** | `administrative_classes` 表；`GET /organization/classes` |
| 学生稳定身份（学号唯一、姓名非键、姓名不自动合并） | **[已实现]** | `students` 表；`GET /organization/students` |
| 半开区间 `[valid_from, valid_to)` 班级归属历史与显式转班 | **[已实现]** | `class_enrollments` 表、GiST 排他约束、历史保护触发器；受控 CLI `transfer-student` |
| 两阶段 CSV 名册导入（预览 → 确认） | **[已实现]**（实现见 [3.10](#310-当前实现与测试状态)） | `student_import_batches` / `student_import_rows` 表；`/organization/student-imports` 4 个操作 |
| 安装级组织权限（4 个固定动作）与角色版本 | **[已实现]** | 迁移插入的权限/角色；`dependencies.ORGANIZATION_POLICIES`；`operations.authorize()` |
| 旧 `is_admin`/成员关系/客户端角色（明确无组织授权，无旁路） | **[已实现]**（= 拒绝语义已实现并验证） | `ORGANIZATION_POLICIES` 与 `resource_security.require_permission`；无 legacy 过滤图入口 |
| 组织维护受控 CLI | **[已实现]** | `python -m huipi_cloud.modules.organization` |
| OpenAPI/TypeScript 合同同步（11 个新操作，无既有操作漂移） | **[已测试]** | `scripts/export_frontend_openapi.py`、`npm run api:generate`、`npm run typecheck`、`npm run lint`（本轮执行通过） |
| 导入子系统测试（单元 + 真实 PostgreSQL 集成） | **[已测试]**（单元 63 passed、真实 PG 集成 19 passed，专用测试库；最终以统一验收为准） | `tests/unit/test_organization_csv.py`、`tests/integration/test_student_import.py` |
| 组织/权限/迁移真实 PostgreSQL 测试 | **[已测试]**（领域单测 23 passed；真实 PG 组织/授权 16 passed；独立迁移往返 1 passed；全量后端 961 passed/16 skipped） | `tests/integration/test_organization*.py`、`tests/unit/test_organization_domain.py` |
| 教学班、任教关系、按年级/班级的组织 Scope | **[暂未实现]** | 见 [4.6](#46-普通任课教师查看本班名单暂未实现) 与 [8](#8-明确不包含范围) |
| 考试场次、试卷版本、参试名单、扫描与阅卷 | **[暂未实现]** | 见 [7](#7-与后续切片的关系b-12-前置缺口) |
| 组织/名册前端页面 | **[暂未实现]** | 本切片不改 Vue 页面 |
| 学生登录/学生身份认证 | **[暂未实现]** | 学生表没有登录凭据或账号绑定 |

---

## 1. 术语与范围

| 术语 | 本切片的真实语义 | 明确不是 |
|---|---|---|
| 学年 `AcademicYear` | 有唯一 `code`、起止日期与 `planned/active/closed` 状态的学年容器。 | 不是租户；不是考试周期。 |
| 学期 `AcademicTerm` | 学年内按 `term_no`（1–4）与 `code` 唯一划分、用 GiST 排他约束禁止同学年时间重叠的学期。 | 不是教学班/考试场次的排课表。 |
| 入学届别 `GradeCohort` | 以入学年份 `intake_year` 标识的届别（如 2025 级）；年级由「学年起始年 - 入学年 + 1」派生，仅为 1/2/3 时映射高一/高二/高三。 | 不是学生身上的永久 `grade_level` 字段；未实现升年级批处理。 |
| 学科 `Subject` | 只有 `code`、`display_name`、`is_active` 的受控字典。 | 不是教学班，也不含任课教师。 |
| 行政班 `AdministrativeClass` | 隶属某个入学届别的固定班号（`code` 全校唯一）。 | 不是教学班（走班）、不是考场、不是座位安排。 |
| 学生 `Student` | 校内稳定身份：`school_student_code` 唯一，`name` 非唯一、仅可检索；没有身份证号/家长联系方式。 | 不是国家学籍系统同步对象；不是登录账号。 |
| 班级归属 `ClassEnrollment` | 学生与行政班之间的半开区间历史 `[valid_from, valid_to)`；同一学生同一时刻最多一条。 | 不是「当前班级」单字段覆写；不是教学班选课。 |
| 名册导入批次 `StudentImportBatch` / 行 `StudentImportRow` | 一次 CSV 上传的冻结预检快照；确认是独立的受控事务。 | 不是考试名单/参试名单锁版；原始 CSV 字节不落库。 |
| 组织操作 `OrganizationOperation` | 组织写操作的追加式幂等/审计事实（actor、request_id、payload SHA、结果摘要）。 | 不是学生数据的第二副本；不含姓名/学号原值。 |

本切片的前置是 [B-1.1a 资源安全基础](../security/exam-resource-security.md)；范围定义见[分切片路线](business-reconstruction-roadmap.md)。

---

## 2. 数据模型与真实 PostgreSQL 约束

迁移 `b11b0a7c4e21`（down_revision `e21a9b4c110a`）新增 **10 张表**，并补一条 B-1.1a 遗留的授权唯一索引。源码见 [models.py](../../src/huipi_cloud/modules/organization/models.py)，DDL 见[迁移文件](../../migrations/versions/b11b0a7c4e21_organization_students.py)。

### 2.1 表与键约束

| 表 | 关键列 | 真实约束 |
|---|---|---|
| `academic_years` | `id`、`code`、`display_name`、`start_date`、`end_date`、`status` | `code` 唯一；`end_date > start_date`；`status IN ('planned','active','closed')`；文本非空。 |
| `academic_terms` | `id`、`academic_year_id`、`term_no`、`code`、起止日期 | 唯一 `(academic_year_id, term_no)`、`(academic_year_id, code)`；`term_no > 0`；`end_date > start_date`；同上 GiST 排他约束；`academic_year_id` 外键 `RESTRICT`。 |
| `grade_cohorts` | `id`、`code`、`display_name`、`intake_year` | `code` 唯一；`intake_year BETWEEN 2000 AND 2100`。 |
| `subjects` | `id`、`code`、`display_name`、`is_active` | `code` 唯一；文本非空。 |
| `administrative_classes` | `id`、`grade_cohort_id`、`code`、`display_name`、`status` | `code` 全校唯一；`grade_cohort_id` 外键 `RESTRICT`；`status IN ('active','inactive')`；索引 `(grade_cohort_id, status)`。 |
| `students` | `id`、`school_student_code`、`name`、`is_active` | `school_student_code` 唯一；`name` 非空但**没有唯一约束**，仅有普通索引 `ix_students_name` 供检索；无身份证明/家长字段。 |
| `class_enrollments` | `id`、`student_id`、`administrative_class_id`、`valid_from`、`valid_to`、`source`、`source_import_batch_id` | 两个外键 `RESTRICT`；`valid_to IS NULL OR valid_to > valid_from`；`source IN ('manual','student_import')`；GiST 排他约束（见 2.2）；索引 `(administrative_class_id, valid_from)`、`(student_id)`。 |
| `student_import_batches` | `id`、`mode`、`status`、`input_sha256`、`file_size_bytes`、`row_count`、`academic_year_id`、`requested_by_user_id`、`preview_counts`、`confirm_result`、`confirmed_*` | `mode IN ('student_only','enrollment_only','student_with_enrollment')`；`status IN ('previewed','confirmed')`；`length(input_sha256)=64`；`file_size_bytes > 0`；`row_count > 0`；`preview_counts` 必须是 JSON 对象；请求/确认用户外键 `RESTRICT`。 |
| `student_import_rows` | `id`、`batch_id`、`row_number`、`student_code`、`normalized_student_code`、`name`、`class_code`、`valid_from`、`row_state`、`outcome`、`errors`、`warnings` | 唯一 `(batch_id, row_number)`；`row_number > 1`（CSV 第 1 行为表头）；`row_state IN ('valid','warning','error')`；`errors`/`warnings` 必须是 JSON 数组。 |
| `organization_operations` | `id`、`actor_user_id`、`request_id`、`action`、`payload_sha256`、`result`、`created_at` | 唯一 `(actor_user_id, request_id)`；`length(payload_sha256)=64`；`result` 必须是 JSON 对象；`action` 有固定枚举 CHECK（见 2.4）。 |

所有新增外键均为 `ondelete="RESTRICT"`：没有级联删除学生、班级或归属历史。

### 2.2 两条 GiST 排他约束

迁移以 `CREATE EXTENSION IF NOT EXISTS btree_gist` 建立 UUID 等值与日期范围组合索引（需数据库所有者权限；部署前须知）。

- **同一学生同一时刻唯一行政班**：`class_enrollments` 上的 `ex_class_enrollments_student_time` 使用 `(student_id =)` + `daterange(valid_from, valid_to, '[)') &&`。区间为**半开** `[valid_from, valid_to)`：`valid_to` 当天不再属于该班；`valid_to IS NULL` 表示仍有效。数据库层面即拒绝重叠归属，而不是只靠 Python 先读后写。
- **同一学年内学期不重叠**：`academic_terms` 上的 `ex_academic_terms_year_range` 使用 `(academic_year_id =)` + `daterange(start_date, end_date, '[]') &&`。学期区间是**闭区间** `[]`，同学年内两个学期共享端点日期也会被拒绝。
- 另由触发器 `validate_academic_term_range`（BEFORE INSERT/UPDATE）校验学期起止日期落在所属学年起止日期内；学年不存在时抛 `23503`。应用层在 `service.create_academic_term()` 中先返回稳定错误码：学年不存在 `academic_year_not_found` 404、已关闭 `academic_year_closed` 409、超出学年范围 `academic_term_outside_academic_year` 422；数据库约束是并发/直连写入的最终防线。

### 2.3 历史与冻结保护触发器

- `protect_class_enrollment_history`（`class_enrollments`，BEFORE UPDATE OR DELETE）：禁止 DELETE；`student_id`、`administrative_class_id`、`valid_from`、`source`、`source_import_batch_id`、`created_at` 不可变；`valid_to` **只能从 NULL 设置一次**，已设置后不可改。因此「转班」= 关闭旧行的 `valid_to` + 插入新行，历史不覆写。
- `protect_student_import_batch`（`student_import_batches`，BEFORE UPDATE OR DELETE）：禁止 DELETE；`confirmed` 批次完全不可变；`previewed` 批次只允许把状态置为 `confirmed` 并写确认字段，`mode/input_sha256/file_size_bytes/row_count/academic_year_id/requested_by_user_id/preview_counts/created_at` 不可变。
- `immutable_student_import_rows`：预检行禁止 UPDATE/DELETE，是冻结快照。
- `immutable_organization_operations`：组织审计/幂等事实禁止 UPDATE/DELETE（应用角色边界；数据库所有者与 TRUNCATE 不在防护范围）。
- 触发器只覆盖应用业务角色；持有数据库凭据的受控运维仍属可信部署边界，不应向教师或模型开放数据库凭据。

### 2.4 组织操作审计与幂等

`organization_operations.action` 只允许：`academic_year.created`、`academic_term.created`、`grade_cohort.created`、`subject.created`、`administrative_class.created`、`class_enrollment.created`、`student.transferred`、`student_import.previewed`、`student_import.confirmed`。

写事务包装在 [operations.operation()](../../src/huipi_cloud/modules/organization/operations.py)：

1. 开启短事务并设 `statement_timeout=5000ms`；以 `identity.refresh_principal(lock=True)` 重新读取**当前启用用户与角色**。
2. 对 `(principal.user_id, request_id)` 取事务级 advisory lock，再查 `organization_operations`：同一 request_id 但 payload SHA 不同 → 409 `organization_request_conflict`；相同则返回首次结果（幂等重放仍重新校验当前权限）。
3. 业务写 + 审计 INSERT 同事务提交。COMMIT 结果未知时返回 503 `organization_commit_outcome_unknown`，原请求同 `request_id` 恢复；确定性数据库/系统故障为 503 `organization_operation_unavailable`。**不承诺 Exactly Once**，也不因一次异常断言已回滚。

### 2.5 学生身份与归属语义

- 学生以 `school_student_code` 唯一标识；**姓名不是键**，姓名候选不会自动合并或纠正身份。
- 年级不存放在 `students` 上；`service.grade_label(intake_year, academic_year_start_year)` 按「学年起始年 - 入学年 + 1」派生，只有 1/2/3 返回 `(1,"高一")`、`(2,"高二")`、`(3,"高三")`，其余返回 `None`（已毕业或尚未入学）。当前没有升年级/毕业批处理任务。
- `current_enrollment()` 判定当前归属时同时检查两个边界：`valid_from <= current_date` 且（`valid_to IS NULL` 或 `valid_to > current_date`）。未来生效的归属属于历史/计划，不会提前被当作当前班。该函数只用于列表查询与转班的「当前」语义；学生导入的归属冲突判定按区间重叠计算，见 [3.6](#36-确认请求当前状态重验与冲突)。
- 转班 `transfer_student`：锁定学生行 `FOR UPDATE`；要求学生启用、目标班存在且为 `active`；不允许转入当前班；`effective_from` 必须晚于当前归属 `valid_from`；将旧行 `valid_to` 设为 `effective_from`（半开区间，生效当天起旧班失效），再插入同日起生效的新行；排他约束冲突映射为 409 `enrollment_overlap`，历史不可变触发器冲突同样稳定映射。整个转班与审计同事务。

### 2.6 迁移升级前置与破坏性降级拒绝

- 升级前检查 `security_grants`：同一 `(user_id, role_version_id, scope_id)` 存在多条未撤销 Grant 时，迁移**拒绝执行**（`security_grant_duplicates_require_cleanup`），须先按审计人工清理。
- 迁移新增部分唯一索引 `uq_security_grants_active`（`UNIQUE (user_id, role_version_id, scope_id) WHERE revoked_at IS NULL`），修复 [B-1.1a 已知限制](../security/exam-resource-security.md)：重复授予被稳定映射为 409 `security_grant_already_active`，一次 revoke 不再可能遗漏同组合的重复 Grant。
- 迁移写入 B-1.1b 权限目录与三个封存角色版本（见 [4.3](#43-角色版本与-bootstrap)）；因 B-1.1a 触发器禁止修改已封存角色，迁移先在未封存状态插入权限再封存，并在 downgrade 中于同一事务内临时关闭相关用户触发器完成受控清理。
- **破坏性降级拒绝**：只要存在任一学年/学期/届别/学科/行政班/学生/归属/导入批次/组织操作，或仍有 Grant 引用三个 B-1.1b 角色版本，`downgrade` 即抛 `organization_downgrade_requires_archived_new_data`。生产回滚应保留新数据并向前修复/恢复备份，不能靠 downgrade 删除学生与归属历史。

---

## 3. 两阶段 CSV 名册导入合同

导入是 B-1.1b 中**唯一**允许经 HTTP 写入名册数据的入口；组织基础字典与显式转班仍只经受控 CLI（见[第 6 节](#6-受控-cli)）。

### 3.1 两阶段语义

1. **预览（preview）[已实现]**：上传 CSV，解析并校验后，只写 `student_import_batches` + `student_import_rows` 两种冻结预检记录；**绝不写** `students`、`class_enrollments` 或任何正式名册数据。返回行级结构化错误/警告与计数，供操作员修正后重新上传。
2. **确认（confirm）[已实现]**：对某个 `previewed` 批次在**单一事务**内重读当前数据库状态并重验；任何与预检不一致或阻塞错误都整体回滚（409），不做部分入库。成功时创建学生/归属并写组织审计，批次置为 `confirmed` 且此后不可变。

### 3.2 三种导入模式与必需列

表头名为固定英文列名；解析时按 `strip + NFKC + lower_case` 规范化后精确匹配（`Student_Code` 可接受，但下划线等字符本身必须一致）。

| 模式 | 必需且仅允许的列 | 语义 |
|---|---|---|
| `student_only` | `student_code`、`name` | 只建立/更新学生稳定身份，不写班级归属。 |
| `enrollment_only` | `student_code`、`class_code`、`valid_from` | 为已存在学生建立行政班归属；学生必须已存在且启用。 |
| `student_with_enrollment` | `student_code`、`name`、`class_code`、`valid_from` | 建立/匹配学生并同时建立归属。 |

未知列、重复列、缺失必需列 → 422 `student_import_columns_invalid`。没有「可选列」概念。表头顺序不限（按列集合匹配），列集合必须与所选模式完全一致；CSV 中第一个非空行视为表头。

### 3.3 文件编码与大小限制

| 限制 | 真实行为 | 错误码/状态 |
|---|---|---|
| 编码 | 只接受 UTF-8 或 UTF-8 BOM（以 `utf-8-sig` 解码）；UTF-16/UTF-32 BOM 或含 NUL 的二进制内容明确拒绝；其余非法 UTF-8 字节明确拒绝，**不猜测 GBK/GB18030 等编码** | 415 `student_import_file_encoding_unsupported` / 415 `student_import_file_encoding_invalid` |
| 文件大小 | `MAX_FILE_BYTES = 2 * 1024 * 1024`（2 MiB）；路由读取 `MAX_FILE_BYTES + 1` 字节后由服务判定 | 413 `student_import_file_too_large` |
| 数据行数 | `MAX_ROWS = 5000`（不含表头） | 413 `student_import_row_limit_exceeded` |
| 空文件 | 空内容、只有表头或没有数据行 | 422 `student_import_file_empty` |
| CSV 语法 | `csv.reader(strict=True)` 解析失败（引号/结构错误） | 422 `student_import_csv_malformed` |

行号使用 `csv.reader.line_num` 的**真实物理行号**（表头为 1），空行跳过但行号连续。解析函数是纯函数：`parse_student_import_file(mode, content) -> ParsedFile`，不访问数据库。

### 3.4 行状态、错误结构与 outcome

- `row_state ∈ valid / warning / error`。
- `errors` / `warnings` 中每个条目结构固定为 `{row_number: int, field: str|null, code: str, message: str}`；`message` 是通用中文说明，**不包含学号/姓名原值**。
- 行 `outcome`（确认后语义）稳定枚举：`create_student`、`create_student_and_enrollment`、`create_enrollment`、`enrollment_only_create`、`unchanged`；`error` 行的 `outcome` 在预检阶段置 `null`。
- 解析层错误码（行级）：`student_code_required`、`student_code_invalid`（不满足 `^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$`）、`name_required`、`name_invalid`（超长或含控制字符）、`class_code_required`、`class_code_invalid`、`valid_from_required`、`valid_from_invalid`（必须是严格 ISO `YYYY-MM-DD`）、`row_field_count_invalid`、`duplicate_student_code_in_batch`、`student_code_case_ambiguous`。
- 进数据库校验后可能出现的行级错误码：`class_code_not_found`、`administrative_class_inactive`、`valid_from_outside_academic_year`、`student_name_mismatch`、`student_code_not_found`、`student_inactive`、`enrollment_overlap`、`enrollment_conflict_requires_transfer`。
- 唯一的 warning 码：`duplicate_name_existing`（存在同名不同学号的学生：允许共存，不阻断，不自动合并）。
- 同批次内学号完全相同 → 这些行 `duplicate_student_code_in_batch`；`casefold` 后相同的多个学号写法 → `student_code_case_ambiguous`。两类检测独立，同一行可能同时得到两类错误；任何不确定都不猜测身份。

### 3.5 预览响应与截断

`POST /api/v1/organization/student-imports` 返回 201 + `StudentImportPreviewResult`：

```json
{
  "batch_id": "UUID",
  "mode": "student_with_enrollment",
  "status": "previewed",
  "input_sha256": "64位SHA-256",
  "file_size_bytes": 1234,
  "row_count": 42,
  "academic_year_id": "UUID",
  "counts": {
    "row_total": 42, "error_rows": 0, "warning_rows": 1, "valid_rows": 41,
    "create_students": 10, "existing_students": 31,
    "create_enrollments": 41, "unchanged_rows": 0
  },
  "can_confirm": true,
  "errors": [],
  "warnings": [{"row_number": 3, "field": "name", "code": "duplicate_name_existing", "message": "存在同名但不同学号的学生，请人工确认"}],
  "truncated": false,
  "created_at": "ISO-8601"
}
```

- `preview_counts` 的键固定为 `row_total`、`error_rows`、`warning_rows`、`valid_rows`、`create_students`、`existing_students`、`create_enrollments`、`unchanged_rows`。
- 返回的 `errors`/`warnings` 各最多 `MAX_PREVIEW_ENTRIES = 500` 条，超出时 `truncated=true`；完整行级明细保存在冻结的 `student_import_rows` 中。
- `can_confirm` 仅在预检可确认时（无 `error` 行、批次未确认）为 `true`；存在阻塞错误时只能修正后重新预览。
- **原始 CSV 字节不落数据库**：批次只保存 `input_sha256` 与 `file_size_bytes`；冻结行只保存规范化后的学号（`student_code` 与 `normalized_student_code` 同存规范化值）、规范化姓名、班级编码与生效日期，不保存原始未净化文本，也不把 CSV 原件写入 PostgreSQL 或对象存储。真实名册数据导入前的隐私/保留责任见 [10](#10-需学校确认事项)。

### 3.6 确认请求、当前状态重验与冲突

```http
POST /api/v1/organization/student-imports/{batch_id}/confirm
Content-Type: application/json

{"request_id": "UUID", "expected_input_sha256": "可选，64位十六进制"}
```

确认事务锁定批次行 `FOR UPDATE`，用与预览**同一个**当前状态评估器重算全部行，任何不一致整体回滚并返回 409：

- 学年仍存在且未 `closed`；`valid_from` 仍落在学年区间内。确认时学年缺失或被置为 `closed` → 409 `student_import_preview_stale`；`academic_year_closed` 只在预览阶段返回。
- 目标行政班仍存在且为 `active`；学生是否出现、姓名规范化后是否变化、学生是否被停用。
- 归属冲突按**区间重叠**而非「当前生效」判定：导入归属视为半开区间 `[row.valid_from, +∞)`，与既有归属 `[e.valid_from, e.valid_to)` 相交即冲突（`e.valid_to IS NULL` 或 `e.valid_to > row.valid_from`）。因此**未来生效**的既有归属也算冲突：同班重叠 → `enrollment_overlap`，其它班（含未来区间）→ `enrollment_conflict_requires_transfer`；同一未来生效归属重复导入时，第二次预检结果为 `unchanged`，而不是确认阶段的数据库 409。
- 该判定**不复用** `service.current_enrollment()`（后者只服务列表/转班的「当前」语义）；预检与确认共用导入专用的评估逻辑，PostgreSQL 排他约束只是并发写入的最后防线。

固定错误码：

| 场景 | 状态 | 错误码 |
|---|---|---|
| 预检后数据变化（含确认时学年被关闭/缺失、班级停用、学生状态或归属变化），需重新预览 | 409 | `student_import_preview_stale` |
| `expected_input_sha256` 与批次不符 | 409 | `student_import_conflict` |
| 批次已确认 | 409 | `student_import_already_confirmed` |
| 预检含阻塞 error 行 | 409 | `student_import_has_blocking_errors` |
| 批次不存在或无权限 | 404 | `resource_not_found_or_forbidden` |
| 同 request_id 不同正文 | 409 | `organization_request_conflict` |
| COMMIT 结果未知 | 503 | `organization_commit_outcome_unknown` |
| 授权/数据库故障 | 503 | `organization_operation_unavailable` |

成功返回 `StudentImportConfirmResult`：`batch_id`、`status="confirmed"`、`created_students`、`created_enrollments`、`unchanged_rows`、`rejected_rows`（阻塞错误已被 409 拦截，故为 0）、`confirmed_at`、`replayed`。同一 `request_id` + 相同正文重放返回首次结果，不重复写入；重放仍重新校验当前权限。

### 3.7 导入的权限与审计

- 预览要求 `student.import.preview`，确认要求 `student.import.confirm`（安装级 Scope，见第 4 节）；状态/历史查询要求 `organization.read`。
- 预览写 `organization_operations.action = student_import.previewed`，确认写 `student_import.confirmed`；payload 摘要只含 `mode/academic_year_id/filename/input_sha256/file_size_bytes`（预览）与 `batch_id/expected_input_sha256`（确认）。
- `organization_operations.result` 只保存计数、内部 ID 与行号级结构化错误，不保存姓名/学号；审计与业务写同事务提交。

### 3.8 导入不替代考试名单

名册导入批次是**组织级可信学生/归属**事实，不是考试参试名单。考试名单必须由 B-1.2 的 `RosterRevision` 按场次冻结行政班/姓名展示/考号等快照（暂未实现）；导入历史不自动成为任何考试的名单版本，也不改变旧提交的 `student_ref`。

### 3.9 导入接口的 OpenAPI 类型

生成的 `frontend/src/api/generated.ts` 包含 multipart 请求体 `Body_preview_student_import_api_v1_organization_student_imports_post`（`request_id`、`mode`、`academic_year_id`、`file`）以及 `StudentImport*` 响应类型；前端没有对应页面（暂未实现），类型仅供后续前端合同使用。

### 3.10 当前实现与测试状态

- 预览/确认/状态/历史四个 HTTP 操作与冻结错误码表已在 `src/huipi_cloud/modules/organization/import_service.py` 与 `csv_import.py` 落地；纯解析函数与 `MAX_FILE_BYTES/MAX_ROWS/MAX_PREVIEW_ENTRIES` 常量是共享合同。
- 测试定义与执行：`tests/unit/test_organization_csv.py`（纯解析：模式/列/编码/上限/行级错误/outcome，单元 63 passed）与 `tests/integration/test_student_import.py`（真实 PostgreSQL：预览不污染正式表、确认幂等/409/预览后变化、审计失败回滚、COMMIT 未知恢复、真实 JWT 授权/撤权、未来归属重复导入，19 passed，专用测试库）。最终以本切片统一验收为准。

---

## 4. 权限：仅安装级 Scope 的最小过渡实现

### 4.1 四个固定动作

迁移在既有 `permission_definitions` 中追加并启用四个动作（**只有安装级 Scope**）：

| 动作 | 允许的操作 |
|---|---|
| `organization.read` | 读取学年/学期、入学届别、学科、行政班、最小学生身份、班级归属历史、导入批次状态/历史。 |
| `organization.manage` | 受控创建学年/学期/届别/学科/行政班、显式转班（当前仅 CLI）。 |
| `student.import.preview` | 上传 CSV 生成冻结预检批次；不修改正式学生/归属。 |
| `student.import.confirm` | 单一事务重验当前状态后确认入库。 |

双保险：代码固定目录 `resource_security.service.CATALOG` 与数据库 `permission_definitions.enabled/scope_kinds` 都必须允许；即便数据库把未来动作改成 enabled，代码目录仍拒绝。`organization.operations.authorize()` 只接受这四个动作，其他动作直接 `organization_permission_disabled`。

### 4.2 只存在安装级 Scope

- Scope 只有既有两种：指向唯一 `SecurityInstallation(id=1)` 的安装范围，或指向真实 Assignment 的范围。B-1.1b **没有新增按年级/班级/考试的组织 Scope 类型**。
- 四个组织动作的 `scope_kinds` 都只有 `installation`；`organization.operations.authorize()` 固定调用 `security.installation_scope()` 后 `require_permission`。
- 因此当前实现是「单校安装级最小过渡权限」，**不是**按年级/班级细分的学校 RBAC；不能提交自由 UUID/JSON Scope。组织实体 Scope 必须在真实 FK、归属校验与执行策略落地后另开切片。

### 4.3 角色版本与 bootstrap

迁移新增三个**已封存**角色版本（固定 UUID）：

| 角色版本 | UUID | 权限 |
|---|---|---|
| `organization_operator` | `11111111-b11b-4000-8000-000000000001` | `security.grants.manage` + 四个组织动作；部署 bootstrap 授予的根角色。 |
| `organization_manager` | `11111111-b11b-4000-8000-000000000002` | `organization.read` + `organization.manage`。 |
| `student_importer` | `11111111-b11b-4000-8000-000000000003` | `organization.read` + `student.import.preview` + `student.import.confirm`。 |

- B-1.1b 起 `deployment_bootstrap` 授予 `organization_operator`（而非 B-1.1a 的 `security_operator`），使「单 Grant 同时覆盖 manage 与拟委派动作/完整 Scope」的委派规则可用。已初始化过的旧部署必须由受控运维显式补授，不得恢复旧管理员捷径。
- 角色版本封存后不可改权限；扩展必须新版本。授权固定绑定真实 User + RoleVersion + Scope；不能从 JWT role/is_admin 或旧成员关系自动映射。
- 委派：同一个有效 Grant 必须同时覆盖 `security.grants.manage`、全部拟委派动作及完整目标 Scope；禁止自授；同一 User/RoleVersion/Scope 的活动 Grant 唯一，重复授予 409 `security_grant_already_active`；撤权为单 Grant 语义，撤权提交后新操作立即拒绝（不承诺取消在途文件流/事务）。
- 每次请求与写事务都会重新读取当前启用用户与有效 Grant（SHARE 锁与撤权 UPDATE 排队），不使用跨请求授权缓存。

### 4.4 旧 `is_admin` / AssignmentMembership 不授权组织数据

- `dependencies.py` 把 11 个组织操作登记到精确路由策略表 `ORGANIZATION_POLICIES`，在进入路由前用 `organization.service.authorize()` + `resource_security.require_permission()` 检查安装级动作；**不会**进入 legacy 作业的 `require_assignment()` 管理员/成员分支。
- 组织/学生表不在 B-1.1a 的 legacy 可见性图里；旧作业接口不会因为组织数据而多返回内容，旧 `/me` 能力字符串也不包含组织动作。
- JWT 的 `role`/`is_admin` 字段、前端路由与页面状态都不授权；普通教师仅凭作业成员关系调用组织接口会被拒绝（无安装级 Grant 时统一安全 404/403 语义）。
- `GET /me` 的能力投影目前**不包含**任何组织动作（只有 legacy 作业/管理能力），因此前端不能从 `/me` 判断组织权限；若后续前端需要组织入口，必须另立只读能力投影，而不能用 `is_admin`/角色字符串推断（暂未实现）。

### 4.5 权限运维入口

- 部署初始化：`uv run python -m huipi_cloud.modules.resource_security bootstrap ...`（见[资源安全基础](resource-security.md)）授予 `organization_operator`。
- 后续授权/撤销：`grant`/`revoke` 受控 CLI，要求 `security.grants.manage` 与完整 Scope；部署变更需独立审批与操作审计。
- 组织写操作：`python -m huipi_cloud.modules.organization` 受控 CLI（第 6 节）。

### 4.6 普通任课教师查看本班名单**[暂未实现]**

- 当前没有教学班 `TeachingClass`、任教关系 `TeachingAssignment`、班级 Scope 或名单范围动作；`GET /organization/students` 是**安装级组织管理员**的全校最小身份查询，**不得**当作「教师查看本班名单」的替代品开放给普通教师，也不得仅凭前端隐藏字段。
- 后续需要：可信任教关系真实 FK、教学班/行政班范围实体 Scope、名单读取动作（如 `roster.read`）、按班级裁剪与越权负向测试、审计与撤权语义。在这些落地前，普通教师查看本班名单保持关闭。
- 学生登录/学生本人查询同样暂未实现；学生表不是账号表。

---

## 5. HTTP API 合同（11 个操作）

所有组织路由前缀 `/api/v1/organization`，通过 `OrganizationRoute` 统一：

- 成功响应头带 `Cache-Control: private, no-store`；认证/业务错误由全局 handler 返回 `Cache-Control: no-store`。
- 路由整体 30 秒超时；数据库/系统/超时故障返回 503 `organization_operation_unavailable`，不泄露 SQL 或学生字段。
- 所有操作要求已登记且启用的可信用户 + 当前安装级 Grant；401/403/404/409/422/503 错误 `detail` 为固定字符串码。

| # | 方法与路径 | 动作 | 说明 |
|---|---|---|---|
| 1 | `GET /organization/academic-years` | `organization.read` | 学年分页，按起始日期倒序。 |
| 2 | `GET /organization/academic-terms?academic_year_id=` | `organization.read` | 学期分页，必须给出学年 ID。 |
| 3 | `GET /organization/grade-cohorts` | `organization.read` | 入学届别分页。 |
| 4 | `GET /organization/subjects` | `organization.read` | 学科分页（默认 limit 100）。 |
| 5 | `GET /organization/classes?cohort_id=&status=` | `organization.read` | 行政班分页，可按届别/状态过滤。 |
| 6 | `GET /organization/students?class_id=&query=&active_only=` | `organization.read` | 最小学生身份分页；`class_id` 只匹配当前有效归属；`query` 最长 64，按学号前缀/姓名包含检索（NFKC + 去控制字符）；`active_only` 默认 true。 |
| 7 | `GET /organization/students/{student_id}/enrollments` | `organization.read` | 某学生的归属历史分页（含班号、起止、来源）；学生不存在统一 404。 |
| 8 | `POST /organization/student-imports` | `student.import.preview` | `multipart/form-data`：`request_id`、`mode`、`academic_year_id`、`file`；成功 201。 |
| 9 | `GET /organization/student-imports` | `organization.read` | 导入批次摘要分页。 |
| 10 | `GET /organization/student-imports/{batch_id}` | `organization.read` | 单批次预览结果/状态。 |
| 11 | `POST /organization/student-imports/{batch_id}/confirm` | `student.import.confirm` | JSON：`request_id`、可选 `expected_input_sha256`；成功 200。 |

分页合同：

- `limit` 1–100（各接口默认 25；`subjects` 与 `enrollments` 默认 100），`offset` 0–1,000,000。
- 响应统一 `records/limit/offset/has_next/next_offset`；实现按 `limit + 1` 查询判断 `has_next`，offset 分页不是并发变更下的冻结快照。
- 列表顺序固定（如学年按起始日期、学生按学号），不返回姓名/学号之外的敏感字段。

稳定错误码（组织/导入常用）：

| 错误码 | 典型状态 | 场景 |
|---|---|---|
| `authentication_required` / `invalid_token` | 401 | 缺少/无效 Token。 |
| `teacher_not_provisioned` | 403 | 已签名但未登记或停用。 |
| `organization_permission_disabled` | 403 | 动作不在固定目录或数据库目录不允许。 |
| `resource_not_found_or_forbidden` | 404 | 无安装级 Grant、资源不存在或越界（统一安全语义）。 |
| `organization_code_conflict` | 409 | 学年/学期/届别/学科/班级 `code` 冲突。 |
| `organization_state_conflict` | 409 | 其他状态冲突。 |
| `organization_request_conflict` | 409 | 同 request_id 不同正文。 |
| `academic_year_not_found` / `grade_cohort_not_found` | 404 | 引用父资源不存在。 |
| `academic_year_closed` | 409 | 向已关闭学年新增学期。 |
| `academic_term_outside_academic_year` | 422 | 学期起止超出学年。 |
| `academic_term_overlap` | 409 | 同学年学期时间重叠（GiST）。 |
| `student_inactive` / `administrative_class_unavailable` | 409 | 转班目标无效。 |
| `student_already_in_class` / `transfer_effective_from_invalid` / `enrollment_overlap` | 409 | 转班语义冲突。 |
| `enrollment_range_invalid` | 409 | 归属区间非法。 |
| 导入相关码 | 413/415/422/409 | 见 [3.3](#33-文件编码与大小限制)、[3.4](#34-行状态错误结构与-outcome)、[3.6](#36-确认请求当前状态重验与冲突)。 |
| `organization_operation_unavailable` | 503 | 数据库/系统/超时故障。 |
| `organization_commit_outcome_unknown` | 503 | COMMIT 结果未知，同 request_id 恢复。 |
| `invalid_resource_id` | 422 | 路径 UUID 非法。 |

**组织基础字典与转班没有 HTTP 写接口**：写操作只经受控 CLI；HTTP 写入口只有导入的预览/确认两阶段。

---

## 6. 受控 CLI

组织基础字典与显式转班当前只通过受控本地 CLI 写入；CLI 要求真实 Bearer Token 从 **stdin** 输入，绝不放进 argv/环境变量/Shell 历史，每次操作重新读取数据库中的启用用户与安装级 Grant。

```bash
uv run python -m huipi_cloud.modules.organization <command> \
  --token-stdin --request-id <UUID> [--payload <本地JSON> | 转班参数]
```

| 命令 | 参数 | payload 字段（与 `schemas.*Create` 一致，`request_id` 由 `--request-id` 注入） |
|---|---|---|
| `create-academic-year` | `--payload` | `code`、`display_name`、`start_date`、`end_date`、可选 `status`（`planned/active/closed`） |
| `create-academic-term` | `--payload` | `academic_year_id`、`term_no`（1–4）、`code`、`start_date`、`end_date` |
| `create-grade-cohort` | `--payload` | `code`、`display_name`、`intake_year`（2000–2100） |
| `create-subject` | `--payload` | `code`、`display_name` |
| `create-administrative-class` | `--payload` | `grade_cohort_id`、`code`、`display_name` |
| `transfer-student` | `--student-id`、`--to-class-id`、`--effective-from YYYY-MM-DD` | 无 payload 文件；内部构造 `StudentTransferRequest` |

约束与输出：

- payload 文件最大 8192 字节；超出 `organization_payload_too_large` 413，JSON 非对象 `organization_payload_invalid` 422。日期为 ISO `YYYY-MM-DD`。
- 结果以 JSON 输出到 stdout，只含内部 UUID/action（如 `{"id": "...", "action": "academic_year.created"}`）；不输出学生姓名或学号。
- 失败输出 `{"status":"failed","code":"..."}` 到 stderr 并以退出码 1 结束；COMMIT 未知可用同一 `request_id` 重放恢复。
- CLI 不提供直接创建学生/归属的命令：学生与班级归属只能通过两阶段 CSV 导入建立，归属变更走 `transfer-student`；这是有意的数据入口收敛。

示例（Token 由受控进程管道传入）：

```bash
# 1) 学年
cat > /tmp/academic-year.json <<'JSON'
{"code":"2026-2027","display_name":"2026-2027学年","start_date":"2026-09-01","end_date":"2027-07-10"}
JSON
uv run python -m huipi_cloud.modules.organization create-academic-year \
  --token-stdin --request-id 11111111-1111-4111-8111-111111111111 \
  --payload /tmp/academic-year.json < /secure/path/token.txt

# 2) 转班（生效日当天起旧班失效，旧归属历史保留）
uv run python -m huipi_cloud.modules.organization transfer-student \
  --token-stdin --request-id 22222222-2222-4222-8222-222222222222 \
  --student-id <学生UUID> --to-class-id <行政班UUID> --effective-from 2026-10-20 \
  < /secure/path/token.txt
```

CLI 与安全初始化/授权的配合见[资源安全基础](resource-security.md)。

---

## 7. 与后续切片的关系：B-1.2 前置缺口

B-1.1b 只交付「组织与可信学生名册」的最小后端基础。B-1.2「考试场次、共享试卷与名单冻结」以及更后切片所需的前置能力**仍未实现**：

### 7.1 组织侧缺口

| 能力 | 状态 | 说明 |
|---|---|---|
| 教学班 `TeachingClass`、教学选课 `TeachingEnrollment`、任教关系 `TeachingAssignment` | **[暂未实现]** | 走班/任教关系与教学班 Scope 不属于本切片；普通教师本班名单因此保持关闭。 |
| 按年级/班级/场的组织 Scope 与细粒度动作（`roster.read/lock` 等） | **[暂未实现]** | 当前只有安装级四个动作；未来动作不能靠预留名称/自由 UUID 启用。 |
| 年级派生之外的升年级/毕业批处理 | **[暂未实现]** | 只有 `grade_label()` 纯函数。 |
| 组织/名册前端页面（维护、导入冲突预检、历史查看） | **[暂未实现]** | 本切片不改 Vue；合同类型已生成。 |
| 学生登录/学生身份认证 | **[暂未实现]** | 没有学生账号、密码或凭据绑定。 |

### 7.2 B-1.2 所需的考试实体（全部**[暂未实现]**）

- `ExamEvent`、`ExamSession`、`SessionPaper`：考试/场次/场次用卷，含生命周期与权限。
- `PaperVersion`、`PaperQuestionMapping`、`PaperSourceFile`：不可变试卷版本、题目映射、题本/空白答题卡原件；需要数据库级冻结（防 UPDATE/DELETE 内容字段）与可信创建事务。
- `RosterRevision`、`RosterEntrySnapshot`、`ExamParticipation`：按场次冻结参试名单、班级/姓名展示/考号快照、参试记录；**不能**用组织导入批次冒充。
- `ExamRoom`/`SeatingAssignment`：物理考场与座位。
- `ScanBatch`/`ScanBatchFile`/`ScanPage`：扫描批次、文件与不可变页；`SplitPlanRevision`、`AnswerScriptVersion`、页占用、`IdentityDecision`、`ExamSubmissionLink` 等分卷/身份/桥接实体。
- 阅卷与成绩：`MarkingPlan/MarkingTask/MarkingRecord`、`ResolvedScore`、`ResultSnapshot`、`Publication` 等仍全部未实现，也不得提前建空表宣称能力存在。
- 受控考试解析处理通道：B-1.4/B-1.5 前置，不在 B-1.1b 实现；旧 Parsing Worker 按设计不能处理受保护考试来源。

### 7.3 合同/流程缺口

- 没有场次/试卷/参试名单的 HTTP 接口；没有名单锁版/更正/回滚流程。
- 组织导入批次只冻结在组织域；考试名单冻结必须另建 `RosterRevision` 并保留独立审计与快照。
- 没有考试相关前端路由、页面或权限能力投影；`frontend/src/api/generated.ts` 中的组织类型只覆盖组织读取与名册导入。
- `docs/architecture/school-exam-domain.md` 中的 Paper/Exam/Scanning/Marking/Results 设计仍为**设计建议**，表名/状态不是现有实现。

B-1.2 的准入条件、验收与回滚见[分切片路线](business-reconstruction-roadmap.md)与[领域设计](../architecture/school-exam-domain.md)。

---

## 8. 明确不包含范围

本切片**明确不包含**（均为 **[暂未实现]**，不得按本文宣传为可用）：

- 考试场次/考试计划、试卷版本与冻结、参试名单/考号、考号生成与核对。
- 扫描批次、文件/页接收、分卷/IdentityDecision、与旧 `Submission` 的桥接物化。
- OMR/答题卡模板、机器身份候选、设备接入与上传授权。
- 阅卷计划/任务/记录、双评/仲裁、有效题分、成绩快照、审批与发布。
- 教学班/走班/任教关系、按班级的名单或考场范围授权。
- 学生登录、学生/家长端、跨校租户隔离、学籍系统对接。
- Vue 组织/名册页面、前端页面重构、由 Mock 冒充已接入。
- 真实学校名册导入与真实数据试用（学校未确认前的安全默认是合成/脱敏数据）。
- 开发数据库升级/降级、向对象存储写入真实名册。

---

## 9. 验证证据与测试状态

### 9.1 本轮实际执行的生成与后端质量门禁（开发库未升级）

| 检查 | 结果 |
|---|---|
| `uv run python scripts/export_frontend_openapi.py` | 通过；导出 `frontend/.cache/openapi.json`（38 paths / 45 operations，其中组织 11 个）。 |
| `npm run api:generate --prefix frontend` | 通过；重生成 `frontend/src/api/generated.ts`。 |
| 合同语义自查 | 对比旧版生成文件：新增 11 个组织操作、21 个 `components.schemas`，**删除 0 个操作、0 个既有 schema**；既有 34 个操作保持不变（无其他漂移）。 |
| `npm run typecheck --prefix frontend` | 通过（`vue-tsc --noEmit`）。 |
| `npm run lint --prefix frontend` | 通过（`eslint .`）。 |
| 文档相对链接检查 | 见 [9.3](#93-文档链接自检)。 |
| `uv run ruff check .` | 通过。 |
| `uv run pytest -q`（独立 `TEST_DATABASE_URL` 测试库） | **961 passed / 16 skipped**（含 B-1.1a/B-1.1b 全部回归；无失败）。 |
| 独立空库 `alembic upgrade head` + `alembic check` + `downgrade` + 再 `upgrade` | 通过；`alembic check` 无差异。 |
| `uv run python scripts/check_identity_migration.py` | 通过（独立库 upgrade/downgrade/再 upgrade，历史行保留）。 |
| `docker compose config --quiet`、`git diff --check` | 通过。 |
| `npm run typecheck` / `npm run lint` / `npm run test` / `npm run build` | 全部通过（Vitest 41 passed）。 |

### 9.2 测试定义与执行状态

| 文件 | 覆盖重点 | 本轮执行结果 |
|---|---|---|
| `tests/unit/test_organization_domain.py` | 年级派生边界；`*Create` 严格 schema（禁止额外字段、日期正序、term_no/intake_year 边界）；四个组织动作仅安装级；旧 admin/新路由无法获得组织动作。 | **23 passed**（单元） |
| `tests/unit/test_organization_csv.py` | 纯解析：三种模式列合同、UTF-8/BOM/非法编码、2 MiB/5000 行上限、行号/字段/错误码、outcome 与 warning。 | **63 passed**（单元） |
| `tests/integration/test_organization.py` | 真实 PostgreSQL：GiST 排他约束、转班历史、同名不合并、学号唯一、分页/合同、JWT 授权/撤权/停用、旧 admin 无新权限、重复 Grant 409、COMMIT 未知。 | **16 passed**（真实 PG） |
| `tests/integration/test_organization_migration.py` | 独立库 upgrade/check/downgrade/再 upgrade、破坏性降级拒绝、旧数据回归。 | **1 passed**（独立临时库） |
| `tests/integration/test_student_import.py` | 预览不写正式表、确认幂等/409/预览后数据变化、审计同事务回滚、COMMIT 未知恢复、真实 JWT 授权/撤权、未来归属重复导入。 | **19 passed**（真实 PG 专用测试库） |

**状态说明**：以上结果来自本切片统一质量门禁：后端全量 `uv run pytest -q` 在独立真实 PostgreSQL 测试库 **961 passed / 16 skipped**，包含本表全部用例与 B-1.1a 安全回归；未用 Mock/SQLite 替代真实数据库结论。前端 typecheck/lint/Vitest/build 与 OpenAPI/TypeScript 合同漂移检查同样通过。

### 9.3 文档链接自检

本文所有相对链接在编写后由脚本核对目标文件存在（`docs/` 内文档、源码、迁移、测试路径）；结果见本节自检记录。

### 9.4 未做/不可宣称

- 未升级开发数据库（`DATABASE_URL` 指向的 `huipi_cloud` 保持旧版本）；部署库的升级与容量验证仍需独立运维审批。
- 未做真实学校名册试用、真实 IdP 验收、前端页面/浏览器验收、模型调用或考试业务端到端。
- 组织迁移的 `btree_gist` 扩展需要数据库所有者权限；部署前须确认目标库允许。

---

## 10. 需学校确认事项

| 事项 | 确认前安全默认 | 影响 |
|---|---|---|
| 学生名册的可信来源、责任人与授权依据 | 只用合成/脱敏名单开发，不导入真实身份数据（[阶段 A 评审](../product/stage-a-review.md)） | 真实学生数据导入与后续真实试用。 |
| 最小必要字段与学号规则（校内学号来源、是否复用国家学籍号） | 只收 `student_code` + 姓名；不冒充法定学籍号、不采身份证/家长联系方式 | CSV 模板、唯一性与匹配规则。 |
| 同名处理与身份核验责任 | 姓名非键；同名不同学号可共存，不自动合并，只给 warning | 导入人工复核工作量与纠纷处理。 |
| 班级/届别/学期编码规范与日期边界 | 系统只校验唯一/正序/学年内包含，具体编码与起止由学校给定 | 组织字典初始化与每学年维护。 |
| 转班生效日期政策与历史更正审批 | 只允许未来/当前语义的显式转班；历史行不可改，无回填猜测 | 学籍异动、期中转班与更正流程。 |
| CSV 实际导出编码（学校系统常导出 GBK/Excel） | 只接受 UTF-8/UTF-8 BOM，明确拒绝其他编码，不猜编码 | 是否需要在后续切片提供受控转码工具。 |
| 组织操作员/导入员的人选、双人审批与离岗撤权 | 仅安装级最小授权；部署 bootstrap 后人工委派，不用旧管理员捷径 | 谁可读全校学生、谁可确认名册。 |
| 真实学生数据的保留、备份、删除与外部模型外传授权 | 名册不进模型/对象存储原件；保留策略实施前不导入真实数据 | 真实数据上线与后续考试切片。 |
| 普通教师查看本班名单的范围、回避与审计要求 | 当前完全关闭；不得用全校学生接口替代 | 后续教学班/任教关系切片。 |
| 与学校现有学籍/教务系统的对接方式 | 不做系统对接，只接受学校授权的受控导入 | B-1.2 名单来源与容量/可信度。 |

以上未确认事项不阻塞合成数据下的后端合同开发，但阻塞真实学生数据和真实考试试用。

---

## 相关文档

- [B-1.1a 考试资源安全隔离](../security/exam-resource-security.md)
- [HTTP 权限矩阵](../security/http-permissions.md)与机器清单 [http-permissions.json](../security/http-permissions.json)
- [资源安全基础：受控部署操作](resource-security.md)
- [单校考试阅卷领域设计](../architecture/school-exam-domain.md)（设计建议，非现有实现）
- [分切片实施与验收路线](business-reconstruction-roadmap.md)
- [阶段 A 评审与学校待确认政策](../product/stage-a-review.md)
- [当前持久化与 HTTP 合同清单](../product/current-contract-inventory.md)
- [教师与管理员合同](../product/teacher-admin-contract.md)
- [项目路线图](../roadmap.md)
