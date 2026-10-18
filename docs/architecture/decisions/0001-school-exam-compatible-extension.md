# ADR-0001：先兼容扩展考试领域，再决定抽取共享题库

日期：2026-10-18。状态：**建议，待独立审查**。依据：[需求 0、12、17 节](../../product/requirements/school-exam-baseline-v1.md)、[仓库审计](../../product/business-capability-audit.md)。不表示已批准迁移。

## 问题

现有 Question 强依赖 Assignment；解析、对齐、人工证据、评分输入和审核都引用旧 UUID。Submission 对同作业 student_ref 唯一，ParsedArtifact 对 Submission 唯一。把这些直接改成考试字段会破坏来源和幂等；按班克隆 Assignment 又破坏共享试卷。

## 方案比较

| 维度 | A 最小兼容扩展 | B 立即抽取独立 PaperQuestion/PaperVersion |
|---|---|---|
| 一致性 | 冻结 PaperVersion 绑定一个 published Assignment；题目映射有真实 FK；新答卷版本桥接旧提交 | 新试卷题目成为权威，旧 Question 需要映射/双写或全量迁移，必须解决并行编辑 |
| 兼容 | 保留旧 UUID/文件/协议/唯一键；考试资源单独授权 | 所有 GradeInput、Review 和来源摘要仍须保留旧身份，不能批量重写 |
| 成本 | 新考试/扫描域 + 安全适配，仍需实作版本和匿名防绕过 | 同时迁题库、考试、权限、前端与解析，不利小切片验收 |
| 迁移风险 | 增量新表、无历史猜测回填；双重语义须有明确标识 | 大量已有 FK/不可变触发器/历史 JSON 引用，破坏面高 |
| 长期扩展 | 受旧题号整数/作业内容结构约束；需显式 stable question key | 原生题库、复用、选做和复杂题组更好，但不是扫描闭环前置 |
| 测试影响 | 旧回归原样保留；新增桥接隔离、版本及幂等测试 | 几乎全部来源/评分回归必须增加双身份映射路径 |

## 推荐决策

采用 A。不是用 Assignment 冒充考试：ExamSession 仍是独立实体；**一个 PaperVersion 唯一绑定一个冻结 Assignment 内容载体**，多个 Session、多班共享它，不按班/学生/场次克隆。更换内容或 Rubric 才创建新的 PaperVersion 和必要的新内容载体；旧版本保持不变。

- 新 `PaperQuestionMapping` 用版本内稳定 UUID 对应现有 Question.id。A/B 卷另有明确版次与题序映射，同题关系必须人工确认，不能靠相同题号或题干相似度。
- 现有已 published Assignment 可作为经过人工确认的导入来源，但不得把它的旧提交自动解释成考试答卷。**首个 B 闭环禁止把含 legacy 提交或现存 legacy 成员的 Assignment 直接改成考试受保护载体**；导入时创建一次专用内容快照载体，保存 source_assignment_id/原题映射/摘要谱系，原 UUID、成员和提交不变。该快照按内容版本一次，不按班/场次复制；没有旧访问者的专用考试载体才可直接绑定。
- 每个已确认 AnswerScriptVersion 对应**新** Submission。`student_ref` 是服务端保留前缀 + 版本 UUID 的不透明处理标识，不是考生 ID，更不是授权依据。现有唯一键无需拆除；实际参试关系只存扫描/考试域。
- 该内部提交注册需要新的短事务适配方法；不能调用会自行 COMMIT 的现有 `create_submission` 后再补写关联，以免半成品。
- 同事务写桥接、文件、任务；未经桥接/隐私隔离不能把新考试资源交给旧 HTTP。旧敏感 API、列表、Copilot/检索必须识别考试载体并默认拒绝，而非继承 admin 全局权限。
- 未来匿名 MarkingRecord 与 GradeReview 独立。可以引用已有 GradeProposal/GradeReview 作为来源，但不得改写或自动升格旧历史。

## 代价和退出条件

A 不是永久题库方案。复杂子题/选做、跨版本题库复用、共享规则编辑成为已验证刚需时，另立 ADR 抽取 PaperQuestion。先建立不可变 question_key 映射，再双读校验；不回写历史 JSON/UUID。移除兼容层须证明全部旧读取/回放仍可用，并先迁权而非先删表。

## 拒绝的捷径

取消提交唯一键；按班克隆题目；修改已应用迁移；令 scanner 拥有 admin；把身份未核实页组写成学生提交；沿用整卷会员授权实现匿名；把 accepted GradeReview 当成绩发布，均不接受。

## 验证门槛

第一条 B 闭环必须证明：2 班同卷、2 场同卷不串人；不同答卷修订独立 Submission；旧文件/成绩引用不变；未知 COMMIT 重放只产生一份桥接；旧列表/下载不能越权；新增迁移在独立库往返并保全历史。详见[路线](../../development/business-reconstruction-roadmap.md)。
