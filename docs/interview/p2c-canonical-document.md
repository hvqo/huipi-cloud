# P2-C 面试复盘：Canonical Document

本轮内容处于 `feat/p2c-canonical-document` 功能分支，是否合并以 PR 与 CI 状态为准。Canonical Document 只稳定文档结构，不做题目识别、答案对齐或批改。

## 1. 为什么不直接把 MinerU 输出作为业务数据？

**简明版**：MinerU 输出服务于解析器。业务需要稳定的版本化结构。

**30 秒口语版**：MinerU 的输出格式和内部字段会随解析器版本变化。作业、检索和复核模块不应直接依赖这些字段。我把通过 P2-B 验证的 MiddleJson 转为自己的 Canonical Document 协议。原始解析产物仍保留，所以后续可以审计或重跑转换。

**原理与边界**：Canonical v1 有自己的 schema version，并单独记录来源解析器、解析器版本、来源 schema、Submission、ParsedArtifact 和 SHA-256。转换不调用模型、不改写或摘要文本。未知 MinerU Block 类型会明确失败，不会静默丢弃。MiddleJson、Markdown、StructuredContent 和 ZIP 继续留在原 S3 Key。

**代码与验证**：`modules/canonical_documents/normalizer.py::normalize_middle_json`；验证 `tests/unit/test_canonical_normalizer.py::test_unknown_or_malformed_nested_types_fail_closed` 与 `tests/integration/test_mineru_e2e.py::test_real_mineru_worker_persists_and_serves_parsed_outputs`。本机实际执行 MinerU E2E 需要配置独立 CLI 和模型目录。

## 2. Canonical Document 是什么？

**简明版**：Canonical Document 是解析器输出和业务模块之间的稳定文档协议。

**30 秒口语版**：它不是一份新的 OCR 结果，而是把已有解析结果整理成固定的 Document、Page 和 Block 结构。每个业务字段都保留来源信息。上层可以逐页读取，不需要直接理解 MinerU 的 JSON。

**原理与边界**：协议名为 `huipi.canonical.document`，版本为 `1.0`。文档记录解析器名称、版本、源 artifact ID、源文件 SHA-256 和 MiddleJson SHA-256。协议 DTO 与 MinerU Python 类无关；转换器只负责解析 MinerU 4.x MiddleJson 2.0。

**代码与验证**：`modules/canonical_documents/protocol.py::CanonicalDocument`；验证 `tests/unit/test_canonical_normalizer.py::test_single_page_text_and_normalized_bbox_are_preserved`。

## 3. Document、Page 和 Block 分别表示什么？

**简明版**：Document 保存来源和页集合。Page 保存页码和块。Block 保存页面内容与原始定位。

**30 秒口语版**：Document 对应一次已解析的提交来源。Page 保留从零开始的来源页索引，也提供从一开始的展示页码。Block 表示 MinerU 页中的顶层内容块，例如文字、公式、表格或图片。嵌套 Span 留在父 Block 的内容树中。MinerU 4.0.10 实际样本的根级 producer 和 document metadata 会保留；当前样本的 Page 只有 `page_idx` 和 `blocks`，若后续版本添加其他页字段，转换器也会保存在 Page 的 `source_fields`。

**原理与边界**：每个顶层 Block 有 UUIDv5、原始 Block index 和文档级连续 `reading_order`。页面数组与块数组保持输入顺序；系统不按坐标重排，也不删除同位置块。Block、Page 的源字段经过敏感存储定位字段过滤；MinerU 结构 PDF 中标题的 `level` 会保存在标题节点的 `source_fields`。`block_count` 只计算 Page 下的顶层 CanonicalBlock。

**代码与验证**：`modules/canonical_documents/protocol.py::CanonicalPage`、`CanonicalBlock`；验证 `tests/unit/test_canonical_normalizer.py::test_mult_page_order_and_ids_are_deterministic_across_pages`。

## 4. 如何保留 OCR 文本、公式、表格和图片？

**简明版**：文本节点保留原文。公式保持独立。表格保留 HTML 或嵌套结构。图片引用 manifest。

**30 秒口语版**：我不会把公式拼成普通文本。内联公式仍是单独的 formula 节点，并按原顺序留在文本块下面。表格保留 MinerU 的原始 HTML 或嵌套内容。图片通过 manifest 的路径和摘要映射到逻辑 asset id。图片对象的 S3 Key 不进入 Canonical JSON。

**原理与边界**：`text`、`equation_inline`、`equation`、`table`、`image`、`chart`、`code`、`list` 和已知布局类型均有映射。未知类型失败。HTTP(S) 图片只保存引用，不发网络请求。Canonical 的 `assets` 保留完整合法 manifest，包括没有被 Block 引用的素材；Block 只引用自己实际使用的素材。data URI 和 `image_base64` 不嵌入 Canonical JSON；引用保留 MiddleJson JSON Pointer、编码类型、媒体类型、解码后的字节数和 SHA-256。调用 `restore_inline_asset` 时，系统先校验 MiddleJson SHA-256，再按 Pointer 解码并校验字节数、媒体类型和图片 SHA-256。当前内嵌图片上限为 16 MiB；没有独立的公网素材恢复 API。

**代码与验证**：`modules/canonical_documents/normalizer.py::_build_content_node`、`_AssetResolver.resolve`、`restore_inline_asset`；验证 `tests/unit/test_canonical_normalizer.py::test_real_mineru_4010_structure_preserves_formula_table_image_and_full_manifest`、`test_inline_image_is_traceable_and_recoverable_from_verified_middle_json`、`test_text_and_inline_formula_are_preserved_as_ordered_typed_nodes` 和 `test_table_html_is_preserved_without_flattening`。真实 MinerU E2E 的结构 PDF 还验证公式、表格 HTML 和图片素材引用。

## 5. 为什么保留原始页码和阅读顺序？

**简明版**：页码和顺序让业务内容能回到原文位置。

**30 秒口语版**：教师复核需要知道内容来自哪一页、哪个原始块。标准化层保持 MinerU 的数组顺序，并保留原始 page index 和 Block index。展示页码另外加一，避免把接口页码和解析器索引混为一谈。

**原理与边界**：`page_index` 和 `source_page_idx` 从 0 开始，`page_number` 从 1 开始。`reading_order` 从文档开始连续递增。不同页面同为 index 0 的 Block 仍得到不同 UUID。转换不做基于坐标的重排，因为当前没有定义可靠的多栏版面排序规则。

**代码与验证**：`modules/canonical_documents/normalizer.py::normalize_middle_json`；验证 `tests/unit/test_canonical_normalizer.py::test_mult_page_order_and_ids_are_deterministic_across_pages`。

## 6. 什么是 BBox？为什么需要坐标？

**简明版**：BBox 是内容块的边界框。它帮助后续功能定位页面区域。

**30 秒口语版**：BBox 描述 MinerU 给出的块坐标。本轮沿用 P2-B 合同中的四个 0 到 1 数值，明确标为归一化页比例。它不是 PDF 像素，也不是物理单位。源数据缺少坐标时，我保持 null，不猜位置。

**原理与边界**：本轮保存 `[x0,y0,x1,y1]` 的源顺序，并检查坐标范围和框宽高为正。没有从 PDF 媒体框推导比例，也不执行坐标变换。具体轴方向依赖 MinerU 源合同；面试时不能声称这是像素或毫米。

**代码与验证**：`modules/canonical_documents/normalizer.py::_parse_bbox`、`protocol.py::CanonicalDocument.bbox_coordinate_space`；验证 `tests/unit/test_canonical_normalizer.py::test_missing_bbox_stays_null_and_coordinates_are_not_rescaled` 和 `test_invalid_bbox_is_rejected`。

## 7. 为什么保留 MiddleJson，不直接覆盖？

**简明版**：MiddleJson 是原始解析证据。Canonical 是业务使用视图。

**30 秒口语版**：转换代码可能升级，或者当前映射可能有缺陷。如果只保存 Canonical，就无法核对源字段，也无法用新版转换器重跑。我保留 P2-B 原始产物，并把 Canonical JSON 作为单独版本化产物保存。

**原理与边界**：ParsedArtifact 的 SHA-256 和对象 Key 不修改。规范化只接受成功的 P2-B 任务，不改写解析状态。`normalizer_version` 和 `canonical_schema_version` 都进入索引唯一键/产物元数据，后续可按版本重跑。没有实现历史版本迁移或原始对象清理。

**代码与验证**：`modules/canonical_documents/service.py::normalize_submission`、`repository.py::register_success`；验证 `tests/integration/test_canonical_documents.py::test_middle_json_checksum_failure_is_recorded_without_changing_parser_success`。

## 8. 什么是确定性转换与幂等？

**简明版**：确定性是同一输入产生同一 JSON。幂等是重复执行只留下一个有效索引。

**30 秒口语版**：文档、Block 和 asset 使用 UUIDv5，不把执行时间写入 Canonical JSON。序列化固定排序和分隔符。重复或并发运行可能上传多个不可变对象，但 PostgreSQL 用来源 artifact 和 normalizer 版本的唯一约束决定唯一有效索引，所以我称数据库登记幂等，不称跨系统 Exactly Once。

**原理与边界**：成功与失败登记都以确定性的 `CanonicalArtifact.id` 作为 PostgreSQL UPSERT 冲突目标；`(parsed_artifact_id, normalizer_version)` 唯一约束仍负责保护数据不变量。若两个同输入请求并发插入，PostgreSQL 即使先遇到主键冲突也会进入幂等处理；已有成功结果不被失败运行降级。数据库提交失败或并发 loser 可能留下 S3 孤儿对象，当前没有对账清理任务。相同 MiddleJson bytes、manifest 和 source metadata 在同一版本下产生同一 canonical bytes。迟到失败用例以事件同步顺序：先让成功索引提交，再释放失败登记，并确认状态仍为 `available`。

**代码与验证**：`normalizer.py::normalize_middle_json`、`repository.py::register_success`、`register_failure`；验证 `tests/unit/test_canonical_normalizer.py::test_mult_page_order_and_ids_are_deterministic_across_pages`、以同步屏障并发上传的 `tests/integration/test_canonical_documents.py::test_concurrent_normalization_has_one_effective_database_index` 和 `test_late_failure_cannot_replace_a_concurrent_success`。

## 9. 为什么 Canonical JSON 放 S3，元数据放 PostgreSQL？

**简明版**：正文是对象。状态、关系和查询索引是关系数据。

**30 秒口语版**：Canonical JSON 可能较大，而且与 PDF、Markdown 和图片素材一样属于文档产物，所以放在私有 S3。PostgreSQL 只存 submission、source artifact、版本、bucket、对象定位、SHA-256、页数和 Block 数。查询状态时不必下载整个 JSON。

**原理与边界**：上传使用每次执行独立不可变 Key。随后 PostgreSQL 以 `(parsed_artifact_id, normalizer_version)` 唯一约束登记成功记录。两个系统没有共同事务；若 DB 写入失败，安全策略是不删除对象，因此可能出现孤儿对象。对象按 SHA-256 和数据库记录的字节数校验后才供页面 API 使用。

**代码与验证**：`modules/canonical_documents/service.py::normalize_submission`、`get_page`；`models.py::CanonicalArtifact`；验证 `tests/integration/test_canonical_documents.py::test_database_failure_after_object_upload_leaves_no_index_and_keeps_immutable_object`。

## 10. 页面 API 如何限制完整 Canonical JSON 的读取资源？

**简明版**：先用索引大小拒绝超限对象，再分块读取并校验内容。解析器还有内存开销，所以部署仍需限制并发。

**30 秒口语版**：按页接口目前会读入整个 Canonical JSON，因为要验证整份文档与索引一致，再返回其中一页。默认文档上限是 32 MiB，配置最大不能超过 64 MiB。代码先检查 PostgreSQL 中登记的大小，再按 64 KiB 分块读取到 bytearray，核对实际大小和 SHA-256，之后校验 Pydantic 结构。无效或过深 JSON 返回安全的 503，不泄露对象 Key。

**原理与边界**：字节上限能限制输入，但 Pydantic 会构造完整对象，峰值 RSS 会高于 JSON 大小；多个并发请求也会叠加。当前没有全局并发 semaphore，也没有分页式对象格式。需通过受控部署限制 API 并发并监控进程 RSS，后续可评估按页索引或流式 JSON 格式。

**代码与验证**：`core/config.py::Settings.canonical_max_document_bytes`、`modules/canonical_documents/service.py::_read_checked_object`、`get_page`；验证 `tests/unit/test_canonical_normalizer.py::test_page_api_document_limit_has_a_hard_upper_bound`、`tests/integration/test_canonical_documents.py::test_page_api_rejects_oversized_index_before_downloading`、`test_page_api_maps_object_stream_failure_to_safe_503` 和 `test_malformed_canonical_json_with_matching_index_is_a_safe_503`。

## 11. 后续如何支持题目识别、答案对齐和人工复核？

**简明版**：后续领域模块引用 Canonical 的 Block ID 和来源定位。

**30 秒口语版**：题目识别可以在 Canonical Block 上建立题目片段索引；答案对齐可以关联题目、答案和评分细则；人工复核可以通过 Block ID 回到提交、解析产物、页码、原始 Block index 和 BBox。当前 P2-C 只提供稳定的输入结构，没有实现这些业务功能。

**原理与边界**：未来需要定义题目切分与跨块引用、识别置信度、教师更正版本、审计记录、权限校验和 Canonical schema 升级策略。Canonical 的外部图片目前只有逻辑引用，没有安全的图片读取 API；学生身份和租户隔离也未实现。

**代码与验证**：来源字段位于 `protocol.py::CanonicalDocument`、`CanonicalPage`、`CanonicalBlock`；API 为 `router.py::get_canonical_document_page`。当前演示可运行 `uv run pytest -q tests/unit/test_canonical_normalizer.py tests/integration/test_canonical_documents.py`，真实 MinerU 样本再运行 `MINERU_E2E=1 ... uv run pytest -q -m mineru_e2e`。
