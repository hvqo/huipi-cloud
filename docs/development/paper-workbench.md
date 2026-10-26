# 试卷管理工作台

## 网页入口与能力

`/papers`、`/papers/{PaperVersion ID}` 与 `/exams` 双向衔接。
复用 PaperFamily/PaperVersion/PaperQuestionMapping 和原 `papers.service`；
最小建卷角色与创建授权来源新增迁移见[普通教师建卷](paper-creator.md)。

有相应当前授权的人员可在网页：

1. 查看授权试卷、家族内可访问版本、题数、Decimal总分与完整性状态。
2. 从有明确成员读取授权的已发布 legacy Assignment 创建独立受保护草稿。
3. 编辑已有题目的正文、题型、满分、教师答案、完整 Rubric，调整显示题号和顺序。
4. 核对答案、Rubric总分、Mapping与摘要，显式锁定；锁定后只读。
5. 通过 fork 创建后继完整版本，或显式退役；不解锁、不覆盖历史。
6. 在考试配置中选择有 `paper.use` 的已锁版本；试卷权限不授予考试、名单、扫描或成绩权限。

当前不支持在卷版内新增或删除题目。应先在原作业维护完整题目并发布，再导入。
试卷PDF、排版、版式模板、图片导入、权限委派界面均不属于本切片。

## 身份、来源与授权

全部入口使用现有真实 OIDC/JWT 验证及当前数据库 Principal、Grant、PaperVersion Scope。
JWT角色、User.is_admin、旧AssignmentMembership不能替代试卷授权。

- 列表、详情与家族历史需要 `paper.read`；每个历史版本单独过滤，不能从可读前版推导后版可读。
- `paper.edit` 用于编辑、重排、fork及退役；`paper.lock` 用于锁定；创建和 fork 另外需要安装级 `paper.create`。
- `paper.use` 仅供考试选卷，不返回题干、标准答案、Rubric，也不允许进入试卷详情。
- 真正新建或 fork 的卷版在同事务按固定创建政策授予创建人本卷的 read/edit/lock；不复制来源成员、全局角色或其他Grant。
  已有版本复用、重放不补发授权，历史版本不回填；详见[创建政策及撤权](paper-creator.md)。
  提交后若权限已撤销，页面仍显示需要授权，不假装可编辑。

旧作业读取具有历史管理员全局访问语义，不能将其作为新网页导入的来源目录。
因此 `GET /papers/sources` 只列出当前用户**明确、未撤销的AssignmentMembership**关联的已发布普通作业，
且还要求独立 `paper.create`。只有摘要名称/学科/年级，不返回正文或答案。
原作业管理员即使能经旧接口读取某份作业，也必须具有明确成员关系才能经网页导入。

固定 `create_member_snapshot` 入口在**原快照事务内**重新锁定当前用户/授权、成员行与来源Assignment，
核对 published、legacy分类，再执行原内容完整性、预算、快照、Mapping及审计逻辑。
客户端无法提交参数关闭此检查。旧 `create_snapshot` CLI合同保持兼容；没有给旧管理员新增考试资源访问路径。

## 并发与失败恢复

所有编辑/重排/锁定/fork/退役正文带 request_id、expected_revision、expected_content_sha256、
expected_mapping_digest，资源ID来自已授权路由，不接受body替换目标ID。
原Service重新鉴权后处理幂等回执；已撤权主体不能回放读取旧结果。
数据库触发器、行锁、审计同事务及 COMMIT 未知恢复沿用原实现。

网页将题目与题序分开保存；存在本地未保存修改时，不能锁定或执行版本状态操作。
冲突后显示刷新提示，不自动覆盖；结果未知时保留原正文与request_id、锁定编辑并允许重试。
该重试意图仅驻留当前页面内存，刷新或关闭后应先查询现有版本；没有承诺跨会话恢复。
锁定前的绿色检查只证明结构/来源完整，不证明教学答案或评分语义正确。

## HTTP合同

统一前缀 `/api/v1/papers`，10个操作：

- `GET /`：有权读取的版本分页，最多20项；响应包含服务端计算的can_create。
- `GET /sources`：专用已授权来源分页，最多20项。
- `POST /`：创建成员来源快照，只返回版本ID、revision和status，不返回保护载体ID或答案。
- `GET /{paper_id}`：有界标准答案及题目完整详情，使用稳定question_key，不暴露旧Question/Assignment载体ID。
- `GET /{paper_id}/history`：同家族逐版本授权过滤分页，不展示无权版本的存在或内容。
- `POST /{paper_id}/questions`：完整替换一题及其答案/Rubric，复用QuestionEdit的Decimal总分校验。
- `POST /{paper_id}/order`：提交完整MappingOrder，复用原唯一键/显示题号/题目集合校验。
- `POST /{paper_id}/lock`、`/fork`、`/retire`：原生命周期和版本操作，不提供解锁或删除接口。

POST流式读取至多1MiB、10秒，操作120秒；每卷至多200题、每题100项Rubric、规范内容2MiB。
详情响应客户端有界3MiB；来源、版本和历史列表均有明确分页。
正文仅作为Vue文本/表单内容展示，不执行HTML或模型生成指令。

所有响应沿用私有no-store与nosniff；错误不返回标准答案、SQL、Token或内部对象路径。
HTTP固定目录及TypeScript合同同时更新。

## 仍需受控工具的操作

首次OIDC身份、最小 creator 角色、明确来源成员及班级成绩授权仍由现有受控安全流程完成。
不能用网页自声明role或actor替代这些授权。原CLI继续可用，见[试卷版本指南](paper-versions.md)。
普通教师使用最小 `paper_creator`；部署级 `paper_operator` 权限很广，不得授予普通教师。
真正新卷的对象授权由固定事务政策创建；已有版本的协作权限仍由可信管理员按原委派规则登记。
退役保留历史考试引用，但会阻止新用卷及要求后续消费者重新核对状态；不是删除或悄悄替换已用试卷。

验收、合成截图与局限见[工作台验收](../evaluation/paper-workbench.md)。
