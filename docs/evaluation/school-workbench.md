# 学校基础数据工作台：合成验收记录

日期：2026-10-26。基线为 PR #58 合并后的 `862abd9ce6e6c3d13e8e14edb6ce38243e2cd988`。仅使用合成组织与学生、独立 PostgreSQL 16、私有 S3 和 TLS 测试 OIDC 的真实签名账号。没有使用用户开发数据库，没有调用 DeepSeek、下载模型或执行真实 MinerU。

## PR #58 收尾

实际审查 HEAD 与指定的 `f784cf89da229bc8027dc9066e9c9611eb26e3ac` 一致。手工审查生产代码、迁移和数据库关联，补跑创建竞争、数据库延迟约束和撤权后继版本专项：3 passed。未发现 P0/P1，转 Ready 后 Squash Merge。

合并提交 `862abd9ce6e6c3d13e8e14edb6ce38243e2cd988`；[main Fast CI 40305622461](https://github.com/hvqo/huipi-cloud/actions/runs/40305622461) 成功。`paper.edit` 包含退役能力仍为明确的现有边界。本工作台不改变该角色语义。

## 新增自动化与回归

- Ruff、锁定 uv/npm 依赖、TypeScript、ESLint、生产构建、静态迁移结构、OpenAPI/生成类型及 `git diff --check`。
- 后端单元：714 passed。
- 前端 Vitest：75 passed，14 个文件。新增学校 API 合同、当前权限、文件与确认分离、阻塞预检、未知结果原请求重试、409过期预检及安全错误码用例。
- 组织/导入组：65 passed，包括17项新工作台测试、原组织和 CSV 集成。真实签名 JWT、实际数据库读取及副作用断言；包括所有五类创建入口、精确三模板、非法文件、2MiB/5000行限制、同名/大小写/日期/班级冲突、分页、撤权、审计回滚和 COMMIT 确认丢失恢复。
- 考试配置：7 passed；复核原用卷、名单、授权、并发和错误回滚合同。
- 两个独立迁移往返测试分别通过：原组织迁移和最新建卷授权迁移。均执行升级、Alembic check、降级及再次升级，核对历史摘要和存在业务事实时的降级拒绝。当前唯一 Head 仍为 `b15b71d5f016`，没有新增/修改数据库迁移。

原组织路由测试保留所有旧断言，精确清单从11扩至19个资源操作（另有 `/me/school-access` 身份元数据）。原迁移测试的历史固定 Head 已陈旧，改为对当前单一迁移图 Head 作等值断言；历史数据摘要、约束、触发器及降级拒绝断言保持。

## 真实浏览器与数据库闭环

使用真实 FastAPI、Vue、Chromium 和隔离 PG/S3。测试 IdP 只服务本地测试，不进入生产路径。浏览器宿主测试通过；Chromium 4 passed、2 skipped。跳过的是独立的旧考试配置扩展场景与旧建卷场景，未将它们计为本次通过。

学校管理场景包含：

1. 通过网页创建学年、学期、入学届别、学科和两个行政班。
2. 下载后端生成的真实 CSV，逐字核对列头。上传四名学生，两名同名、不同学号和班级。
3. 预检显示有效行/警告/错误，尚未正式导入。确认请求实际提交数据库后模拟丢失响应，人工重试相同正文和 request_id，恢复同一结果。
4. 按姓名、班级及日期查询学生；从学校入口进入考试配置，新建考试和科目场次、绑定现有锁定试卷、选择两个真实班级、保存并冻结四人名单。学生卷别保留待核验，没有自动选择A卷。
5. 冻结后再正式导入第五名学生，重新进入考试场次，冻结名单仍为四人，没有自动追加。
6. 使用另一真实签名会话：仅有 `paper_creator` 的普通教师没有学校导航，直接访问 `/school` 不能读取学生或显示管理操作；只有预检权限的合成岗位不能正式确认。
7. 浏览器后独立 SQL 断言正式学生为五人、已确认导入批次为两个、当前冻结名单为四人；普通命题教师实际 Grant 只有 `paper_creator`。

原三项浏览器场景也执行通过：作答 Unicode 选区/人工复核/未知提交恢复，原件预览与创建作业，以及扫描上传/原件 SHA/逐页预览/分卷/受保护解析。解析使用明确的 Synthetic Executor，仅用于既有协议回归，不作为真实 MinerU 验收。

测试中遇到的 Vue 模板编译和分页/筛选问题已修复并重测。Chromium 对 no-store 响应正文的延迟读取曾导致测试侧 `Network.getResponseBody` 失败；扫描场景改为在 response 事件立即读取，学校字典 JSON 写入以真实 `route.fetch` 响应原样回填，均没有伪造业务结果或额外写入。上传的多页 PDF 仍由浏览器直接发送，未用 Mock 成功替代。

## 证据位置

本机受控目录：`/home/amber/.cache/huipi-cloud/school-workbench-20261026/`。

- `organization-final.log`、`exam-regression.log`、`migration-final.log`、`unit-verified.log`。
- `frontend-delivery.log`、`build-delivery.log`、`browser-delivery.log`、`chromium-verified.txt`。
- `screenshots/school-dictionaries.png`：基础组织。
- `screenshots/school-import-preview.png`：未正式导入的预检。
- `screenshots/school-import-confirmed.png`：确认结果。
- `screenshots/school-frozen-roster.png`：同一真实组织链路的冻结考试名单。

截图是合成测试结果，不是真实学校资料。日志仅留在仓库外；测试基础设施凭据另存受限文件，不进入提交。验收时129项受保护文件（包括 `.env` 与冻结评测资料）摘要、原有非 main 分支和 stash 均未变化。

## 尚未支持

组织读取仍为明确批准的安装级学校管理能力，不是普通任课教师的班级 Scope。初始角色审批、权限分配、转班及学生状态变更仍依赖受控部署/CLI。没有历史班级删除、覆盖冻结名单、学生自助注册、安全管理员自助分配、真实学校 IdP 部署或正式成绩发布。
