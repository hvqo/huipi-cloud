# 试卷管理工作台验收

2026-10-26，基线 main `7b91fec14ec08aac91f53f04860d551b324bdcc7`。
仅合成业务数据、独立 PostgreSQL 16、私有测试 S3 与真实 FastAPI/Chromium。
没有新的模型调用、权重下载、开发数据库迁移或正式成绩发布。

## PR #56 前置门禁

指定 HEAD `b1bd333fc29a428c4fc639fb681142ba3f5b162c` 与实际一致，Fast CI 成功，无未解决审查线程。
实际复核19个固定HTTP动作、User/Grant/真实Scope、原Service幂等及审计、日期名单及旧资源隔离，结论 APPROVE。
独立补测撤权重放/范围过滤、审计与有界请求、paper.use元数据不泄漏答案，真实 PG/S3 3项通过（12.00秒）。
随后Squash Merge为本页基线，合并后 [main Fast CI](https://github.com/hvqo/huipi-cloud/actions/runs/40271357711) 成功。

## 业务和安全实现

- `/papers`、`/papers/{id}`，与 `/exams` 双向链接；10个固定HTTP操作、9个新增路径。
- 沿用原PaperFamily、PaperVersion、Mapping、Question、AnswerKey、Rubric和数据库冻结保护。
  无新增模型或迁移，唯一Head `b15a60c4f015`，26个迁移、65张映射表。
- 列表与每个历史版本都需paper.read；paper.use不能读正文、答案或Rubric。
- 来源查询独立于旧管理员全局作业读取语义，只返回明确成员授权、已发布legacy作业的名称摘要。
  网页创建在原事务内再次验证paper.create、成员/来源状态和内容完整性；不接受客户端关闭检查。
- 原create_snapshot CLI保留；固定create_member_snapshot只收紧网页导入，后续仍复用原快照事务、摘要、审计和原子保护载体创建。
- 编辑/重排/锁定/fork/退役带原request_id、revision和双摘要，调用原Service；不裸SQL写题库。
- 创建和fork不新增Grant；新版本无读取权时显示需要授权。没有考试、名单或成绩权限扩张。
- 比较合并基线OpenAPI：旧路径与旧Schema全部不变；新增响应投影独立命名，Decimal输出为字符串。
  保护载体及旧题目UUID不进入网页详情合同，question_key保持稳定。

## 真实数据库测试

相关回归一次运行 **53项通过（182.40秒）**，包含新网页首批6项、既有试卷、考试配置与考试场次测试。
其中保留真实SQL/ORM冻结写入拒绝、来源与锁卷并发、撤权、审计回滚、COMMIT未知、历史提交/评分保全、
独立数据库升级/check/降级/再升级及有业务事实时拒绝破坏性降级。

最终新增网页专项 **8项全部通过（24.47秒）**，其中6项与上述53项重叠，合计55个不同用例：

1. 真实HTTP导入/幂等、编辑答案与Rubric、重排、锁定、HTTP和直接SQL拒绝锁后改写、fork旧版不变、旧HTTP隔离。
2. 原管理员旧接口可以读源作业，但无明确成员不能网页导入；未发布来源拒绝且无半成品。
3. paper.use及伪造JWT角色不能读取答案、历史、来源或其他试卷。
4. 仅编辑权限不能锁卷，撤权后新请求和原request_id重放均拒绝。
5. Decimal/Rubric不等、重复Mapping拒绝；两个并发编辑只提交一个revision。
6. 实际数据库审计故障整体回滚；真实commit完成后注入确认丢失，HTTP503；相同request_id恢复唯一版本。
7. 网页退役保留已绑定SessionPaper的全部历史字段和题目内容，不删除或替换场次用卷事实。
8. 历史逐版授权过滤，前版只读用户看不到未授权后继；fork前后Grant集合不变。

初次新测试的失败属于测试部署错误：教师账号管理HTTP刻意不允许将管理员当普通教师登记成员；
改为明确的测试部署成员事实后再验收，没有放松生产授权。另修正测试中先查询后未结束事务就调用原Service、
以及未具备完整委派能力的测试主体。失败日志保留，没有删断言或放宽权限。

## Chromium实际流程与截图

真实TLS测试OIDC、PKCE、签名用户、当前数据库Grant、实际FastAPI和私有S3。
测试部署显式授予合成命题教师根操作角色，以验证创建新版本后继续操作；这不是生产自动授予权限。
默认浏览器解析执行器仍为明确的Synthetic执行器，不把它当真实MinerU或DeepSeek验收。

最终Python驱动1项通过（48.45秒），其内部Chromium 4个场景通过（40.9秒）：

- 原Unicode作答证据、提交未知重放、并发冲突和只读权限。
- 原图片预览及作业表单。
- 实际混班PDF接收、逐页预览、私有原件下载及SHA校验。
- **完整新流程**：从明确授权来源创建试卷草稿 → 编辑题干/标准答案/Rubric → 调整题序 →
  锁定 → fork并锁定后继 → 创建考试及A/B场次 → 选择刚生成的两个锁定版本 →
  两班四人名单（两人同名） → 显式考号/卷别 → 保存/冻结及后继更正 → 扫描工作台。
  同时检查配置不授予班级成绩权限。

首次浏览器运行碰到新组件的模板编译错误，3个旧场景失败；修正模板事件处理后，完整4场景串行复跑通过。
Vite生产构建单独执行，未以仅TypeScript通过替代构建验收。没有改变原功能断言。
本轮还补充同组件路由切换/离开时的未保存提示、结果未知的原正文重试和只读禁用状态。

截图为1440像素宽的合成数据全页图，已实际查看：

- [草稿编辑与题序](assets/paper-workbench/paper-workbench-draft.png)
- [锁定后只读](assets/paper-workbench/paper-workbench-locked.png)
- [后继版和家族历史](assets/paper-workbench/paper-workbench-successor.png)

## 静态、合同和保护检查

- 锁定依赖安装、Ruff、705项后端纯单测通过。
- TypeScript、ESLint、64项Vitest、Vite生产构建通过。
- OpenAPI导出、生成TypeScript、静态迁移结构、Compose、文档链接及git diff --check通过。
- 用户.env及冻结资料逐文件SHA、其他分支及stash对照检查；没有更改开发数据库或既有迁移。
- 详细本地证据：`~/.cache/huipi-cloud/paper-workbench-20261026/`，凭据文件不可公开或提交。

## 限制及后续

这是结构完整性与授权验收，不证明标准答案、Rubric教学语义或实际评分准确率。
没有试卷PDF/排版、草稿增删题、公开授权后台或正式成绩审批发布。
首次身份/Grant、明确来源成员、组织及学生维护、卷版权限与成绩班级授权仍依赖受控工具。
退役影响后续使用时必须重新核对，不能把历史关联保留理解为仍可继续解析或评分。
已有真实MinerU/DeepSeek链路沿用[冻结证据](production-quote-selection.md)，本轮不重复收费请求。
