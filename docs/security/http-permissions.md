# HTTP 权限矩阵

范围：所有 FastAPI HTTP 路由。机器清单见 [http-permissions.json](http-permissions.json)，测试与 OpenAPI 的方法/路径逐项比对。JWT 的角色字段不授权；下列「管理员」仅指本地 User.is_admin。「成员」必须未撤销且 User 启用。

## 公开，无业务数据

- `GET /api/v1/health`：存活检查，不查询依赖或业务数据。
- `GET/HEAD /openapi.json`、`/docs`、`/docs/oauth2-redirect`、`/redoc`：静态接口描述与文档；不会在文档中提供 Token。

## 教师身份，列表与新建

- `POST /api/v1/assignments`：已建立的教师可创建，服务器将自己加入可写成员；不能指定其他教师为自己。
- `GET /api/v1/assignments`：普通教师仅当前成员作业，SQL 过滤后分页；管理员全部。

## 作业读权限（成员或管理员）

- `GET /api/v1/assignments/{assignment_id}`：作业详情、题目、标准答案及 Rubric；教学答案只在作业授权后读取。
- `GET /api/v1/assignments/{assignment_id}/submissions`：该作业内提交列表。
- `GET /api/v1/submissions/{submission_id}`：提交及文件元数据。
- `GET /api/v1/submissions/{submission_id}/file`：敏感原文件流，仅 Submission 所属作业授权后访问 S3。
- `GET /api/v1/submissions/{submission_id}/parsing-task`：任务状态。
- `GET /api/v1/submissions/{submission_id}/parsed-document`：解析状态与索引摘要。
- `GET /api/v1/submissions/{submission_id}/parsed-document/markdown`：敏感解析原文流。
- `GET /api/v1/submissions/{submission_id}/canonical-document`：标准化状态。
- `GET /api/v1/submissions/{submission_id}/canonical-document/pages/{page_number}`：敏感页面来源。
- `GET /api/v1/submissions/{submission_id}/answer-alignment`：对齐摘要。
- `GET /api/v1/submissions/{submission_id}/answer-alignment/questions/{question_id}`：单题来源；另须 Question 和 Submission 同属一个 Assignment，即使管理员也不能跨作业拼接。

## 作业写权限（can_write 成员或管理员）

- `POST /api/v1/assignments/{assignment_id}/questions`：新增题目，仍受草稿状态规则限制。
- `PUT /api/v1/questions/{question_id}/answer-key`：经 Question 向上核对 Assignment，修改标准答案。
- `PUT /api/v1/questions/{question_id}/rubric`：经 Question 向上核对 Assignment，整体替换规则。
- `POST /api/v1/assignments/{assignment_id}/publish`：发布，仍执行现有完整性校验。
- `POST /api/v1/assignments/{assignment_id}/submissions`：教师代录模拟学生提交；不是匿名学生上传入口。

## 仅数据库管理员

- `PUT /api/v1/assignments/{assignment_id}/members/{user_id}`：给已建立的 User 授予/更新只读或可写作业权限，不改变 User.is_admin。
- `DELETE /api/v1/assignments/{assignment_id}/members/{user_id}`：幂等撤销，不删除业务数据。

## 拒绝与未开放

缺 Token/无效 Token 为 401；未建立/停用用户、非管理员管理成员为 403；不存在或无资源授权统一 404；认证配置、JWKS、授权数据库故障为 503；无精确方法/路径策略为 403。未绑定历史作业不自动分配给教师。除上述公开端点外，没有保留匿名敏感 HTTP 访问。

没有用户自助注册、管理员提权、学生身份认证、Copilot 聊天或评分/复核 HTTP 写接口。Worker、知识导入、评分和复核 CLI 仍是拥有数据库凭据的受控操作员功能，不向互联网暴露。文件流开始前授权，运行中的流与已通过检查的事务不承诺撤销后立即终止。

## 教师作答证据复核 HTTP

- `GET /api/v1/submissions/{submission_id}/answer-reviews`：所属 Assignment 读权限；`question_id` 筛选也须属于同一作业。分页 1–25 条，明确 has_next/next_offset。历史不宣称当前来源有效。
- `GET /api/v1/submissions/{submission_id}/answer-reviews/questions/{question_id}`：所属作业读权限；验证当前 Canonical/Alignment，返回来源版本、最新状态与有界证据指针，不返回标准答案、私有对象 Key、图片 URI 或完整大文档。
- `POST /api/v1/submissions/{submission_id}/answer-reviews/questions/{question_id}`：所属作业写权限；必须提交 expected_source 与 expected_revision。审核人由可信 Principal 绑定，不能自声明身份。两 ID 即使对管理员也须同属一作业。

新复核在 S3 校验后、短追加事务内重新验证 User 和 Membership，并以 SHARE 锁持有到提交；撤权先提交则本次写入拒绝，复核先取得保护锁则撤权等待其提交。已有其他 HTTP 接口的请求边界保证没有扩展成全系统在途取消。复核是作答存在性判断，不是评分复核或成绩发布。
