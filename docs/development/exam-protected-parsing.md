# 受保护考试答卷解析

本切片打通扫描、人工分卷、身份确认到 MinerU / Canonical 的真实后端和浏览器链路。没有启用 AI 评分、自动身份识别、到考/缺考判断或成绩发布。

## 使用与部署

1. 按[扫描指南](exam-scan-intake.md)建立场次、锁卷、冻结名单、上传 PDF；在[答卷工作区](exam-answer-scripts.md)整理页序并确认身份、实际卷别及完整性。
2. 经学校批准，为操作员在真实 Event/Session Scope 授予 `exam_parse_operator`。旧扫描/分卷角色不会自动获得新权限。
3. 运行 API、Vue 及**独立考试 Worker**。旧 Worker 继续仅处理 legacy。
4. `/scans/sessions/{session_id}/scripts` 选中完整已确认答卷，点击「开始解析」，观察实际队列状态，完成后查看页内容与原扫描页对应关系。

部署初始化需要数据库凭据和已有安全委派权限，不是教师自助提权：

```bash
uv run python -m huipi_cloud.modules.exam_parsing enable-operator \
  --actor-user-id UUID --target-user-id UUID --request-id UUID \
  --approval-ref 审批编号 --confirm-deployment-operation
```

它只创建固定 `exam_parse_deployer` 安装级委派根，并在原安全审计表记录 bootstrap；actor UUID不是登录凭据。再通过现有 `resource_security grant --token-stdin` 委派具体场次。JWT不放进argv、文档或日志。

使用[已配置的本地 MinerU 4.x Basic/ONNX](mineru.md)，不自动下载模型：

```bash
uv run python -m huipi_cloud.workers.exam_parsing_worker
```

Worker只构建现有 `build_mineru_executor`，没有生产 Fake 分支或本地生成模型回退。测试执行器仅在测试模块内显式构造。

## 数据与复用

- `ExamParsingJob`：准确的Script/Version/Session复合外键、唯一来源版本、原可信请求人、独立状态/租约/最多3次尝试、实际产物外键。不是旧队列的额外可见性模式。
- `ExamSubmissionLink`：不可变且一版本一关联，明确保存名单版/Entry/Participation、SessionPaper/PaperVersion、Manifest SHA和真实Submission/File/Task外键。有序原页通过版本的不可变`AnswerScriptPage`追溯，不用任意JSON冒充关系。
- 复用锁卷的`canonical_assignment_id`作为**exam_protected**载体。`student_ref`是答卷版本UUID，姓名/考号不作为唯一提交身份。不同答卷版本生成不同Submission/File和解析结果。
- 旧`ParsingTask`仅作为现有`ParsedArtifact`真实FK的兼容记录；考试Job是唯一执行队列。兼容Task登记后为pending，成功时同事务置succeeded；其进度、失败、重试以考试Job为准。旧Worker的ORM过滤使它无法领取或修改该记录。
- 复用原`ParsingInput / ParsedArtifactResult`、`MinerUParserExecutor`、MiddleJson校验及`normalize_middle_json`，没有另一套解析器或Canonical Schema。
- 现有`ParsingWorker`增加显式repository依赖注入，复用持续心跳、取消、超时和故障退出生命周期。旧入口默认后端不变；考试入口只能使用固定的Job后端。
- ParsedArtifact、CanonicalArtifact与Job成功状态、审计同一PG事务提交。历史产物保留；来源过期时专用查询隐藏结果引用并报告invalidated。

## 来源物化与时效

发起、领取、续租、成功登记和结果读取各自核对当前账号/Grant、真实Session归属、当前答卷版本、当前冻结名单、完整性、有效SessionPaper和locked PaperVersion。持有paper.use不会获得标准答案读取能力。

Job的服务端请求人代表已认证的任务意图。Worker不保存或伪造JWT；仅在具体Job处理中构造短期内部Principal，逐次重新读取该User及Grant。撤权/停用会阻断下一阶段；不能把此内部函数开放成任意用户的特权会话。

每次消费从数据库重新取有序原页，计算六字段`manifest_digest`并与保存值比较；错误摘要409 `script_manifest_mismatch`。Manifest读取也执行此检查，不能仅验证摘要长度。

1. HTTP核验当前来源与实际S3字节后，短事务登记pending及请求审计；同版本唯一Job，重复点击复用。另一授权操作员请求共享Job时记录其实际Principal，不把请求审计归给原申请人；Job授权意图仍固定在原申请人，不能通过重放转移授权。
2. Worker `SKIP LOCKED`领取独立Job，生成新token/DB时间租约。
3. 流式核验原PDF及选中JPEG，子进程按可信原`page_index`拼接PDF；保留原分辨率/文本，不覆盖ScanFile/Page。新输入使用Job/token独立私有Key，记录大小及SHA，再实际回读验证。
4. 再检查租约/来源，原子建立Submission/File/兼容Task/Link和物化审计。名单/答卷已变化则不登记。
5. MinerU执行前和产物准备后再次核验原来源字节。解析与Canonical上传均在执行任务内，持续心跳；最终事务重新检查current、权限、租约和精确输入身份，才登记成功。

原件物化过程中变化或S3故障不会产生成功Link；数据库故障和COMMIT未知不删除对象。成功登记确认丢失后可读状态并重放原请求恢复。输入/输出写入使用不可变尝试Key，失败、取消、进程崩溃可能留下孤立对象；GC必须延后按Job/token、Link和产物索引对账，本切片未实现自动GC或Exactly Once。

## 权限与HTTP

新增固定 `exam.parse.start`、`exam.parse.read`，范围仅真实 installation/Event/Session。发起要求同Grant覆盖start+script.read+identity.verify，另有同场次roster.read。读取独立要求read及名单权限；页正文读取同Grant覆盖read+script.read+identity.verify。两条不完整Grant不能拼成完整发起权限。旧admin、Membership、JWT role没有捷径。

固定角色版本：

- `11111111-b14b-4000-8000-000000000001`：operator，分卷/扫描/名单读取和两项解析动作。
- `...0002`：deployer，另含安全委派。
- `...0003`：reader，解析/分卷/身份核验/名单只读，无start。

HTTP均要求真实Bearer身份、固定路由策略和当前数据库授权；不返回私有对象Key或内部Worker控制能力：

- POST `/api/v1/answer-scripts/{id}/parsing`：仅`request_id`和`expected_revision`，202返回job_id，不接受客户端manifest、SHA、学生或存储位置。
- GET `/api/v1/answer-scripts/{id}/parsing`：当前版本Job或not_started。
- GET `/api/v1/exam-parsing-jobs/{id}`：精确历史Job的当前有效性与状态。
- GET `/api/v1/exam-parsing-jobs/{id}/pages/{page_number}`：**从零开始**，返回Canonical页及原ScanPage/File/page_index/SHA映射；读取S3后再次验证版本、权限和当前状态。

状态：pending、running、retry_wait、succeeded、failed、invalidated。状态接口报告已登记事实及数据库来源有效性，不承诺S3实时健康；正文读取实际核验Canonical大小和SHA。状态仅表示解析，不代表评分或成绩。confirmed但incomplete、未确认身份、过期名单、取消场次或退役卷不能发起。

来源或授权已失效的Job不自动复活；已用尽尝试也不无限重试。操作员核对后建立新答卷版本、重新确认再发起。503或连接中断时保留原request_id/正文，先刷新状态；异正文同键409。已经过期的来源不能通过重放恢复访问。

## 锁序与资源限制

- HTTP：当前User/Grant SHARE → Event SHARE/Session UPDATE → 来源指针 → 请求锁/Job读取或创建 → 审计。
- Worker：Job `FOR NO KEY UPDATE SKIP LOCKED` → 当前User/Grant → Event/Session → 来源/名单/卷版。No Key Update允许审计FK的KEY SHARE，避免重复发起持Session锁时与Worker互等。不同业务版本仍受Session短事务串行化；不是无限并行处理。
- 租约固定180秒，数据库`clock_timestamp`在Job锁后判断；心跳复用现有配置。单句8秒、锁等待5秒。失去租约取消执行，子进程由原监督器清理；不合作执行仍按原Worker故障退出策略处理。
- 每答卷100页、8原文件、来源核验总量256MiB；每原文件仍32MiB/50页。合成输入PDF最多32MiB。
- 拼接子进程40秒墙钟、35秒CPU、1GiB地址空间、32MiB输出文件硬上限，父进程死亡信号及进程组回收；单次文件核验/组装/上传最多75秒（短数据库事务另受单句和锁超时约束），临时源文件落盘，不无界读入内存。
- Worker单进程串行；部署需另行限制MinerU主机内存/进程并发。本切片不新增MinerU硬内存上限，不能把解析超时宣称为容器RSS配额。
- 发起/Manifest/Canonical页读取本API进程合计最多2个，整体HTTP100秒；Canonical对象按现有配置有界读取，页面正文最多1MiB。JSON发起请求64KiB。
- 扫描预览是人工确认的辅助来源；不做新旋转/裁剪变换。原页顺序映射为解析第i页→有序清单第i项。

## 下一轮Alignment / GradeInput

`GET Job`给出准确的answer_script_version_id、ParsedArtifact/CanonicalArtifact受控引用；页接口给出原文件、原页码、SHA。可信身份与卷别通过不可变Link→RosterEntry/Participation/SessionPaper/PaperVersion获取，不由模型选择。

后续必须建立**任务绑定**的考试Alignment/GradeInput处理入口：每次消费及提交重新检查Job/来源版本、名单与授权。不能直接调用旧`align_submission`或`prepare_grade_input`并放开全局ORM过滤。

`PaperQuestionMapping`真实关联锁卷`canonical_assignment_id`下的`legacy_question_id`。后续题目、AnswerKey和Rubric从所选PaperVersion快照读取，不能从原作业或另一个卷版匹配题号。当前没有自动对齐、评分输入准备、候选评分、教师评分复核或发布；也未新增这些HTTP能力。

## 验证

[PG/S3专项](../../tests/integration/test_exam_parsing.py)、[纯单元](../../tests/unit/test_exam_parsing_protocol.py)、[浏览器流程](../../frontend/e2e/workbench.spec.ts)。真实MinerU和浏览器均显式opt-in；默认CI不下载模型、不调用收费API。执行证据、截图及限制见[验收记录](../evaluation/exam-protected-parsing.md)。

新迁移`b14b41a2e903`只增量建立三表和新角色，旧UUID和迁移不改。有Job/Link/审计或该切片Grant时拒绝破坏性降级；生产恢复应采用保留历史的向前修复。
