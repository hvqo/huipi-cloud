# 人工分卷与身份确认

本功能提供实际受保护的 HTTP 和 Vue 工作区。整理扫描页面、确认学生身份和实际卷别，分卷本身不执行 OCR、评分或成绩发布；已确认完整答卷的专用解析见[受保护解析指南](exam-protected-parsing.md)。

## 页面与使用流程

登录后进入「考试扫描」(`/scans`)，选择有权场次，进入「人工分卷与身份确认」(`/scans/sessions/{session_id}/scripts`)。

1. 选择全部批次或一个批次，默认显示未认领页。缩略图按需加载，可打开受保护大图；不按文件名、上传顺序或姓名推测学生。
2. 依次加入页面，上移、下移或移除。可跨同场次文件、批次补页。勾选完整性是人工声明，系统不自动推断漏页。
3. 填写分卷依据，保存答卷草稿。草稿也占用页面，身份仍为 unclaimed。
4. 填写确认依据，打开名单搜索，按姓名、学号、考号、考试时班级区分学生。只返回当前冻结名单，每组20人。
5. 明确实际卷别后确认。已知名单卷别自动展示但仍经服务校验；未知卷别保持空选项，必须按原卷手动选择。同名学生具有不同的名单项和稳定 Participation。
6. 修改已确认答卷时可直接重新确认新页组，或保存新版本草稿。后者解除**当前**身份确认，旧确认留在历史。撤销产生新版本并释放页面，不删除原卷和历史。
7. 查看历史版本、页序和身份。若名单已更正或卷版退役，当前答卷显示须重新核验；不把旧确认作为解析准入。

当前只支持追加/重排/移除原始页，不支持裁剪、旋转修复、将一张物理页拆给不同学生、自动分卷或名单更正界面。重复扫描的不同 ScanPage 不能自动证明是同一物理纸张，须操作员核对。跨场次更正需撤销错误答卷，再在正确场次重新接收原件，不能直接跨场次移动页面。

## 数据合同

- `AnswerScript`：稳定场次身份，只有 revision、current_version_id、current_participation_id可按规则切换。当前 Participation 唯一，避免同一考生同时拥有两份已确认答卷；历史确认仍可追溯。
- `AnswerScriptVersion`：只追加；首版必须草稿。每次保存、确认、撤销都生成完整新快照，版次递增、唯一后继、真实同脚本/场次外键。保存 actor、时间、原因、manifest_sha256，以及确认使用的真实 RosterRevision、Entry、Participation、SessionPaper。
- `AnswerScriptPage`：不可变有序原页清单，记录 ScanPage、ScanFile、原零基页码、文件SHA、JPEG SHA。数据库检查来源归属和摘要，页序必须连续；只能在新版本创建事务中插入，不可事后往历史补页。
- `CurrentPageAllocation`：ScanPage 为全局主键。每张页最多属于一份当前有效答卷，包含草稿。数据库延迟约束要求占用集合恰好等于当前非撤销版本页组；指针与占用一起原子切换，不能直接删除占用或转移所有者。
- `AnswerScriptOperation`：actor/request_id 唯一的追加审计和幂等回执。记录动作、请求摘要及小型结果，不保存完整名单、Token、对象Key。

ScanFile、ScanPage、RosterEntrySnapshot不回写学生身份或新页序。身份来自所选当前冻结名单项，不接受客户端 Student UUID、Participation UUID、actor、角色或私有定位。

完整性与身份是两个维度：允许确认不完整答卷的身份，但 readiness=incomplete，Manifest准入拒绝。draft=unclaimed；revoked不占页；confirmed是已记录的人工决定，只有实时检查通过才是identity_confirmed，仍不表示已解析、可评分或已发布。未知卷别不能确认身份。本版若实际卷别与名单中已知卷别不符，须先通过名单更正流程冻结新版，再重新确认。

## 权限与部署

固定动作 `exam.script.read/edit/confirm`、`exam.identity.verify` 仅支持已有真实 installation/Event/Session Scope。旧is_admin、Membership、JWT role没有访问捷径。

- 分卷读取需script.read；页面选择还要求同一个Grant同时覆盖script.read+scan.file.read。
- 创建草稿需edit；确认需同一个Grant覆盖edit+confirm+identity.verify，并独立验证同场次roster.read。
- 修改/撤销当前已确认答卷需要同一个Grant覆盖edit+confirm，不能仅靠普通编辑权限取消可信身份。
- 学生名单读取独立需要roster.read。没有该权限时，答卷/历史投影隐藏名单、Participation、卷别标识及学生信息；页面预览仍走原scan.file.read接口。
- 每个请求读取当前账号/Grant；写事务持授权共享锁，撤权等待已进入事务的写入完成，之后的新操作和幂等重放均拒绝。存储核验期间不持数据库事务，最后重新授权。

迁移不自动扩展旧角色或授权用户。新 `script_operator` (`11111111-b14a-4000-8000-000000000001`) 包含分卷四动作、扫描四动作及roster.read；只能按学校确认的场次范围委派。`script_deployer` (`...0002`) 另含security.grants.manage，是受控部署根，不是普通教师。`script_reader` (`...0003`) 只读分卷/扫描，不读名单；`script_editor` (`...0004`) 可编辑草稿及读名单，不能改已确认结果；`script_verifier` (`...0005`) 只含confirm/identity.verify，不能单独或与editor Grant拼接完成确认。需要完整确认时应显式委派operator的单一Grant。

```bash
uv run python -m huipi_cloud.modules.answer_scripts enable-operator \
  --actor-user-id UUID --target-user-id UUID --request-id UUID \
  --approval-ref 审批编号 --confirm-deployment-operation
```

该命令需数据库凭据并检查当前部署授权；actor UUID不是认证凭据，不存在公共自助提权入口。再由持有效JWT的部署根用既有 `resource_security grant --token-stdin` 将operator角色授予真实Session/Event Scope；撤销复用原revoke命令。不要将令牌放入argv或日志。

## HTTP合同

全部位于 `/api/v1`，真实OIDC/JWT认证，private/no-store/nosniff。OpenAPI和TypeScript类型与固定路由目录同步。

- GET `/exam-sessions/{id}/script-pages`：同场次来源页，`batch_id`可选，`unclaimed`筛选，分页与当前占用所有者。文件名不用于身份。
- GET `/exam-sessions/{id}/identity-candidates`：`q`按字面包含匹配；当前冻结名单与可用卷别选择，只返回该场次的必要快照标签。
- POST `/exam-sessions/{id}/answer-scripts`：`{request_id,page_ids,completeness,reason}`，201和小型回执，首版草稿。
- GET `/exam-sessions/{id}/answer-scripts`、`/answer-scripts/{id}`：当前版本、实时readiness、必要来源和授权后身份。
- GET `/answer-scripts/{id}/versions`：倒序分页历史，明确previous_version_id。
- POST `/answer-scripts/{id}/versions`：完整页组、`request_id/expected_revision/action/completeness/reason`；action为save_draft/confirm/revoke。confirm另需`roster_revision_id/roster_entry_id/session_paper_id`。revoke保留当前完整页组，避免隐式改页。
- GET `/answer-scripts/{id}/manifest`：只接受当前完整且身份实时有效版本，重新核验原件/预览SHA，返回Manifest 1.0，不含Key或存储凭据。

列表最多20条、答卷列表/历史默认10条，offset≤100000；每场次最多4000份脚本、每脚本最多100版、每版1–100页、8个来源文件。核验来源总量≤256MiB（原件去重+所选JPEG）。JSON请求体64KiB，确认/Manifest本进程最多2个，HTTP总时限100秒，存储总核验60秒、单对象45秒，DB每句8秒/锁5秒。文件/页本身继续遵守扫描32MiB/50页/单JPEG2MiB上限。

缩略图仅加载当前分页（最多20张），最多2个同时读取；切换页/退出即释放Blob。整体仍是单API进程MVP上限，多进程会倍增，未验收1600页容量。

## 事务、故障与来源时效

1. 短事务确认当前Grant、预期revision、场次ready、当前冻结名单及同场次有效卷别，捕获来源页组。
2. confirm在事务外有界下载所选原件与JPEG，逐字节核验长度/SHA，不新增S3对象。
3. 短事务重新授权；actor/request advisory锁 → Event SHARE → Session UPDATE → Script UPDATE → 有序Page锁 → Roster/Paper共享锁。重新检查版本、名单、卷版和来源。
4. 新版本/有序页组、指针、唯一占用、当前Participation和审计在同一事务提交。数据库触发器/唯一键/延迟约束覆盖直接SQL核心不变量。

同请求同正文返回原回执，不代表仍为当前版本；之后GET刷新。不同正文409。冲突409、依赖/存储或提交结果未知503；不返回SQL、私有Key或原始异常。未知COMMIT保留request_id和正文重放，不删除S3对象。审计失败整体回滚，无假确认。确认失败不清除原稿/历史。

列表readiness是数据库身份与状态检查，**不承诺对象存储当前仍完好**。确认与Manifest会实际核验字节；S3故障、缺页、摘要不符不能产生新的confirmed决定或可用Manifest。可信账号/DB所有者故意禁用触发器、TRUNCATE或直接伪造审计不属于应用授权边界。

## 下一切片解析桥接

只读Manifest 1.0提供：稳定Script ID、准确Version ID、Session、当前RosterRevision/Entry、Participation、SessionPaper/PaperVersion、按顺序的ScanPage/File ID、原零基page_index、文件和JPEG SHA、页组摘要、validated_at。原页无裁剪/旋转变换，不伪造题区坐标。页组摘要只覆盖有序的ScanPage/File/Batch身份、原页码、文件和JPEG SHA；展示文件名和临时占用投影不参与摘要。返回前和桥接消费前均重算并比较保存摘要，拒绝摘要不一致。具体六个字段为`scan_page_id/file_id/batch_id/source_page_index/page_sha256/file_sha256`，使用现有`exams.digest`的排序键、紧凑JSON、UUID字符串和UTF-8 SHA-256；数组顺序不重排。

它不是长效许可。`bridge_implemented=true`仅表示现已实现独立考试Job桥接，不表示本次读取建立任务或永久授权。桥接必须在消费前和提交时重新验证权限、current version/名单、Paper状态及源文件SHA；只对完整identity_confirmed版本生成受保护来源，不降级legacy、不让旧Worker直接领取。扫描接收时的batch.roster_revision_id只作历史上下文，不能代替当前名单。

本轮没有创建Submission、ParsingTask、正式成绩或到考/缺考记录；未认领、未扫描、缺页、无身份、空答和零分互不替代。

## 验证入口

- [真实PG/S3专项](../../tests/integration/test_answer_scripts.py)：签名JWT、唯一占用、直接SQL历史保护、名单更正、身份冲突、撤权、审计回滚、未知COMMIT、源字节核验、退役与跨场次。
- [协议单元测试](../../tests/unit/test_answer_script_protocol.py)：严格输入、页预算、显式身份、来源顺序摘要。
- [真实Chromium](../../tests/integration/test_teacher_workbench_browser.py)：显式`RUN_WORKBENCH_E2E=1`，复用隔离IdP、真实API/PG/S3，混班PDF到两份同名学生答卷与版本化补页。默认CI不启动收费模型。

迁移 `b14a30f1d902` 是增量；有任何新脚本/审计/角色Grant时拒绝破坏性降级。生产回滚使用保留事实的向前修复，不能清空业务数据以便downgrade。
