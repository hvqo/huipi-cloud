<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  ArrowLeft,
  BadgeCheck,
  Ban,
  ChevronDown,
  ClipboardCheck,
  ListChecks,
  Quote,
  RefreshCcw,
  Sparkles,
  TriangleAlert,
} from 'lucide-vue-next'
import { services, type S, type GradeProposalSummary } from '../../services'
import { UnsupportedCapabilityError } from '../../services/types'
import { session } from '../../app/session'
import { ApiError, type Node } from '../../api/client'
import { selectedRegion } from '../../lib/selection'
import { PendingReview } from '../../lib/review'
import { mapGradeQuote } from '../../lib/grade-quote'
import { compareDecimals, formatDecimal, sumDecimals, validateScoreInput } from '../../lib/decimal'
import {
  formatRelative,
  formatScore,
  gradingReasonLabels,
  gradingStatusLabels,
  label,
  questionTypeLabels,
  reviewStatusLabels,
  sourceStatusLabels,
} from '../../lib/labels'
import EmptyState from '../../components/EmptyState.vue'
import PageState from '../../components/PageState.vue'

/**
 * AI 候选评分审核。
 *
 * 候选与人工审核均不发布正式成绩（grade_published 恒为 false）。
 * 修订使用十进制字符串运算；改变评分项需理由与原文引文证据。
 * 真实后端暂无候选列表端点（inspect 需先知道 proposal_id），
 * 真实模式下列表区如实展示能力缺口。
 */
const route = useRoute(),
  aid = String(route.params.aid),
  sid = String(route.params.sid),
  qid = String(route.params.qid)

const loading = ref(false),
  error = ref(''),
  submission = ref<S['SubmissionRead']>(),
  workbench = ref<S['AssignmentWorkbench']>(),
  list = ref<GradeProposalSummary[] | null>(null),
  listUnsupported = ref(false),
  selectedPid = ref(''),
  inspection = ref<
    | (S['GradeReviewHTTPInspection'] & {
        candidate_rationales?: { criterion_id: string; rationale: string }[]
      })
    | null
  >(null),
  history = ref<S['GradeReviewHTTPHistory'] | null>(null),
  notice = ref(''),
  saveError = ref(''),
  busy = ref(false),
  frozen = ref(false)
const mode = ref<'idle' | 'accepted' | 'revised' | 'rejected' | 'regrading_required'>('idle')
const reasonCodes = ref<string[]>([])
const revisedRows = ref<
  {
    criterion_id: string
    points: string
    rationale: string
    quote: S['QuoteEvidenceReference'] | null
  }[]
>([])
const quoteTarget = ref<string | null>(null)
const quoteOpen = ref(false)
const confirmOpen = ref(false)

const pendingKey = `huipi.grading.${session.actor}.${sid}.${qid}`
const pending = new PendingReview(pendingKey)
const pendingTarget = ref(sessionStorage.getItem(pendingKey + '.target') ?? '')
const historyOffset = ref(0)
const fullRubric = ref<S['RubricCriterionRead'][]>([])
function clearPending() {
  pending.clear()
  pendingTarget.value = ''
  sessionStorage.removeItem(pendingKey + '.target')
}
const nodes = new Map<string, string>(),
  currentNodes = new Map<string, Node>()
const nodePages = new Map<string, number>()
/** 引文选择对话框展示的节点清单（Map 不能直接用于模板遍历）。 */
const quoteNodes = ref<{ pointer: string; text: string; page: number }[]>([])

const question = computed(() => workbench.value?.questions.find((q) => q.id === qid))
const rubricById = computed(() => new Map(fullRubric.value.map((c) => [c.id, c])))
const editable = computed(
  () =>
    !!workbench.value?.can_write &&
    !frozen.value &&
    inspection.value?.source_status === 'current' &&
    !busy.value,
)
const abstained = computed(() => inspection.value?.grading_status === 'grading_abstained')
const stale = computed(() => inspection.value?.source_status === 'source_stale')
const revisedTotal = computed(() => {
  try {
    return revisedRows.value.length
      ? formatDecimal(sumDecimals(revisedRows.value.map((r) => r.points)))
      : '—'
  } catch {
    return '—'
  }
})
const revisedChanged = computed(() =>
  revisedRows.value.filter(
    (row) =>
      !/^[0-9]+(?:\.[0-9]{1,2})?$/.test(row.points.trim()) ||
      compareDecimals(
        (inspection.value?.criterion_scores ?? []).find((c) => c.criterion_id === row.criterion_id)
          ?.proposed_points ?? '0',
        row.points,
      ) !== 0,
  ),
)
const revisedError = computed(() => {
  for (const row of revisedRows.value) {
    const max =
      inspection.value?.criterion_scores?.find((c) => c.criterion_id === row.criterion_id)
        ?.max_points ?? '0'
    const issue = validateScoreInput(row.points, max)
    if (issue) return `${rubricById.value.get(row.criterion_id)?.description ?? '评分项'}：${issue}`
  }
  if (revisedChanged.value.length && !reasonCodes.value.length)
    return '修改评分项时必须选择处理原因'
  for (const row of revisedChanged.value) {
    const points = row.points.trim()
    if (compareDecimals(points, '0') > 0 && !row.quote)
      return `「${rubricById.value.get(row.criterion_id)?.description ?? ''}」需选择原文引文证据`
    if (!row.rationale.trim())
      return `「${rubricById.value.get(row.criterion_id)?.description ?? ''}」需填写修改理由`
  }
  return ''
})

async function load() {
  loading.value = true
  error.value = ''
  listUnsupported.value = false
  try {
    const svc = await services()
    const [s, w, detail] = await Promise.all([
      svc.submissions.detail(sid),
      svc.assignments.workbench(aid, 0, qid),
      svc.assignments.detail(aid),
    ])
    if (s.assignment_id !== aid) throw new Error('提交与作业不匹配。')
    submission.value = s
    workbench.value = w
    fullRubric.value = detail.questions.find((q) => q.id === qid)?.rubric_criteria ?? []
    try {
      list.value = await svc.grading.list(sid, qid)
    } catch (e) {
      list.value = null
      if (e instanceof UnsupportedCapabilityError) listUnsupported.value = true
      else throw e
    }
    const preferred = String(
      pendingTarget.value || route.query.p || list.value?.[0]?.proposal_id || '',
    )
    if (preferred) await selectProposal(preferred)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    loading.value = false
  }
  frozen.value = !!pending.body
}

async function chooseProposal(pid: string) {
  try {
    await selectProposal(pid)
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : '候选读取失败，请重试。'
  }
}
async function selectProposal(pid: string) {
  if (pending.body && pendingTarget.value !== pid) {
    saveError.value = '请先确认上一笔评分审核结果，不能切换候选。'
    return
  }
  historyOffset.value = 0
  selectedPid.value = pid
  inspection.value = null
  history.value = null
  mode.value = 'idle'
  reasonCodes.value = []
  notice.value = ''
  saveError.value = ''
  const svc = await services()
  const [inspect, hist] = await Promise.all([
    svc.grading.inspect(sid, qid, pid),
    svc.grading.history(sid, qid, pid),
  ])
  inspection.value = inspect
  history.value = hist
  revisedRows.value = (inspect.criterion_scores ?? []).map((c) => ({
    criterion_id: c.criterion_id,
    points: c.proposed_points,
    rationale: '',
    quote: null,
  }))
  await loadQuotePages()
}

async function moreHistory(offset: number) {
  try {
    history.value = await (await services()).grading.history(sid, qid, selectedPid.value, offset)
    historyOffset.value = offset
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : '历史读取失败'
  }
}
async function moreEvidence() {
  if (!inspection.value?.next_evidence_offset) return
  try {
    const next = await (
      await services()
    ).grading.inspect(sid, qid, selectedPid.value, inspection.value.next_evidence_offset)
    if (
      next.proposal_sha256 !== inspection.value.proposal_sha256 ||
      next.grade_input_sha256 !== inspection.value.grade_input_sha256 ||
      next.source_status !== 'current'
    )
      throw new Error('候选来源已变化，请刷新后重新审核。')
    inspection.value = {
      ...next,
      evidence_locations: [
        ...(inspection.value.evidence_locations ?? []),
        ...(next.evidence_locations ?? []),
      ],
      candidate_rationales: inspection.value.candidate_rationales,
    }
    await loadQuotePages()
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : '引文读取失败'
  }
}
async function loadQuotePages() {
  const svc = await services()
  const pages = [
    ...new Set(inspection.value?.evidence_locations?.map((e) => e.page_number) ?? []),
  ].sort((a, b) => a - b)
  nodes.clear()
  currentNodes.clear()
  quoteNodes.value = []
  for (const pageNumber of pages) {
    const result = await svc.submissions.page(sid, pageNumber)
    result.page.blocks.forEach((b, i) => {
      const pointer = `/pages/${result.page.page_index}/blocks/${i}/content`
      indexNode(b.content, pointer, pageNumber)
    })
  }
  for (const [pointer, text] of nodes)
    if (inspection.value?.evidence_locations?.some((e) => e.content_pointer === pointer))
      quoteNodes.value.push({ pointer, text, page: nodePages.get(pointer) ?? 1 })
}

function indexNode(node: Node, pointer: string, pageNumber: number) {
  currentNodes.set(pointer, node)
  nodePages.set(pointer, pageNumber)
  if (typeof node.value === 'string') nodes.set(pointer, node.value)
  ;(node.children || []).forEach((child, i) =>
    indexNode(child, `${pointer}/children/${i}`, pageNumber),
  )
}

function snippetOf(pointer: string, start: number, end: number) {
  const text = nodes.get(pointer)
  return text ? Array.from(text).slice(start, end).join('') : ''
}

function criterionLabel(criterionId: string) {
  return rubricById.value.get(criterionId)?.description ?? '评分项'
}

function quoteForCriterion(criterionId: string) {
  const evidence = inspection.value?.evidence_locations?.find((e) => e.criterion_id === criterionId)
  if (!evidence) return null
  return {
    text: snippetOf(
      evidence.content_pointer,
      evidence.canonical_text_start,
      evidence.canonical_text_end,
    ),
    page: evidence.page_number,
  }
}

function openMode(next: typeof mode.value) {
  if (!editable.value) return
  if ((next === 'accepted' || next === 'revised') && abstained.value) {
    saveError.value = '模型弃权的候选没有可接受的分数，只能拒绝或要求重新评分。'
    return
  }
  mode.value = next
  reasonCodes.value = []
  saveError.value = ''
}

function openQuotePicker(criterionId: string) {
  quoteTarget.value = criterionId
  quoteOpen.value = true
}

function captureQuote() {
  if (!quoteTarget.value) return
  try {
    const region = selectedRegion(window.getSelection(), nodes)
    const row = revisedRows.value.find((r) => r.criterion_id === quoteTarget.value)
    if (row) row.quote = mapGradeQuote(region, nodes, inspection.value?.evidence_locations ?? [])
    quoteOpen.value = false
    saveError.value = ''
  } catch (e) {
    saveError.value = (e as Error).message
  }
}

function buildBody(): string {
  if (!inspection.value) throw new Error('候选未加载。')
  const requestId = crypto.randomUUID()
  const expected = inspection.value.latest_review_revision
  if (mode.value === 'accepted')
    return JSON.stringify({
      request_id: requestId,
      expected_revision: expected,
      review_status: 'accepted',
    })
  if (mode.value === 'rejected' || mode.value === 'regrading_required') {
    if (!reasonCodes.value.length) throw new Error('请选择处理原因。')
    return JSON.stringify({
      request_id: requestId,
      expected_revision: expected,
      review_status: mode.value,
      review_reason_codes: reasonCodes.value,
    })
  }
  // revised：完整 Rubric 快照
  if (revisedError.value) throw new Error(revisedError.value)
  return JSON.stringify({
    request_id: requestId,
    expected_revision: expected,
    review_status: 'revised',
    review_reason_codes: reasonCodes.value,
    criterion_reviews: revisedRows.value.map((row) => ({
      criterion_id: row.criterion_id,
      reviewed_points: row.points.trim(),
      rationale: row.rationale.trim() || null,
      evidence_references: row.quote ? [row.quote] : null,
    })),
  })
}

async function submit() {
  if (busy.value || !inspection.value) return
  busy.value = true
  confirmOpen.value = false
  saveError.value = ''
  try {
    const body = buildBody()
    pending.freeze(body)
    pendingTarget.value = selectedPid.value
    sessionStorage.setItem(pendingKey + '.target', selectedPid.value)
    frozen.value = true
    const svc = await services()
    const result = await svc.grading.save(sid, qid, selectedPid.value, pending.body!)
    clearPending()
    frozen.value = false
    mode.value = 'idle'
    reasonCodes.value = []
    await selectProposal(selectedPid.value)
    notice.value = `已保存审核决定（${label(reviewStatusLabels, result.record.review_status)}）。候选与人工审核均不发布正式成绩。`
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      clearPending()
      frozen.value = false
      saveError.value = e.message
    } else if (e instanceof ApiError && !e.uncertain) {
      clearPending()
      frozen.value = false
      saveError.value = e.message
    } else {
      saveError.value = pending.body
        ? '保存结果待确认，不代表没有提交。请点击“重试原请求”以同一请求标识重试。'
        : e instanceof Error
          ? e.message
          : '保存未完成。'
    }
  } finally {
    busy.value = false
  }
}

async function retryPending() {
  if (!pending.body || busy.value) return
  if (!pendingTarget.value || pendingTarget.value !== selectedPid.value) {
    saveError.value = '原请求缺少可确认的候选目标，已阻止重放。请由管理员核查历史记录。'
    return
  }
  busy.value = true
  try {
    const svc = await services()
    const result = await svc.grading.save(sid, qid, selectedPid.value, pending.body)
    clearPending()
    frozen.value = false
    mode.value = 'idle'
    await selectProposal(selectedPid.value)
    notice.value = `已确认保存（${label(reviewStatusLabels, result.record.review_status)}）。`
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) {
      clearPending()
      frozen.value = false
      saveError.value = e.message
    } else {
      saveError.value = '保存结果仍待确认，请稍后再次重试原请求。'
    }
  } finally {
    busy.value = false
  }
}

watch(selectedPid, () => (notice.value = ''))
onMounted(load)
onBeforeUnmount(() => (quoteOpen.value = false))
</script>
<template>
  <div class="hp-section-gap">
    <RouterLink class="hp-linkbtn" :to="`/assignments/${aid}/submissions/${sid}`"
      ><ArrowLeft aria-hidden="true" style="width: 14px; height: 14px" />返回提交详情</RouterLink
    >
    <PageState :loading="loading" :error="error" @retry="load">
      <div v-if="submission && question" class="hp-section-gap">
        <div class="hp-card" style="padding: 16px 20px">
          <div class="hp-flex" style="justify-content: space-between; align-items: flex-start">
            <div class="hp-grow" style="min-width: 260px">
              <p class="hp-eyebrow">
                {{ workbench?.assignment.title }} · {{ submission.student_ref }} · AI 候选评分审核
              </p>
              <h1 style="font-size: 19px; font-weight: 700; margin-top: 4px">
                第 {{ question.question_number }} 题
                <span class="hp-badge" style="margin-left: 4px">{{
                  label(questionTypeLabels, question.question_type)
                }}</span>
                <span class="hp-badge">满分 {{ question.max_score }} 分</span>
              </h1>
              <p class="hp-pagehead-desc" style="max-width: 620px">
                {{ question.stem.slice(0, 80) }}{{ question.stem.length > 80 ? '…' : '' }}
              </p>
            </div>
            <span class="hp-badge hp-badge-warning"
              ><TriangleAlert aria-hidden="true" style="width: 12px; height: 12px" />候选 ≠
              正式成绩</span
            >
          </div>
        </div>

        <div v-if="notice" class="hp-note hp-note-success" role="status">
          <ClipboardCheck aria-hidden="true" /><span>{{ notice }}</span>
        </div>
        <div v-if="frozen" class="hp-note hp-note-warning" role="alert">
          <TriangleAlert aria-hidden="true" />
          <span
            >上一笔保存结果待确认，编辑已锁定。重试会发送原 request_id
            与完全相同的正文，不会重复写入。</span
          >
          <button
            class="hp-btn hp-btn-primary hp-btn-sm"
            type="button"
            :disabled="busy"
            @click="retryPending"
          >
            重试原请求
          </button>
        </div>
        <div v-if="saveError" class="hp-note hp-note-danger" role="alert">{{ saveError }}</div>
        <div v-if="!workbench?.can_write" class="hp-note hp-note-info" role="note">
          此作业为只读授权，不能保存评分审核。
        </div>

        <div
          class="rv-work"
          style="grid-template-columns: minmax(260px, 4fr) minmax(340px, 7fr) minmax(320px, 5fr)"
        >
          <section class="rv-pane" aria-label="候选列表">
            <div class="rv-pane-head">
              <span class="rv-pane-title"><Sparkles aria-hidden="true" />候选列表</span>
            </div>
            <div class="rv-pane-body" style="display: flex; flex-direction: column; gap: 8px">
              <template v-if="list?.length">
                <button
                  v-for="item in list"
                  :key="item.proposal_id"
                  type="button"
                  class="rv-option"
                  :class="{ 'is-selected': item.proposal_id === selectedPid }"
                  :disabled="busy || frozen"
                  @click="chooseProposal(item.proposal_id)"
                >
                  <span class="rv-radio" aria-hidden="true"></span>
                  <span style="min-width: 0">
                    <span class="rv-option-name"
                      >候选 {{ (list ?? []).indexOf(item) + 1 }} ·
                      {{
                        item.grading_status === 'grading_abstained'
                          ? '模型弃权'
                          : `${formatScore(item.proposed_total_score)} / ${formatScore(item.max_score)} 分`
                      }}</span
                    ><br />
                    <span class="rv-option-hint"
                      >{{ formatRelative(item.created_at) }} ·
                      {{ label(sourceStatusLabels, item.source_status) }} ·
                      {{
                        item.latest_review_status
                          ? label(reviewStatusLabels, item.latest_review_status)
                          : '待人工审核'
                      }}</span
                    >
                  </span>
                </button>
              </template>
              <EmptyState
                v-else-if="listUnsupported"
                title="候选列表待后端提供"
                desc="真实后端的候选检查接口需要先知道候选标识；枚举候选的列表端点尚未开发。演示模式（npm run dev:demo）可体验完整候选审核流程。"
              />
              <EmptyState
                v-else
                title="该题暂无候选评分"
                desc="解析与候选生成完成后会出现在这里。"
              />
            </div>
          </section>

          <section class="rv-pane" aria-label="候选详情">
            <div class="rv-pane-head">
              <span class="rv-pane-title"><ListChecks aria-hidden="true" />候选详情</span>
              <span
                v-if="inspection"
                class="hp-badge"
                :class="{ 'hp-badge-warning': stale }"
                style="margin-left: auto"
                >{{ label(sourceStatusLabels, inspection.source_status) }}</span
              >
            </div>
            <div v-if="!inspection" class="rv-pane-body">
              <EmptyState
                title="选择一个候选"
                desc="在左侧候选列表中选择，查看 AI 给出的总分、逐项分数与依据。"
              />
            </div>
            <template v-else>
              <div
                v-if="stale"
                class="hp-note hp-note-warning"
                style="margin: 12px 14px 0"
                role="alert"
              >
                <TriangleAlert aria-hidden="true" />
                <span
                  ><b>候选来源已过期。</b
                  >作答内容在候选生成后发生过人工修订，仍可审计下方历史，但不能追加新的评分决定。</span
                >
              </div>
              <div class="rv-pane-body">
                <div v-if="abstained" class="hp-note hp-note-info" role="note">
                  <span
                    ><b>模型弃权：</b
                    >该候选未给出分数（缺失材料不是学生零分）。可以拒绝候选或要求重新评分。</span
                  >
                </div>
                <div v-else class="hp-flex" style="gap: 18px; margin-bottom: 14px">
                  <div>
                    <div class="hp-fine">AI 候选总分</div>
                    <div
                      style="font-size: 32px; font-weight: 700; color: var(--hp-violet)"
                      class="num"
                    >
                      {{ formatScore(inspection.proposed_total_score) }}
                      <small style="font-size: 14px; color: var(--hp-ink-400)"
                        >/ {{ formatScore(inspection.max_score) }} 分</small
                      >
                    </div>
                  </div>
                  <div
                    v-if="inspection.latest"
                    style="border-left: 1px solid var(--hp-border); padding-left: 18px"
                  >
                    <div class="hp-fine">最新人工审核</div>
                    <span
                      class="hp-badge"
                      :class="{
                        'hp-badge-success': inspection.latest_review_status === 'accepted',
                        'hp-badge-primary': inspection.latest_review_status === 'revised',
                        'hp-badge-danger': inspection.latest_review_status === 'rejected',
                        'hp-badge-warning':
                          inspection.latest_review_status === 'regrading_required',
                      }"
                      style="height: 30px; font-size: 13px; margin-top: 4px"
                    >
                      {{ label(reviewStatusLabels, inspection.latest_review_status) }}
                      <template v-if="inspection.latest.reviewed_total_score"
                        >· {{ formatScore(inspection.latest.reviewed_total_score) }} 分</template
                      >
                    </span>
                  </div>
                </div>

                <div
                  v-for="row in inspection.criterion_scores"
                  :key="row.criterion_id"
                  class="rv-block"
                >
                  <span class="rv-block-tag">{{ criterionLabel(row.criterion_id) }}</span>
                  <div class="hp-flex" style="justify-content: space-between">
                    <b class="num">{{ formatScore(row.proposed_points) }} 分</b>
                    <span class="hp-fine">上限 {{ formatScore(row.max_points) }} 分</span>
                  </div>
                  <p class="hp-fine" style="margin-top: 4px">
                    {{
                      inspection.candidate_rationales?.find(
                        (c) => c.criterion_id === row.criterion_id,
                      )?.rationale ?? '当前接口不提供模型理由正文，请结合原文与评分细则审核。'
                    }}
                  </p>
                  <p v-if="quoteForCriterion(row.criterion_id)?.text" class="hp-fine">
                    “{{ quoteForCriterion(row.criterion_id)?.text }}”
                  </p>
                  <p v-if="quoteForCriterion(row.criterion_id)" class="hp-fine">
                    <Quote
                      aria-hidden="true"
                      style="width: 11px; height: 11px; vertical-align: -1px"
                    />
                    原文引文 · 第 {{ quoteForCriterion(row.criterion_id)!.page }} 页
                  </p>
                </div>
                <button
                  v-if="
                    inspection.evidence_truncated &&
                    (inspection.evidence_locations?.length ?? 0) < 100
                  "
                  type="button"
                  class="hp-btn hp-btn-secondary hp-btn-sm"
                  @click="moreEvidence"
                >
                  加载更多引文
                </button>
                <p v-if="inspection.evidence_truncated" class="hp-fine">
                  当前为部分引文，最多加载100条；未显示的片段不能在此选用。
                </p>
                <p class="hp-fine" style="margin-top: 8px">
                  AI 候选仅供教师参考；接受、修订或拒绝均不发布正式成绩。
                </p>
              </div>
            </template>
          </section>

          <aside class="rv-pane rv-pane-decision" aria-label="教师决定">
            <div class="rv-status">
              <span
                >候选状态
                <b>{{ label(gradingStatusLabels, inspection?.grading_status ?? '') }}</b></span
              >
              <span style="margin-left: auto"
                >审核版本 <b>R{{ inspection?.latest_review_revision ?? 0 }}</b></span
              >
            </div>
            <div class="rv-form">
              <div class="hp-flex hp-gap-sm">
                <button
                  class="hp-btn hp-btn-secondary hp-btn-sm"
                  type="button"
                  :disabled="!editable"
                  @click="openMode('accepted')"
                >
                  <BadgeCheck aria-hidden="true" />接受候选
                </button>
                <button
                  class="hp-btn hp-btn-secondary hp-btn-sm"
                  type="button"
                  :disabled="!editable || abstained"
                  @click="openMode('revised')"
                >
                  逐项修订
                </button>
                <button
                  class="hp-btn hp-btn-danger hp-btn-sm"
                  type="button"
                  :disabled="!editable"
                  @click="openMode('rejected')"
                >
                  <Ban aria-hidden="true" />拒绝
                </button>
                <button
                  class="hp-btn hp-btn-secondary hp-btn-sm"
                  type="button"
                  :disabled="!editable"
                  @click="openMode('regrading_required')"
                >
                  <RefreshCcw aria-hidden="true" />要求重新评分
                </button>
              </div>

              <template v-if="mode === 'accepted'">
                <p class="hp-fine">
                  接受表示确认<b>原始模型候选</b>分数（不是接受上一次人工修订）。确认后仍不发布成绩。
                </p>
                <button
                  class="hp-btn hp-btn-primary hp-btn-block"
                  type="button"
                  :disabled="busy"
                  @click="confirmOpen = true"
                >
                  确认接受候选
                </button>
              </template>

              <template v-else-if="mode === 'revised'">
                <p class="hp-fine">
                  修改评分项需填写理由，且正分项必须有原文引文证据；保留未改动的评分项，以保存完整的审核记录。
                </p>
                <div
                  v-for="row in revisedRows"
                  :key="row.criterion_id"
                  style="
                    border: 1px solid var(--hp-border);
                    border-radius: 10px;
                    padding: 10px 12px;
                  "
                >
                  <div class="hp-flex" style="justify-content: space-between; margin-bottom: 6px">
                    <span style="font-size: 13px; font-weight: 600">{{
                      criterionLabel(row.criterion_id)
                    }}</span>
                    <span class="hp-fine"
                      >候选
                      {{
                        formatScore(
                          (inspection?.criterion_scores ?? []).find(
                            (c) => c.criterion_id === row.criterion_id,
                          )?.proposed_points ?? '0',
                        )
                      }}
                      分</span
                    >
                  </div>
                  <div class="hp-flex hp-gap-sm">
                    <label class="hp-field" style="width: 110px">
                      <span class="hp-field-label">修订分值</span>
                      <input
                        v-model="row.points"
                        class="hp-input"
                        style="height: 32px"
                        inputmode="decimal"
                      />
                    </label>
                    <label class="hp-field hp-grow">
                      <span class="hp-field-label">修改理由</span>
                      <input
                        v-model="row.rationale"
                        class="hp-input"
                        style="height: 32px"
                        placeholder="说明与候选不一致的原因"
                      />
                    </label>
                  </div>
                  <div class="hp-flex hp-gap-sm" style="margin-top: 6px">
                    <button
                      class="hp-btn hp-btn-ghost hp-btn-sm"
                      type="button"
                      @click="openQuotePicker(row.criterion_id)"
                    >
                      <Quote aria-hidden="true" />{{ row.quote ? '重选引文' : '选择原文引文' }}
                    </button>
                    <span v-if="row.quote" class="hp-fine hp-grow"
                      >“{{ row.quote.quote.slice(0, 30) }}…”</span
                    >
                  </div>
                </div>
                <div class="hp-flex" style="justify-content: space-between">
                  <span class="hp-field-label">修订合计</span>
                  <b class="num">{{ revisedTotal }} 分</b>
                </div>
                <div>
                  <span class="hp-field-label" style="display: block; margin-bottom: 6px"
                    >处理原因（可多选）</span
                  >
                  <div class="hp-flex hp-gap-sm">
                    <label
                      v-for="(text, code) in gradingReasonLabels"
                      :key="code"
                      class="hp-check"
                      style="font-size: 12.5px"
                    >
                      <input v-model="reasonCodes" type="checkbox" :value="code" />{{ text }}
                    </label>
                  </div>
                </div>
                <p v-if="revisedError" class="hp-field-error">{{ revisedError }}</p>
                <button
                  class="hp-btn hp-btn-primary hp-btn-block"
                  type="button"
                  :disabled="busy || !!revisedError"
                  @click="confirmOpen = true"
                >
                  提交修订
                </button>
              </template>

              <template v-else-if="mode === 'rejected' || mode === 'regrading_required'">
                <p class="hp-fine">
                  {{
                    mode === 'rejected'
                      ? '拒绝候选分数并记录原因。'
                      : '记录后续处理要求，不会自动触发新的模型调用。'
                  }}
                </p>
                <div>
                  <span class="hp-field-label" style="display: block; margin-bottom: 6px"
                    >处理原因（可多选）</span
                  >
                  <div class="hp-flex hp-gap-sm">
                    <label
                      v-for="(text, code) in gradingReasonLabels"
                      :key="code"
                      class="hp-check"
                      style="font-size: 12.5px"
                    >
                      <input v-model="reasonCodes" type="checkbox" :value="code" />{{ text }}
                    </label>
                  </div>
                </div>
                <p v-if="!reasonCodes.length" class="hp-field-error">请至少选择一个原因</p>
                <button
                  class="hp-btn hp-btn-primary hp-btn-block"
                  type="button"
                  :disabled="busy || !reasonCodes.length"
                  @click="confirmOpen = true"
                >
                  {{ mode === 'rejected' ? '确认拒绝' : '确认要求重新评分' }}
                </button>
              </template>

              <EmptyState
                v-else
                title="选择处理方式"
                desc="接受候选、逐项修订、拒绝或要求重新评分。所有决定都会记录审核版本，且不会发布正式成绩。"
              />

              <details class="hp-details" open style="margin: 0">
                <summary>
                  <ChevronDown aria-hidden="true" class="chev" />审核历史
                  <span class="hp-details-count"
                    >（{{ history?.records.length ?? 0 }} 条 · 只读）</span
                  >
                </summary>
                <div class="rv-history history-panel">
                  <p v-if="!history?.records.length" class="hp-fine" style="padding: 8px 0">
                    尚无审核记录。
                  </p>
                  <article
                    v-for="record in history?.records"
                    :key="record.review_id"
                    class="history-row rv-hrow"
                  >
                    <span class="rv-hrev">R{{ record.review_revision }}</span>
                    <div class="hp-grow" style="min-width: 0">
                      <div class="rv-hline">
                        <b>{{ label(reviewStatusLabels, record.review_status) }}</b>
                        <span
                          class="hp-badge"
                          :class="{
                            'hp-badge-success': record.review_status === 'accepted',
                            'hp-badge-primary': record.review_status === 'revised',
                            'hp-badge-danger': record.review_status === 'rejected',
                            'hp-badge-warning': record.review_status === 'regrading_required',
                          }"
                          style="height: 20px; font-size: 11px"
                        >
                          <template v-if="record.reviewed_total_score"
                            >修订后 {{ formatScore(record.reviewed_total_score) }} 分</template
                          >
                          <template v-else
                            >候选 {{ formatScore(record.proposed_total_score) }} 分</template
                          >
                        </span>
                      </div>
                      <div class="rv-hmeta">
                        {{ formatRelative(record.created_at) }} ·
                        {{
                          record.reviewer_kind === 'authenticated_teacher'
                            ? '认证教师'
                            : '历史自声明身份（未认证）'
                        }}
                      </div>
                      <div v-if="record.review_reason_codes.length" class="rv-hmeta">
                        原因：{{
                          record.review_reason_codes
                            .map((c) => label(gradingReasonLabels, c))
                            .join('、')
                        }}
                      </div>
                      <details class="hp-details">
                        <summary>逐项评分与前序记录</summary>
                        <div
                          class="hp-fine"
                          v-for="c in record.criterion_scores"
                          :key="c.criterion_id"
                        >
                          {{ criterionLabel(c.criterion_id) }}：候选
                          {{ formatScore(c.proposed_points) }} / 人工
                          {{ formatScore(c.reviewed_points) }} 分{{
                            c.changed ? '（相对候选已修改）' : '（与候选一致）'
                          }}
                        </div>
                        <p class="hp-fine">
                          {{
                            record.supersedes_review_id ? '承接前一次人工审核' : '首条人工审核'
                          }}；历史只读。
                        </p>
                      </details>
                    </div>
                  </article>
                  <div class="hp-flex hp-between">
                    <button
                      class="hp-linkbtn"
                      :disabled="historyOffset === 0"
                      @click="moreHistory(Math.max(0, historyOffset - 5))"
                    >
                      上一页历史</button
                    ><button
                      class="hp-linkbtn"
                      :disabled="!history?.has_next"
                      @click="moreHistory(history?.next_offset ?? 0)"
                    >
                      下一页历史
                    </button>
                  </div>
                </div>
              </details>

              <details class="hp-details" style="margin: 0 0 12px">
                <summary><ChevronDown aria-hidden="true" class="chev" />来源校验（诊断）</summary>
                <div class="rv-diag">
                  <div class="rv-diag-row">
                    <span class="rv-diag-key">评分输入</span
                    ><span class="rv-diag-val"
                      >{{ inspection?.grade_input_sha256.slice(0, 12) ?? '—' }}…</span
                    >
                  </div>
                  <div class="rv-diag-row">
                    <span class="rv-diag-key">候选摘要</span
                    ><span class="rv-diag-val"
                      >{{ inspection?.proposal_sha256?.slice(0, 12) ?? '—' }}…</span
                    >
                  </div>
                  <div class="rv-diag-row">
                    <span class="rv-diag-key">修订基准</span
                    ><span class="rv-diag-val"
                      >expected_revision = {{ inspection?.latest_review_revision ?? 0 }}</span
                    >
                  </div>
                </div>
              </details>
            </div>
          </aside>
        </div>

        <el-dialog
          v-model="confirmOpen"
          title="确认评分审核决定"
          width="480"
          :close-on-click-modal="false"
        >
          <p style="margin-bottom: 8px">
            决定：<b>{{
              {
                idle: '—',
                accepted: '接受候选',
                revised: '逐项修订',
                rejected: '拒绝候选',
                regrading_required: '要求重新评分',
              }[mode]
            }}</b>
          </p>
          <p v-if="mode === 'revised'" style="margin-bottom: 8px">
            修订合计：<b class="num">{{ revisedTotal }} 分</b>（候选
            {{ formatScore(inspection?.proposed_total_score) }} 分）
          </p>
          <p class="hp-fine">
            提交前会重新校验候选版本与来源。此操作只追加审核记录，<b>不发布正式成绩</b>。
          </p>
          <template #footer>
            <el-button @click="confirmOpen = false">再检查一下</el-button>
            <el-button type="primary" :loading="busy" @click="submit">确认提交</el-button>
          </template>
        </el-dialog>

        <el-dialog
          v-model="quoteOpen"
          title="选择原文引文"
          width="640"
          :close-on-click-modal="false"
        >
          <p class="hp-fine" style="margin-bottom: 10px">
            在下方作答内容中拖选一段文字作为该评分项的证据，然后点击“使用当前选区”。一次选取同一段落中的文字，不跨越未验证的来源范围。
          </p>
          <div
            style="
              max-height: 46vh;
              overflow: auto;
              border: 1px solid var(--hp-border);
              border-radius: 10px;
              padding: 12px;
            "
          >
            <div
              v-for="node in quoteNodes"
              :key="node.pointer"
              class="rv-block is-related"
              style="margin-bottom: 8px"
            >
              <span class="rv-block-tag">第 {{ node.page }} 页 · 文本</span>
              <p
                class="rv-block-text"
                :data-source-pointer="node.pointer"
                style="user-select: text"
              >
                {{ node.text }}
              </p>
            </div>
          </div>
          <template #footer>
            <el-button @click="quoteOpen = false">取消</el-button>
            <el-button type="primary" @click="captureQuote">使用当前选区</el-button>
          </template>
        </el-dialog>
      </div>
    </PageState>
  </div>
</template>
