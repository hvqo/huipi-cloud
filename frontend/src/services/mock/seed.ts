import type { S } from '../types'
import type {
  DemoAnswerKey,
  DemoAssignment,
  DemoCanonical,
  DemoConversation,
  DemoKnowledgeDoc,
  DemoMembership,
  DemoOpsLog,
  DemoProposal,
  DemoQuestion,
  DemoReviewRecord,
  DemoRubricCriterion,
  DemoState,
  DemoSubmission,
  DemoUser,
} from './state'
import { buildCanonical, type ScriptEntry } from './canonical'

/**
 * 演示环境种子数据。
 *
 * 全部为合成资料：学生使用编号标识，教师姓名仅为演示展示；
 * 作业、题目、提交、Canonical、人工核对、候选评分、授权与审计之间的关系保持一致。
 */

const NOW = Date.now()
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString()
const agoD = (days: number, hour = 10, minute = 0) => {
  const date = new Date(NOW - days * 86_400_000)
  date.setHours(hour, minute, 0, 0)
  return date.toISOString()
}

function fakeSha(seed: string): string {
  let out = ''
  let h = 0x811c9dc5
  for (let i = 0; i < 64; i++) {
    h ^= seed.charCodeAt(i % seed.length) + i
    h = Math.imul(h, 0x01000193) >>> 0
    out += ((h >>> 24) & 15).toString(16)
  }
  return out
}

const CODE_POINTS = (text: string) => Array.from(text).length

/* ---------------- 题目脚本 ---------------- */

const ASG1_STEMS = [
  '1. 函数 f(x) = x³ − 3x + 1 在 x = 1 处的导数值为（ ）\nA. −3　B. 0　C. 3　D. 1',
  '2. 曲线 y = x³ − 3x + 1 在点 (1, −1) 处的切线方程为________。',
  '3.（12 分）已知函数 f(x) = x³ − 3x + 1，求曲线 y = f(x) 在点 (1, f(1)) 处的切线方程。',
  '4.（12 分）求函数 f(x) = x³ − 3x + 1 的单调递增区间。',
  '5.（14 分）求函数 f(x) = x³ − 3x + 1 的极大值与极小值。',
  '6.（12 分）证明：当 x > 1 时，x³ − 3x + 1 > −1。',
]
const ASG1_ANSWERS = [
  '学生作答：选 B',
  '学生作答：y = −1',
  '学生作答：解：f(1) = 1 − 3 + 1 = −1，所以切点为 (1, −1)。f′(x) = 3x² − 3，则 f′(1) = 0，所以切线方程为 y = −1，即 y + 1 = 0。',
  '学生作答：解：f′(x) = 3x² − 3，令 f′(x) > 0，得 x < −1 或 x > 1，所以单调递增区间为 (−∞, −1) 与 (1, +∞)。',
  '学生作答：解：f′(x) = 3x² − 3 = 0，得 x = ±1。f(−1) = 3 为极大值，f(1) = −1 为极小值。',
  '学生作答：证明：设 g(x) = f(x) + 1 = x³ − 3x + 2，g(1) = 0，g′(x) = 3x² − 3 > 0（x > 1），故 g 在 (1, +∞) 递增，g(x) > g(1) = 0，证毕。',
]
const ASG2_STEMS = [
  '1. sin 30° + cos 60° = ________。',
  '2.（10 分）求函数 y = 2sin(x + π/6) 的最小正周期与振幅。',
  '3.（12 分）已知 sin α = 3/5，α ∈ (π/2, π)，求 cos 2α。',
  '4.（12 分）画出 y = sin 2x 在 [0, π] 上的示意图，并写出单调递减区间。',
]
const ASG3_STEMS = [
  '1. 牛顿第一定律表明，物体在不受外力时保持（ ）\nA. 匀速直线运动或静止　B. 匀加速运动　C. 圆周运动　D. 减速运动',
  '2. 质量为 2 kg 的物体受到 6 N 的水平恒力，加速度为________ m/s²。',
  '3.（12 分）一物体以 4 m/s 初速度在摩擦力作用下匀减速滑行，加速度大小 1 m/s²，求 3 s 内的位移。',
  '4.（12 分）解释超重与失重现象，并举一个生活中的例子。',
  '5.（10 分）两名同学拔河时绳的张力处处相等吗？说明理由。',
]

interface Seeded {
  users: DemoUser[]
  assignments: DemoAssignment[]
  questions: Record<string, DemoQuestion>
  rubrics: Record<string, DemoRubricCriterion[]>
  answerKeys: Record<string, DemoAnswerKey>
  submissions: DemoSubmission[]
  canonicals: Record<string, DemoCanonical>
  reviews: Record<string, DemoReviewRecord>
  proposals: DemoProposal[]
  memberships: DemoMembership[]
  audit: S['AdminAuditSummary'][]
  knowledge: DemoKnowledgeDoc[]
  conversations: DemoConversation[]
  ops: DemoOpsLog[]
}

function buildSeed(): Seeded {
  /* ---- 用户 ---- */
  const users: DemoUser[] = [
    {
      id: 'u-teacher-li',
      name: '李文静',
      role: 'teacher',
      is_active: true,
      subject: '数学',
      created_at: agoD(60, 9),
    },
    {
      id: 'u-teacher-wang',
      name: '王志强',
      role: 'teacher',
      is_active: true,
      subject: '物理',
      created_at: agoD(58, 9),
    },
    {
      id: 'u-teacher-chen',
      name: '陈晓',
      role: 'teacher',
      is_active: false,
      subject: '数学',
      created_at: agoD(40, 14),
    },
    {
      id: 'u-teacher-zhao',
      name: '赵敏',
      role: 'teacher',
      is_active: true,
      subject: '英语',
      created_at: agoD(12, 11),
    },
    {
      id: 'u-admin-zhou',
      name: '周明远',
      role: 'admin',
      is_active: true,
      subject: '教务',
      created_at: agoD(90, 9),
    },
  ]

  /* ---- 作业与题目 ---- */
  const assignments: DemoAssignment[] = [
    {
      id: 'asg-1',
      owner_id: 'u-teacher-li',
      title: '导数及其应用 · 课后练习',
      subject: '数学',
      grade_level: '高二（3）班',
      description: '围绕导数几何意义与单调性、极值的综合练习，共 6 题。',
      status: 'published',
      created_at: agoD(14, 9),
      updated_at: ago(120),
      question_ids: [],
    },
    {
      id: 'asg-2',
      owner_id: 'u-teacher-li',
      title: '三角函数 · 单元测验',
      subject: '数学',
      grade_level: '高二（3）班',
      description: '三角恒等变换与图象性质测验。',
      status: 'published',
      created_at: agoD(9, 15),
      updated_at: agoD(2, 18),
      question_ids: [],
    },
    {
      id: 'asg-3',
      owner_id: 'u-teacher-wang',
      title: '牛顿运动定律 · 随堂作业',
      subject: '物理',
      grade_level: '高一（7）班',
      description: '牛顿三定律基础应用。',
      status: 'published',
      created_at: agoD(6, 8),
      updated_at: agoD(3, 10),
      question_ids: [],
    },
    {
      id: 'asg-4',
      owner_id: 'u-teacher-li',
      title: '数列 · 复习讲义',
      subject: '数学',
      grade_level: '高二（5）班',
      description: '等差等比数列复习（草稿，待补全评分细则）。',
      status: 'draft',
      created_at: agoD(5, 20),
      updated_at: agoD(1, 21),
      question_ids: [],
    },
    {
      id: 'asg-5',
      owner_id: 'u-teacher-wang',
      title: '机械能守恒 · 课后练习',
      subject: '物理',
      grade_level: '高一（7）班',
      description: '机械能守恒定律应用练习。',
      status: 'published',
      created_at: agoD(4, 9),
      updated_at: agoD(4, 9),
      question_ids: [],
    },
  ]

  const questions: Record<string, DemoQuestion> = {}
  const rubrics: Record<string, DemoRubricCriterion[]> = {}
  const answerKeys: Record<string, DemoAnswerKey> = {}

  function addQuestion(
    assignmentId: string,
    number: number,
    type: S['QuestionType'],
    stem: string,
    maxScore: string,
    criteria: [string, string][],
    answer: string,
  ) {
    const id = `q-${assignmentId.slice(4)}-${number}`
    questions[id] = {
      id,
      assignment_id: assignmentId,
      question_number: number,
      question_type: type,
      stem,
      max_score: maxScore,
      created_at: assignments.find((a) => a.id === assignmentId)!.created_at,
      updated_at: assignments.find((a) => a.id === assignmentId)!.updated_at,
    }
    rubrics[id] = criteria.map(([description, points], index) => ({
      id: `crit-${id}-${index + 1}`,
      question_id: id,
      description,
      points: points === '满分' ? maxScore : points,
      sort_order: index,
    }))
    answerKeys[id] = { content: answer, updated_at: questions[id].created_at }
    assignments.find((a) => a.id === assignmentId)!.question_ids.push(id)
    return id
  }

  const asg1q = ASG1_STEMS.map((stem, index) => {
    const type: S['QuestionType'] =
      index === 0 ? 'single_choice' : index === 1 ? 'fill_blank' : 'essay'
    return addQuestion(
      'asg-1',
      index + 1,
      type,
      stem,
      ['5.00', '5.00', '12.00', '12.00', '14.00', '12.00'][index]!,
      index === 2
        ? [
            ['正确求出切点坐标', '4.00'],
            ['正确求出导数与斜率', '4.00'],
            ['写出切线方程', '4.00'],
          ]
        : [['结果与过程正确', '满分']],
      ASG1_ANSWERS[index]!.replace('学生作答：', '').split('。')[0] + '。',
    )
  })
  const asg2q = ASG2_STEMS.map((stem, index) => {
    const max = ['6.00', '10.00', '12.00', '12.00'][index]!
    const criteria = [
      [
        ['结果与结论正确', '3.60'],
        ['过程与依据完整', '2.40'],
      ],
      [
        ['结果正确', '6.00'],
        ['过程完整', '4.00'],
      ],
      [
        ['公式运用正确', '7.20'],
        ['计算准确', '4.80'],
      ],
      [
        ['图象正确', '6.00'],
        ['区间表述规范', '6.00'],
      ],
    ][index] as [string, string][]
    return addQuestion(
      'asg-2',
      index + 1,
      index === 0 ? 'fill_blank' : index === 3 ? 'essay' : 'short_answer',
      stem,
      max,
      criteria,
      '参考答案：略（见讲义第 3 章）。',
    )
  })
  const asg3q = ASG3_STEMS.map((stem, index) => {
    const criteria: [string, string][] =
      index <= 1
        ? [['结果正确', ['5.00', '5.00'][index]!]]
        : [
            ['概念理解正确', '6.00'],
            ['推理与表达', '6.00'],
          ]
    return addQuestion(
      'asg-3',
      index + 1,
      index === 0 ? 'single_choice' : index === 1 ? 'fill_blank' : 'short_answer',
      stem,
      ['5.00', '5.00', '12.00', '12.00', '10.00'][index]!,
      criteria,
      '参考答案：A；3；6 m；略；略。',
    )
  })
  addQuestion(
    'asg-4',
    1,
    'short_answer',
    '1.（10 分）已知等差数列 {aₙ} 满足 a₁ = 2，a₃ = 8，求公差 d 与 a₁₀。',
    '10.00',
    [
      ['公式运用正确', '5.00'],
      ['计算准确', '5.00'],
    ],
    'd = 3，a₁₀ = 29。',
  )
  addQuestion(
    'asg-4',
    2,
    'essay',
    '2.（12 分）已知等比数列 {bₙ} 的首项为 1，公比为 2，求前 10 项和 S₁₀。',
    '12.00',
    [
      ['求和公式正确', '6.00'],
      ['计算准确', '6.00'],
    ],
    'S₁₀ = 1023。',
  )
  addQuestion(
    'asg-4',
    3,
    'essay',
    '3.（12 分）数列 {cₙ} 满足 c₁ = 1，cₙ₊₁ = 2cₙ + 1，求通项公式。',
    '12.00',
    [],
    '',
  )
  ;[1, 2, 3, 4].map((n) =>
    addQuestion(
      'asg-5',
      n,
      'short_answer',
      `${n}.（10 分）机械能守恒定律应用第 ${n} 题。`,
      '10.00',
      [
        ['分析正确', '5.00'],
        ['计算准确', '5.00'],
      ],
      '略。',
    ),
  )

  /* ---- 提交与 Canonical ---- */
  const submissions: DemoSubmission[] = []
  const canonicals: Record<string, DemoCanonical> = {}
  const answerPointerOf: Record<
    string,
    Record<string, { pointer: string; page: number; text: string }>
  > = {}

  function addSubmission(
    id: string,
    assignmentId: string,
    studentRef: string,
    submittedAt: string,
    parseStatus: DemoSubmission['parsing']['status'],
    script?: ScriptEntry[],
  ) {
    submissions.push({
      id,
      assignment_id: assignmentId,
      student_ref: studentRef,
      file_name: `导数练习-${studentRef.slice(-3)}.png`,
      file_type: 'image/png',
      file_size: 412_300,
      submitted_at: submittedAt,
      created_at: submittedAt,
      updated_at: submittedAt,
      parsing: {
        task_id: `pt-${id}`,
        status: parseStatus,
        attempt_count: parseStatus === 'retry_wait' ? 2 : parseStatus === 'failed' ? 3 : 1,
        max_attempts: 3,
        started_at: submittedAt,
        finished_at: parseStatus === 'succeeded' ? submittedAt : null,
        next_run_at: parseStatus === 'retry_wait' ? ago(-5) : null,
        last_error_code: parseStatus === 'failed' ? 'page_layout_unrecognized' : null,
        last_error_message: parseStatus === 'failed' ? '页面版式无法识别（合成演示错误）' : null,
      },
    })
    if (script) {
      const { canonical, answerPointers } = buildCanonical(id, script)
      canonicals[id] = canonical
      answerPointerOf[id] = answerPointers
    }
  }

  const stemOf = (qid: string) => questions[qid]!.stem
  const fullScript = (qids: string[], withImage = false): ScriptEntry[] =>
    qids.map((qid, index) => {
      const asgStems = qid.startsWith('q-1-')
        ? ASG1_STEMS
        : qid.startsWith('q-2-')
          ? ASG2_STEMS
          : ASG3_STEMS
      const answers = qid.startsWith('q-1-') ? ASG1_ANSWERS : null
      return {
        qid,
        number: index + 1,
        stem: asgStems[questions[qid]!.question_number - 1] ?? stemOf(qid),
        answer: answers
          ? answers[questions[qid]!.question_number - 1]
          : '学生作答：内容完整，过程规范。',
        imageAsset: withImage && qid === 'q-1-3' ? `asset-${qid}-hand` : undefined,
      }
    })

  addSubmission('sub-1', 'asg-1', 'STU-001', ago(70), 'succeeded', fullScript(asg1q, true))
  addSubmission('sub-2', 'asg-1', 'STU-002', ago(180), 'succeeded', [
    { qid: asg1q[0]!, number: 1, stem: ASG1_STEMS[0]!, answer: '学生作答：选 C' },
    { qid: asg1q[1]!, number: 2, stem: ASG1_STEMS[1]! },
    {
      qid: asg1q[2]!,
      number: 3,
      stem: ASG1_STEMS[2]!,
      answer: '学生作答：解：f(1) = −1，所以切点是 (1, −1)。',
    },
    {
      qid: asg1q[3]!,
      number: 4,
      stem: ASG1_STEMS[3]!,
      answer: '学生作答：解：令 3x² − 3 > 0，得 x > 1，递增区间为 (1, +∞)。',
    },
    { qid: asg1q[4]!, number: 5, stem: ASG1_STEMS[4]!, answer: '学生作答：极大值 3，极小值 −1。' },
    { qid: asg1q[5]!, number: 6, stem: ASG1_STEMS[5]!, answer: '学生作答：构造函数证明，见卷面。' },
  ])
  addSubmission('sub-3', 'asg-1', 'STU-003', agoD(1, 16), 'succeeded', [
    { qid: asg1q[0]!, number: 1, stem: ASG1_STEMS[0]!, answer: '学生作答：选 A' },
    { qid: asg1q[1]!, number: 2, stem: ASG1_STEMS[1]!, answer: '学生作答：y = 2x − 3' },
    {
      qid: asg1q[2]!,
      number: 3,
      stem: ASG1_STEMS[2]!,
      answer: ASG1_ANSWERS[2]!.replace('学生作答：', '学生作答：'),
    },
    {
      qid: asg1q[3]!,
      number: 4,
      stem: ASG1_STEMS[3]!,
      answer: '学生作答：解：f′(x) = 3x² − 3，递增区间为 (−∞, −1) 和 (1, +∞)。',
    },
    { qid: asg1q[5]!, number: 6, stem: ASG1_STEMS[5]!, answer: '学生作答：用单调性定义证明。' },
  ])
  addSubmission('sub-4', 'asg-1', 'STU-004', ago(40), 'running')
  addSubmission('sub-5', 'asg-1', 'STU-005', agoD(1, 11), 'retry_wait')
  addSubmission('sub-6', 'asg-1', 'STU-006', agoD(2, 9), 'failed')
  addSubmission(
    'sub-7',
    'asg-2',
    'STU-011',
    agoD(2, 17),
    'succeeded',
    fullScript(asg2q.slice(0, 3)),
  )
  addSubmission('sub-8', 'asg-2', 'STU-012', agoD(1, 18), 'succeeded', fullScript(asg2q))
  addSubmission(
    'sub-9',
    'asg-2',
    'STU-013',
    agoD(1, 15),
    'succeeded',
    fullScript(asg2q.slice(0, 2)),
  )
  addSubmission(
    'sub-10',
    'asg-3',
    'STU-021',
    agoD(3, 9),
    'succeeded',
    fullScript(asg3q.slice(0, 3)),
  )
  addSubmission('sub-11', 'asg-3', 'STU-022', agoD(3, 10), 'pending')

  /* 对齐状态 */
  const alignedOf = (
    sid: string,
    qids: string[],
    overrides: Record<string, DemoCanonical['aligned'][string]> = {},
  ) => {
    const map: DemoCanonical['aligned'] = {}
    for (const qid of qids) {
      const answer = answerPointerOf[sid]?.[qid]
      const evidence: DemoCanonical['aligned'][string]['evidence_pointers'] = [
        // 题干块指针（题号标记）
        { pointer: stemPointerOf(sid, qid), page_number: 1, text_start: 0, text_end: 2 },
      ]
      if (answer)
        evidence.push({
          pointer: answer.pointer,
          page_number: answer.page,
          text_start: 0,
          text_end: CODE_POINTS(answer.text),
        })
      for (const page of canonicals[sid]!.pages)
        for (const [index, block] of page.page.blocks.entries()) {
          if (
            block.content.normalized_type === 'image' &&
            block.content.asset_refs?.some((a) => a.asset_id === `asset-${qid}-hand`)
          )
            evidence.push({
              pointer: `/pages/${page.page.page_index}/blocks/${index}/content`,
              page_number: page.page.page_number,
              text_start: null,
              text_end: null,
            })
        }
      map[qid] = overrides[qid] ?? { matching_status: 'aligned', evidence_pointers: evidence }
    }
    canonicals[sid]!.aligned = map
  }
  function stemPointerOf(sid: string, qid: string): string {
    const pages = canonicals[sid]!.pages
    for (const page of pages)
      for (const block of page.page.blocks)
        if (block.content.value && block.content.value.startsWith(questions[qid]!.stem.slice(0, 2)))
          return `/pages/${page.page.page_index}/blocks/${page.page.blocks.indexOf(block)}/content`
    return `/pages/0/blocks/0/content`
  }
  alignedOf('sub-1', asg1q, {
    'q-1-5': {
      matching_status: 'review_required',
      evidence_pointers: [
        { pointer: stemPointerOf('sub-1', 'q-1-5'), page_number: 2, text_start: 0, text_end: 2 },
      ],
    },
  })
  alignedOf('sub-2', asg1q)
  alignedOf('sub-3', asg1q, {
    'q-1-5': { matching_status: 'not_observed', evidence_pointers: [] },
  })
  alignedOf('sub-7', asg2q.slice(0, 3))
  alignedOf('sub-8', asg2q)
  alignedOf('sub-9', asg2q.slice(0, 2))
  alignedOf('sub-10', asg3q.slice(0, 3))

  /* ---- 人工核对历史 ---- */
  const reviews: Record<string, DemoReviewRecord> = {}
  function sourceOf(sid: string, stale = false): S['ReviewSourceVersion'] {
    const canonical = canonicals[sid]!
    return {
      canonical_document_id: canonical.document_id,
      canonical_sha256: stale ? fakeSha(`old-${sid}`) : canonical.canonical_sha256,
      alignment_artifact_id: canonical.alignment_artifact_id,
      alignment_sha256: stale ? fakeSha(`old-aln-${sid}`) : canonical.alignment_sha256,
      aligner_version: canonical.aligner_version,
      assignment_questions_digest: canonical.questions_digest,
    }
  }
  function seedReview(
    sid: string,
    qid: string,
    decisions: {
      decision: S['ReviewRequest']['decision']
      reason: string
      regions: S['ReviewRegionSelection'][]
      at: string
      reviewerKind?: S['RevisionSummary']['reviewer_kind']
    }[],
  ) {
    const regions: Record<string, S['ReviewRegionSelection'][]> = {}
    const history = decisions.map((item, index) => {
      const decisionId = `dec-${sid}-${qid}-${index + 1}`
      regions[decisionId] = item.regions
      return {
        decision_id: decisionId,
        question_id: qid,
        decision: item.decision,
        revision: index + 1,
        request_id: `req-${sid}-${qid}-${index + 1}`,
        supersedes_decision_id: index > 0 ? `dec-${sid}-${qid}-${index}` : null,
        created_at: item.at,
        reviewer_ref: 'teacher_demo',
        reviewer_kind: item.reviewerKind ?? 'authenticated_teacher',
        reviewer_user_id: item.reviewerKind === 'legacy_cli_claim' ? null : 'u-teacher-li',
        source: sourceOf(sid, qid === 'q-1-4'),
        region_counts: {
          response_regions: item.decision === 'response_present' ? item.regions.length : 0,
          excluded_prompt_regions: item.decision === 'response_absent' ? item.regions.length : 0,
          uncertain_regions: item.decision === 'uncertain' ? item.regions.length : 0,
        },
        reason_codes: [item.reason],
      } satisfies S['RevisionSummary']
    })
    reviews[`${sid}:${qid}`] = { source: sourceOf(sid), history, regions }
  }
  const fullRange = (sid: string, qid: string): S['ReviewRegionSelection'] => {
    const answer = answerPointerOf[sid]![qid]!
    return { content_pointer: answer.pointer, text_start: 0, text_end: CODE_POINTS(answer.text) }
  }
  seedReview('sub-1', 'q-1-1', [
    {
      decision: 'response_present',
      reason: 'student_work_visible',
      regions: [fullRange('sub-1', 'q-1-1')],
      at: agoD(2, 10),
    },
  ])
  seedReview('sub-1', 'q-1-2', [
    {
      decision: 'response_present',
      reason: 'student_work_visible',
      regions: [fullRange('sub-1', 'q-1-2')],
      at: agoD(2, 10),
    },
  ])
  seedReview('sub-1', 'q-1-3', [
    { decision: 'uncertain', reason: 'ambiguous_handwriting', regions: [], at: agoD(2, 14) },
    {
      decision: 'response_present',
      reason: 'student_work_visible',
      regions: [fullRange('sub-1', 'q-1-3')],
      at: ago(150),
    },
  ])
  seedReview('sub-1', 'q-1-4', [
    {
      decision: 'response_present',
      reason: 'student_work_visible',
      regions: [fullRange('sub-1', 'q-1-4')],
      at: agoD(1, 9),
    },
  ])
  seedReview('sub-2', 'q-1-1', [
    {
      decision: 'response_present',
      reason: 'student_work_visible',
      regions: [fullRange('sub-2', 'q-1-1')],
      at: ago(300),
    },
  ])
  seedReview('sub-2', 'q-1-2', [
    {
      decision: 'response_absent',
      reason: 'printed_prompt_only',
      regions: [
        {
          content_pointer: stemPointerOf('sub-2', 'q-1-2'),
          text_start: 0,
          text_end: CODE_POINTS(stemOf('q-1-2')),
        },
      ],
      at: ago(290),
    },
  ])
  seedReview('sub-3', 'q-1-1', [
    {
      decision: 'response_present',
      reason: 'student_work_visible',
      regions: [fullRange('sub-3', 'q-1-1')],
      at: ago(60),
    },
  ])
  seedReview('sub-8', 'q-2-1', [
    {
      decision: 'response_present',
      reason: 'student_work_visible',
      regions: [fullRange('sub-8', 'q-2-1')],
      at: agoD(1, 16),
    },
  ])

  /* ---- 候选评分 ---- */
  const proposals: DemoProposal[] = []
  function gradeRevision(input: {
    pid: string
    revision: number
    status: S['GradeReviewRequest']['review_status']
    proposed: string | null
    reviewed: string | null
    criteria: [string, string, string, boolean][] // [criterion_id, proposed, reviewed, changed]
    reasonCodes: S['GradeReviewRequest']['review_reason_codes']
    at: string
    reviewerKind?: S['RevisionSummary']['reviewer_kind']
  }): S['GradeRevisionSummary'] {
    return {
      review_id: `gr-${input.pid}-${input.revision}`,
      proposal_id: input.pid,
      grade_input_artifact_id: `gi-${input.pid}`,
      proposal_sha256: fakeSha(`proposal-${input.pid}`),
      grade_input_sha256: fakeSha(`grade-input-${input.pid}`),
      review_revision: input.revision,
      supersedes_review_id: input.revision > 1 ? `gr-${input.pid}-${input.revision - 1}` : null,
      reviewer_ref: 'teacher_demo',
      reviewer_user_id: input.reviewerKind === 'legacy_cli_claim' ? null : 'u-teacher-li',
      reviewer_kind: input.reviewerKind ?? 'authenticated_teacher',
      review_reason_codes: input.reasonCodes ?? [],
      review_comment_present: false,
      request_id: `greq-${input.pid}-${input.revision}`,
      review_status: input.status,
      proposed_total_score: input.proposed,
      reviewed_total_score: input.reviewed,
      criterion_scores: input.criteria.map(([criterionId, proposed, reviewed, changed]) => {
        const criterion = Object.values(rubrics)
          .flat()
          .find((c) => c.id === criterionId)
        return {
          criterion_id: criterionId,
          proposed_points: proposed,
          reviewed_points: reviewed,
          max_points: criterion?.points ?? '0.00',
          changed,
        }
      }),
      created_at: input.at,
      grade_published: false,
    }
  }
  function addProposal(input: {
    id: string
    sid: string
    qid: string
    status: DemoProposal['grading_status']
    sourceStatus: DemoProposal['source_status']
    total: string | null
    criteria: { qid: string; critIndex: number; points: string; rationale: string }[]
    at: string
    abstainReason?: string
  }) {
    const question = questions[input.qid]!
    const evidence = input.criteria.flatMap((c) => {
      const answer = answerPointerOf[input.sid]?.[input.qid]
      if (!answer) return []
      const start = Math.min(2, CODE_POINTS(answer.text))
      return [
        {
          criterion_id: rubrics[input.qid]?.[c.critIndex]?.id ?? `crit-${input.qid}-1`,
          content_pointer: answer.pointer,
          page_number: answer.page,
          text_start: start,
          text_end: Math.min(start + 12, CODE_POINTS(answer.text)),
          quote: Array.from(answer.text)
            .slice(start, start + 12)
            .join(''),
        },
      ]
    })
    proposals.push({
      id: input.id,
      submission_id: input.sid,
      question_id: input.qid,
      grading_status: input.status,
      source_status: input.sourceStatus,
      proposed_total_score: input.total,
      max_score: question.max_score,
      criteria: input.criteria.map((c) => ({
        criterion_id: rubrics[input.qid]?.[c.critIndex]?.id ?? `crit-${input.qid}-1`,
        proposed_points: c.points,
        rationale: c.rationale,
      })),
      evidence,
      abstain_reason: input.abstainReason ?? null,
      created_at: input.at,
      reviews: [],
    })
    return proposals[proposals.length - 1]!
  }

  addProposal({
    id: 'prop-1',
    sid: 'sub-1',
    qid: 'q-1-1',
    status: 'completed_candidate',
    sourceStatus: 'current',
    total: '5.00',
    criteria: [{ qid: 'q-1-1', critIndex: 0, points: '5.00', rationale: '选项判断正确' }],
    at: ago(65),
  })
  addProposal({
    id: 'prop-2',
    sid: 'sub-1',
    qid: 'q-1-2',
    status: 'completed_candidate',
    sourceStatus: 'current',
    total: '4.00',
    criteria: [
      { qid: 'q-1-2', critIndex: 0, points: '2.40', rationale: '结果正确' },
      { qid: 'q-1-2', critIndex: 1, points: '1.60', rationale: '过程完整' },
    ],
    at: ago(64),
  })
  addProposal({
    id: 'prop-3',
    sid: 'sub-1',
    qid: 'q-1-3',
    status: 'completed_candidate',
    sourceStatus: 'current',
    total: '9.50',
    criteria: [
      { qid: 'q-1-3', critIndex: 0, points: '4.00', rationale: '切点 (1, −1) 计算正确' },
      { qid: 'q-1-3', critIndex: 1, points: '3.50', rationale: '导数求得正确，斜率表述略欠规范' },
      { qid: 'q-1-3', critIndex: 2, points: '2.00', rationale: '切线方程写出，但未化简为一般式' },
    ],
    at: ago(63),
  })
  addProposal({
    id: 'prop-4',
    sid: 'sub-2',
    qid: 'q-1-1',
    status: 'grading_abstained',
    sourceStatus: 'current',
    total: null,
    criteria: [],
    at: ago(170),
    abstainReason: '作答区域含大量手写涂改，无法可靠识别（合成演示）',
  })
  const p5 = addProposal({
    id: 'prop-5',
    sid: 'sub-1',
    qid: 'q-1-4',
    status: 'completed_candidate',
    sourceStatus: 'source_stale',
    total: '11.00',
    criteria: [{ qid: 'q-1-4', critIndex: 0, points: '11.00', rationale: '区间求解完整' }],
    at: agoD(2, 12),
  })
  p5.reviews.push(
    gradeRevision({
      pid: 'prop-5',
      revision: 1,
      status: 'accepted',
      proposed: '11.00',
      reviewed: null,
      criteria: [['crit-q-1-4-1', '11.00', '11.00', false]],
      reasonCodes: [],
      at: agoD(2, 11),
    }),
  )
  const p6 = addProposal({
    id: 'prop-6',
    sid: 'sub-3',
    qid: 'q-1-3',
    status: 'completed_candidate',
    sourceStatus: 'current',
    total: '10.50',
    criteria: [
      { qid: 'q-1-3', critIndex: 0, points: '4.00', rationale: '切点正确' },
      { qid: 'q-1-3', critIndex: 1, points: '4.00', rationale: '导数与斜率正确' },
      { qid: 'q-1-3', critIndex: 2, points: '2.50', rationale: '方程正确，表达欠规范' },
    ],
    at: ago(55),
  })
  p6.reviews.push(
    gradeRevision({
      pid: 'prop-6',
      revision: 1,
      status: 'revised',
      proposed: '10.50',
      reviewed: '11.50',
      criteria: [
        ['crit-q-1-3-1', '4.00', '4.00', false],
        ['crit-q-1-3-2', '4.00', '4.00', false],
        ['crit-q-1-3-3', '2.50', '3.50', true],
      ],
      reasonCodes: ['rubric_interpretation_error'],
      at: ago(30),
    }),
  )

  /* ---- 授权与审计 ---- */
  const memberships: DemoMembership[] = [
    {
      assignment_id: 'asg-1',
      user_id: 'u-teacher-li',
      can_write: true,
      is_active: true,
      created_at: agoD(14, 9),
      revoked_at: null,
    },
    {
      assignment_id: 'asg-2',
      user_id: 'u-teacher-li',
      can_write: true,
      is_active: true,
      created_at: agoD(9, 15),
      revoked_at: null,
    },
    {
      assignment_id: 'asg-3',
      user_id: 'u-teacher-li',
      can_write: false,
      is_active: true,
      created_at: agoD(6, 9),
      revoked_at: null,
    },
    {
      assignment_id: 'asg-4',
      user_id: 'u-teacher-li',
      can_write: true,
      is_active: true,
      created_at: agoD(5, 20),
      revoked_at: null,
    },
    {
      assignment_id: 'asg-3',
      user_id: 'u-teacher-wang',
      can_write: true,
      is_active: true,
      created_at: agoD(6, 8),
      revoked_at: null,
    },
    {
      assignment_id: 'asg-5',
      user_id: 'u-teacher-wang',
      can_write: true,
      is_active: true,
      created_at: agoD(4, 9),
      revoked_at: null,
    },
    {
      assignment_id: 'asg-2',
      user_id: 'u-teacher-chen',
      can_write: true,
      is_active: false,
      created_at: agoD(8, 10),
      revoked_at: agoD(1, 15),
    },
  ]
  const audit: S['AdminAuditSummary'][] = [
    {
      operation_id: 'opid-9',
      request_id: 'req-9',
      action: 'teacher_registered',
      target_user_id: 'u-teacher-zhao',
      assignment_id: null,
      state: { is_active: true },
      created_at: agoD(12, 11),
      actor_user_id: 'u-admin-zhou',
    },
    {
      operation_id: 'opid-8',
      request_id: 'req-8',
      action: 'assignment_membership_revoked',
      target_user_id: 'u-teacher-chen',
      assignment_id: 'asg-2',
      state: { can_write: false },
      created_at: agoD(1, 15),
      actor_user_id: 'u-admin-zhou',
    },
    {
      operation_id: 'opid-7',
      request_id: 'req-7',
      action: 'teacher_activation_changed',
      target_user_id: 'u-teacher-chen',
      assignment_id: null,
      state: { is_active: false },
      created_at: agoD(1, 14),
      actor_user_id: 'u-admin-zhou',
    },
    {
      operation_id: 'opid-6',
      request_id: 'req-6',
      action: 'assignment_membership_granted',
      target_user_id: 'u-teacher-li',
      assignment_id: 'asg-3',
      state: { can_write: false },
      created_at: agoD(6, 9),
      actor_user_id: 'u-admin-zhou',
    },
    {
      operation_id: 'opid-5',
      request_id: 'req-5',
      action: 'assignment_membership_granted',
      target_user_id: 'u-teacher-wang',
      assignment_id: 'asg-5',
      state: { can_write: true },
      created_at: agoD(4, 9),
      actor_user_id: 'u-admin-zhou',
    },
    {
      operation_id: 'opid-4',
      request_id: 'req-4',
      action: 'assignment_membership_granted',
      target_user_id: 'u-teacher-li',
      assignment_id: 'asg-2',
      state: { can_write: true },
      created_at: agoD(9, 15),
      actor_user_id: 'u-admin-zhou',
    },
    {
      operation_id: 'opid-3',
      request_id: 'req-3',
      action: 'teacher_registered',
      target_user_id: 'u-teacher-chen',
      assignment_id: null,
      state: { is_active: true },
      created_at: agoD(40, 14),
      actor_user_id: 'u-admin-zhou',
    },
    {
      operation_id: 'opid-2',
      request_id: 'req-2',
      action: 'teacher_registered',
      target_user_id: 'u-teacher-wang',
      assignment_id: null,
      state: { is_active: true },
      created_at: agoD(58, 9),
      actor_user_id: 'u-admin-zhou',
    },
    {
      operation_id: 'opid-1',
      request_id: 'req-1',
      action: 'teacher_registered',
      target_user_id: 'u-teacher-li',
      assignment_id: null,
      state: { is_active: true },
      created_at: agoD(60, 9),
      actor_user_id: 'u-admin-zhou',
    },
  ]

  /* ---- 知识库 ---- */
  const knowledge: DemoKnowledgeDoc[] = [
    {
      id: 'kd-1',
      title: '导数与切线 · 教学讲义（高二数学）',
      category: '教学讲义',
      file_type: 'pdf',
      size_bytes: 820_400,
      status: 'indexed',
      chunk_count: 3,
      version: 2,
      assignment_id: 'asg-1',
      created_at: agoD(15, 10),
      chunks: [
        {
          id: 'kd-1-c1',
          text: '导数的几何意义：函数 f(x) 在 x₀ 处的导数 f′(x₀) 就是曲线 y = f(x) 在点 (x₀, f(x₀)) 处切线的斜率。求切线方程的步骤：先求切点坐标，再求导数得到斜率，最后用点斜式写出方程。',
          page_hint: '第 12 页',
        },
        {
          id: 'kd-1-c2',
          text: '常见误区：学生常把“在某点处的切线”与“过某点的切线”混淆。前者切点已知，后者需要设切点并解方程。讲解时建议配合图象，让学生标注切点与斜率。',
          page_hint: '第 14 页',
        },
        {
          id: 'kd-1-c3',
          text: '利用导数研究单调性：在区间 D 上 f′(x) > 0 则 f 递增，f′(x) < 0 则 f 递减。对于含参问题，注意分类讨论的完整性，先看定义域再看不等式解集。',
          page_hint: '第 18 页',
        },
      ],
    },
    {
      id: 'kd-2',
      title: '期中命题双向细目表（数学）',
      category: '命题资料',
      file_type: 'xlsx',
      size_bytes: 65_800,
      status: 'indexed',
      chunk_count: 2,
      version: 2,
      assignment_id: null,
      created_at: agoD(10, 16),
      chunks: [
        {
          id: 'kd-2-c1',
          text: '高二数学期中双向细目表：导数几何意义（理解）12 分；单调性与极值（掌握）20 分；三角恒等变换（掌握）18 分；综合应用（运用）15 分。',
          page_hint: 'Sheet1',
        },
        {
          id: 'kd-2-c2',
          text: '命题说明：压轴题围绕“切线 + 单调性”综合情境，重点考查数学运算与逻辑推理素养。',
          page_hint: 'Sheet2',
        },
      ],
    },
    {
      id: 'kd-3',
      title: '导数错题集 · 学生高频错误分析',
      category: '错题分析',
      file_type: 'pdf',
      size_bytes: 1_240_000,
      status: 'indexed',
      chunk_count: 3,
      version: 3,
      assignment_id: 'asg-1',
      created_at: agoD(7, 14),
      chunks: [
        {
          id: 'kd-3-c1',
          text: '高频错误 1：求切线方程时未先求 f(1)，直接把导数值当斜率并使用错误切点，导致方程整体错误。建议课堂强调“先定点、再定斜率”。',
          page_hint: '第 2 页',
        },
        {
          id: 'kd-3-c2',
          text: '高频错误 2：单调区间写成并集。单调递增区间 (−∞, −1) 和 (1, +∞) 应使用“和”连接而非并集符号。',
          page_hint: '第 5 页',
        },
        {
          id: 'kd-3-c3',
          text: '高频错误 3：极大值与最大值混淆。极值是局部概念，教学中建议画出函数图象辅助理解。',
          page_hint: '第 6 页',
        },
      ],
    },
    {
      id: 'kd-4',
      title: '切线问题解法总结（备课笔记）',
      category: '教学讲义',
      file_type: 'docx',
      size_bytes: 48_200,
      status: 'indexing',
      chunk_count: 0,
      version: 2,
      assignment_id: null,
      created_at: ago(25),
      chunks: [],
    },
  ]

  /* ---- AI 助教会话 ---- */
  const conversations: DemoConversation[] = [
    {
      id: 'conv-1',
      title: '导数切线问题的教学建议',
      updated_at: ago(95),
      messages: [
        {
          id: 'msg-1',
          role: 'user',
          content: '学生在求切线方程时经常忘记先求切点，有什么教学建议？',
          created_at: ago(100),
        },
        {
          id: 'msg-2',
          role: 'assistant',
          content:
            '根据知识库中的资料，可以尝试以下做法：\n\n1. 强调“先定点、再定斜率”的解题顺序。错题集显示这是最高频错误，建议在板书时用两种颜色分别标注切点与斜率的来源。\n2. 区分“在某点处的切线”与“过某点的切线”。讲义第 14 页的图象标注方法可以直接用于课堂演示。\n3. 结合双向细目表，这部分属于“导数几何意义（理解）”考点，建议用 1 课时做专题纠正练习。',
          created_at: ago(96),
          toolCalls: [{ name: '检索教学资料', status: 'done', summary: '命中 3 段相关资料' }],
          citations: [
            {
              index: 1,
              doc_id: 'kd-3',
              doc_title: '导数错题集 · 学生高频错误分析',
              chunk_id: 'kd-3-c1',
              quote: '高频错误 1：求切线方程时未先求 f(1)……',
            },
            {
              index: 2,
              doc_id: 'kd-1',
              doc_title: '导数与切线 · 教学讲义（高二数学）',
              chunk_id: 'kd-1-c2',
              quote: '常见误区：学生常把“在某点处的切线”与“过某点的切线”混淆……',
            },
            {
              index: 3,
              doc_id: 'kd-2',
              doc_title: '期中命题双向细目表（数学）',
              chunk_id: 'kd-2-c1',
              quote: '导数几何意义（理解）12 分……',
            },
          ],
        },
      ],
    },
    {
      id: 'conv-2',
      title: '三角函数图象性质复习思路',
      updated_at: agoD(2, 11),
      messages: [
        {
          id: 'msg-3',
          role: 'user',
          content: '下周要讲 y = A sin(ωx + φ) 的图象变换，如何安排复习节奏？',
          created_at: agoD(2, 12),
        },
        {
          id: 'msg-4',
          role: 'assistant',
          content:
            '知识库中暂未收录三角函数图象变换的专门资料，我无法在没有依据的情况下给出建议。建议先在“教学知识库”上传相关讲义，之后我可以结合资料给出有据可依的复习思路。',
          created_at: agoD(2, 11),
          toolCalls: [{ name: '检索教学资料', status: 'done', summary: '未命中相关资料' }],
        },
      ],
    },
  ]

  /* ---- 教师动态 ---- */
  const ops: DemoOpsLog[] = [
    {
      id: 'opseed-1',
      summary: '保存了作答核对记录',
      detail: '导数及其应用 · 课后练习 · STU-001 · 第 3 题（R2）',
      at: ago(150),
    },
    {
      id: 'opseed-2',
      summary: '上传了学生答卷',
      detail: '导数及其应用 · 课后练习 · STU-004',
      at: ago(40),
    },
    {
      id: 'opseed-3',
      summary: '保存了作答核对记录',
      detail: '三角函数 · 单元测验 · STU-012 · 第 1 题（R1）',
      at: agoD(1, 16),
    },
  ]

  return {
    users,
    assignments,
    questions,
    rubrics,
    answerKeys,
    submissions,
    canonicals,
    reviews,
    proposals,
    memberships,
    audit,
    knowledge,
    conversations,
    ops,
  }
}

export function seedDemoState(): DemoState {
  const seeded = buildSeed()
  return {
    version: 3,
    seeded_at: new Date().toISOString(),
    ...seeded,
    counters: {},
    requestCache: {},
  }
}
