# 小批扫描接收验收记录

日期：2026-10-22。仅使用人工合成资料，无模型调用、无真实学生数据外传。此记录验证扫描接收，不代表分卷、身份确认、OCR、评分或生产容量验收。

## 基线和隔离

- 基于main `84933c9a8f9a60654d49f96da34b0d3ba6ce995d`；没有合并或依赖仍为Draft的名单性能优化，也未合并历史前端分支。
- 最终重验：任务独占PostgreSQL **16.14**，私有S3兼容SILO `RELEASE.2026-09-16T00-00-00Z`；回环55434/59004，独立`*_test`数据库与专用测试桶，按UUID前缀清理自身对象。
- 原先Docker Desktop在补回归时退出，55432不可达导致该次运行无效；没有重启Desktop或更改其他容器。随后使用已有本地镜像，在现存默认Docker引擎创建两台本任务隔离容器并重跑验证。用户开发数据库、数据卷及.env均未修改。
- 默认Fast CI不带PG/S3/浏览器；本地真实集成证据与CI纯单元证据分别报告，不能互相替代。

## 执行与结果

- `uv sync --locked`：通过，未增加依赖。
- `uv run ruff check .`：通过。
- `uv run pytest -q tests/unit`：**633 passed**，无跳过。
- 相关真实集成：**168 passed**，无跳过。覆盖`test_scan_intake`、`test_teacher_authorization`、`test_resource_security`、`test_exam_rosters`、`test_exam_sessions`、`test_paper_versions`、`test_organization_migration`、`test_submissions_api`和`test_parsing_runtime`。
- 最终图片型PDF及匿名S3拒绝专项：`uv run pytest -q tests/integration/test_scan_intake.py`：**19 passed**，无跳过；这是上面扫描子集的最终复验，不累加成新的独立案例数。
- `RUN_WORKBENCH_E2E=1 uv run pytest -q tests/integration/test_teacher_workbench_browser.py`：Python入口1 passed，内部实际Chromium **3 passed**，包含原有两条工作台业务链和新增扫描链。不是Mock上传/存储。
- `npm ci --prefix frontend`、typecheck、lint、build：通过；Vitest **43 passed**。
- OpenAPI重导出、TypeScript重生成后无漂移；新扫描方法与固定权限清单一致，54个受保护HTTP操作逐一拒绝匿名请求。
- `uv run python scripts/check_migration_structure.py`：唯一Head **b13a21f0c901**，22个迁移、53张映射表。
- 在独立测试库执行upgrade/check/downgrade至b12c9d20e411/re-upgrade/check，模型无差异。专项测试在含旧作业、用户、组织、共享卷、场次和名单的图上比较升级前后逐表摘要；有扫描事实时降级明确拒绝且Head及4页记录保留。旧安全/试卷/名单迁移回归同时保留原提交与审核图。
- `docker compose config --quiet`、`git diff --check`：通过。
- 修改的公开文档相对链接检查通过；既有101项保护文件SHA、备份/视觉分支和stash保持不变。

## 实际浏览器闭环

合成数据包括一个ready场次、两行政班、四名学生（两名同名但Student ID不同）、一份共享已锁卷和current冻结名单。PDF由四张人工合成答题页图像组成，无二维码，不提供可信身份提示给服务端。

浏览器通过TLS测试IdP完成真实Code+PKCE和签名JWT验证，选择真实入库场次，新建批次，multipart上传PDF。服务调用真实PDFium子进程和私有S3，PG记录stored和4个稳定页面；浏览器切换第2页，认证fetch获得JPEG Blob，下载原件逐字节且SHA一致。页面均为unconfirmed；Participation仍是预先名单的4条，没有自动创建Submission、ParsingTask或成绩。

S3原件匿名HEAD实际返回403。文件与页面HTTP不包含bucket/key/token；无Grant、其他场次Grant、旧管理员、伪造JWT role、撤权后重放均拒绝，返回JSON不泄露目标是否存在。

截图输出（本地忽略的测试产物）：`frontend/test-results/scan-intake.png`；浏览器摘要：`frontend/test-results/browser-summary.txt`。自查修正了局部样式覆盖主按钮的问题，并增加受保护Blob大图入口。

## 故障与一致性证据

19项专项覆盖实际坏PDF、空文件、错误声明、超过配置字节上限、51页和加密PDF；均无成功页索引。已上传后注入存储失败、请求取消、相同请求并发、审计写失败、最终COMMIT成功但客户端丢确认，分别验证失败状态、同File ID新尝试重试、页索引原子回滚、仅一条有效文件、对象保留与幂等恢复。S3原件内容被改写后，下载在发送文件字节前因SHA/长度失败而503。存储I/O期间实际撤销Grant后，最终提交拒绝，不留下stored或页面。

5项纯单元边界测试验证两上传名额与取消释放、声明/分块请求体限额、真实子进程超时kill及回收、硬资源配置与客户端分类字段拒绝。

## 容量与后续

32MiB、50页、每批20文件、每场100批；单Linux API进程同时2上传/4下载。预览1600px/2MiB每页、32MiB总量，渲染45秒墙钟/40秒CPU/1GiB地址空间。未做1600页或长期负载压测，不能把配置上限当作吞吐承诺。

取消不能强制终止底层同步S3线程；进程崩溃/失败/未知COMMIT可能留下孤立对象。本轮不做危险异常删除，保留独立attempt Key，需后续带宽限期的引用对账清理。没有断点续传、自动重试队列、自动GC或跨系统Exactly Once。

下一轮需为有序页集合建立不可变AnswerScriptVersion，并以真实Participation及冻结名单快照确认身份，处理名单更正与重确认；再通过单独授权桥接现有解析/评分。当前API不能直接修改页面身份或生成正式成绩。完整部署和API合同见[扫描接收指南](../development/exam-scan-intake.md)。
