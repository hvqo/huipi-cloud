# 教师认证与作业授权复盘

本轮只实现资源服务器验证与作业成员权限。没有自建登录、学生身份、完整组织 RBAC、租户隔离或公开 Agent 服务。真实 IdP 部署验收仍需单独完成。

## 1. JWT 验签为什么不等于授权？

简洁解释：先验证签名、发行方、受众与时间。再通过稳定 subject 查询本地启用用户。最后检查当前作业成员记录。

30 秒回答：Token 只能证明某个可信发行方给这个身份签过名。这个教师能不能看一份作业，是我们自己的业务授权。我们不读取 Token 的 role 直接授予管理员，而是从数据库查 User 和 AssignmentMembership。撤销后下一次请求重新查库，旧 Token 不会保留旧权限。

2 分钟深入：Issuer 和 subject 联合唯一，避免多个发行方的同名用户碰撞；Audience 防止把给其他服务的 Token 拿来用。User 是操作员预先映射的，陌生但有效的 Token 不会自动注册。Principal 是服务器内部不可变合同。Question 和 Submission 的 URL ID 不能直接作为权限依据，必须向上找到所属 Assignment。不存在和无权统一 404，减少枚举。这里没有多租户角色继承，也不能把 Assignment 作用域称为完整组织权限系统。

追问：客户端发 teacher_id 呢？忽略其权威性。知道管理员的 UUID 呢？仍需验签后的 subject 映射，不能靠 UUID 登录。管理员降权呢？后续请求和授权工具会重新查数据库。历史作业呢？没有成员记录就默认拒绝普通教师。

代码：`infrastructure/auth/jwt.py:TokenVerifier.verify`；`modules/identity/service.py:resolve_principal/require_assignment`。验证：`test_real_signature_claims_and_no_roles_in_verified_identity`、`test_invalid_signature_and_signed_role_cannot_impersonate_existing_admin`。

## 2. JWKS 如何缓存和轮换？

简洁解释：只向配置的 HTTPS URL 取键。缓存有有效期。未知 kid 有刷新冷却。缓存过期且取键失败时拒绝访问。

30 秒回答：我们固定 RS256，不能让 Token 决定算法或公钥 URL。异步获取 JWKS 时限制时间、响应大小和键数；未知 kid 可以触发有冷却的刷新，所以新密钥可以进入缓存，又避免每个随机 kid 都发网络请求。缓存过期后网络故障返回 503，不继续信任无限期旧键。

2 分钟深入：HTTPX 不跟随重定向，也不读取环境代理。JWK 只接受签名用途的 RSA 键，最小 2048 位，按 kid 选择。验签仍用显式算法白名单，不能把公钥当 HMAC secret。刷新完成后整体替换缓存，移除旧键。正常默认 300 秒缓存意味着密钥撤销有传播窗口，未知 kid 最短 10 秒冷却也意味着轮换有延迟。生产应使用不同 kid 轮换和合理重叠发布；不是实时令牌撤销服务。

追问：JWKS 故障能临时免登录吗？不能。为什么 503 而不是说教师没权限？这是依赖不可用，不是授权判断结果。如何不依赖外网测试？生成临时 RSA 私钥签名，MockTransport 返回真实公钥 JWK，再执行真实密码学验证。

代码：`TokenVerifier._refresh/verify`。验证：`test_jwks_cache_rotation_unknown_kid_and_no_token_endpoint_influence`、`test_expired_cache_does_not_fall_back_to_stale_keys`。

## 3. 如何防止跨作业读取？

简洁解释：每个资源先解析父 Assignment。复合 URL 的父 Assignment 必须一致。SQL 查询按成员权限过滤列表后分页。

30 秒回答：我们统一保护所有业务 HTTP 路由。单独的 Question、Submission 不能只验它自己的 UUID，必须查出 Assignment，再核对当前教师成员记录。一个复合 URL 有两个来源时，还要证明它们属于同一作业，即使管理员也不能把不同作业的提交和题目拼起来。

2 分钟深入：请求依赖使用独立短会话，避免认证 SELECT 自动开启业务写会话事务。每阶段有超时，数据库不可用就拒绝。新作业和创建者成员在同一个事务写入，失败不会留下无主半成品。列表是在 SQL 层加成员 EXISTS 后再 LIMIT/OFFSET。文件端点在进入对象存储之前检查授权，没有只保护新摘要而留旧匿名下载。已开始的文件流与已通过检查的事务不能承诺中途被撤销，这个边界在指南中明确。

追问：为什么不缓存权限？本阶段优先保证撤销后的请求生效。新路由忘写策略呢？统一依赖拒绝未知方法/模板，矩阵测试检查 OpenAPI 清单。有没有行级安全？没有，当前靠服务端授权；数据库凭据仍只交给可信操作员。

代码：`identity/dependencies.py:authorize_request`、`assignments/repository.py:list_assignments`。验证：`test_cross_question_submission_and_same_caller_two_grants_cannot_mix_scopes`、`test_all_old_http_routes_require_token_including_every_private_download`、`test_owner_membership_and_assignment_creation_are_one_transaction`。

## 4. Agent 如何接收教师身份？

简洁解释：可信入口先验签，再创建固定 Assignment 的授权工具。模型只提供工具参数，不能提供身份。

30 秒回答：新增的授权工具工厂接收 Bearer Token，验证后生成 Principal，模型不知道密钥，也不能选择教师身份。每次工具运行前后重查当前成员权限，引用验证也再次检查。原来的 CLI 仍是持有数据库凭据的操作员入口，我们没有把它直接开放成聊天 HTTP 服务。

2 分钟深入：`--token-stdin` 避免把 Token 放到 argv。检索模型预加载前先验证身份，工具结果返回前再查授权，降低长 I/O 期间撤销后仍释放结果的风险。底层 SQL 工具仍执行既有 READ ONLY 事务与固定工具注册表；模型说执行 SQL 或换 Assignment 不会增加权限。授权故障转成安全错误。评分候选和人工决定仍与正式成绩区分，没有修改评分合同，也没有切换 RAG 默认模式。

追问：可以让客户端直接反序列化 Principal 吗？不能，它必须来自可信服务入口。操作员 CLI 为什么还能读其他作业？它属于部署管理面，持有数据库凭据；必须隔离，不能给不可信教师执行。是否保证并发撤销的所有在途操作停止？不保证，当前是明确的请求边界与工具前后检查。

代码：`copilot/authorized.py:AuthorizedBusinessTools`、`copilot/__main__.py:run`。验证：`test_trusted_principal_agent_scope_rechecks_revocation_before_tool`、`test_user_disable_admin_demotion_and_token_expiry_apply_to_agent`。

## 5. 如何安全升级已有数据？

简洁解释：只新增身份和成员表，不自动给历史作业授权。使用随机独立测试库验证迁移往返，保留历史记录。

30 秒回答：历史 Assignment 和提交完全保留，但没有可证明的拥有者，所以不能凭标题或 reviewer_ref 猜教师。迁移只增加表和约束；之后由可信管理员显式绑定。独立测试库先升级旧结构放一条哨兵，再升级、check、降回旧版、重新升级，确认原始作业一直存在。

2 分钟深入：User 的 issuer/subject 唯一，Membership 的作业/用户唯一并有外键、撤销时间和可写标记。降级仅删除新授权表，因此也会失去其中授权数据，生产回滚前需备份与停流量评估。检查脚本只创建并删除自己命名的 UUID 测试库，不改开发库。CI 默认使用临时签名密钥和真实 PostgreSQL/S3，不访问外部身份服务、不调用收费模型。

追问：能用 SQLite 证明吗？不能，我们执行真实 PostgreSQL 的迁移和约束。测试通过就算生产身份接入完成了吗？不算，还需真实 IdP、部署 TLS、秘密管理、审计和组织权限验收。

代码：`migrations/versions/6a9d8c7e2f10_teacher_identity.py`、`scripts/check_identity_migration.py`。验证：脚本日志与 `test_assignments_list_filters_before_pagination_and_history_is_not_claimed`。
