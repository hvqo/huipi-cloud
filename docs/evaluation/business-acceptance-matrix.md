# 考试阅卷验收矩阵与硬不变量

**规划文件；下列新业务测试尚未实现/未执行，不是通过报告。** 原 T01–T30 的权威文字在[需求第18节](../product/requirements/school-exam-baseline-v1.md)。这里补足输入、操作、真实断言与证据方式，不改写验收含义。

## 1. 测试分层

- 单元：状态转移、scope predicate、Decimal、页映射/坐标、payload digest，不调用收费模型。
- 合同集成：独立真实 PostgreSQL + 私有 S3，合成学生/试卷、固定签名 IdP；检查实际表/字节/审计，SQLite 或 Fake 存储不能报告 PG/S3 E2E。
- 浏览器：真实签名身份登录、操作页面、网络返回字段/图像、安全下载与重放；Mock 仅前端交互辅助。
- 设备验收：授权的真实设备样本/连接/方向/重传、容量实测，单独报告。
- 教学质量：授权真实材料、独立教师双评/仲裁，另建冻结标签；合成测试不得宣称手写/评分准确率。

并发用 asyncio.Event/Barrier/数据库锁同步，不靠 sleep 碰运气。测试只清自己的独立库/对象前缀，不清开发库、开发桶或卷。

## 2. T01–T30 可执行规划

每行未来落到相应新模块 `tests/unit/`、`tests/integration/` 和前端 E2E；这些是建议测试目标名，不指向尚不存在的文件。

| ID | 阶段 | 可复现前置 | 执行动作 | 必须检查的数据/行为 | 证据方式 |
|---|---|---|---|---|---|
| T01 | B/B-2 | 1班名单、纸质作业、legacy 作业并存 | 人工分卷并上传/复核 | 真实 Submission/File/Task 与学生绑定，旧一次提交409规则不变 | PG+S3+浏览器；复用旧授权/上传回归 |
| T02 | B-1.2 | 8班共用1卷版、400参试者 | 建场次/名单后按班查询 | 数据库仅1份内容/规则；每考生班级快照且不靠题号串联 | PG FK/计数/摘要、API |
| T03 | B-1.3～1.5 | 8班混排1600页合成PDF | 有界接收、人工重排/拆合/确认 | 每页唯一有效占用、页序/归属可手工核对；未归页不丢；记录内存磁盘而非虚构吞吐 | PG+S3 manifest+浏览器+合成压测 |
| T04 | B-1.2/1.4 | 两场同日同卷，同一模板 | 无场次上传再人工绑定，故意绑错场 | 图片匹配不能确认场次；未知留隔离，冲突409；已确认对象同域 | PG/API+页面状态 |
| T05 | B/C | A/B题号相同题序不同 | 映射并分别解析/人工核验 | 来源 question_key/legacy question FK 来自选定PaperVersion；跨版引用拒绝 | PG+S3+映射单元 |
| T06 | B-1.2/1.4 | 2班同名、考号/姓名冲突 | 只给姓名候选，再选可信名单项 | 不能自动核实；核实保依据/actor/名单版；另一人不占位 | PG+API+浏览器 |
| T07 | B-1.4/C | 应有第3页而缺失 | 确认页组/尝试生成评分输入 | incomplete阻断就绪/有效分，不生成缺页零分，其他原件仍保存 | PG+来源门禁 |
| T08 | B修复/C | 跨批次附加解答纸 | 人工归组、核实、产生新版 | 新页序含原始file/page/变换，旧来源不改；不硬限固定4页 | PG+S3字节/manifest |
| T09 | C-3/D/E | 已阅21题，新补页涉及21题 | 换current来源版、重评检查 | 旧分保历史，21题失效；未影响题仅在可证明依赖相同时复用；未知整卷隔离 | PG source_epoch+并发写 |
| T10 | C-1/2 | 1–20客观、21–23主观答题卡 | 配准/OMR/原图人工证据查询 | 各自producer/source，不造文本pointer；统一question_key、坐标可回原图 | 真实模板样本另验；PG+S3合成 |
| T11 | C-2 | 擦痕、多涂、模糊 | 识别并人工判读 | ambiguous/多涂独立状态，识别失败不转零分；候选不覆盖人评 | OMR标注+API门禁 |
| T12 | D-1 | 3班/4班教师各自整卷或题组授权 | 任务获取/文件访问/提交 | 只有授权范围成功，班级路径参数不能扩权，返回内容无他班数据 | 真实JWT+PG+字节检查 |
| T13 | D-1 | 跨班18题匿名任务 | 遍历题区和旧34操作/新资产/CLI工具 | 仅18题安全区域，无身份页/其他题/总分/真实资源ID；旧admin也无旁路 | API+浏览器网络响应+图像核验 |
| T14 | D/E | 选择自动候选、填空按班、压轴跨班 | 生成任务/Record再合分 | 同Session不同题组策略，候选不直接有效分，题组每轮覆盖不重叠、同题不重复计分，所有需要题按规则覆盖 | PG策略参数化+Decimal |
| T15 | D-2 | 双评分差超学校设定阈值 | 提交两评、发仲裁 | 无Resolution validated，生成第三评/终评且记录两独立来源 | PG+状态机 |
| T16 | D-2 | 同一User的两个岗位/Token | 并发领取一评和二评 | 独立性guard拒绝同人完成两轮，不能靠换角色或新Token绕过 | PG Barrier+unique/guard |
| T17 | B安全/D | 教师已打开阅卷页 | 撤授权或停用账号后提交/重放 | 拒写且无新Record；已提交历史不删；审计不含Token/答案 | PG Event+真实JWT |
| T18 | D-1 | 2教师同时领取同slot | Barrier同步领取并提交旧token | 只有一个current claim；旧token/到期不能提交；无重复有效题分 | 真实PG并发锁 |
| T19 | D-1/2 | 部分任务已有Record | 组长改plan revision并回收 | 完成记录不变；仅未完成当前slot重派；旧lease提交失败 | PG历史SHA+新旧计划查询 |
| T20 | B/X/E | 缺考、补考、晚扫、空答样本 | 名单状态变更/新attempt/接收 | 缺考、not_scanned、blank_confirmed、score_zero分开；补考追溯原记录 | PG/API状态维度 |
| T21 | E-1 | 未阅题/过期证据/未仲裁 | 准备快照/审批 | 完整性检查列出阻断项，不用0补齐，不进入approved/publication | PG+API负向 |
| T22 | E-2 | 已发布快照与待复查题 | 有权更正/重新审批发布 | 新Snapshot/Publication关联旧版，旧字节和记录不可覆盖 | PG触发器+来源链 |
| T23 | B-1.3/1.4 | 重复文件/相同request/COMMIT丢确认 | 并发重传、注入ACK丢失、原请求恢复 | 一个有效Link/Submission，已引用对象存在；空查询不证明回滚，原请求异body409 | PG+S3故障注入/摘要 |
| T24 | 全阶段 | 历史作业/Review/Proposal种子 | 新迁移前后及旧客户端操作 | 旧UUID/文件/JSON/SHA不变；OpenAPI兼容；真实回归而非Mock替代 | 独立迁移往返+旧HTTP/浏览器 |
| T25 | B-3 | 仅场次上传权限设备凭据 | 试读原卷/名单/其他batch/角色接口 | 全部拒绝，不能冒充User；撤凭据立刻停新传输 | PG+设备协议+HTTP |
| T26 | B-1.4 | 无身份、混页、收卷顺序未知 | 请求自动/人工无依据确认 | unclaimed/source_insufficient，不能物化可信学生提交/正式成绩 | PG无Link/Record+UI原因 |
| T27 | B/P/D/E | 同卷两班不同Rubric | 关联独立评分策略版/比较分析 | 必须显式版次/范围，不偷偷共用；可比性待审核，不强合排名 | PG version FK+API |
| T28 | B/O/E | 考试时3班、后来转4班 | 更新当前班关系再看旧考试 | 旧名单快照/统计仍3班；任教可见范围仍按当时结果授权策略 | PG历史摘要+查询 |
| T29 | P/C-3/D | 已有来源和Record | 新Rubric版本，不原位改锁卷 | 旧来源/分数留历史，受影响重评，老任务不能按新规则提交 | PG锁版/版本失效 |
| T30 | C-1/3 | 旋转/裁剪偏移、换图、未知坐标 | 构造不一致bbox/图SHA/变换 | 拒绝错误位置映射，unresolved、人工处理；不靠归一化掩盖偏移 | 坐标单元+S3 SHA+API |

## 3. 业务不变量 I01–I30

以下对应需求附录 A，并补充实现时必须验证的并发/隐私不变量；不表示已有 DB 约束。

1. I01：已确认答卷只有一个当前有效场次、卷版和参试身份；未核实不能出正式分。
2. I02：同一物理 ScanPage 的当前响应占用最多一份 script；跨 batch/plan 也要查 CurrentPageAllocation。
3. I03：任何题目来源可回溯 file SHA、source_page_index、变换和页组版本。
4. I04：班级不是考试/试卷主键，跨班共享卷版不复制题库。
5. I05：同卷图像不证明场次；无可信上下文不自动绑定。
6. I06：A/B 不靠 printed_label 唯一；PaperQuestionMapping FK 决定题目。
7. I07：确认/物化以可信 actor+原请求摘要幂等，异正文同键冲突。
8. I08：OCR/模板/身份候选不自动变成人工确认事实。
9. I09：缺考、未扫、缺页、识别失败、人工空答和有效零分互不替代。
10. I10：模板不符或坏 PDF 拒业务关联但不丢原件/错误谱系。
11. I11：机器候选不改人工证据/评分历史。
12. I12：每条评分引用冻结题目/Rubric、作答/证据版和操作者。
13. I13：来源变化先失效/隔离再异步求细粒度影响，不能有短暂有效窗口。
14. I14：重派/撤销任务不删除已提交 Record。
15. I15：独立双评同一真实 User 不得完成两轮，跨 token/角色仍拒。
16. I16：任务权限不衍生完整 Submission/Canonical/原件读取权限。
17. I17：题分/总分 Decimal，范围、舍入和选做策略版本固定。
18. I18：缺必要评分/来源/轮次时不能 approved 或 published。
19. I19：候选完成/教师接受不是 Publication。
20. I20：更正产生新快照/发布链，旧快照不可静默改写。
21. I21：私有文件和敏感身份逐资源授权，默认不公开 Key/token。
22. I22：COMMIT 未知保留可能引用对象；空查询不证明可删。
23. I23：旧 student_ref 不未经核验转成真实 Student/Participation。
24. I24：分卷/身份/评分确认记录可信 actor、版本、时间、原因和 scope。
25. I25：新 exam-protected 资源在所有旧 HTTP、列表、Agent/CLI 中不能走 legacy admin bypass。
26. I26：同一 source version 只物化一次，新版不能改旧 SubmissionFile/ParsedArtifact。
27. I27：User/RoleGrant 以数据库当前状态校验；role声明/菜单/缓存不授予权限。
28. I28：一条 grant 满足完整 scope predicate，不组合不同岗位范围扩权。
29. I29：设备只能指定接收，不可浏览全桶、考生名册或评分。
30. I30：匿名题区不能携带身份（含原图手写姓名），无法安全裁剪须隔离而非返回整页。

## 4. 权限负向与并发故障附加用例

| 编号 | 注入/攻击 | 数据断言 | 计划阶段 |
|---|---|---|---|
| N01 | 未签名/错误issuer/audience/过期JWT、伪造role | 未进入业务读取，安全401/403；无权限新增 | B安全；保留旧JWT用例 |
| N02 | 停用User、撤Grant、RoleRevision紧急停用 | 新请求拒绝；写事务与撤权有明确锁序；不产生新Record | B/D |
| N03 | 两行grant分别覆盖动作/范围企图拼接 | 不产生交叉扩大；predicate完整匹配 | B安全 |
| N04 | 同一老师既任课又匿名跨班阅卷，切工作空间 | 原件/完整Canonical/总分仍拒绝；受批准破窗另审计 | D |
| N05 | 旧admin对新桥接ID调用所有34操作/CLI检索 | 无资料/学生ID/其他题字节；legacy作业仍原规则 | B桥/D |
| N06 | scanner枚举其他场次名单、题目答案 | 404/403且响应无身份；有权identity.verify仅目标名单 | B |
| N07 | scan设备读取/删除S3或冒充User | 无接口/拒绝，凭据隔离 | B设备 |
| N08 | 同一页两plan/两batch并发确认 | 一个current allocation，失败方无Link/Task半成品 | B桥 |
| N09 | 确认后、物化S3期间换页/改身份/撤授权 | COMMIT前epoch/token校验拒绝，不写过期Link | B桥/C |
| N10 | 上传中取消，线程仍读文件 | 生命周期有界，不能删在途临时源；取消不等于杀线程；不登记成功 | B接入 |
| N11 | S3失败、DB flush失败、真实COMMIT成功ACK丢 | 各自不同安全结果；已登记对象存在，未知结果原请求恢复 | B桥 |
| N12 | 作答/题目/Rubric摘要变化后提交评分 | 409，不追加当前有效记录；旧不可变历史保留 | C/D |
| N13 | malformed page pointer/bbox/Unicode surrogate | 拒绝非法来源，不以其他节点相同偏移冒充；无DB写入 | C |
| N14 | 两轮任务同时被同人领取/不同人同task | guard/slot锁拒绝不独立或重复claim；旧token不能完成 | D |
| N15 | 撤权后读取operation幂等结果/重放 | 当前授权先校验，不能泄露历史响应 | B/D |
| N16 | API或审计泄露姓名、Key、原文/Token | 对响应字段/日志/文件内容断言，必要身份仅限授权核验投影 | 全阶段 |
| N17 | 审计插入失败/授权DB不可用 | 事务回滚，503，既不放行也不显示成功 | B/D |
| N18 | 成绩发布同时来源变化/自批自发 | 快照锁/epoch阻断，无有效Publication；审批历史仍在 | E |
| N19 | 重复student_ref保留前缀试图伪造考试提交 | legacy public上传拒绝，必须可信桥接事务 | B桥 |
| N20 | 使用过期结果分页cursor/直接复制asset URL | 重查scope/版本，无跨考场/其他题/学生资料 | B/D |

## 5. 非法状态转换测试

对每个状态机做允许边的参数化测试及其补集拒绝：receiving 不能直接 confirmed；unclaimed 不能直接 materialized+verified；incomplete 不能 ready-for-scoring；proposed OMR 不能直接 published；过期 claim 不能 submitted；submitted 不直接删除变 pending；invalidated 分不能进新快照；prepared 未审核不可 published。

错误时同时验证：无副作用业务行、审计/Operation真实记录、S3对象保留或已证实安全补偿；不要只断言HTTP状态码。pending job 的重试、达到上限、Worker重启、租约过期和非合作取消同样需测试。

## 6. 迁移及历史数据保护

独立测试库：建旧head→写legacy Assignment/User/Membership/Submission/Review/Proposal→保存行数/关键JSON/文件SHA→upgrade新head→check→验证旧API及新FK/UNIQUE/触发器→downgrade仅测试新增数据可清理情形→re-upgrade。另测已有新业务数据时拒绝破坏性回退或提供显式归档方案，不能将测试可downgrade当生产可删数据。

旧回归参考：[教师授权](../../tests/integration/test_teacher_authorization.py)、[管理角色](../../tests/integration/test_admin_role_http.py)、[作答复核](../../tests/integration/test_answer_review_http.py)、[评分复核](../../tests/integration/test_grade_review_http.py)、[解析恢复](../../tests/integration/test_parsing_runtime.py)。阶段A仅阅读它们，未声称本轮重跑。

## 7. 交付证据记录格式

每切片验收记录：commit/head、数据库隔离标识、migration head、用例ID、输入合成/授权性质、执行命令、实际passed/skipped、对象/DB摘要、失败原因和残余风险。合成硬门禁、真实设备、教学双评质量分开写。默认CI禁止收费云调用或自动下载模型。
