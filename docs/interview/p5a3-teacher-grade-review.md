# 教师候选评分审核 HTTP：技术复盘

## 为什么授权先于幂等重放？

简洁解释：先确认当前用户及作业写权限，再读取旧请求。request_id 是重试身份，不是访问凭证。

30 秒口语：原 CLI 可以先找已经提交的记录，方便恢复丢失的确认。HTTP 不能直接沿用这个顺序。用户被撤权后，即使拿着原来的 request_id，也不能再读到审核结果。因此入口先验签和检查真实资源关系，服务的重放短事务再复查当前权限。

深入回答：`identity/dependencies.py` 使用固定方法和路径模板。`authorize_proposal` 联接 Proposal、GradeInput、Submission 和 Question，检查真实 ID 与绑定摘要，再向 Assignment 授权。`get_review_by_request` 和追加事务都复查 Principal。新请求摘要加入 user_id，防止同请求被另一名教师接管；旧 CLI 摘要不变。来源后来变化时，仍有权限的原请求可恢复已提交记录，但不能追加一个新决定。

追问：管理员能跨题拼接吗？不能，管理员跳过成员判断，不跳过资源绑定。Token 中角色能授权吗？不能，角色来自内部 User。

验证：`test_read_write_scope_revocation_and_authorization_before_storage`、`test_admin_cannot_splice_resources_even_within_same_assignment`、`test_replay_body_and_actor_binding_and_stale_revision`，均在 `tests/integration/test_grade_review_http.py`。

## 撤权与追加怎样并发？

简洁解释：S3 校验在事务外。提交短事务锁定 Proposal，并持有 User/Membership SHARE 锁直到提交。

30 秒口语：入口授权不能保证文件读完后权限仍有效。因此提交时再查。撤权先完成，就拒绝审核；审核先拿到保护锁，撤权会等待本次事务结束。这是确定的提交顺序，不是假称可以取消所有在途请求。

深入回答：Proposal 行锁使同候选修订串行化，expected_revision 防止覆盖。用户启用状态、成员撤销状态、can_write 由真实数据库检查；SHARE 锁阻止并发修改已检查的身份行。锁等待与语句执行均有上限，锁内没有对象存储或模型 I/O。唯一约束兜底 request、revision 与 successor。历史更新和删除由原数据库触发器拒绝。

追问：两名教师同修订号？只有一条成功。相同请求同正文？两次响应可以恢复同一行。长期等待用户编辑？不持锁，提交时用乐观修订号。

代码：`grade_review_repository.append_review`、`identity.service.require_assignment`。验证：`test_two_teachers_same_revision_only_one_append`、`test_short_permission_share_locks_block_concurrent_revocation`。

## COMMIT 确认丢失是否等于失败？

简洁解释：不等于。返回未知状态，保留记录与源对象；以相同请求重试。

30 秒口语：数据库可能已提交，但响应在网络上丢失。不能盲目再写一个新 revision。服务把这类异常标为 commit_outcome_unknown；教师用同一 request_id、正文、身份和预期修订重试，拿回原记录。

深入回答：写入前的异常按事务规则回滚；一旦进入 COMMIT，异常被保守分类为未知，不能声明确定回滚。审核只写 PostgreSQL，不增加跨系统上传或删除。重试仍需当前授权，同 ID 不同内容或身份为冲突。测试实际提交事务后注入确认丢失，而不只模拟调用前失败。

验证：`test_actual_commit_ack_loss_is_unknown_and_actor_bound_replay_recovers`、`test_insert_rollback_lost_commit_ack_and_safe_replay`。

## revised 和 accepted 的分数含义是什么？

简洁解释：revised 提交完整 Rubric 快照，changed 相对原始候选；accepted 确认原始候选，不确认上一条修订。

30 秒口语：人工修订不是增量补丁。每次都交全部 Criterion，服务计算总分、验证满分和引用。这样任一历史版本都可独立解释，不必重放一串补丁。接受候选也不是发布成绩。

深入回答：原 `_build_review_criteria` 检查完整集合、Decimal 精度、合法原文 Quote 和正分证据；改变项必须有理由和证据，未改变项保留候选内容。A=5/B=5 经 A=3/B=5，再 A=3/B=4，各行保留完整分数、原分和前序 ID，旧行不可修改。rejected 与 regrading_required 没有人工总分，后者不自动调用模型。来源正确也不证明教学评分合理，P3 质量门禁继续保留。

验证：`test_signed_review_full_snapshots_history_and_original_sources_unchanged`、`test_revised_keeps_decimal_rubric_and_quote_gates`。HTTP DTO：`grade_review_http.py`；原协议：`grade_review_protocol.py`。

## 为什么历史字符串不能证明身份？

简洁解释：只有可信 Principal 产生的 User 外键才标为认证教师。历史自声明文字保持原样。

30 秒口语：旧 CLI 可以填写任意 reviewer_ref。即使它恰好以 teacher_ 开头，也不能补认成真实教师。本轮新增可空外键，HTTP 写入真实 User ID，CLI 历史仍为 NULL，两类来源在输出中明确区分。

深入回答：迁移不回填身份，不改历史摘要或 1.0 文档；数据库 CHECK 保证非空外键和派生字符串一致，RESTRICT 阻止删除被引用用户。原不可变触发器继续保护 UPDATE/DELETE。降级保留旧行却会移除认证元数据，必须先保留审计备份；重新升级不会恢复已失去的身份。

验证：`test_legacy_identity_and_pg_update_delete_protection`、`test_grade_review_migration_roundtrip_keeps_legacy_snapshot_and_immutability`。

## 教师界面还缺什么？

当前提供检查、分页、完整快照写入和受保护的 Canonical 坐标跳转。默认摘要不返回学生全文、长理由、评论或私有对象 Key。后续需要独立设计完整审计正文、前端交互、真实 IdP 部署验收与正式成绩发布合同；不能把 CLI 凭据或前端传入教师 ID 当成授权。
