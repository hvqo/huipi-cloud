# 考试扫描接收 MVP

本切片提供真实 HTTP 与 Vue 接收链路。场次、共享锁卷、冻结名单沿用现有业务；原件进入私有 S3，预览不调用 OCR、MinerU、LLM 或收费 API。页面始终待身份核验。此阶段不能进行分卷、身份确认、阅卷或成绩发布。

## 数据与隔离

- `ScanBatch`：真实 `ExamSession` 外键，服务端固定 `exam_protected`；记录创建时的 current frozen `RosterRevision`（可为空），通过复合外键确保同场次。该字段是历史上下文，不代表后来仍是当前名单或页面属于名单中的某个人。
- `ScanFile`：批次、可信上传人、request_id、来源 `manual_pdf`、安全原文件名、类型、大小、SHA-256、私有定位、receiving/stored/failed、上传尝试、租约、失败码。上传人/request_id 唯一，批次/原件SHA唯一。
- `ScanPage`：稳定 UUID5(file_id,page_index)、从零开始页序、JPEG尺寸/大小/SHA及私有定位；只允许 `identity_status=unconfirmed`，没有学生/考号字段。
- `ScanOperation`：操作者、请求、尝试次数、动作、摘要、时间和小型结果；只追加，不保存 PDF 内容、Token、学生姓名或原件对象 Key。

批次、stored 文件和页面在数据库中禁止原位修改或删除。页面仅能插入仍有有效租约的 receiving 文件，stored 切换要求完整连续页序；页索引和 stored 状态及成功审计在同一事务提交。失败记录不等于原件不存在，不能当作成功结果。

扫描表不建立 Submission、SubmissionFile 或 ParsingTask，因此旧版解析 Worker 无法领取这些文件。后续每个确认的 AnswerScriptVersion 必须经受保护桥接流程关联现有解析链路，本轮没有开放这种桥接。

## 授权与初始化

复用现有 OIDC/JWT、Principal、真实 Event/Session Scope 和当前 Grant。`is_admin`、旧 AssignmentMembership、JWT role 不授予扫描访问。不存在与越权使用同样的404；数据库不可用返回503。每次请求重新授权，存储 I/O 前后均校验上传权限；授权撤销后不能完成上传或重放读取结果。已开始的合法文件传输不会因为之后撤权回收已经送达的字节。

四个固定动作：`scan.batch.create/read`、`scan.file.upload/read`，范围仅 installation、真实 exam_event、真实 exam_session。角色版本如下：

- `11111111-b13a-4000-8000-000000000001`：scan_operator，包含四项动作。
- `11111111-b13a-4000-8000-000000000002`：scan_reader，只读批次与文件。
- `11111111-b13a-4000-8000-000000000003`：scan_deployer，受控部署委派根。

迁移不自动给现有用户授权。已有安全部署根持数据库凭据并经审批执行：

```bash
uv run python -m huipi_cloud.modules.scans enable-operator \
  --actor-user-id UUID --target-user-id UUID --request-id UUID \
  --approval-ref 审批编号 --confirm-deployment-operation
```

actor UUID不是登录凭据。此命令是可信部署操作，不是公共注册或 Agent 工具。再由实际登录的部署根，通过既有安全 CLI 将 scan_operator 委派到目标场次已建立的 Scope（不提供自由 UUID Scope 创建）：

```bash
uv run python -m huipi_cloud.modules.resource_security grant --token-stdin \
  --request-id UUID --user-id TEACHER_UUID \
  --role-version-id 11111111-b13a-4000-8000-000000000001 --scope-id SESSION_SCOPE_UUID
```

JWT从受控stdin读取，不放入argv、日志或文档。撤销继续使用该CLI的revoke。该角色不授予题目、标准答案、名单正文读取或其他场次权限。

## HTTP 合同

均位于 `/api/v1`，要求 Bearer Access Token；固定目录同时登记在两个安全清单中：

- GET `/scan-sessions`：当前有scan.batch.read授权的场次，按时间/ID稳定排序，有界分页，返回接收限制及是否存在冻结名单。
- POST `/exam-sessions/{id}/scan-batches`：`{request_id,title}`；只接受ready场次，Event需draft/active。未知分类等额外字段422。
- GET `/exam-sessions/{id}/scan-batches`、`/scan-batches/{id}`：批次列表/详情。
- POST `/scan-batches/{id}/files`：multipart `request_id`、`file`。首次成功201，已完成同请求同正文在仍可接收场次内重放200；场次已关闭或取消时改用GET查看既有结果，不恢复接收。
- GET `/scan-batches/{id}/files`、`/scan-files/{id}`：包含接收状态、失败码、request_id、SHA及页数，不包含私有定位。
- GET `/scan-files/{id}/pages`：仅stored文件，按page_index分页。
- GET `/scan-files/{id}/original`：受保护附件PDF。
- GET `/scan-pages/{id}/preview`：受保护JPEG。

列表limit默认20，最大100，offset最大100000。每场次最多100批、每批最多20文件。文件失败也占一个文件名额，保留故障事实；同请求可重试，不支持删除后逃避限制。

错误：400坏PDF/空文件；401缺少有效身份；404不存在/无授权；409请求正文冲突、同内容重复、正在接收、场次不接收；413大小/页数/预览超限；415非PDF；422字段错误；429本进程繁忙；503依赖失败、超时、提交结果未知。失败摘要只返回稳定安全原因码。

## 上传事务与中断

1. HTTP固定策略验证真实父级授权；multipart有总字节上限，分块暂存并计算SHA。
2. 短事务再次授权、按Actor/request advisory锁、Event/Session/Batch/File锁顺序预登记receiving，数据库时间租约180秒，与begin审计一起提交。
3. 临时目录中由受监督子进程验证PDF并生成JPEG。没有跨存储I/O的长数据库事务。
4. 原件与各页写入独立不可变Key：`<prefix>/exam-scans/{session}/{batch}/{file}/{attempt}/original.pdf`和`page-{index}.jpg`；文件名不参与Key。
5. 短事务重读当前授权、Session状态和准确attempt_token/DB租约，批量写页、切换stored、写成功审计后返回。

连接取消/处理失败尽力将准确receiving尝试标为failed，不覆盖stored或后继尝试。进程突然退出可能留下receiving；180秒后，同上传人、同request_id、同文件指纹可以重试，保留File ID，生成新attempt/Key。不能强行认定旧S3上传已经停止。浏览器保留本次内存请求以便重试；刷新后可查看文件状态/request_id，重新上传需要保留原request_id。首版不提供跨页面持久化上传队列或断点续传。

同一批次相同SHA的不同新请求409；不能靠文件名或上传顺序推测学生。reserve或最终COMMIT确认丢失均返回503，保留对象；相同请求可查询/重放恢复。旧接收租约的迟到完成不能登记新页面。

**一致性边界**：S3与PG不共享事务。同步boto3线程取消后可能继续读取自己持有的文件句柄。本轮不在异常处理中删除对象，以免误删已提交或仍上传对象；失败、过期和进程崩溃可能产生孤立原件/预览。以后按File/attempt前缀对账，在确认数据库引用、足够延迟及没有活跃上传后清理。未实现自动GC和Exactly Once。

## 明确资源上限

- 原件32MiB，PDF最多50页；配置可收紧，不能越过硬上限。只允许扩展名PDF、声明application/pdf、PDF签名与实际解码全部通过；拒绝加密PDF。
- 请求体最多文件上限+64KiB multipart开销；分块64KiB，spool阈值1MiB，禁止任意大字节读入。
- 仅面向Linux单API进程MVP部署：本进程同时最多2个扫描上传、4个原件/预览读取，超限429；多进程会倍增该限额，需部署时限制worker数量。
- 渲染独立进程组：45秒墙钟（可收紧，最大60）、40秒CPU、1GiB虚拟地址空间硬限制、每文件2MiB写入上限；取消/超时kill进程组并回收，Linux parent-death signal处理父进程崩溃。
- JPEG最长边1600px、2.56M像素、每页2MiB、总预览32MiB。子进程受地址空间约束；不能将这些限制宣称为容器总RSS或主机并发容量保证。
- 上传业务默认120秒（60–150可配），HTTP含body整体150秒；租约180秒。DB单语句8秒、锁等待5秒；重试保留状态，不无限循环。
- 原件/预览先有界落盘验证长度及SHA再返回，下载45秒，HTTP60秒，输出private/no-store/nosniff。浏览器最多缓存当前单页JPEG，下载原件Blob有32MiB级上限。
- 服务端每请求临时磁盘约96MiB（multipart暂存、原件副本、全部预览），下载另有最多4×32MiB；实际须留足开销与并发磁盘余量。

这些是小批接收限制，**不支持1600页、扫描仪直连、任意大PDF或续传**。PDFium子进程是资源隔离，不是通用安全沙箱，部署需及时更新解析库并限制服务账号与网络。

## 本地运行和浏览器流程

1. 按[开发指南](quickstart.md)配置数据库、私有桶与OIDC（保留已有.env；不要覆盖）。仅对明确目标执行正式迁移；开发数据库升级由操作员另外决定。
2. 通过既有组织/试卷/考试/名单CLI创建场次、绑定共享锁卷并准备ready；名单可冻结但不是接收前提。为教师授予上述场次扫描角色。
3. 启动API：`uv run uvicorn huipi_cloud.main:app --host 127.0.0.1 --port 8000`。
4. 前端：`npm ci --prefix frontend`，配置前端OIDC环境与API代理，`npm run dev --prefix frontend`。详见[工作台指南](teacher-workbench.md)。
5. 登录后进入「考试扫描」(`/scans`)，选择已授权场次，新建批次，选择PDF，上传并生成预览；切换页面查看「待身份核验」，下载原件或查看SHA。
6. 出现503或网络中断时先刷新文件状态；保留原PDF和request_id，同请求重试。不要因为上传失败而重新授予权限或关闭鉴权。

浏览器E2E：显式`RUN_WORKBENCH_E2E=1`执行`tests/integration/test_teacher_workbench_browser.py`。它启动隔离测试IdP、真实签名JWT、实际FastAPI/Vue/Chromium，使用独立TEST_DATABASE_URL及私有测试桶前缀。不是Mock上传，不依赖用户真实身份服务。新增混班场景包括两班四学生、同名学生、共享锁卷、冻结名单、4页无二维码PDF，验证原件逐字节/SHA一致及4页都未确认。默认CI不启浏览器或收费服务。

## 下一切片合同

人工分卷应引用稳定ScanPage/File和Session，对有序页集合建立不可变AnswerScriptVersion；身份确认应明确指向可信Participation、冻结RosterRevision/EntrySnapshot和同场次SessionPaper，检查名单更正、撤销、历史引用与重确认。不能回写本轮原始页身份、覆盖文件或直接建立旧Worker可领取任务；受保护桥接必须单独授权和审计。尚无相关写接口，不宣称已经实现。
