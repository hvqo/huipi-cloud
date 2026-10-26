# 多人单评阅卷验收

日期：2026-10-26。所有学生、账号、试卷、答卷均为合成数据；没有收费模型调用，没有下载模型，也没有连接或迁移用户开发数据库。

## 基线收尾

PR #59 的 HEAD 为 `b201a911ce509e38daae29c233aaad8b84cd7c12`，最终 Fast CI 通过，审查线程为空。检查学校组织授权、CSV预检/确认和现有验收后，未发现核心阻塞，按授权 Squash Merge 为 `e45552b6299ffcaa89576735324fb7f55a0f109a`。合并后 main Fast CI `40332207632` 成功。多人阅卷使用该 main 为基线，独立 Draft PR，不合并其他视觉或历史功能分支。

## 实际验证

- 后端单元：718 passed。
- 新任务真实 PostgreSQL/私有 S3 专项：12 passed，包括完整领取/作答确认/原评分Worker/GradeReview/任务提交与成绩门禁、并发领取与分配、重派后旧审核失效、撤权重放、直接SQL历史保护、审计回滚、COMMIT确认丢失恢复、退役来源、跨任务request_id冲突、请求体上限及非法UUID。
- 其中两项暂停 Synthetic Provider 的实际异步执行，在模型等待期间重派任务或撤销被分配人的 Grant；迟到响应均不能登记 Proposal，任务实际变为 invalidated，不是假装模型失败为零分。
- 相关任务、旧考试评分、成绩预览及迁移历史组：66 passed / 2 skipped，耗时627.81秒；含9项较早任务专项，其后完整12项任务专项又单独通过。两项真实模型 opt-in 未开启，不计为通过。真实库隔离，不以Mock或SQLite代替。
- 前端 Vitest：79 passed / 15 files；npm ci、TypeScript、ESLint、Vite生产构建、OpenAPI导出/生成、Ruff及静态迁移结构检查通过。
- 实际 Chromium：4 passed / 3 skipped。包含原作答证据/未知提交恢复、原文件预览、混班扫描/分卷/受保护解析及新增多人阅卷。跳过另行 opt-in 的考试配置、建卷、学校管理扩展，不计入通过。
- Alembic 唯一 Head `b16a82e6a017`。独立数据库保留真实旧业务种子，upgrade/check/downgrade/re-upgrade 比较历史表与全部原角色摘要；新增任务/操作/岗位授权事实阻止破坏性降级。既有迁移文件没有修改。

## 浏览器真实业务路径

使用本机独立 PostgreSQL 16、私有 S3、真实 FastAPI、Vue、签名 OIDC + PKCE 和 Chromium。测试服务使用独立数据库，测试 IdP 不进入生产路径。

1. 两个行政班、四名合成学生，其中两人同名但学号和班级不同；同一考试场次及共享锁定试卷、当前冻结名单。
2. 原扫描场景实际上传混排多页 PDF、人工组成两份答卷并确认身份，受保护解析生成 Canonical；解析和评分使用明确的 Synthetic 执行器/Provider，仅验证已存在合同，不宣称为本轮真实模型验收。
3. 负责人进入「阅卷管理」，将一班同名学生整卷两题分配给教师A；将二班另一位同名学生的第一题分配给教师B。两位教师只有本场次 task.work，没有全名单、成绩、试卷或混班PDF通用读取权限。
4. 分配后旧审核不再自动产生完整成绩。两个独立浏览器上下文登录教师A/B，分别查看个人任务、领取、在原批改组件核对原页和 AI 建议、保存本人 accepted GradeReview，并明确提交任务。教师A连续进入下一题。
5. 负责人进度为教师A完成两题、教师B完成一题；浏览器之后直接查询真实数据库，核对3个 current completed 任务和两位教师的实际 GradeReview reviewer_user_id。
6. 成绩预览中第一位学生为2/2、20.00完整总分；第二位为1/2、10.00小计，完整总分仍未完成。另两人没有确认答卷，不推断缺考或零分。没有成绩发布。

## 证据及截图

仓库外本机目录：`/home/amber/.cache/huipi-cloud/marking-tasks-20261026/`。

- `tasks-delivery.log`：12项最终任务 PG/S3 专项。
- `related-final.log`：旧考试评分/成绩、任务及真实迁移回归。
- `unit-delivery.log`、`frontend-delivery.log`、`build-delivery.log`。
- `browser-final.log`、`chromium-final.txt`：真实浏览器宿主测试及四场景终态。
- `screenshots/marking-management.png`：跨班派单。
- `screenshots/marking-marker_a-review.png`、`marking-marker_b-review.png`：两位教师原页、AI建议与人工复核。
- `screenshots/marking-progress.png`：负责人当前任务进度。
- `screenshots/marking-results-preview.png`：完整总分与缺评小计分开。
- `preservation-final.json`：129项受保护文件摘要（包括 `.env` 与评测资料）、26个原有非main分支和stash保持不变。

截图已实际打开检查，沿用原有工作台布局，没有全站视觉重构。初次测试发现的浏览器链接可访问名称、测试侧受保护ORM投影和迁移新增角色比较范围已修正后重测；没有删除原授权或历史数据断言。原始失败日志保留在同目录。

## 部署与残余边界

新岗位不自动回填给旧管理员或普通教师；首次审批与岗位 Scope 分配仍是受控部署流程。当前账号没有持久化可信教师姓名，UI明示账号编号。首次任务管理不提供全量派单历史浏览、跨页一键派全场、自动均衡、双评、仲裁、正式成绩发布或全站新视觉。

进度的“已完成”指任务提交记录；数据库版本或授权异常会单独提示，成绩预览继续独立校验全部来源及分数证据。统计不承诺为全场次每份S3原件持续做字节监控。原真实 MinerU/DeepSeek 及 Quote Selection 验收沿用既有冻结证据，本轮不重新评估模型教学准确率。
