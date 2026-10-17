# 前端运行与验收

## 本地启动

```bash
npm ci --prefix frontend
npm run dev:demo --prefix frontend
```

按终端地址打开 `/login`。教师与管理员分别选择演示入口；退出后可切换角色，资料状态在同一浏览器保留。右上角“重置”恢复合成初始资料。

真实 API 使用 `npm run dev --prefix frontend`，配置见 [后端交接说明](backend-integration-handoff.md)。此模式不提供演示身份或合成数据。

## 可操作的演示流程

1. 教师 → 我的作业 → 新建草稿 → 新增题目 → 填写题干、答案、完整评分细则 → 保存 → 返回作业 → 发布前检查 → 发布。
2. 上传学生 PDF/PNG/JPG，每文件单独填写学生标识 → 查看提交与解析状态 → 预览原文件。演示解析仅生成合成作答，不能验证上传文档内容识别。
3. 打开预置“导数及其应用” → 学生 STU-001 → 第3题作答核对。选中文字加入证据，选择存在/不存在/不确定及对应理由，确认后保存。刷新可查不可变历史。
4. 第3题候选评分 → 接受或逐项修订。修订需完整快照，变化项要理由、正分变化要原文引文。拒绝/重评也有历史；所有操作均不发布成绩。
5. 知识库 → 上传合成 Markdown → 选择分类/关联作业 → 等待索引 → 检索原文 → AI助教新建会话提问 → 查看引用。试无命中问题、停止发送、删除会话与资料。
6. 教学统计切换时间范围；设置页查看当前身份和退出。
7. 管理员 → 登记外部合成身份 → 停用/启用 → 管理授权 → 分配只读或可写作业 → 撤销 → 审计中查看事件。系统状态只读，不提供配置写入或提权。

## 自动化命令

```bash
npm run typecheck --prefix frontend
npm run lint --prefix frontend
npm run test --prefix frontend
npm run build --prefix frontend
npm run e2e:demo --prefix frontend
uv run python scripts/export_frontend_openapi.py
npm run api:generate --prefix frontend
git diff --exit-code -- frontend/src/api/generated.ts
RUN_WORKBENCH_E2E=1 uv run pytest -q tests/integration/test_teacher_workbench_browser.py
git diff --check
```

首次浏览器使用前执行 `cd frontend && npx playwright install chromium`。Demo E2E 不依赖后端；真实浏览器测试使用项目独立 `TEST_DATABASE_URL`、私有测试 S3 和临时 OIDC 签名服务。它会应用测试库迁移，不得把开发库配置为测试库。测试身份服务仅存在于测试进程。

真实浏览器测试继续验证 PKCE、带认证 PDF/PNG、Unicode 跨 DOM 文本选区、提交确认丢失后逐字节重放、多人409冲突、只读授权、跨作业404、撤销授权与401退出。它不调用 DeepSeek，也不运行模型。

Demo E2E 使用六个独立浏览器上下文：教师创建发布上传、作答/评分审核、知识库/助教、管理员授权审计、多宽度路由及截图、评分引文及503确认丢失后刷新重放。单元测试覆盖精确分值、来源映射、幂等、角色、撤销、发布规则与取消，不把本地模拟当作 PostgreSQL 并发测试。

## 视觉验收

教师页面按 1366、1440、1920 三种宽度检查横向溢出；1440 截图记录工作台、作业详情、三栏作答核对、评分审核、知识库、助教、统计、设置。管理员五页单独截图。截图只含合成身份与作答。常规 E2E 截图写到被忽略的 `frontend/test-results/visual`；需要更新文档截图时显式运行 `UPDATE_SCREENSHOTS=1 npm run e2e:demo --prefix frontend`，避免每次测试改写受追踪截图。

视觉取向：蓝白工作区、分组侧栏、青色状态辅助、克制阴影、卡片化入口、明确空态/错误/只读提示。UUID、SHA、来源版本等放诊断区域，批阅操作使用教师能理解的说明。

本文件说明验证方法；实际运行结果以本次 PR 的最终提交检查为准，不用旧测试数量替代当前结果。
