# 共享试卷版本与锁卷（B-1.2a）

本切片只有受控 CLI 和 Python Service，没有新增 HTTP、Vue 考试页面、考试场次、考生名单、扫描、阅卷或成绩发布。内容从明确授权的已有作业复制；不是 PDF 导入。

## 数据合同

- `PaperFamily`：服务端稳定家族 ID。初次导入键由来源 Assignment UUID 与试卷标题生成，唯一键和行锁避免两个请求重复创建同一家族。
- `PaperVersion`：家族内递增 `version_no`、写并发 `revision`、专用内容载体 `canonical_assignment_id`（全表唯一）、来源 Assignment、前一版、创建人和时间、状态、锁定时间及五类摘要。前版复合 FK 保证同家族，唯一后继形成线性历史。
- `PaperQuestionMapping`：版本内 `question_key`、真实 Question、显示题号和顺序，保留来源 Question FK、源 Rubric 摘要及前版 Mapping FK。两个复合外键同时证明 Question 与 PaperVersion 绑定的是**同一内容载体**。
- `PaperOperation`：actor/request_id 唯一的 append-only 操作与摘要结果。审计不保存标准答案全文、JWT、学生正文或 S3 Key；创建人和权限关联的 User 由服务端查询。

当前家族创建标识固定为来源 Assignment 与标题，同源同标题不能自然创建两个独立家族；需要独立命题家族时应由后续显式身份合同处理，不能偷偷改标题绕过审计。本切片尚无此业务阻塞，不更改既有标识算法。

一个版本只有一份专用内容；多班/多场次以后引用这份版本，不按班复制。题号仅用于显示，不证明逻辑同题。初次导入生成 question_key；明确 fork 保留题目键与映射谱系，不用题干相似度猜测。跨家族的逻辑等价关系尚未开放。

## 快照事务

1. 当前 User 与 `paper.create` 安装级 Grant 必须有效；再独立验证来源 legacy Assignment 的读权限。命题权限不能借用别人的作业成员关系。
2. 同 actor/request_id 的事务 advisory lock 防重放；同正文返回原操作结果，不同正文 409。重放仍检查当前权限。
3. 锁家族、来源 Assignment、实际 Question/AnswerKey/Rubric 行，再读取快照。**published 不代表旧内容数据库不可变**，不能靠该状态省略锁。
4. 同事务 INSERT 新 `exam_protected` Assignment、复制内容、建立 PaperVersion、Mapping、真实 paper_version Scope、审计结果。没有普通载体先提交再补保护的窗口，也不复制 Membership/Submission。
5. 相同来源内容的未变化版本可复用；已编辑草稿不能冒充原导入结果，返回 `paper_draft_exists`。来源后来变化也不能自动推断题目连续性：已有锁版家族返回 `paper_new_version_requires_fork`，要求显式 fork 独立载体再编辑，旧版不修改。

内容与题目/Rubric摘要以 PostgreSQL `jsonb` 的规范文本计算 SHA-256；定点分值保存/输出为十进制字符串。映射另有身份与顺序摘要。摘要算法由本迁移内函数固定，不把 Python JSON 序列化结果冒充同一协议。原始来源 Rubric 摘要记录创建时事实，后续来源变化不追改已建立版本。

上限：每份 1–200 题，源题每题不超过 100 条 Rubric，总内容表示不超过 2 MiB。内容检查失败整体回滚；不会删图片标记来伪造可评分文本。本切片锁卷只证明已填写标准答案和分值规则完整，**不证明教学内容正确，也不生成候选分数**。

## 状态、修订与数据库冻结

`draft → locked → retired`；草稿也可直接废弃。废弃只停止当前使用，内容保留；没有解锁或原地改旧版功能。`version_no` 是内容版次；`revision` 是每次受控写递增的并发版本，二者不混用。

所有写请求还需 `expected_revision`、`expected_content_sha256`、`expected_mapping_digest`。服务锁定 PaperVersion 后重算摘要，拒绝过期请求。即使受控运维直接改了草稿而未推进 revision，旧摘要也不能继续锁卷。

数据库触发器覆盖：

- Assignment 的 INSERT/UPDATE/DELETE，包括状态、标题及删除级联入口。
- Question 的 INSERT/UPDATE/DELETE，以及从旧载体移动出去或移动进来的父级变化。
- AnswerKey、RubricCriterion 的新增、更新、删除和批量替换。
- PaperQuestionMapping 的新增、删除、题号重排及关联变化。
- 锁定或废弃 PaperVersion 的内容、摘要、载体、来源、前版与身份字段；锁后只允许保持其余字段不变的显式退役状态迁移。

直接 Core/SQL、已加载对象的 ORM flush 同样被数据库拒绝。旧 ORM 入口还会先隐藏 exam_protected，但测试不把「查不到」当成数据库不可变的证明。锁卷时数据库再次核对每题标准答案、Rubric 总分、完整映射、published 载体及实际摘要。

内容触发器对所属 PaperVersion 取 SHARE 锁，锁卷/编辑服务取 UPDATE 锁。先完成的草稿编辑会使旧摘要/修订请求失败；先持有锁卷保护的事务提交后，迟到的直接写入失败。锁卷的摘要读取不再反向锁内容子行，避免 Paper/Question 锁倒置。并发冲突或数据库死锁被安全映射为 409，不自动重试并改变请求身份。

行触发器不保护数据库所有者的 TRUNCATE、关闭触发器或任意特权 SQL。不得把应用数据库凭据交给教师或模型；这不是不可篡改存证或 RLS。

## 权限与部署

固定动作：`paper.create` 仅安装级；`paper.read/edit/lock` 支持安装级与真实 `paper_version` Scope。新增 Scope 有 PaperVersion FK、互斥 CHECK 和唯一约束；不存在实体的 ExamSession 等动作仍不可执行。旧 User.is_admin、Membership、JWT role 不授予任何试卷权。

角色版本：

- `11111111-b12a-4000-8000-000000000001`：`paper_operator`，受控部署安全根，含已有安全/组织与新试卷动作；**不得当作普通命题教师角色**。
- `...0002`：`paper_editor`，paper.read/edit；`...0003`：`paper_locker`，paper.read/lock；`...0004`：`paper_reader`，paper.read。普通人员应绑定具体试卷 Scope。

现有部署不会自动把旧管理员/操作员 Grant 升级到新根。持数据库凭据的部署人员经独立审批运行：

```bash
uv run python -m huipi_cloud.modules.papers enable-operator \
  --actor-user-id <已有安全管理授权的用户UUID> --target-user-id <目标用户UUID> \
  --request-id <请求UUID> --approval-ref <审批编号> --confirm-deployment-operation
```

该命令验证操作者当前 `security.grants.manage`，授权和审计同事务。它属于持数据库凭据的可信部署代操作，实际运维人员与审批的绑定由部署管理保障，**不宣称参数中的 actor UUID 代表 OIDC 登录**；不能暴露为教师 HTTP、Agent 工具或 Bearer 自助提权。

其余命令必须通过 stdin 输入裸 JWT（不带 `Bearer ` 前缀），执行真实签名/Issuer/Audience/有效期验证和当前数据库 Principal 解析。不得把 JWT 放进 argv、Git、日志或公开示例。

```text
uv run python -m huipi_cloud.modules.papers create --token-stdin --payload request.json
uv run python -m huipi_cloud.modules.papers inspect --token-stdin --paper-version-id UUID
uv run python -m huipi_cloud.modules.papers edit-question --token-stdin --payload request.json
uv run python -m huipi_cloud.modules.papers reorder --token-stdin --payload request.json
uv run python -m huipi_cloud.modules.papers lock --token-stdin --payload request.json
uv run python -m huipi_cloud.modules.papers fork --token-stdin --payload request.json
uv run python -m huipi_cloud.modules.papers retire --token-stdin --payload request.json
```

创建正文为 `request_id/source_assignment_id/title`；写正文的公共字段见上文。edit-question 还需 question_key、question（沿用 QuestionCreate）、answer_content、rubric（完整 criteria）；reorder 提交全部 `question_key/printed_label`，不接受任意目标 Question ID。完整严格 DTO 见 [schemas.py](../../src/huipi_cloud/modules/papers/schemas.py)。

创建结果含真实 `paper_version_id`；Scope 可由受控部署按 FK 查得，用既有安全 grant/revoke CLI 授予具体 PaperVersion 的 editor/locker/reader。没有公开试卷授权后台。拥有某卷的 read/edit 不授权其他卷、组织名册或学生作答。旧 Assignment/Question/Submission、Canonical、审核、Copilot 与 Knowledge 路径依然默认拒绝考试内容。

## 错误与恢复

- 404 `resource_not_found_or_forbidden`：缺失或无权限，不返回私有内容。
- 409 `paper_locked`、`paper_revision_conflict`、`paper_content_changed`、`paper_mapping_set_mismatch`、`paper_request_conflict`、`paper_source_incomplete`：读取最新合法状态后重新确认；不盲换 request_id。
- 413 `paper_content_too_large`：内容/请求超限，事务回滚。
- 503 `paper_operation_unavailable`：数据库或授权故障，默认拒绝。
- 503 `paper_commit_outcome_unknown`：不能声称失败已回滚；保留原正文与 request_id，按当前权限重放。没有网络解析或模型推理占用事务，没有 S3 补偿删除。

原请求成功结果是不可变历史回执，后续版本可能已编辑或退役；再次 inspect 才是当前状态。权限撤销后的回执重放仍拒绝。审计写入失败没有半成品载体、映射或家族。

## 验证与后续

[测试](../../tests/integration/test_paper_versions.py)使用独立真实 PostgreSQL，验证真实数据库错误原因、SQL/ORM 绕过、来源/锁卷/撤权并发、幂等与未知 COMMIT、原始文件与评分审核历史摘要，以及独立库 upgrade/check/downgrade/再 upgrade。存在新家族、版本、操作、Scope 或新角色 Grant 时拒绝破坏性降级；生产回滚保留这些数据并向前修复。

B-1.2b 增加 ExamEvent/ExamSession/SessionPaper 与真实场次 Scope；名单和参试关系留在 B-1.2c；必须显式要求引用锁定且可用的卷版，不从旧 Assignment 状态猜测。后续独立「试卷原件与版式」切片才实现 PaperSourceFile（题本 PDF/空白卡）与上传、私有 S3/SHA/MIME/大小校验及锁后不可变清单；已锁版本新增或更换原件必须建立新版本。本切片没有 PDF 上传/解析、版式模板或文件预览。

## 后续考试引用与退役边界

retired 禁止建立新的场次关联；已存在的关联必须保留卷版 ID、摘要和关联历史，不能级联删除、替换或悄悄改指向。退役不等于取消历史考试；使用中的卷版退役后，后续准备动作应显式阻止并要求有权限的人员取消场次或在草稿中追加更正关联。已准备/关闭场次不得直接覆盖用卷。撤卷属于单独的显式审计业务，不恢复旧管理员全局访问。B-1.2b 通过行锁协调绑定与退役，保留历史事实。
