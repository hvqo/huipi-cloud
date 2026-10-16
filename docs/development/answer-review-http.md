# 教师作答证据复核 HTTP

这是人工作答存在性与证据范围确认，不是 AI 分数复核或成绩发布。接口使用[教师身份与作业授权](teacher-auth.md)，没有匿名写入口；真实外部 IdP 部署验收仍未完成。

## 接口

- `GET /api/v1/submissions/{submission_id}/answer-reviews`：历史摘要，可选 `question_id`，`limit` 默认 10、最大 25，`offset` 最大 100000；返回 `has_next/next_offset`。按题号与 revision 排序，offset 分页不承诺并发追加时的冻结快照，应以 decision_id 去重。SQL 只取元数据和 JSON 区域计数，不加载历史大段证据。
- `GET /api/v1/submissions/{submission_id}/answer-reviews/questions/{question_id}`：验证当前来源，返回 `source`、`expected_revision`、当前 `answer_presence`、最新历史摘要及来源是否匹配。旧决定仍可查询，但来源改变后当前状态为 `unreviewed`，不会把旧确认当作当前证据。
- `POST` 同一单题路径：追加一条完整决定，创建返回 201；相同请求幂等重放返回 200。没有 UPDATE/DELETE 复核接口。

GET 要求读权限，POST 要求可写成员或内部管理员；Question/Submission 必须同属一个 Assignment。跨作业和不存在资源统一 404，包括历史列表的 question_id 筛选。23 个业务方法/路径均在[固定权限矩阵](../security/http-permissions.md)内。

## 输入与身份

HTTP `ReviewRequest` 与原 `AnswerReviewDecisionCreate 1.0` 分开；原 CLI、旧数据和导出合同保持兼容。必填：

- `request_id`：客户端生成的 UUID，每一次新决定用新 ID；网络失败重试必须保持相同 ID 和完整正文。
- `expected_revision`：GET 返回的全历史最新修订号，无历史为 0。旧决定来源失效也不把 revision 重置为 0。
- `expected_source`：原样使用 GET 的 source，含 Canonical ID/SHA、Alignment ID/SHA、aligner_version 与题目集合摘要。
- `decision`：`response_present`、`response_absent`、`uncertain`。
- `response_regions/excluded_prompt_regions/uncertain_regions`：同一 Canonical ContentNode 的来源路径与 Unicode 码点半开范围。
- `reason_codes`：沿用现有有限原因码。

不能传 reviewer_ref、reviewer_id、reviewer_user_id、teacher_id、assignment_id、role 等身份/作用域字段。服务端从 Principal.user_id 生成 `teacher_<32位UUIDhex>`（40 字符），并写入 `reviewer_user_id` 外键。只有外键非空的记录标为 `authenticated_teacher`；历史 CLI 即使恰好使用相同文本前缀，也仍为 `legacy_cli_claim`。不回填旧身份，不返回发行方 subject 或姓名。

为避免把字符串前缀误作身份凭证，本轮新增一个可空外键、相应约束/索引；原行字段值和 1.0 JSON 不改。数据库触发器禁止复核表 UPDATE/DELETE。可信管理员仍能执行数据库级维护/TRUNCATE，这不是防御数据库超级用户的安全机制。未来删除作业及隐私清理必须单独设计受控保留策略，不能依赖级联删除绕过历史保护。 降级会删除新增的审核人外键元数据并移除触发器；部署回滚前必须备份和停止相关写入，不能把可回滚测试理解为生产回滚无损。

## 安全证据与来源

单题摘要最多返回 50 个来源指针，默认 25，支持 evidence_offset/evidence_limit。证据 JSON 预算 48 KiB，截断有明确标记及 next_evidence_offset。摘要不含学生正文、标准答案、S3 Key、内联图像或外部 URI；`canonical_page_path` 指向已有受保护页面接口，原文件仍经受保护下载。

检查来源会读取整份有大小/SHA 限制的 Canonical 与 Alignment，不是按需 S3 页面读取；沿用现有最大对象字节限制及结构校验，仍需在部署层安排并发内存预算。一个 GET 是查看时的来源，不保证用户阅读期间持续不变。POST 重新读取/验 SHA，并在短提交事务核对来源索引与题目集合摘要。

父子 ContentNode、跨题区域、共享图片、角色冲突及 Unicode 范围全部复用原服务校验。归属不明不能直接确认专属作答；可保留 uncertain。HTTP 也拒绝 response_present 与 printed_prompt_only、blank_response_area、shared_figure_attribution_unclear 的明确矛盾组合。

`aligned` 只证明来源映射，不证明学生作答；映射区域可能包含题干。服务不能自动可靠区分所有印刷题干与学生解答，教师必须查看原文/页面再选择。不能只复制首个映射区域就确认作答；不可信或缺失来源应保留 uncertain。人工确认仍不等于 ready_for_grading 或教学评分正确。

## 事务、并发与重试

复用 record_decision 与 append_decision：先在事务外读取和验证 S3 来源，再锁 Submission 分配修订链，重新验证用户和作业写权限，锁 Assignment 并核对来源版本，追加记录。HTTP 提交短事务对 User/Membership 取 SHARE 锁；撤权和用户停用必须与之串行。锁等待预算 3 秒、SQL statement 预算 5 秒，不在用户阅读或 S3 I/O 期间持有授权锁。其他旧 HTTP 工作流仍按原请求边界授权，不宣称全系统中途撤销。

两个教师从同一 expected_revision 提交不同请求时，只有一个追加成功，另一个收到 409，必须重新 GET 后人工决定。相同 request_id/正文/身份可重放；不同正文、expected_revision 或教师身份冲突。请求摘要同时绑定来源、服务端身份和原始 expected_revision。

COMMIT 客户端确认丢失时返回 503，不声称已回滚。用相同请求重试可找到已提交行；本流程不上传新 S3 对象，因此没有清理对象的补偿动作。若来源也已改变，重新 POST 可能 409，应先查看历史 request_id 确认结果，不能自动换新 ID 重试。无论接口重放或并发，都不修改 P3 Proposal、GradeInput 或 GradeReview。

历史 API 的 source_validity 为 not_checked；单题 GET 明确 latest_source_matches；POST 的 validated_at_commit 指提交时已验证的来源及数据库索引，不能证明对象在未来永远不变。

## 错误

- 401：缺 Token、签名/Issuer/Audience/时间无效。
- 403：用户未建立、停用或无管理员操作资格。
- 404：资源不存在、无作业读写授权或 Question/Submission 跨作业。
- 409：review_source_changed、review_source_not_ready、review_revision_conflict、review_request_conflict；尚无当前对齐时也返回既有 409。
- 422：输入/身份字段不合法或 review_region_invalid。
- 503：认证/授权数据库、存储、来源完整性或 review_unavailable（包括锁超时与未知提交结果）。响应不包含原始数据库错误。

## 受控操作示例

先安全载入 HUIPI_ACCESS_TOKEN、SUBMISSION_ID、QUESTION_ID。下面只做来源检查并保存 uncertain；不会自动推断学生已作答：

```python
import os
from uuid import uuid4
import httpx

with httpx.Client(base_url="http://127.0.0.1:8000/api/v1", headers={
    "Authorization": "Bearer " + os.environ["HUIPI_ACCESS_TOKEN"],
}) as client:
    root = f"/submissions/{os.environ['SUBMISSION_ID']}/answer-reviews"
    target = f"{root}/questions/{os.environ['QUESTION_ID']}"
    response = client.get(target)
    response.raise_for_status()
    view = response.json()
    result = client.post(target, json={
        "request_id": str(uuid4()),
        "expected_revision": view["expected_revision"],
        "expected_source": view["source"],
        "decision": "uncertain",
        "reason_codes": ["source_quality_insufficient"],
    })
    result.raise_for_status()
    print({"created": result.json()["created"],
           "revision": result.json()["record"]["revision"]})
```

不要把 Token 作为 CLI 参数、写入日志或提交文件。确认 response_present 必须先人工检查页面，再构造准确 response_regions 和 student_work_visible 等原因；response_absent 必须引用题干或空白区域。复核结果仍不代表正式成绩。

## 验证

`tests/integration/test_answer_review_http.py` 使用运行时签名 Token、真实独立 PostgreSQL 与私有 S3；来源是合成 MiddleJson 生成并严格验证的 Canonical/Alignment，不假装真实 MinerU 输出。覆盖创建作业/题目/Rubric、上传原文件、present→uncertain、历史链、双教师并发、撤权、跨域、篡改来源、插入失败与丢失 COMMIT 确认恢复。同步使用 Event/Barrier 和数据库锁超时，不用 sleep 碰运气。

迁移测试另建 UUID 数据库，放入一条合法历史复核，downgrade 至 6a9d8c7e2f10 后重新 upgrade/check，比较旧行数据及触发器，不修改开发数据库。CI 不调用外部 IdP、收费模型或本地模型。教师真实语义判断与生产 IdP 验收仍是独立工作。
