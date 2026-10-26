# 普通教师安全建卷验收

2026-10-26；基线 main `f9c3e4aaa733c2c22380cf24806a37e554cbc55f`。
全为合成数据，隔离 PostgreSQL 16.15、私有测试 S3、真实 FastAPI/Chromium。
没有开发数据库迁移、收费模型调用、模型下载或正式成绩发布。

## 前置 PR #57 独立门禁

实际 HEAD 与指定 `7c06817c7a33c16927f550b83c0b01f2f0cd90d1` 一致；Fast CI 成功，无审查线程或冲突。
复核实际来源成员检查、创建时再次鉴权、逐版Scope、双摘要、旧入口隔离和退役历史。
独立补跑真实 PG/S3 的来源、对象撤权和审计故障3项通过（8.81秒），结论 APPROVE。
随后 Squash Merge 到本页基线；[合并后 main Fast CI](https://github.com/hvqo/huipi-cloud/actions/runs/40291748877) 成功。

## 新政策与真实角色证据

最小 creator installation Grant 只有 `paper.create`，无 security.grants.manage、全局paper读写或任何考试/学生/成绩动作。
创建者 author Grant 只有本卷 `paper.read/edit/lock`；初次角色审批由独立可信管理员通过原 grant_role 执行。
保留角色不允许通用委派或安装级绑定；政策、运维审批及撤权细节见[开发指南](../development/paper-creator.md)。

新建Scope、Grant、授权来源表、内容和PaperOperation在同事务。
唯一键及延迟检查将真实新卷、创建人、Grant和创建审计关联；没有历史权限回填。
已存在家族、幂等重放、并发失败者不能获得附加授权；fork要求当前建卷资格和原卷编辑权限。
创建角色撤销与对象撤销独立，不把前者误称为自动撤销已有对象；完全收回需撤销相应对象Grant。

## 数据库、并发与故障

专项 `test_paper_creator.py` 加新增迁移测试首轮 **14项通过（70.68秒）**，包括：

- 真实签名教师HTTP创建、编辑答案/Rubric、调序、锁定、fork；后台仅有1个creator和2个精确对象author Grant。
- 未授权来源和他人卷版、伪造JWT角色、管理Grant、自授或保留角色通用委派均拒绝。
- 实际考试、场次、名单、成绩/XLSX及paper.use目录访问独立拒绝/过滤，没有用不存在的UUID代替越权验证。
- 复用家族、相同/不同request_id、正文冲突及两个不同成员并发争用，不产生额外对象权限。
- 创建与撤权使用实际PG锁等待，先前授权操作提交后撤权，后续新请求拒绝。
- 创建者授权/来源登记/审计三个位置注入数据库故障，版本、载体、Grant和新增审计整体回滚。
- 实际commit成功后丢失确认，返回503，同request_id恢复同一Grant和版本；不通过重新创建恢复。
- 错误Scope/用户绑定、历史来源记录UPDATE/DELETE及错误审计结果均被数据库拒绝。
- 新迁移 upgrade/check/downgrade/re-upgrade；旧作业、成员、内容、试卷、Grant及审计摘要不变，无自动回填。
  新角色Grant/创建事实存在时拒绝破坏性降级；额外13.33秒复跑验证已撤销Grant仍然阻止降级，
  且所有既有RoleVersion/RolePermission摘要在往返后不变。

试卷、网页与新迁移组合回归 **39项通过（135.04秒）**，与14项专项部分重叠，不能相加作为不同用例数量。
保留锁卷直接SQL拒绝、内容来源不变、退役后SessionPaper历史保留和旧HTTP保护载体隔离断言。

交付前增加后继卷撤权的 fork 回执重放检查，最终创建/迁移及全部网页试卷用例一起 **23项通过（95.08秒）**。
父卷仍可读也不能绕过后继卷撤权；原锁定内容、父子版本及幂等记录保持不变。
相关安全/考试/名单/成绩/评分回归及最终修正记录见下方运行记录。

迁移测试适配仅发生在测试代码：

1. 旧表摘要集合明确排除新增授权表，防止降到旧基线后仍读取不存在的未来表。
2. 更早版本迁移的种子使用合法真实PG草稿内容及原FK构造，不在旧Schema上调用要求新角色/新表的创建API。
3. 评分/成绩旧迁移拒绝测试先证明新创建授权事实的降级门禁，再在隔离测试库清除该后来切片的测试授权元数据，
   独立证明原评分/成绩门禁及旧业务摘要不变。没有删除任何生产历史或放宽降级触发器。
4. 旧撤权测试同步撤销新增对象Grant，继续验证“当前全部相关权限撤销后拒绝”，不再误认为删除全局角色会隐式收回对象权限。

## 真实浏览器

TLS测试IdP完成OIDC Code+PKCE与真实签名验证；后端实际查当前User、Grant和成员，不用Mock业务响应。
新增独立 `creator` 账号，`is_admin=false`。它未被授予既有浏览器扫描/考试操作员角色，
仅经测试安全管理员审批一个最小 creator Grant，并通过原HTTP建立/发布自己的两题来源作业。

Chromium最终四项通过（25.8秒），考试配置扩展场景未启用，明确1项跳过。
原Unicode作答复核、图片预览、真实扫描上传/私有S3原件SHA及逐页预览三项保留；第四项为：

登录普通教师 → 来源目录仅本人已发布作业 → 创建草稿 → 编辑标准答案与Rubric → 调序 →
锁定后只读 → fork后继草稿并立即可编辑。

Python驱动最终1项通过（35.23秒）；浏览器结束后直接数据库核验该用户**只有3条Grant**：
1条installation creator、2条新卷paper_version author，以及相应两条创建授权来源。
没有考试、名单、扫描、成绩、导出或安全管理权限。管理员不参与教师日常编辑操作。

已实际查看的合成数据全页截图（1440像素宽）：

- [草稿编辑](assets/paper-creator/paper-creator-draft.png)
- [锁定只读](assets/paper-creator/paper-creator-locked.png)
- [后继草稿](assets/paper-creator/paper-creator-successor.png)

## 静态与兼容

- 锁定依赖、Ruff、后端纯单测 **714项通过**。
- TypeScript、ESLint、Vitest **64项**、Vite生产构建通过；OpenAPI生成TypeScript无差异。
- 唯一Alembic Head `b15b71d5f016`，27个迁移，66张映射表；仅新增迁移，不修改任何已应用迁移。
- 无新增HTTP/DTO、无Vue视觉改造、README未改；Compose和diff检查通过。
- `.env`与冻结资料129个保护文件SHA核对一致；其他分支及stash保留。

## 限制

初始身份、来源成员、建卷角色审批和其他协作人员授权仍用受控管理流程，未开发自助授权后台。
历史卷版不自动回填；旧版协作授权用原reader/editor/locker，不复用保留author角色。
有效Grant最多100条，满额新建整体拒绝；本轮没有扩张授权容量体系。
`paper.edit`仍包含原有退役动作；若未来需单独审批退役，必须另行版本化动作和角色。
单次合成浏览器验收不代表真实学校已部署、取得IdP账号或通过教学质量审核。

## 最终相关回归运行记录

`test_paper_creator.py`、`test_resource_security.py`、`test_exam_sessions.py`、
`test_exam_rosters.py`、`test_exam_configuration.py`、`test_exam_results.py`、`test_exam_grading.py`
首轮134个用例：127通过、5个旧迁移测试失败、2个真实模型测试按opt-in规则跳过（1111.09秒）。
5个失败均为后来切片的测试元数据/新降级门禁与旧迁移测试的种子或预期不兼容，没有改动生产安全检查。
调整上述测试夹具后，用独立真实PG逐项复跑：

- 旧安全迁移往返1项通过（14.58秒）。
- 旧场次、名单迁移往返2项通过（39.58秒）。
- 旧成绩迁移往返1项通过（20.94秒）。
- 原评分事实降级拒绝1项通过（9.80秒）。

因此这134个不同用例最终132项已验证通过、2项保持未执行；不是声称整组在修正后又完整重跑。
新增真实场次隔离用例另通过（8.05秒），并已包含最终14项专项复跑。
补强“失败后无保护载体/家族残留”和既有卷版不能再领保留授权的断言后，4项故障/非法授权用例再次复跑通过（12.21秒），也已包含最终23项运行。
原始失败日志保留在本机验收目录，未覆盖为成功。

运行日志：`/home/amber/.cache/huipi-cloud/paper-creator-20261026/`；
主要文件 `paper-final.log`、`creator-special-final.log`、`security-exam-regression.log`、
`legacy-migration-final.log`、`exam-migrations-final.log`、`results-migration-final.log`、
`grading-migration-final.log`、`creator-fault-final.log`、`creator-final.log`、
`creator-migration-final.log`、`creator-cli-final.log`、`browser-final.log`、`browser-summary.txt`。
目录仅存合成验收数据；容器凭据文件限本机读取，不随PR提交。


额外验证可信管理员签名JWT的原安全CLI审批：1项通过（3.74秒）。
同request_id不重复分配，普通教师即使JWT自声明admin仍不能自批。
未新增自助授权入口，测试管理员未参与教师日常题目编辑。


按最终测试ID去重，本轮相关PG/S3集合共167项，其中165项已由上述组合运行及补测验证，
2项真实模型opt-in测试未执行；不把部分重叠的测试轮次简单相加。
本机 `verification-collected.txt` 与 `verification-manifest.json` 保存去重清单及未执行项。
