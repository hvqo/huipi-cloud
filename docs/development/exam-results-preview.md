# 考试整卷成绩预览

## 用途与边界

`/exam-results` 或 `/scans/sessions/{session_id}/results` 提供内部教师预览。扫描工作台也有「成绩预览」入口。
数据由服务端从当前冻结名单、当前确认答卷、实际 SessionPaper/PaperVersion 的全部题目映射和教师最新复核记录重新计算。
不创建正式成绩表，不审批、发布、排名或推断到考。没有答卷不等于缺考；未复核不补零。

每份实际卷版的全部映射题目均视为必答题。目前没有可信选做规则合同，不能用本功能处理选做计分，
也不能把 A/B 卷同题号自动合并。同一锁定卷版仍可跨班、跨场次复用，不复制试卷内容。

## 分数与当前性

- `accepted`：原 GradeReview 已明确接受的候选分值。
- `revised`：该次完整 Criterion 修订快照中的教师分值，不从前一条记录增量累加。
- 最新 `rejected` / `regrading_required` 覆盖旧的接受记录；弃权、缺少复核和任务失败没有有效题分。
- 已配置答题卡模板的客观题采用当前答卷的最新 OMR 教师确认记录；识别建议本身不计分，
  确认空白后的零分与尚未确认的空白分开。模板中的题目以答题卡确认为准，不与同题文本复核相加；
  非模板题目继续使用原 GradeReview。纯客观题可在未运行文本解析时完成预览。
- 按 GradeInput 的真实题目、Rubric、Proposal 及私有对象 SHA 重新验证，保留原 Decimal、正分 Quote、总分约束。
- 当前答卷/名单、卷版、来源 Canonical 与原扫描文件/预览字节都要有效；原评分申请人的授权也继续校验。
- 所有必要题目有效复核后才有 `total`；否则只有已复核题目的 `subtotal`。没有任何有效复核时小计也是 `null`。
  明确复核零分才是 `"0.00"`。拒绝和来源失效不转换成零分。
- `attendance_status=not_determined`、`published=false` 固定表明未确认到考、未发布成绩。

当前记录 ID、review_revision、script_version_id、paper_version_id 和 roster_revision_id 可回溯。
客观题结果以 `evidence_source=omr` 和 `omr_decision_id` 标记，原图及识别证据由答题卡工作台查看。
逐题对话框展示当前记录，完整修订历史通过「进入答卷批改」查看现有工作区。
来源失效不显示旧完整总分；答卷更正草稿取消身份关联时显示待重新确认，而非沿用旧学生分数。

## 明确班级授权

新增 `ExamResultClassAccess` 只收紧本模块的预览和导出。它通过真实外键关联一个 SecurityGrant、ExamSession、AdministrativeClass；
不是客户端传入班级 ID 就获得授权，也不是学校任教关系的替代模型。

读取所需的 `exam.mark.read`、`exam.roster.read`、`exam.script.read`、`exam.identity.verify` 必须全部来自同一个父 Grant，
且父 Grant 的真实安装/Event/Session Scope 覆盖该场次。导出还要求同一 Grant 的 `exam.score.review` 和该班级行的 `export_allowed=true`。
不同 Grant 不能拼接。旧管理员、AssignmentMembership 和 JWT role 均无捷径。
若可查看班级中含不可导出的班级，不带班级筛选的整场次导出会拒绝，必须显式选择获准导出的班级；不会悄悄省略行。

受控部署人员持有同 Scope 的可委派授权与 `security.grants.manage` 才能登记班级限制；不能自己给自己登记。
班级必须已存在于该场次的当前冻结名单。撤销班级行、撤销父 Grant、停用用户后，新请求立即拒绝。
Grant/用户/班级行在操作内持 SHARE 锁，已开始读取与撤权按事务锁顺序完成；不承诺已发送给浏览器的文件能远程收回。

通过现有 OIDC 验证的 JWT 从标准输入读取，不把 Token 写入命令行参数或日志：

```bash
uv run python -m huipi_cloud.modules.exam_results grant-class \
  --token-stdin --request-id <UUID> --grant-id <UUID> \
  --session-id <UUID> --class-id <UUID> --export-allowed
uv run python -m huipi_cloud.modules.exam_results revoke-class \
  --token-stdin --request-id <UUID> --class-access-id <UUID>
```

由安全输入管道提供一行 Token。不要把凭据写入 shell 历史。相同 request_id 依现有安全操作摘要幂等，正文冲突拒绝。
授权及 SecurityAudit 同事务，数据库触发器禁止删除/改写历史，只允许首次撤销。没有公共班级提权接口。

**兼容边界：**旧扫描、分卷和批改接口仍保留原 Event/Session 授权，不在本轮自动变成全系统班级 RBAC。
若教师已有整场次批改/名单权限，它在旧工作区的能力未被此限制撤销；部署需同时审查原 Grant。
后续按班细分阅卷应单独收紧这些旧入口，不能仅隐藏页面。

## HTTP 合同

- `GET /api/v1/exam-result-sessions?exam_event_id=&offset=`：仅返回有明确班级授权的场次及其授权班级，20 个场次分页。
- `GET /api/v1/exam-sessions/{id}/results-preview?class_id=&participation_id=&offset=&limit=`：班级/参试身份在服务端过滤；默认20人，最多50人。
- `GET /api/v1/exam-sessions/{id}/results-preview.xlsx?class_id=`：重新鉴权、重新读取当前数据，不使用前端已缓存的分数或直链。

响应只含学生最小名单字段、分数、状态和来源 ID，不含学生答案、Quote、模型理由、私有对象 Key。
读取班级之外的 ID 与不存在的资源统一404；未登录401，停用403；限制413，服务不可用503，输入参数422。
没有名单时返回空集合，不生成缺考/零分。生成的 OpenAPI TypeScript 合同供前端复用。

## 一致性、导出与上限

一次读取使用 PostgreSQL `REPEATABLE READ`，并锁住场次行；现有名单/答卷/评分复核写入在该行串行化。
私有产物校验包含在这次有界事务内，因此并发复核只能落在本次导出之前或之后，不混用版本。
这是小规模 MVP 的一致性取舍，不是大规模吞吐承诺；未来需要批量投影或可验证快照，不得去掉来源检查换取性能。

- 单次最多100个明确班级限制行、100道映射题；导出最多100人、2000个题目单元。
- JSON最多4MiB、xlsx最多8MiB，累计来源读取128MiB。
- 整次数据库读取/来源校验60秒；单条SQL8秒、锁等待5秒。超过预算返回安全错误，不能回退旧分数。
- xlsx 三张表：成绩预览、来源版本、导出说明。Decimal直接转数值字符串；学号/考号保留前导零。
- 用户文字均使用 OOXML `inlineStr`，以 `= + - @` 开头也不是公式；无外部关系、宏或公式。
- 附生成时间、来源版本、摘要及「未发布」说明；空白表示缺评或不适用，不代替0分。
- 工作表保护仅防误改，**不是**加密、签名或正式成绩发布凭证。下载后由学校负责保管。

## 迁移与下一步

新增迁移 `b15a60c4f015`，只建班级访问表，不改已有数据或历史迁移。
有授权事实时拒绝破坏性降级；部署升级需由操作员执行，不自动升级开发数据库。
真实验证记录见[验收报告](../evaluation/exam-results-preview.md)。

正式发布仍缺：可信缺考/到考、选做规则、审批与发布版本、纠错撤回、成绩冻结/审计导出策略、真实教师语义验收。
当前候选评分仍可能发生教学高估，字符定位合法不能代替人工复核；不因新增预览启用新评分策略。
