# 教师工作台

工作台提供“查看作业 → 查看学生提交 → 对照原始文件及 Canonical → 选取作答证据 → 保存人工决定 → 查看历史”的浏览器流程。候选评分界面尚未开放；本工作台不生成或发布成绩，不调用收费模型。

## 环境与启动

- Node.js 24（仓库 `frontend/.nvmrc` 固定开发版本），npm；Python/uv、PostgreSQL 和私有 S3 沿用后端配置。
- Vue 3、TypeScript、Vite、Vue Router、Element Plus；只用少量共享认证状态，没有引入 Pinia。
- 先按[教师认证](teacher-auth.md)配置真实 OIDC 资源服务器和内部 User/Membership。数据库迁移由操作员独立执行；前端不会升级数据库。已有作业、资料、模型和用户 `.env` 不变。

后端使用已有启动方式：

```bash
uv sync --locked
uv run uvicorn huipi_cloud.main:app --reload
```

前端首次配置：

```bash
cd frontend
npm ci
cp -n .env.example .env.local
# 编辑 .env.local 中的公开 OIDC 客户端配置
npm run dev
```

开发地址为 `http://127.0.0.1:5173`，API 通过同源 `/api` 代理到 `http://127.0.0.1:8000`。避免把 localhost 与 127.0.0.1 的回调混用。`WORKBENCH_API_TARGET` 是开发代理目标，不会成为浏览器可传入的任意 URL。

## OIDC 登录

使用标准 [oidc-client-ts](https://authts.github.io/oidc-client-ts/interfaces/UserManagerSettings.html) Authorization Code + PKCE（S256），显式 nonce/state。配置公共 SPA client，不使用 client secret：

- `VITE_OIDC_AUTHORITY`：可信 IdP 的 HTTPS issuer；生产构建拒绝非 HTTPS authority，开发仅另允许 loopback HTTP。
- `VITE_OIDC_CLIENT_ID`：IdP 中登记的公共客户端。
- 回调地址：`http://127.0.0.1:5173/auth/callback`；退出回调：`http://127.0.0.1:5173/login`。部署域名变化需同时修改 IdP 登记。
- `VITE_OIDC_SCOPE`：IdP 已授权的 scope。API 要求的是 access token audience，不是 ID token audience。`VITE_OIDC_AUDIENCE` 为支持该参数的 IdP 传 audience；`VITE_OIDC_RESOURCE` 提供标准资源指示器。两者均须按实际 IdP 合同配置，不能靠前端修改角色授权。

access/ID token 只存内存；sessionStorage 仅保存短期 PKCE 状态和不含作答正文的未确认复核请求。没有 localStorage 长期 JWT、匿名业务模式、手填 Token 登录或测试身份开关。完整刷新需要重新走身份服务；过期或 401 清除本地认证状态。显式退出会清理本地令牌及复核草稿，并尝试标准 IdP 退出；若远端退出无法确认，会明确提示。

`VITE_*` 会打包为公开配置，严禁填写 DeepSeek Key、数据库密码或其他秘密。当前没有真实学校 IdP 验收证据；未配置时登录页保持锁定，不会匿名请求业务接口。

## 页面与 API 合同

- `/scans`：按真实场次Scope接收扫描PDF、查看逐页预览与待核验状态；参见[扫描接收MVP](exam-scan-intake.md)。
- `/assignments`：分页作业与新建草稿，只显示已授权资源。
- `/assignments/:aid`：题目、Rubric 概要、提交列表。没有伪造的编辑/删除操作。
- `/assignments/:aid/submissions/:sid`：文件元数据、解析与 Canonical 状态、逐题复核入口。
- `/assignments/:aid/submissions/:sid/questions/:qid/review`：来源、区域选择、决定预览、提交及本题历史。

前端类型来自真实 OpenAPI：

```bash
uv run python scripts/export_frontend_openapi.py
npm run api:generate --prefix frontend
```

生成的 `frontend/src/api/generated.ts` 纳入版本控制；CI 重生成后检查差异。客户端只请求固定同源 `/api/v1/`，设置 Authorization Header、AbortController、15 秒请求上限（文件 30 秒）、2MiB JSON / 21MiB 旧作业文件读取上限；扫描独立允许32MiB级下载与180秒上传客户端上限；离开页面取消读取。超限明确报错，不截断成假结果。401、403、404、409、422、503 区分处理，日志不打印服务内部错误正文或令牌。

两个最小读取补齐：

1. `GET /assignments/{assignment_id}/workbench`：不读取 AnswerKey；分页最多 25 题，可按 question_id 在同一 Assignment 内精确过滤；Rubric 每题显示前 5 项并给出总数/截断标记；can_write 来自服务端当前授权。只读接口也接入固定权限矩阵。
2. 作答 inspect 增加 confirmed_regions 坐标投影：仅返回与本次 Canonical/Alignment 匹配的最新人工范围，分角色分页，最多 50 条及 48KiB。过期历史不会涂成当前确认。历史仍为不可编辑/删除的只读摘要。

提交摘要的解析状态已支持 pending/running/retry_wait/succeeded/failed，避免已解析资料无法进入工作台。没有模型或迁移变化。

## 原始文件与来源

原始 PDF/JPG/PNG 通过认证 fetch 获取 Blob，用短期 Blob URL 预览。关闭、换页和组件销毁都会 revoke；不把 Bearer Token 放入 URL，不公开对象 Key。原始文件当前最大 20MiB。Canonical 页面仍由后端验证来源对象后提供，前端用文本渲染，绝不执行其中的 HTML、Markdown 或外部 URI。图片素材对照原始文件；尚未提供解析图片资产的独立预览接口。

浅色边框表示 Alignment 关联；绿色标示当前来源的确认作答，灰色为排除题干，黄色为不确定。人工历史范围与本次待提交选区分开，绝不自动用关联区域或历史首段冒充新作答。

## 选区与 Unicode

`selection.ts` 验证 Range 起止属于同一 `data-source-pointer` 节点，使用原始 textContent 比较，再把 UTF-16 code unit 转为 Unicode code point 半开区间。拒绝截断代理对、显示文本变化和跨 Canonical 节点的单次选择。一个节点内部可跨多个 TextNode（含只读高亮标记）；多个 Canonical 节点需分次添加，允许相邻但拒绝重叠范围。中文、Emoji、生僻字、组合字符和换行均按码点处理，不做 Unicode 归一化，也不换算为 UTF-8 字节。

独立图片节点只能经明确按钮选取，服务端仍检查真实素材与题目归属；共享或不可信素材应保留 uncertain。来源不完整不等于学生未作答。

## 保存、并发与历史

新决定生成新 UUID，并提交 expected_revision、expected_source、明确决定、原因和必要区域。存在作答需实际选择 response_regions；不存在需选择题干/空白范围，不能以“没选中”推断缺答。不确定只有既有合同允许的原因可无区域。

预览后先重新检查版本，再发送冻结的正文；服务端仍进行提交时授权、来源与修订检查。保存期间锁定编辑与重复点击。网络失败、超时或 503 保留 request_id 与原始 JSON 字节，只能重试原请求；不宣称已经回滚。重新登录同一身份可恢复 tab 内的未确认请求。显式退出会清理本地草稿，此后应先检查服务端历史，不能盲目重建请求。

409 锁定旧编辑并提示人工刷新，不自动覆盖他人记录；新来源须重新选取。成功后读取最新状态与历史。历史的“与本次读取来源相符”仅指版本匹配，不证明教学判断正确；可信教师与历史自声明身份明确区分。

## 检查与合成浏览器验收

```bash
npm ci --prefix frontend
npm run typecheck --prefix frontend
npm run lint --prefix frontend
npm run test --prefix frontend
npm run build --prefix frontend
cd frontend && npx playwright install chromium && cd ..
RUN_WORKBENCH_E2E=1 uv run pytest -q tests/integration/test_teacher_workbench_browser.py
```

E2E 必须配置独立 `_test` PostgreSQL 和独立私有测试桶；沿用后端 fixture 的安全检查和自有对象清理。测试生成 RSA、TLS 证书和独立 Mock OIDC，真实走浏览器 redirect、PKCE、Token 验证、数据库授权与 S3；Canonical/Alignment 是合成 fixture，不冒充真实 OCR。测试身份与控制端点只存在测试文件，不进入生产包；截图仅含合成资料，trace 关闭以免收集令牌。

CI 有独立 teacher-workbench job，验证锁文件、生成合同、类型、ESLint、单测、构建及真实浏览器链路，不调用收费模型或下载 Embedding/Rerank 权重。默认后端测试显式跳过该浏览器 opt-in，独立前端 job 必须执行。

## 生产与后续边界

`npm run build` 的 dist 由 HTTPS 静态站点服务，`/api/` 反向代理 FastAPI 并保留 Authorization；SPA 路由回退 index.html。Vite 仅用于开发，不是生产认证服务。真实 IdP、浏览器部署策略和学校教师数据仍须独立验收。

P5-B2 需新增候选评分审核页面、完整 Rubric 快照编辑、受限理由/证据核对及审计正文读取设计；复用已有候选审核 HTTP，但不自动发布成绩。P3-D2B、学生身份、聊天端点、多学校租户体系不在本工作台范围。
