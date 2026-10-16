# 教师工作台验证方法与边界

这里记录浏览器流程的工程验证，不评估 OCR、模型评分或真实教学准确率。默认 Vitest 使用 Mock API；真实浏览器验收使用独立 PostgreSQL/S3 和测试身份服务，不调用模型。

## 单元与 DOM

`frontend/tests/selection.test.ts` 覆盖中文、ASCII、Emoji、生僻字、组合字符、换行、同一 ContentNode 中多个 TextNode、跨节点拒绝、显示变化拒绝和相邻/重叠范围。使用真实 DOM Range，不只验证手工数字。

`review.test.ts` 检查三种明确决定、合法原因、不能从空选区推断缺答、身份字段隔离、来源版本和原请求字节冻结。`client.test.ts` 检查 Header Token、401 清除、403/404/409/422/503 区分、任意 URL 拒绝、流式上限、取消及成功响应无法消费时的未知提交边界。

`workflow.test.ts` 使用真实 Vue 组件与 Mock API，检查可编辑/只读、来源过期、无自动选区、503 原请求重放、409 不覆盖、保存前来源变化拒绝、历史身份、来源不可用和 PDF/PNG/JPG Blob 释放。`auth.test.ts` 验证无 IdP 拒绝、内存存储、到期状态及内部返回路径约束。

## PostgreSQL/S3

`test_teacher_workbench_projection.py` 使用真实数据验证不含标准答案、Rubric 截断、读写权限、解析后提交的合法状态、人工坐标分页及过期记录不高亮。保留全部既有认证、来源、幂等和不可变历史回归。

## 原生 Chromium 闭环

```bash
RUN_WORKBENCH_E2E=1 uv run pytest -q tests/integration/test_teacher_workbench_browser.py
```

该 fixture 在独立测试库和私有测试桶建立合成作业/题目/提交，实际上传 PDF/PNG，经现有 Canonical/Alignment 生成和验证。解析源是合成 fixture，不是新跑的 MinerU。运行时生成 RSA 与短期 TLS 证书，Mock OIDC 真实检查 Code/PKCE；FastAPI 仍验签、校验 audience、内部身份和 Assignment 授权，未覆盖为固定 Principal。

浏览器场景检查：登录门禁、授权作业、Rubric、提交及原始 PDF/PNG、含多码元字符与多 TextNode 的选区、三个决定、历史、提交已成功但响应变成 503 的原字节重试、第二教师先写引发冲突、只读成员、资源不匹配、真实撤权后文件/保存拒绝、401 退出与新建草稿。

未知提交场景保留真实 PostgreSQL 写入，只替换浏览器收到的第一次响应；不会使用 Mock 存储或 Fake API 冒充业务 E2E。测试完成后核对数据库审核人外键、原请求唯一性、修订数及码点范围，清理仅限专用测试库与自有对象。

截图仅含合成资料，保存到忽略目录 frontend/test-results；关闭 trace，不上传凭据。测试 IdP 的 actor/revoke 控制端点只属于独立 fixture，生产包中没有测试登录开关。

## 不能从测试推导的结论

这些验证不证明学校 IdP 已部署、不证明真实教师工作体验已完成验收、不证明原始图片或 OCR 完整，也不证明人工判断或候选分数在教学语义上正确。真实 IdP、受控教师可用性、原文件/资产查看质量与授权数据应另行验收。候选评分界面与正式成绩发布未实施。
