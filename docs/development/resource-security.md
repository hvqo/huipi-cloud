# 资源安全基础：受控部署操作

先备份并评审数据库迁移。应用、Worker 和 CLI 必须同时升级，禁止继续运行不了解分类字段的旧程序。只在明确获准的环境运行 `uv run alembic upgrade head`；自动验收用独立测试库，不升级用户开发库。

## 初始化安全操作员

旧 `User.is_admin` 不自动获得新权限。部署操作员需持数据库凭据、记录独立审批号，并为一个已登记且启用的数据库管理员执行一次：

```bash
uv run python -m huipi_cloud.modules.resource_security bootstrap \
  --user-id <已登记用户UUID> --request-id <本次请求UUID> \
  --approval-ref <部署审批编号> --confirm-deployment-bootstrap
```

这只是授权基础的受控初始化，不开放公共注册或管理员自助提权。整套部署只允许一次 bootstrap；同一请求可幂等重放。后续权限由有完整委派权限的可信主体 grant/revoke，不能再次 bootstrap 绕过撤权。若全部安全操作员丢失，应走备份/审批恢复流程，不恢复旧管理员考试访问捷径。

## 固定标识与命令

- 安装 Scope：`11111111-b11a-4000-8000-000000000001`，只指向数据库技术根。
- 安全操作员角色版本：`11111111-b11a-4000-8000-000000000002`。
- 载体创建者角色版本：`11111111-b11a-4000-8000-000000000003`。
- 分类审计员角色版本：`11111111-b11a-4000-8000-000000000004`。

非 bootstrap 命令都需要 `--token-stdin`：从标准输入安全传入真实 Bearer Token，不写进命令行参数、Git、Shell 历史或日志。用户 ID 不替代登录凭据。OIDC 签名/Issuer/Audience/有效期及数据库身份仍由现有验证器检查。

```text
grant --token-stdin --user-id UUID --role-version-id UUID --scope-id UUID --request-id UUID
revoke --token-stdin --grant-id UUID --request-id UUID
create-protected --token-stdin --payload 本地JSON文件 --request-id UUID
inspect-classification --token-stdin --assignment-id UUID --scope-id UUID
audit --token-stdin
```

以上均追加在 `uv run python -m huipi_cloud.modules.resource_security` 后。Token 每次由受控进程管道输入，不把 Token 放进上述模板。payload 使用已有 AssignmentCreate 字段（title、subject、grade_level、可选 description），最大 8KiB；受保护分类由服务决定。输出仅 ID/状态；audit 最多 50 条最近事件，包含操作者内部 ID，不包含教学正文或存储定位。

当前受保护载体**没有旧 HTTP 读写入口**。任何新 Grant 都不能使旧作业接口、文件下载或 Copilot 读取考试内容。不要用它导入真实考试数据；新考试实体、范围、组织和冻结流程尚未完成。Assignment Scope 的受控登记和岗位管理 UI 也未开放；未来须依真实外键建立，不能提交自由 JSON。

## 验收与回滚

使用独立 `TEST_DATABASE_URL`（名称以 `_test` 结尾，与 DATABASE_URL 分离），以及独立 S3 测试桶。运行 `uv run pytest -q tests/integration/test_resource_security.py` 会创建并删除自己命名的额外迁移测试库，因此测试数据库账号需要测试环境的 CREATEDB 权限；生产应用账号不应拥有该权限。禁止对开发库执行这些 TRUNCATE 或迁移降级步骤。

有新安全数据时 downgrade 会主动拒绝。生产回滚须保留安全分类及新授权表，评估向前修复或恢复整库备份；不得删掉分类字段让保护资源重新按 legacy 暴露。

完整机制与调用清单见[安全隔离审计](../security/exam-resource-security.md)。
