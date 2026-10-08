# 架构与模块边界

慧批云端采用 src 布局的模块化单体。当前实现包含教师侧作业管理和 PostgreSQL 持久化；不连接 MongoDB、MinIO、消息队列或模型服务。

## 分层职责

- api/v1：HTTP 路由、请求与响应边界。应用入口注册领域错误和数据库异常到安全、稳定的 HTTP 响应。
- core：应用配置、日志和跨模块基础能力；不包含教学领域规则。
- modules/assignments：作业、题目、人工标准答案、评分细则和草稿发布规则。
- modules/parsing、grading、copilot、review：后续领域位置，当前没有实际业务代码。
- infrastructure/database：Async Engine、请求级 AsyncSession 和 SQLAlchemy Base。
- infrastructure/storage、llm、messaging、retrieval：后续适配器位置，当前没有实现。
- workers：后续异步任务入口，当前没有队列或后台执行代码。
- migrations：Alembic 数据库版本管理。FastAPI 启动时不创建表。

## 核心数据关系

一个 Assignment 有多道 Question；一道 Question 最多有一个 AnswerKey，并可以有多条 RubricCriterion。删除作业时，题目、答案和评分细则通过外键级联删除。

题号在同一作业内唯一。每道题的答案键唯一。评分细则顺序在同一道题内唯一。作业只有在至少包含一道题、每道题都有答案与评分细则、且该题评分细则总分等于满分时才能发布。

## 字段与约束选择

- 主键使用 UUID，便于 API 和后续对象存储、任务消息引用同一记录；UUID 不代表调用方已获授权。
- 作业标题、学科、年级为必填字段，说明文字可选，避免创建草稿时要求教师先写完全部说明。
- 题型使用当前支持的有限枚举，并有数据库检查约束；新增题型需要同时更新 Pydantic 校验与迁移约束。
- 标准答案保存 source。当前教师接口固定写入 teacher，数据库允许 model_generated 作为未来来源；本轮没有模型生成路径。
- 分值使用 Numeric(8, 2) 与 Decimal，限制两位小数。评分项总分与题目满分的跨行关系由发布事务检查。
- 时间列使用带时区类型；应用侧默认值来自 UTC。RubricCriterion 暂无时间戳，因为当前接口整体替换规则且没有版本历史。

## API 与事务边界

统一 API 前缀为 /api/v1。Router 负责输入输出与状态码；Service 执行业务规则并定义事务；Repository 封装本模块所需的 SQLAlchemy 查询；ORM 模型声明外键、唯一约束和检查约束。

每个写操作使用一个数据库事务。修改题目内容前先对所属 Assignment 行加锁；同一作业的题目、答案、评分细则修改及发布按作业行串行化。数据库约束作为业务校验之外的最终完整性边界。评分细则 PUT 是整体替换，多个并发修改按数据库锁释放顺序串行执行，后完成的替换生效；当前没有客户端版本号或冲突合并。

读取作业详情通过显式关系预加载，避免 AsyncSession 序列化时触发隐式懒加载。所有时间列使用带时区的时间类型，应用生成时间使用 UTC。

AsyncSession 工厂可以是进程级共享对象，但每次调用依赖都会创建并关闭一个新的 Session；Session 本身不会跨请求共享。写操作在事务内构造好响应对象，并在事务提交后才返回，避免提交后再读到其他并发请求的新值。PostgreSQL 的 `SELECT FOR UPDATE` 保护同一作业的状态转换和编辑。

跨行规则由 Service 校验：例如 Rubric 总分必须等于题目满分。数据库约束负责外键、唯一值、单列范围和非空等可由数据库直接保证的条件。SQLAlchemy 数据库异常不会原样返回客户端：已知唯一约束映射为 409，数据库不可用映射为 503，其他数据库错误返回通用 500 响应。写事务由 `session.begin()` 在异常时回滚，请求结束时 Session 关闭。

`GET /api/v1/health` 当前是存活探针，不查询 PostgreSQL；它只能说明应用进程能够响应请求，不能证明数据库已就绪。

## 数据库与配置

数据库连接串从 DATABASE_URL 环境变量读取。Docker Compose 只定义本地 PostgreSQL 服务，绑定到回环地址；应用数据库与名称以 _test 结尾的测试数据库分开。测试不得将 TEST_DATABASE_URL 指向开发数据库。

ORM 类型和迁移脚本是两份需要检查的一致性定义。开发者通过 Alembic upgrade head 应用迁移，通过 Alembic check 检查模型是否存在未迁移变更。不会使用 SQLAlchemy create_all 自动建表。GitHub Actions 在 Pull Request 和 main 推送时运行 Ruff、PostgreSQL 迁移、Alembic 一致性检查和 pytest。

## 安全边界与限制

本轮没有登录、RBAC、教师归属字段或租户隔离。作业 API 仅供本地开发和受控环境验收，不能作为生产安全接口。后续需先设计身份、资源归属和授权，再对外提供教师作业管理能力。
