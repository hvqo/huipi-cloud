# 教师账号、密码与授权

慧批由后端验证本地账号和密码。教师使用邮箱或手机号登录；旧 OIDC 登录和旧外部身份登记入口不再用于业务认证。已有 `User`、作业成员、考试授权和历史审核记录不会自动重建或改写；历史用户需要由管理员核对身份后显式绑定本地账号。

## 首次部署

先备份并确认数据库目标，检查迁移后再由操作员执行升级。以下命令会修改所配置的数据库，不能对不确定的数据库运行：

```bash
uv sync --locked
uv run alembic current
uv run alembic heads
uv run alembic upgrade head
```

检查是否已有历史管理员：

```bash
uv run python -m huipi_cloud.modules.identity list-admins
```

新安装、没有历史管理员时，由拥有数据库凭据的可信操作员创建首个本地管理员：

```bash
uv run python -m huipi_cloud.modules.identity bootstrap-local-admin \
  --account '管理员邮箱或手机号' --display-name '管理员姓名' \
  --confirm-local-admin-bootstrap
```

程序在终端隐藏输入密码并要求二次确认；密码不要写在命令参数、环境变量、Git 或聊天记录中。密码须为 6 至 128 个字符，可以使用纯数字、纯字母或符号。没有默认账号或默认密码。

如果数据库已有历史管理员，不能新建另一个管理员来绕过既有权限；在核实身份后使用同一命令加 `--existing-user-id <历史管理员UUID>`，将本地凭据绑定到原有用户 ID。首个管理员只能新建一次；若有多个历史管理员，应逐一核实并显式绑定各自的原用户，不能用网页自助提权。数据库中的学校组织、作业与考试授权仍需按原有受控流程分别分配；`User.is_admin` 不自动授予受保护考试资料和成绩的全部权限。

## 教师账号

管理员登录后进入 `/admin/accounts`：

- 创建教师账号，输入姓名、邮箱或手机号及初始密码。教师首次登录必须修改密码。
- 已有历史教师先在线下核对用户编号，再显式关联本地账号；不凭邮箱、手机号或姓名自动合并用户。
- 可以停用或启用教师，或为教师设置临时密码。停用和管理员重置都会撤销该教师已有会话和未使用的找回链接。
- 普通教师不能创建账号、重置别人的密码或提升管理员权限。原 `/admin/teachers` 外部身份登记 POST 已停用；旧用户启停和作业成员管理接口仍受现有管理员权限控制。

管理操作保存操作者、目标用户、动作、请求号和时间，不保存明文密码。重复请求使用相同请求号和相同输入；如果密码已更改或正文不同，返回冲突。密码重置后须通过安全渠道将临时密码交给教师。

## 忘记密码

可以同时使用已验证邮箱自助找回及管理员重置。管理员重置不依赖邮件服务。邮箱找回必须先配置 SMTP，并由教师登录后在“修改密码”页面绑定并验证找回邮箱；**手机号账号同样可以绑定邮箱**。未绑定邮箱或邮件服务不可用时，请联系管理员。

在受保护的服务端配置中设置：

```dotenv
AUTH_PUBLIC_BASE_URL=https://慧批前端域名
AUTH_SMTP_HOST=受控SMTP服务器
AUTH_SMTP_PORT=587
AUTH_SMTP_USER=邮件账号
# AUTH_SMTP_PASSWORD 通过受保护的本地配置或秘密管理器提供
AUTH_SMTP_SENDER=发件邮箱
```

SMTP 必须支持 STARTTLS。公开地址必须是固定 HTTPS 来源；本地开发才允许回环 HTTP。邮件链接在 URL fragment 中携带一次性随机令牌，不经 HTTP 路径或查询参数传入服务端访问日志；令牌 30 分钟过期，数据库仅保存摘要。重置成功后撤销该用户全部会话及未使用的找回链接。请求找回对不存在账号返回相同提示，避免通过响应枚举账号。邮件发送失败不会伪称重置成功。

如果唯一管理员无法登录且未配置邮件服务，由拥有数据库访问权限的可信操作员先通过 `list-admins` 核实原管理员 ID，然后在本机交互式重置该已有管理员的密码：

```bash
uv run python -m huipi_cloud.modules.identity reset-local-admin-password \
  --user-id '<历史管理员UUID>' --confirm-local-admin-password-reset
```

此操作不会创建或提升管理员；它撤销该账号的旧会话和未使用链接，并要求管理员下次登录时修改临时密码。审计记录注明由本地操作员重置，不伪造网页操作者身份。

## 会话与部署边界

密码采用独立盐值的 Argon2id 哈希，服务端不保存明文。登录会生成随机会话，数据库仅保存摘要；每个请求重新读取账户和当前授权。浏览器会话 Cookie 为 HttpOnly、SameSite=Strict，生产环境使用 Secure 和 `__Host-` 前缀；写操作需匹配当前会话的 CSRF 标记，登录及找回请求需匹配受控来源。默认会话最长 12 小时，当前没有独立空闲超时。

默认 `AUTH_ALLOWED_ORIGINS` 为 `http://127.0.0.1:5173,http://localhost:5173`。生产部署应设置 `APP_ENV=production`、明确的 HTTPS 前端来源和 HTTPS 反向代理，不依赖客户端角色字段。登录按账号及连接来源使用数据库共享的 15 分钟限流；多实例部署时仍需正确配置可信代理与外围限流。找回链接、密码、Cookie、邮件内容及 API Key 不应写入日志。

受控 CLI 如需认证，先在本机交互输入密码，将短时会话仅通过管道传给目标命令；不将其打印到终端或放入命令行参数：

```bash
uv run python -m huipi_cloud.modules.identity session-token --account '教师邮箱或手机号' | \
  uv run python -m huipi_cloud.modules.resource_security audit --token-stdin
```

该示例仍要求该教师拥有相应安全审计权限。登录会话的撤销、账号停用和原有资源授权均在服务端生效；不能以账号本身推导学校全部数据访问权。
