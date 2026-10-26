# 学校基础数据管理工作台

## 业务范围

`/school` 使用现有 AcademicYear、AcademicTerm、GradeCohort、Subject、AdministrativeClass、Student、ClassEnrollment、StudentImportBatch 和 StudentImportRow。没有新建组织表、没有新增迁移，也没有修改学生身份或冻结名单协议。

学校管理人员可以在网页创建五类组织字典，按日期查看行政班人数、分页查询学生及班级归属，下载三种 CSV 模板、预检、人工确认并查看导入记录。考试配置继续使用同一组真实学生与归属关系，不复制学生。

## 授权合同

登录必须通过现有 OIDC Code + PKCE。`GET /api/v1/me/school-access` 只返回 `can_read`、`can_manage`、`can_preview`、`can_confirm` 四个当前能力布尔值；每项由当前数据库 User、安装级 Scope、未撤销 Grant 和 sealed Role 计算。该身份元数据接口不返回任何组织或学生资料。

组织资源继续全部受 `identity.dependencies.ORGANIZATION_POLICIES` 固定映射约束：

- `organization.read`：组织、学生、归属、导入历史及冻结预检行读取。
- `organization.manage`：创建五类组织字典。
- `student.import.preview`：下载对应模式模板和提交 CSV 预检。
- `student.import.confirm`：确认正式导入。

导航与按钮只采用服务端能力投影；它们不是授权依据。每次资源请求和写入仍重新鉴权。旧 `is_admin`、JWT 的 `role`、AssignmentMembership、`paper_creator` 均不能提供组织权限。组织管理不自动获得考试、名单冻结、班级成绩或安全授权管理权限。部署人员必须通过已有受控授权流程分别批准相应岗位。没有新增授权 HTTP 或自动管理员分配。

## HTTP 增量

以下路径均在 `/api/v1` 下：

- `GET /me/school-access`：当前身份能力，无学生正文。
- `POST /organization/academic-years`：调用原 `create_academic_year`。
- `POST /organization/academic-terms`：调用原 `create_academic_term`。
- `POST /organization/grade-cohorts`：调用原 `create_grade_cohort`。
- `POST /organization/subjects`：调用原 `create_subject`。
- `POST /organization/classes`：调用原 `create_administrative_class`。
- `GET /organization/class-summaries`：按学年、届别与 `as_of` 日期返回有界班级人数；计数使用有效学生和半开归属区间。
- `GET /organization/student-import-template?mode=...`：由后端 `MODE_COLUMNS` 生成 UTF-8 BOM 表头，不在前端重建格式。
- `GET /organization/student-imports/{batch_id}/rows`：分页读取已保存的预检行、结果及问题，不重新计算或改变预检。

原学生列表增加可选 `as_of`、`academic_year_id`、`cohort_id`。学年/届别通过真实班级及当日归属过滤，未分班学生可清除这些条件后查询；所有新参数均未传时保留原读取语义。读取默认每页25、最大100；班级选项明确为本页，字典及学生可分页。学年过滤展示高中三年对应届别，不把自由文本班名当作可信关系。

所有新增写入调用原业务 Service。保留 request_id、同正文重放、正文冲突、当前权限、事务锁、审计和 COMMIT 未知恢复。组织 JSON 请求限制8KiB、读取10秒；原请求处理上限30秒。CSV 仍使用原2MiB上限与原解析器，不经 S3。

## 两阶段导入

三种模式及精确列名：

- `student_only`：`student_code,name`。
- `enrollment_only`：`student_code,class_code,valid_from`。
- `student_with_enrollment`：`student_code,name,class_code,valid_from`。

只接收 UTF-8 / UTF-8 BOM，最多2MiB、5000行。拒绝 GBK/UTF-16，不猜编码或列名。学生学号不转数值；同名按不同学号显示，大小写身份冲突、姓名不一致、班级不存在和归属冲突沿用原错误规则。

1. 选择学年和模式、下载模板、上传合成或已授权 CSV。
2. **预检**只保存预检批次和行，不写正式 Student/ClassEnrollment。页面分别展示有效行、警告行和错误行；明细分页25行，问题汇总沿用原500条截断标志。
3. 有阻塞错误时不能确认；只有预检权限的用户没有确认按钮，直接调用也被服务端拒绝。
4. 人工确认使用保存批次的 SHA 和新 request_id。原 Service 重新检查当前数据库；预检已过期时409拒绝，不静默改变输入，页面要求重新预检。
5. 网络中断或503不被描述为回滚。页面保留原请求正文/request_id，仅允许人工重试原请求核实结果；没有后台自动重试。退出后需从导入历史核对。

本页文件、预检、学生明细和待重试信息仅在内存中。离开页面会中止请求并清理引用、文件输入和对象 URL。没有 CSV 控制台输出、localStorage 持久化或原件对象存储。HTTP 返回 `private, no-store`，公有错误信息只采用有界固定错误码，不显示 SQL、Token 或验证输入正文。

## 考试联动和历史

`/exams` 仍通过既有配置接口按场次的业务日期读取 ClassEnrollment。学校导入仅维护组织事实，不直接写入 RosterEntrySnapshot。名单草稿保存和冻结依旧要求独立的 roster 权限；卷别未知保持待核验。冻结后新增学生、转班或再次导入不会更新旧快照，需明确建立名单更正版本。

真实浏览器验收使用单独批准组织/导入及考试/名单岗位的合成账号；该测试部署不意味着组织管理员天然拥有所有考试权限。

## 限制

本轮没有组织删除、历史覆盖、学生自助注册、学籍注销/更名网页、转班网页、教学班/任教关系或班级级组织 Scope。转班仍通过原受控 Service/CLI；初始角色审批和授权仍通过受控部署流程。没有自动发布成绩，也没有模型调用。

测试与截图见 [学校工作台验收](../evaluation/school-workbench.md)。原组织域的历史实现记录见 [organization.md](organization.md)。
