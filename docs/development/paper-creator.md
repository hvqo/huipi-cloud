# 普通教师安全建卷

## 角色与审批

迁移 `b15b71d5f016` 新增两个不可改写的 sealed Role v1，保留全部旧角色版本：

- `paper_creator`：`11111111-b15b-4000-8000-000000000001`，只能绑定 installation Scope，只有 `paper.create`。
- `paper_author`：`11111111-b15b-4000-8000-000000000002`，只绑定一个真实 PaperVersion Scope，只有 `paper.read/edit/lock`。
  这是保留的业务创建角色，不允许通过通用 `grant` 接口委派，不能绑定 installation、场次或其他人的卷版。

初始建卷角色仍须经过可信管理员审批，由已有 JWT 安全 CLI 发放：

```text
uv run python -m huipi_cloud.modules.resource_security grant --token-stdin \
  --request-id <审批请求UUID> --user-id <教师User UUID> \
  --role-version-id 11111111-b15b-4000-8000-000000000001 \
  --scope-id 11111111-b11a-4000-8000-000000000001
```

Token 通过 stdin 安全输入，不放入参数、日志或文档。操作者必须是当前可信 Principal，且**同一条有效 Grant**覆盖
`security.grants.manage`、`paper.create` 和 installation；只有管理动作不能委派自己没有的能力。
可使用已独立审批的部署操作员完成，不把 `paper_operator` 授予普通教师，也不以旧 admin 标志/JWT role 代替审批。
普通教师不能管理自己的 Grant，没有新增公开注册、提权或授权 HTTP。

## 新对象创建政策 v1

网页入口保持 `/papers`，HTTP/TypeScript合同不变。
来源列表和实际创建事务仍要求当前有效、明确的 AssignmentMembership、已发布普通作业、完整题目/答案/Rubric，
以及独立安装级 `paper.create`。不借旧管理员全局可见性导入。

`paper-creator-v1` 是固定的领域创建授权，不是通用自委派：

1. 服务端创建真正的新卷版、独立 exam_protected 载体及真实FK的 Scope。
2. 仅为本次操作者和本次新卷版创建一条 `paper_author` 对象 Grant。
3. `paper_creator_authorities` 用真实外键连接卷版、Grant、操作者和 PaperOperation；审计及授权与内容在同一事务。
4. 数据库约束检查新卷创建时间、初始状态、创建人、Scope、角色、操作动作和操作回执的卷版ID；
   延迟到 COMMIT 验证完整关联。授权来源记录不可 UPDATE/DELETE。
5. 客户端不能指定接收人、角色、Grant或Scope。通用授权不能补发保留 author 角色；需要另行授权已有版本时，
   管理员使用原 reader/editor/locker 等受控角色，按原委派规则操作。

相同来源/标题的已有家族仍按原规则复用或拒绝，不为调用人追加 Grant；调用人先须有该既有版本的读取权。
旧 request_id 重放重新检查当前建卷、成员和卷版读取权限，只回放原最小结果，不重建授权、不恢复已撤销 Grant。
后继卷回执重放也重新核对后继卷当前读取权，父卷权限不能替代。
历史版本不回填对象授权。来源作业成员、其他用户Grant和旧角色内容不复制。

fork 需要**原版本的当前 `paper.edit` 加安装级 `paper.create`**；成功时只给真正新建的后继草稿建立同样的对象授权。
这增加了新版本创建的当前资格检查，不影响历史内容读取。子版本权限不从父版本继承，原版本和其他人员权限不变。
锁定、退役继续沿用原状态机和数据库内容不可变保护。`paper.edit` 目前也包含原有退役动作，未拆分新权限。

## 撤权、并发及故障

- 撤销 creator Grant：新建、创建重放及 fork 拒绝；已有对象 Grant 独立保留。若需完全收回，另撤具体对象 Grant。
- 撤销对象 Grant：该版本读取、编辑、锁定、fork及创建重放拒绝；不能靠另一个 request_id 恢复。
- 账号停用：沿用当前数据库 Principal 的拒绝规则，Token 的旧角色声明不提供旁路。
- 原主体/授权行共享锁、request_id事务锁及源/家族/版本锁保持原顺序；新版本创建在同事务使用操作者预算锁，
  序列化同一人不同来源的授权计数。持锁期间竞争撤权等待，提交后撤权生效；之后的新请求必拒绝。
- 保留现有最多100条有效Grant的限制；满额拒绝并回滚新内容。退役不会自动删除或撤销历史授权，
  大量建卷的角色/容量优化仍需后续独立设计。
- Scope、Grant、来源记录或审计任一步失败均整体回滚；提交回执丢失返回原 `paper_commit_outcome_unknown`，
  以同一 request_id 恢复，不重复授权。不调用S3或模型来分配权限。

数据库触发器约束可信应用的持久化一致性，不把持有任意SQL/表所有者权限的部署人员当作不可信教师。
教师没有SQL执行入口；数据库凭据必须只供可信服务和受控运维使用。

## 迁移与兼容

新迁移新增一张授权来源表、两个角色和约束，不修改旧应用迁移、User/Assignment/审核UUID或教学记录。
在独立数据库执行 upgrade/check/downgrade/re-upgrade，旧作业、内容、试卷、Grant和审计摘要保持一致。
没有新事实时可回退；存在任何新角色Grant（包括已撤销）或创建授权来源后，拒绝破坏性 downgrade。
生产故障需前向修复/受控备份恢复，不能为降级删除审计。

对象作者不包含 `paper.use`、考试配置、名单、扫描、批改、班级成绩/导出或 `security.grants.manage`。
已有场次选卷仍独立核验 `paper.use`；没有建立任何自动正式成绩发布路径。
初始身份、成员、角色批准及其他教师协作授权仍由受控安全流程完成；没有教师自助授权管理页面。

验收与截图见[最小建卷验收](../evaluation/paper-creator.md)。
