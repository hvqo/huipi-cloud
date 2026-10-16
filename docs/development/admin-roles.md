# 角色与管理员接口开发指南

前端数据合同、能力矩阵、全部管理接口和错误码见[教师与管理员合同](../product/teacher-admin-contract.md)。本文说明实现和部署边界。

## 身份与数据

- `User` 沿用 id、issuer、subject、is_active、is_admin；`AssignmentMembership` 沿用唯一 user/assignment、can_write、revoked_at。没有新增密码或客户端角色字段。
- `/me` 重新查询本地身份，role/capabilities 由数据库计算。Principal 合同不变，禁止模型或调用者伪造身份。
- 管理路由进入精确的固定策略表，并在业务短事务中再次检查启用管理员，以 SHARE 锁持有到提交。停用/降权先完成则操作拒绝；操作先取得锁则停用等待本次提交。不承诺取消已经开始的文件流。
- 管理员可登记的 Issuer 仅为配置的 AUTH_ISSUER。不会根据 JWT 自动创建账号，也不向外部 IdP 发请求。

## 首位管理员与高权限操作

只有持有部署数据库权限的可信操作员执行：

```bash
uv run python -m huipi_cloud.modules.identity provision --subject '<可信IdP的稳定sub>' --admin
```

该命令只在新建用户时设置管理员；重跑不会提升、重新启用或降低已存在用户。已有账号的管理员晋升、降权、管理员停用应走独立部署变更流程：双人批准（组织自行实施）、备份、按内部 UUID 精确修改数据库、核对 `/me` 及管理接口拒绝/允许行为，并保留部署审计。没有 HTTP 提权或管理员资料修改入口；不要把 SQL/部署凭据交给普通教师。

部署操作员 CLI 和数据库所有者能越过 HTTP 权限、触发器乃至修改表结构。新增审计覆盖应用的 HTTP 管理操作，**不是数据库所有者的防篡改日志**；部署操作仍需独立审计。当前不提供管理员晋升/撤销的 Web 工作流，也不伪称已完成这类高权限审批系统。

## 管理审计与事务

新增 `AdminOperation`：UUID、actor_user_id、target_user_id、可空 assignment_id、request_id、action、payload_sha256、布尔 state、UTC created_at。身份 UUID 是事件引用，刻意没有删除级联或外键：即使以后删除资源或账号，也应保留审计事实；HTTP 没有删除账号入口。有效 actor/target 在写事务中查库验证，不接受客户端 actor。

- 权限变更与审计 INSERT 同事务，审计失败则权限修改回滚。
- 唯一约束 `(actor_user_id, request_id)`；事务级 advisory 锁只串行同一操作者/请求。它是 PostgreSQL 本地事务锁，不是分布式锁服务。
- 请求摘要包含操作、目标和内容；不存储完整 OIDC subject 或请求正文。摘要不对外返回。
- PostgreSQL BEFORE UPDATE/DELETE 触发器拒绝审计改写。保留读取接口，禁用普通业务的修改/删除入口。数据库部署账号仍须按最小权限配置，TRUNCATE/DDL 不属于此触发器的保护范围。
- COMMIT 结果未知返回 503；没有外部对象补偿删除，原 request_id 可重新取得已提交事件。不能把一次异常解释为确定回滚。
- 新前端必须提供 request_id。旧成员授权调用省略时服务端产生 ID，保留兼容，但不能依赖相同请求 ID 恢复。
- 成功管理操作才有事务内审计事件；被拒绝的鉴权尝试需要部署层安全日志，不伪造成功变更记录。

## 独立迁移与验证

开发数据库不由本任务升级。操作员部署前应先在独立数据库检查迁移 `c72d9e14a510`，升级会新增审计，不修改 User、成员关系、作业或历史评分。

```bash
uv sync --locked
uv run python scripts/check_identity_migration.py
uv run pytest -q tests/integration/test_admin_role_http.py tests/integration/test_teacher_authorization.py
uv run ruff check .
uv run python scripts/export_frontend_openapi.py
npm run api:generate --prefix frontend
```

迁移脚本创建随机独立 `_test` 库，验证 upgrade/check、管理员审计迁移 downgrade/re-upgrade、历史 User/Assignment 保留，并验证整条身份迁移回滚；最后仅删除自身测试库。回滚新增迁移会删除审计表，实际部署应先导出留存，不把降级当作日常数据清理。

默认测试使用真实独立 PostgreSQL、私有 S3 和运行时签名 JWT/JWKS，不调用收费模型或真实身份服务。前端本轮只更新生成类型，沿用现有锁定依赖、类型、Lint、单测、构建及两个真实浏览器场景；没有管理员 Vue 界面或视觉调整。
