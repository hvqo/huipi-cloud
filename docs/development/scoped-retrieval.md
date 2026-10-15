# 教师知识材料与作用域检索

这是受控本地操作指南。没有登录/RBAC/租户授权，也没有匿名导入或对话HTTP端点。Assignment参数只是操作员选择的数据范围，不是可信教师身份。先阅读[Copilot指南](teacher-copilot.md)。

## 材料与许可

仅导入操作员确认可用的教师参考Markdown/TXT，编码为UTF-8，最大64KiB。必须明确声明`synthetic`或`authorized`并提供原文件SHA-256。许可声明是操作员确认，系统不能自动判断版权、隐私或真实授权。

不自动索引学生原始作业、个人信息、未确认OCR、教师私人标准答案、GradeProposal或GradeReview。不枚举现有S3桶，不安装Embedding、Rerank、OCR或向量数据库。正文需由操作员确认不含私有凭据或不应外传的数据；字段投影不等于自动正文脱敏。

## 初始化与导入

```bash
uv sync --locked
# 操作员确认目标DATABASE_URL后，手动升级；应用不会自动升级或create_all。
uv run alembic upgrade head
uv run python -m huipi_cloud.modules.knowledge --help
```

复用本地PostgreSQL和私有S3设置。已有`.env`无需覆盖。以下使用自己的合成作业ID；不要将真实材料路径写进公开日志：

```bash
read -r ASSIGNMENT_ID
printf '# 合成热学资料\n\n热传导通过接触传递能量。\n' > /tmp/huipi-teacher-notes.md
SOURCE_SHA=$(sha256sum /tmp/huipi-teacher-notes.md | cut -d ' ' -f1)
uv run python -m huipi_cloud.modules.knowledge import \
  --assignment-id "$ASSIGNMENT_ID" --file /tmp/huipi-teacher-notes.md \
  --title '合成热学资料' --sha256 "$SOURCE_SHA" --permission synthetic
uv run python -m huipi_cloud.modules.knowledge search \
  --assignment-id "$ASSIGNMENT_ID" --query '热传导' --top-k 3
```

只有本地文件可导入，没有URL下载。CLI只读最多65537字节后验证大小、UTF-8和SHA。默认逻辑document_id由Assignment与标题确定；同一标题视为同一份资料。需要同名独立资料或更名保留历史时，显式传`--document-id`。

同一document_id下，相同原文SHA、标题、类型、许可和chunker版本的导入幂等，不重复建立索引。正文或元数据变化产生新source_version；旧版本标为superseded，内容不被覆盖。重放旧输入只返回其旧状态，不会将superseded/revoked重新激活。幂等不是损坏对象修复；检索发现对象缺失或hash不符会阻断，不能静默重上传覆盖历史。

导入先上传独立UUID对象，再在短PostgreSQL事务锁定Assignment、分配版本并整体登记Chunk。唯一约束保护版本、输入摘要和同一逻辑文档的唯一active版本。已确认回滚或并发输家对象尽力清理；COMMIT结果未知时保留对象，一次空查询不足以证明未提交。进程崩溃、上传ACK丢失、取消或清理失败仍可能留下孤立对象，当前没有GC。后续清理必须按保留期、索引和提交证据对账，不能看到一次空查询就删除。

存储适配器每实例只允许一个upload_path正在执行；多个CLI进程使用独立适配器。共享实例忙时安全返回存储不可用，不会登记虚假成功；没有无限等待或自动重试。同步上传线程不因asyncio取消立即终止，复用的upload_path在线程内持有文件句柄，取消后不登记来源；迟到对象需后续对账。

## 版本历史与撤销

从导入回执取实际document_id：

```bash
read -r DOCUMENT_ID
uv run python -m huipi_cloud.modules.knowledge history \
  --assignment-id "$ASSIGNMENT_ID" --document-id "$DOCUMENT_ID"
uv run python -m huipi_cloud.modules.knowledge revoke \
  --assignment-id "$ASSIGNMENT_ID" --document-id "$DOCUMENT_ID"
```

撤销将该逻辑文档现有版本置为revoked并记录UTC时间，不删除S3原文或Chunk，保留审计依据。之后显式导入不同的新输入可创建新版本；重放被撤销输入不能复活它。history最多100条并明确truncated。旧引用可按document_id/source_version/chunk_id/SHA审计，但不能在当前回答中重新作为有效证据。

## 分块与检索

分块版本`paragraph-unicode-v1`：每块最多400个Unicode码点，优先在后半部分寻找空行/标题边界，其次换行，无法满足时按字符截断；相邻块overlap固定48字符。保留原文，不归一化换行或删改正文。text_start/text_end为原始解码文本的半开区间，不是UTF-8字节偏移；content_pointer固定`/text`。Chunk ID由来源版本、顺序、范围和文本SHA确定。

当前是`unicode-weighted-coverage-v1`词法基线，不是BM25：仅在匹配阶段做NFKC/casefold；中文单字权重1、相邻二元组权重3、英文/数字词权重3。分数为查询中被命中的唯一词项权重之和除以查询总权重。分数不是教学评分、概率或语义可信度。按分数降序、document_id、chunk_index排序，结果可复现。

数据库查询强制Assignment和active/许可过滤，最多扫描4096个当前Chunk；每作用域最多32份active文档，导入事务检查容量。超额明确拒绝，不静默漏扫。top_k最多5；工具返回还受字节预算限制，truncated说明未返回全部候选。查询是有界普通文本，不接收SQL、表名、URL或模型指定作用域。

返回原文前校验S3桶、完整原文大小/SHA和Chunk字符范围/SHA；正文损坏或缺失不会被当成空结果。S3读取使用有界流并显式关闭。数据库短READ ONLY事务在对象I/O前结束，源读取后再检查当前版本状态，防止读取期间撤销仍通过。

## Agent与引用

新工具`search_assignment_materials`只有query和top_k两个参数。作用域绑定BusinessTools。沿用[Copilot预算](teacher-copilot.md)，不会把大量材料无限加入上下文。

```bash
uv run python -m huipi_cloud.modules.copilot \
  --assignment-id "$ASSIGNMENT_ID" --question '请根据本作业资料解释热传导，并引用来源。'
```

需要提前单独配置Copilot云端Key与外传许可；或显式使用`--use-grading-key`。所有生成与规划仍为云端DeepSeek，关闭Thinking，零自动重试。

工具返回citation_id、逻辑文档ID、标题、版本、Chunk ID、字符范围、正文片段、两个SHA、相关性分数及截断标记，不返回bucket或对象Key。模型回答使用`[[K加32位原样ID]]`。服务拒绝本轮未出现的ID、畸形引用、有命中却没有引用的回答；回答前再检查源状态、版本、范围和S3内容。`citations`只输出最终实际引用的片段。

无命中时由服务端直接返回证据不足，不再付费请求模型编答案；本轮任一检索无命中采取保守结束策略，即使此前另一查询有命中。后续多查询部分回答需另行设计。工具故障沿用错误解释且禁用工具策略，不自动重试。引用只证明身份、范围与版本可追溯，不证明自然语言推断或教学解释正确；模型仍可能被不可信正文影响。

检查发生在有限的读取时点，不保证返回后永远有效；没有跨PostgreSQL/S3的原子读取或对象锁。应按引用记录的版本审计，需要最新结论时重新查询。存储管理员绕过服务修改对象会被后续SHA检查发现，但当前未配置S3 Object Lock。

## 验证与扩展

```bash
uv run pytest -q tests/unit/test_knowledge_retrieval.py tests/integration/test_scoped_retrieval.py
RUN_RETRIEVAL_E2E=1 RETRIEVAL_SYNTHETIC_TRANSFER_AUTHORIZED=1 \
  uv run pytest -q tests/integration/test_retrieval_cloud_e2e.py
```

默认CI只用Fake Provider，真实PG/S3；收费E2E显式opt-in且只导入合成文本，不依赖MinerU。真实结果与局限见[检索验收记录](../evaluation/scoped-retrieval.md)。

`Retriever.search(assignment_id, query, top_k)`与`validate_citations`分开，P4-B2可替换召回/排序实现；Chunk的版本、原文范围和SHA保持为来源合同。未来Embedding与Rerank可以本地运行，但不得让向量查询或模型自行决定权限。密集/稀疏融合、Recall@K/MRR、真实资料质量、授权HTTP和教师身份均未实现。
