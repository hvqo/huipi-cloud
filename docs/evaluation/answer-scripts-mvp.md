# 人工分卷 MVP 本地验收

日期：2026-10-22。全部使用合成数据，没有真实学生资料、收费模型调用或模型下载。用户开发库和`.env`未升级或覆盖。

## 环境和边界

- Python 3.12.3、Node 24.18.0，uv/npm锁定依赖。
- 独立PostgreSQL 16.14容器，私有PGSTY SILO S3兼容服务（release2026-09-16）；真实对象上传、下载与SHA校验。不是SQLite或Mock存储。
- 独立测试数据库名以`_test`结尾，应用数据库指向未连接的隔离名称；S3使用测试桶及每测试独立Key前缀。只清理这些测试生成的数据/对象，不删除开发桶或开发卷。
- 浏览器为真实Chromium，运行真实FastAPI与Vite，通过隔离TLS IdP的Code+PKCE登录和签名JWT访问实际数据库/对象存储。
- 默认Fast CI只运行纯单元、Ruff、静态迁移和必要前端合同；不把CI成功冒充PG/S3或浏览器验收。

## 执行与结果

- `uv sync --locked`、`uv run ruff check .`、`git diff --check`：通过。
- `uv run pytest -q tests/unit`：**644 passed，0 skipped**。
- 扫描、名单、考试、试卷安全、resource_security、教师授权、组织迁移、作答复核HTTP、评分审核HTTP及新分卷专项组合：**196 passed，0 skipped**，468.62秒。
- 随后补全文件名投影、将Manifest摘要限定为有序来源身份及SHA，并对最终代码重新运行 `tests/integration/test_answer_scripts.py`：**25 passed，0 skipped**，65.87秒。这25项与上述组合重叠，不相加宣称更多独立用例。
- `RUN_WORKBENCH_E2E=1 uv run pytest -q tests/integration/test_teacher_workbench_browser.py`：**1个pytest包装场景通过**，内部**3个Chromium场景通过**。包括原Unicode证据/幂等/冲突工作流、PNG/作业创建，以及完整扫描分卷流程。最终截图由该次运行产生。
- `npm ci --prefix frontend`、typecheck、lint、build、format:check：通过；Vitest **47 passed**。
- OpenAPI导出及TypeScript生成、固定路由权限目录单元检查通过。`docker compose config --quiet`通过，仅解析配置，不启动开发Compose。
- 新迁移`b14a30f1d902`：独立空库upgrade head、Alembic check、downgrade至`b13a21f0c901`、再次upgrade head通过；静态检查单一Head、23个迁移、58张映射表。已有业务图的迁移往返摘要保全继续由相关集成用例验证；有新答卷事实时downgrade拒绝并保持Head/答卷不变。没有执行用户开发库迁移。

首轮相关回归发现两处旧扫描迁移测试仍以旧Head及旧表集合为基准；更新测试的版本与新增表排除范围后重跑196项通过。没有改写历史迁移或放宽旧业务摘要断言。

## 浏览器实际流程

一个场次、两班四人，其中两人同名；一张共享锁卷及当前冻结名单。上传四页无二维码混班图像PDF：页面按N001第2页、N002第1页、N001第1页、N002第2页混排；另两名名单学生未扫描，不推断缺考。

1. 查看逐页预览并下载原件，逐字节及SHA一致。
2. 按3、1页组成第一份答卷，明确调序，保存草稿。
3. 搜索/选择第一位可信同名名单项，核对考号、班级和实际A卷，确认新版本。
4. 选择第2页创建第二份不完整答卷，按考号N002选择另一班同名学生并确认身份；第4页仍未认领，界面明确提示答卷不完整。
5. 给第二份答卷追加第4页，人工核对完整性后保存为v3草稿；v2确认及v1草稿仍在历史，不无条件沿用原身份。
6. 1366×768、1440×900、1920×1080均检查无横向溢出；核对截图中文、页组、身份、未认领状态和当前版本。截图只代表合成业务验收，不是教学识别准确率。

截图：

- [两位同名学生分别确认，仍有未认领页](assets/answer-scripts/two-students.png)
- [真实当前名单选择及卷别确认](assets/answer-scripts/identity-check.png)
- [1366宽度与版本化补页](assets/answer-scripts/workspace-1366.png)
- [1920宽度与版本化补页](assets/answer-scripts/workspace-1920.png)

## 关键负向证据

[分卷专项测试](../../tests/integration/test_answer_scripts.py)实际检查HTTP正文、数据库状态及对象字节：

- 同页并发创建只有一份当前答卷；两份草稿竞争同页确认只有一方成功，失败方原占用保留。页主键唯一与延迟集合约束共同实施。
- 同一Script并发更正只产生一个后继；改序、跨文件/批次补页、撤销全部保留历史。
- 数据库直接改写版本、删除历史页或删除占用导致不完整集合均被拒绝。
- 同名不同Student/Participation不混同；同一Participation不能被两份当前确认答卷占用。
- 旧is_admin、JWT伪造role、跨场次请求、两条不完整Grant拼接、撤权后重放都不能授权；只有分卷读取权限的投影隐藏学生/名单标识及敏感操作说明。
- 当前名单更正使旧决定不就绪；存储核验期间冻结更正名单或撤权，最终确认拒绝。
- A/B引用真实同场次关系。已知A不能直接改成B；名单更正为待核验后，操作员可明确确认B并留下A/B历史。跨场次卷别404，未知不默认A。
- S3故障、JPEG字节篡改、审计写入失败不产生虚假确认；失败前草稿与占用保留。故障通过适配器/事务注入测试，并保留真实S3正向链路，未把Fake存储当E2E。
- 真实COMMIT成功后模拟丢ACK，原request_id恢复唯一回执；不是“查不到就删除”的补偿逻辑。
- 原PDF字节、ScanPage全字段不因确认改变；未新增旧Submission或ParsingTask。完整性人工标为incomplete时Manifest拒绝，不生成缺页零分。

## 仍未实施

解析桥接、OCR/自动分卷、自动身份、OMR、到考/缺考、阅卷和成绩发布全部未实施。Manifest不是长效执行许可。原始扫描或不同ScanPage是否属于同一物理重复纸页，仍需人工辨别。

单份答卷最多100页、8文件、核验处理总量256MiB，资源限制见[开发指南](../development/exam-answer-scripts.md)。跨两份已确认答卷交换身份需显式更正，不能绕过当前Participation唯一约束。浏览器未知提交重试保留在当前页面内存，刷新后应先查看已有答卷状态，不宣称跨刷新持久化请求队列。

业务配置仍需有权操作员通过既有组织、试卷、场次、名单CLI完成；本次只新增分卷工作区，不宣称已提供完整考试配置前端。
