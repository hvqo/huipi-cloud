# P1-A 面试复盘：核心数据模型与作业管理

本轮实现教师侧作业草稿、题目、人工标准答案、评分细则、查询和发布检查。没有实现学生提交、文件上传、登录鉴权、教师权限、OCR、批改 Agent 或 Copilot。

以下口头回答按约 30–60 秒设计。代码路径和演示步骤指向当前仓库中的真实实现。

## 1. 本轮实际实现了什么？

### 简明解释（ASD-STE100 风格）

教师可以创建作业草稿。教师可以添加题目、答案和评分细则。系统可以读取作业详情。系统只在作业内容完整时发布作业。系统将数据写入 PostgreSQL，并通过 Alembic 管理表结构。

### 面试口头回答

本轮做的是教师侧作业管理的第一段闭环。我实现了作业草稿、题目、人工标准答案和 Rubric 的数据模型，也实现了创建、查询、编辑答案与评分细则、发布这些 API。发布时会检查每道题是否有标准答案、是否有评分细则，以及评分项总分是否等于题目满分。项目使用 PostgreSQL 和 Alembic。学生提交、上传文件、鉴权和 AI 批改都没有实现，所以目前的接口仅用于本地开发和受控验收。

### 真实代码

- API：src/huipi_cloud/modules/assignments/router.py
- 业务规则：src/huipi_cloud/modules/assignments/service.py
- 数据模型：src/huipi_cloud/modules/assignments/models.py
- 迁移：migrations/versions/14cbab704b41_作业核心模型与作业管理.py

### 现场演示

启动 PostgreSQL 和 API，在 /docs 中创建作业、添加题目、设置答案和 Rubric，最后调用发布接口。尝试发布空作业应得到 409。

## 2. 为什么选择 PostgreSQL？

### 简明解释（ASD-STE100 风格）

作业、题目、答案和评分项有明确关系。数据库可以检查外键、唯一值、必需字段和分值范围。PostgreSQL 在数据库中执行这些检查。一次事务可以一起保存多个相关改动。

### 面试口头回答

这个阶段的数据以结构化关系为主。一个作业包含多个题目，一道题有一个当前标准答案和多个评分项。PostgreSQL 可以用外键、唯一约束和检查约束保护这些关系，也支持事务和行级锁。因此我把它作为当前业务数据的主存储。未来解析结果可能是文档型数据，文件也可能放在对象存储，但这不需要现在把作业核心关系移出 PostgreSQL。

### 真实代码

- 约束与关系：src/huipi_cloud/modules/assignments/models.py
- 本地数据库：compose.yaml
- 数据库连接：src/huipi_cloud/infrastructure/database/session.py

### 现场演示

运行 docker compose exec postgres psql -U huipi_cloud -d huipi_cloud，然后执行 \d questions 和 \d rubric_criteria 查看外键、唯一约束与分值字段。

## 3. 为什么用 SQLAlchemy 2.x Async？

### 简明解释（ASD-STE100 风格）

FastAPI 路由使用 async 函数。SQLAlchemy Async 让数据库操作使用 await。每个请求取得自己的 AsyncSession。不同并发任务不能共享同一个 AsyncSession。

### 面试口头回答

API 路由是异步函数，PostgreSQL 驱动也使用 asyncpg。SQLAlchemy 2.x Async 让查询和事务都使用显式 await，同时保留 ORM 的类型映射和关系加载能力。项目为每个请求创建独立的 AsyncSession，并在请求结束时关闭它。读取作业详情时显式预加载关系，避免序列化过程中发生隐式懒加载。需要注意的是，AsyncSession 是有状态对象，不能让多个并发任务共同使用一个 Session。

### 真实代码

- Async Engine 与请求级 Session：src/huipi_cloud/infrastructure/database/session.py
- 异步查询与事务：src/huipi_cloud/modules/assignments/service.py
- 显式关系预加载：src/huipi_cloud/modules/assignments/repository.py

### 现场演示

在代码中查看 get_db_session，再查看 service.py 中的 async with session.begin()。运行 uv run pytest tests/integration/test_assignments_api.py 可观察真实 PostgreSQL 请求。

### 官方资料

- SQLAlchemy AsyncIO：https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html

## 4. 为什么需要 Alembic？

### 简明解释（ASD-STE100 风格）

应用模型描述当前代码需要的表结构。Alembic 保存结构变化的版本。开发和部署环境可以按相同顺序应用迁移。应用启动不会自行创建表。

### 面试口头回答

create_all 只能按当前模型创建缺少的表，不能完整描述已有环境如何从旧结构升级到新结构。Alembic 用版本迁移记录每一次 DDL 变化，因此开发、测试和后续部署可以从空库按相同版本升级，也可以审查迁移内容。本项目使用 SQLAlchemy Async 引擎连接数据库，再通过 Alembic 的同步迁移上下文执行迁移。应用启动路径没有 create_all。

### 真实代码

- 异步迁移环境：migrations/env.py
- 当前版本迁移：migrations/versions/14cbab704b41_作业核心模型与作业管理.py
- 配置：alembic.ini

### 现场演示

在一个空的专用 PostgreSQL 数据库上运行 uv run alembic upgrade head，然后运行 uv run alembic current 和 uv run alembic check。

### 官方资料

- Alembic AsyncIO Cookbook：https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic

## 5. 四类数据之间是什么关系？

### 简明解释（ASD-STE100 风格）

一个 Assignment 可以有多道 Question。每道 Question 有零个或一个 AnswerKey。每道 Question 可以有多条 RubricCriterion。发布时每道题都必须有答案和评分项。

### 面试口头回答

Assignment 是教师的一份作业。Question 通过 assignment_id 属于一个作业，题号在同一作业内唯一。AnswerKey 通过唯一 question_id 表示一道题当前唯一的一份标准答案。RubricCriterion 通过 question_id 表示一个评分维度，sort_order 在同一道题内唯一。AnswerKey 保存 source，当前 API 固定写入 teacher，数据库也为 model_generated 留出来源值。发布校验读取完整关系后，再将作业状态改为 published。

### 真实代码

- 外键、关系和唯一约束：src/huipi_cloud/modules/assignments/models.py
- 来源枚举：src/huipi_cloud/modules/assignments/enums.py
- 发布规则：src/huipi_cloud/modules/assignments/service.py 中的 publish_assignment

### 现场演示

创建完整作业后调用 GET /api/v1/assignments/{assignment_id}，检查返回的 questions、answer_key 和 rubric_criteria。

## 6. 为什么分值使用 Decimal？

### 简明解释（ASD-STE100 风格）

评分分值使用十进制定点数。PostgreSQL 使用 Numeric(8, 2)。API 限制最多两位小数。系统用 Decimal 比较评分项总分和题目满分。

### 面试口头回答

分值是业务数据。二进制浮点数不能精确表示所有十进制小数。若用 float 累加，可能出现非常小的舍入误差，进而影响满分比较。我在请求模型中使用 Decimal，在 PostgreSQL 中使用 Numeric(8, 2)，并将输入限制为最多两位小数。发布时以 Decimal 做精确求和，要求评分项总分严格等于题目满分。

### 真实代码

- Numeric 列：src/huipi_cloud/modules/assignments/models.py
- Decimal 请求校验：src/huipi_cloud/modules/assignments/schemas.py
- 精确总分检查：src/huipi_cloud/modules/assignments/service.py

### 现场演示

给满分 10.00 的题目设置 10.01 分评分项，应得到 422；设置总分 9.00 的 Rubric 后发布，应得到 409；总分为 10.00 时发布成功。

## 7. 如何处理事务、并发更新和一致性？

### 简明解释（ASD-STE100 风格）

每次写操作在一个数据库事务中运行。修改作业内容前锁定作业行。同一作业的写操作按顺序执行。外键和唯一约束阻止无效关系。

### 面试口头回答

Service 层使用 session.begin() 包住一次完整写操作。如果校验或 SQL 失败，事务会回滚。所有作业内容更新和发布都会先用 SELECT FOR UPDATE 锁定所属 Assignment 行。同一作业的新增题目、答案替换、Rubric 替换和发布因此会串行运行。唯一约束和检查约束仍由数据库兜底。当前并发 PUT 采用后完成的替换结果生效；API 没有版本号，所以还没有检测用户基于旧页面提交的陈旧修改。

### 真实代码

- 事务和行锁：src/huipi_cloud/modules/assignments/service.py
- 数据库约束：src/huipi_cloud/modules/assignments/models.py
- 请求级 Session：src/huipi_cloud/infrastructure/database/session.py

### 现场演示

运行 uv run pytest tests/integration/test_assignments_api.py，重点查看完整发布、缺答案发布和总分不一致发布测试。可在两个并发请求中更新同一作业的 Rubric，观察数据库串行化后的替换结果。

## 8. 当前设计有哪些限制？

### 简明解释（ASD-STE100 风格）

系统没有用户身份和教师权限。系统没有学生作答和文件管理。当前答案没有历史版本。并发替换采用最后写入生效。作业 API 只用于本地开发和受控验收。

### 面试口头回答

当前模型没有 teacher_id 或 tenant_id，API 也没有登录和 RBAC。因此系统无法判断调用者是否有权访问或修改某份作业，不能把这些接口当作生产安全接口。答案只有当前版本，没有修订历史或审核轨迹。异步任务、文件上传、学生提交和作业就绪探测也未实现。下一阶段需要先补身份与资源归属，再设计上传生命周期、任务状态和答案版本管理。

### 真实代码

- 当前路由无认证依赖：src/huipi_cloud/modules/assignments/router.py
- 当前核心字段：src/huipi_cloud/modules/assignments/models.py
- 未实现模块仅有包位置：src/huipi_cloud/infrastructure/storage/__init__.py、src/huipi_cloud/infrastructure/llm/__init__.py

### 现场演示

查看 /docs 中的作业接口。OpenAPI 未声明认证方案；运行 uv tree --depth 1 可确认本轮没有引入对象存储、队列或模型框架依赖。

## 9. 未来如何扩展 MongoDB、MinIO 和批改 Agent？

### 简明解释（ASD-STE100 风格）

PostgreSQL 继续保存作业关系和评分结果。MinIO 保存原始文件。MongoDB 可保存大型解析文档。批改 Agent 读取已发布的题目、答案和 Rubric。当前没有这些服务或 Agent。

### 面试口头回答

我会让 PostgreSQL 继续作为作业、题目、标准答案和评分规则的主数据源。MinIO 保存图片或 PDF 对象，PostgreSQL 保存对象键、大小和校验值等必要元数据。MongoDB 只在解析结果或审阅轨迹确实需要灵活文档结构时引入，并用 assignment_id 和 question_id 关联，而不重复保存核心关系。批改 Agent 后续从已发布的答案和 Rubric 读取输入，将任务状态、最终分数和审计引用写回持久层。跨服务流程需要消息幂等、重试和补偿；不能假设多个存储能共享一个数据库事务。

### 真实代码

- 当前 PostgreSQL 领域服务：src/huipi_cloud/modules/assignments/service.py
- 未来存储位置：src/huipi_cloud/infrastructure/storage/__init__.py
- 未来模型适配器位置：src/huipi_cloud/infrastructure/llm/__init__.py
- 未来阶段：docs/roadmap.md

### 现场演示

运行 uv tree --depth 1 查看实际依赖。MongoDB、MinIO 和模型框架不在依赖树中；storage 与 llm 目录目前没有适配器实现。

## 并发与一致性补充

### 10. AsyncSession 如何保证请求隔离？

#### 简明解释（ASD-STE100 风格）

应用共享 Session 工厂。每个请求从工厂取得一个新的 AsyncSession。请求结束时，应用关闭这个 Session。并发请求不共享同一个 Session。

#### 面试口头回答

`session_factory` 是进程级对象，它负责创建 Session。`get_db_session` 每次被请求依赖调用时，都会进入一次 `factory()` 上下文并创建独立的 AsyncSession。Service 收到该请求自己的 Session。依赖退出时，上下文关闭 Session。如果 Service 中的事务失败，`session.begin()` 会回滚；请求结束后不会把这个有状态对象留给下一个请求。并发任务如果需要并行数据库操作，也不能共同使用一个 AsyncSession。

#### 真实代码

- `src/huipi_cloud/infrastructure/database/session.py`：`session_factory`、`get_db_session`
- `src/huipi_cloud/modules/assignments/router.py`：`DbSession` 路由依赖

#### 现场演示

在 `get_db_session` 设置断点，并发请求两次查看得到的是不同 AsyncSession 实例；检查依赖退出时执行的上下文关闭。

### 11. PostgreSQL 事务如何处理发布与修改并发？

#### 简明解释（ASD-STE100 风格）

每次写操作使用一个事务。写操作先锁定所属作业行。相同作业的编辑和发布按顺序执行。拿到锁后，操作会重新读取作业状态。

#### 面试口头回答

创建题目、替换答案、替换 Rubric 和发布都会先在一个事务中锁定 Assignment 行。PostgreSQL 的 `SELECT FOR UPDATE` 会让同一作业的写操作等待锁。等待结束后，后一个操作再检查草稿状态，所以如果发布先提交，编辑会得到 409；如果编辑先提交，发布会基于编辑后的数据做完整性检查。锁的范围是单个作业行，不需要分布式锁。测试用 `asyncio.Barrier` 同步启动真实 PostgreSQL API 请求，并检查最终答案与成功操作一致。

#### 真实代码

- `src/huipi_cloud/modules/assignments/repository.py`：`get_assignment_for_update`
- `src/huipi_cloud/modules/assignments/service.py`：`add_question`、`replace_answer_key`、`replace_rubric`、`publish_assignment`
- `tests/integration/test_assignments_api.py`：`test_publish_and_answer_update_are_serialized`

#### 现场演示

运行 `uv run pytest -q tests/integration/test_assignments_api.py -k serialized`，检查发布和答案更新的并发结果。

### 12. 数据库约束与 Service 校验分别负责什么？

#### 简明解释（ASD-STE100 风格）

数据库约束保护每一行和表之间的关系。Service 校验需要读取多行或检查业务状态的规则。两层检查不能互相替代。

#### 面试口头回答

外键、作业内题号唯一、答案唯一、评分顺序唯一、分值正数等规则可以直接由 PostgreSQL 约束保证。请求 Schema 会尽早拒绝格式错误，Service 再检查当前业务状态和跨行规则。比如 Rubric 总分要和 Question 满分比较，这需要读取多个评分项和题目分值，不适合仅靠普通行级 CHECK 约束实现。Service 校验给出清楚的业务错误，数据库约束则防止其他写入路径或并发边界破坏数据。已知数据库约束异常也映射为安全的 409 或 422。

#### 真实代码

- `src/huipi_cloud/modules/assignments/models.py`：唯一和检查约束
- `src/huipi_cloud/modules/assignments/schemas.py`：字段、Decimal 和顺序校验
- `src/huipi_cloud/modules/assignments/service.py`：总分和发布规则
- `src/huipi_cloud/main.py`：数据库异常映射

#### 现场演示

用重复题号验证数据库唯一约束兜底；用空评分列表和重复顺序验证请求校验；用评分总分不等于满分的作业验证发布业务校验。

### 13. 为什么 Rubric 总分相等属于业务规则？

#### 简明解释（ASD-STE100 风格）

Rubric 有多行。它的总分要与另一张表中的题目满分比较。发布前必须执行这项规则。

#### 面试口头回答

评分项总分不是单个评分项的属性，而是同一道题的一组评分项之和，并且要和 Question 的 max_score 比较。普通 CHECK 约束只能可靠检查当前行，不能安全地聚合其他行并读取另一张表。因此 Service 在替换 Rubric 时拒绝超过题目满分的总分，并在发布事务中要求总分严格等于满分。这样草稿可以先保存部分评分细则，但不完整的评分方案不能发布。

#### 真实代码

- `src/huipi_cloud/modules/assignments/service.py`：`replace_rubric`、`publish_assignment`
- `src/huipi_cloud/modules/assignments/schemas.py`：`RubricReplace` 非空与排序约束

#### 现场演示

设置总分低于满分的评分细则可以保存为草稿，但发布会返回 409；总分高于满分则在替换时返回 422。

### 14. Rubric 整体替换失败时如何避免部分写入？

#### 简明解释（ASD-STE100 风格）

Service 在同一个数据库事务中删除旧评分项并写入新评分项。如果新数据写入失败，事务会回滚。旧评分项会保留。

#### 面试口头回答

Rubric PUT 定义为整体替换。Service 先取得作业行锁并验证新评分项，再在 `session.begin()` 事务里删除旧项、添加整组新项并 flush。如果任何插入触发数据库异常，事务上下文会回滚删除和已执行的插入，因此数据库不会只留下半组新规则。API 层将数据库异常转换为不暴露 SQL 或数据库消息的通用响应。集成测试用 PostgreSQL 触发器在插入中途注入失败，然后读取作业详情确认原评分项完整保留。

#### 真实代码

- `src/huipi_cloud/modules/assignments/service.py`：`replace_rubric`
- `src/huipi_cloud/main.py`：`database_error_handler`
- `tests/integration/test_assignments_api.py`：`test_failed_rubric_replacement_rolls_back_all_changes`

#### 现场演示

运行 `uv run pytest -q tests/integration/test_assignments_api.py -k rolls_back`。测试会建立短期 PostgreSQL 触发器，检查错误响应不泄露数据库消息，并确认旧 Rubric 仍然存在。
