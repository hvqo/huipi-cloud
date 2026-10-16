# 教师工作台：面试复盘

## 浏览器为什么不能自声明教师身份？

简洁解释：浏览器是请求发起方，不是授权方。access token 标识身份，服务器 User/Membership 决定能访问哪些作业。

30 秒：前端用标准 OIDC Code + PKCE 登录，但不把前端角色当权限。所有业务请求由 FastAPI 验签并核对资源归属；工作台的按钮显示只改善体验，不能代替写权限。一个教师即使修改 URL 或构造请求，也不能读取其他作业。

深入：oidc-client-ts 管理 state/nonce/PKCE，Token 仅在内存。服务器校验 Issuer/Audience/算法/有效期，再解析内部 Principal。页面只读取不含标准答案的工作台投影，can_write 来源于真实数据库。Mock OIDC 仅在独立测试 fixture 中，生产没有认证旁路。真实学校 IdP 尚未验收。

追问：Token 刷新页面后怎么办？重新走 IdP；会话不能靠长期 localStorage JWT 保持。退出失败怎么办？先清本地状态，再报告远端退出未确认。

代码：`frontend/src/auth.ts`、`api/client.ts`、`identity/dependencies.py`。验证：auth/client 单测与 `test_teacher_workbench_browser.py`。

## UTF-16 为什么不能直接传给 Python？

简洁解释：JavaScript offset 通常是 UTF-16 code unit，Python 使用 Unicode code point。Emoji 和生僻字可能占两个 UTF-16 单元。

30 秒：把浏览器 offset 原样传后端，会让 Emoji 后的证据错位。我们先验证选区在同一 Canonical 节点，比较 textContent 与原文，再按码点转换。跨 TextNode 可以，但跨 Canonical 节点必须分别选择。

深入：Range 前缀长度得到 UTF-16 位置，拒绝截断代理对，再使用 Array.from 计算码点。组合字符按多个码点保留，不做 NFC 归一化；半开范围与后端一致。文本高亮可以拆成多个 DOM TextNode，但整体 textContent 不变。显示变化就拒绝提交，保存前还检查来源版本。

追问：为什么不按可见字形计数？后端合同是码点，不是 grapheme。跨页怎么选？分别加入多个独立范围，不拼接伪造文本。

代码：`lib/selection.ts`、`SourceNode.vue`。验证：selection DOM 单测与原生 Chromium 多 TextNode/Emoji 选区，数据库核对实际范围。

## 超时后为什么不能重新生成 UUID？

简洁解释：超时不等于没有提交。原请求重放才能确定同一次操作的结果。

30 秒：服务端可能已经提交，只是响应丢了。前端保存发送前的 request_id 和完整 JSON 字节，503 或超时后锁定编辑，只重试原请求。409 则要求重新核对，不覆盖别人。

深入：待确认正文仅含来源元数据、坐标与原因，按身份和资源保存于 tab sessionStorage，不包含 Token 或学生正文。重试仍由服务器重新授权。后端 request 摘要、期望修订及短事务锁是权威边界。即使收到 201 但响应体无法消费，也不能声称已回滚。

追问：两名教师同时改呢？只有符合 expected_revision 的一个追加成功。失去权限还能重试旧请求吗？不能，授权在重放之前。

代码：`lib/review.ts`、`views/Review.vue`。验证：Mock API 503/409 与真实提交后替换响应的浏览器 E2E。

## 为什么不能直接在 iframe 上放下载 URL？

简洁解释：资源需要 Authorization Header，普通 src 导航不能直接附加应用持有的 Bearer Header。

30 秒：先通过认证 fetch 获取有界文件，再建 Blob URL 预览；不把 Token 拼进查询串。关闭或离开页面释放对象 URL，避免文件继续留在当前组件。

深入：只允许 PDF/JPG/PNG，读取最多 21MiB，有超时和 AbortController；组件销毁取消请求，异步完成后不再创建失去管理的 URL。Canonical 用原文文本渲染，不执行学生内容中的 HTML 或加载外部链接。原图与文本证据应对照核验，不能假称来源完整。

代码：`FilePreview.vue`、`api/client.ts`。验证：三类文件释放单测，真实 S3 PDF/PNG 浏览器预览。

## 接口测试和浏览器测试有什么区别？

简洁解释：接口测试验证业务与数据库；浏览器测试还验证登录跳转、真实 DOM、交互和文件生命周期。

30 秒：Python 回归保证授权、并发与来源规则，Vitest 快速检查前端错误分支，Playwright 验证整条实际链路。测试身份服务不代表真实学校单点登录，合成解析源也不代表真实 OCR。

深入：浏览器 E2E 使用实际 PostgreSQL 和私有 S3、运行时 RSA、TLS Mock OIDC，并检查 PKCE 后的签名 Token。只在一次确认丢失的专项中替换响应，数据库仍真写入。测试库与开发库分开，CI 无收费请求、无模型权重下载。生产构建不包含 fixture 控制端点。

代码与验证：`tests/integration/test_teacher_workbench_browser.py`、`frontend/e2e/workbench.spec.ts`、GitHub teacher-workbench job。

## 下一步候选评分界面还需要什么？

当前仅作答存在性复核。候选评分界面需复用已存在的审核 HTTP，提供完整 Rubric 快照、逐项分数/理由/来源、历史身份与冲突恢复；默认摘要没有完整理由和证据，详细审计访问需单独设计。教师接受不绕过评分质量门禁，也不自动发布成绩。
