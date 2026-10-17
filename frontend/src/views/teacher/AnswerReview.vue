<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  ArrowRight,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ClipboardPen,
  FileText,
  Info,
  ListChecks,
  ShieldCheck,
  ZoomIn,
  ZoomOut,
} from 'lucide-vue-next'
import { services, type S } from '../../services'
import { UnsupportedCapabilityError, type SubmissionReviewProgress } from '../../services/types'
import { session } from '../../app/session'
import { ApiError, type Node, type Page, type Region, type Review } from '../../api/client'
import { addRegion, selectedRegion } from '../../lib/selection'
import {
  allowedReasons,
  makeReview,
  PendingReview,
  reasons as reasonLabels,
  sourceMatches,
  type Decision,
} from '../../lib/review'
import {
  formatDateTime,
  contentTypeLabels,
  formatRelative,
  label,
  presenceLabels,
  questionTypeLabels,
} from '../../lib/labels'
import FilePreview from '../../components/FilePreview.vue'
import PageState from '../../components/PageState.vue'
import SourceNode from '../../components/SourceNode.vue'

/**
 * 作答证据人工核对（核心页面）。
 *
 * 三栏工作区：原始试卷（受保护 Blob 预览）｜作答内容（Canonical 选区）｜人工判断。
 * 严格保留：Unicode 码点转换、来源校验、request_id 幂等、expected_revision、
 * 409 冲突锁定与 503 未知结果重试（同一请求字节）。
 */
const route = useRoute(),
  router = useRouter(),
  aid = String(route.params.aid),
  sid = String(route.params.sid),
  qid = String(route.params.qid)

const loading = ref(false),
  error = ref(''),
  submission = ref<S['SubmissionRead']>(),
  workbench = ref<S['AssignmentWorkbench']>(),
  view = ref<Review>(),
  history = ref<S['ReviewHistory']>(),
  page = ref<Page>(),
  progress = ref<SubmissionReviewProgress | null>(null),
  pageNumber = ref(1),
  pageCount = ref(1),
  historyOffset = ref(0),
  notice = ref(''),
  conflict = ref(false),
  preview = ref(false),
  verified = ref(false),
  busy = ref(false),
  frozen = ref(false),
  permissionDenied = ref(false),
  originalUrl = ref(''),
  originalError = ref(''),
  originalBusy = ref(false),
  zoom = ref(1)
const decision = ref<Decision>('response_present'),
  reason = ref('student_work_visible'),
  regions = ref<Region[]>([])

const pending = new PendingReview(`huipi.review.${session.actor}.${sid}.${qid}`)
frozen.value = !!pending.body
const nodes = new Map<string, string>(),
  currentNodes = new Map<string, Node>()
const originalController = new AbortController()

const question = computed(() => workbench.value?.questions.find((q) => q.id === qid))
const questions = computed(() =>
  (workbench.value?.questions ?? []).slice().sort((a, b) => a.question_number - b.question_number),
)
const currentIndex = computed(() => questions.value.findIndex((q) => q.id === qid))
const related = computed(() => new Set(view.value?.evidence.map((e) => e.content_pointer) || []))
const editable = computed(
  () =>
    !!workbench.value?.can_write &&
    !frozen.value &&
    !conflict.value &&
    !permissionDenied.value &&
    !busy.value,
)
const presenceOf = (questionId: string) =>
  progress.value?.questions.find((q) => q.question_id === questionId)?.answer_presence
const presenceDotClass = (presence?: string) =>
  presence === 'response_present'
    ? 'qdot-present'
    : presence === 'uncertain'
      ? 'qdot-uncertain'
      : presence === 'response_absent'
        ? 'qdot-absent'
        : 'qdot-unreviewed'

const decisionOptions: { key: Decision; name: string; hint: string }[] = [
  { key: 'response_present', name: '确认存在作答', hint: '选取学生书写的内容作为证据' },
  { key: 'response_absent', name: '确认不存在作答', hint: '需选取题干或空白区域佐证' },
  { key: 'uncertain', name: '标记不确定', hint: '笔迹难以判断、来源归属不明时使用' },
]

function index(node: Node, pointer: string) {
  currentNodes.set(pointer, node)
  if (typeof node.value === 'string') nodes.set(pointer, node.value)
  ;(node.children || []).forEach((child, i) => index(child, `${pointer}/children/${i}`))
}

async function loadPage(signal?: AbortSignal) {
  const svc = await services()
  const result = await svc.submissions.page(sid, pageNumber.value, signal)
  const current = await svc.submissions.canonicalStatus(sid, signal)
  if (
    result.document_id !== view.value?.source.canonical_document_id ||
    current.canonical_document?.canonical_sha256 !== view.value?.source.canonical_sha256
  )
    throw new ApiError(409, 'source_changed')
  currentNodes.clear()
  result.page.blocks.forEach((b, i) =>
    index(b.content, `/pages/${result.page.page_index}/blocks/${i}/content`),
  )
  page.value = result
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const svc = await services()
    const [s, c, v, w] = await Promise.all([
      svc.submissions.detail(sid),
      svc.submissions.canonicalStatus(sid),
      svc.reviews.inspect(sid, qid),
      svc.assignments.workbench(aid, 0),
    ])
    if (s.assignment_id !== aid) throw new Error('提交与作业不匹配。')
    submission.value = s
    view.value = v
    workbench.value = w
    pageCount.value = c.canonical_document?.page_count || 1
    pageNumber.value = Math.min(pageNumber.value, pageCount.value)
    regions.value = []
    nodes.clear()
    conflict.value = false
    permissionDenied.value = false
    verified.value = false
    await loadPage()
    history.value = await svc.reviews.history(sid, qid, historyOffset.value)
    try {
      progress.value = await svc.reviews.progress(sid)
    } catch (e) {
      progress.value = null
      if (!(e instanceof UnsupportedCapabilityError)) throw e
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    loading.value = false
  }
}

async function loadOriginal() {
  if (originalBusy.value || originalUrl.value || originalError.value) return
  originalBusy.value = true
  originalError.value = ''
  try {
    const blob = await (await services()).submissions.file(sid, originalController.signal)
    if (originalController.signal.aborted) return
    if (!['application/pdf', 'image/png', 'image/jpeg'].includes(blob.type))
      throw new Error('此文件类型不能安全预览。')
    originalUrl.value = URL.createObjectURL(blob)
  } catch (e) {
    originalError.value = e instanceof Error ? e.message : '预览未完成。'
  } finally {
    originalBusy.value = false
  }
}

function revokeOriginal() {
  if (originalUrl.value) URL.revokeObjectURL(originalUrl.value)
  originalUrl.value = ''
}

function changeDecision(value: Decision) {
  decision.value = value
  reason.value = allowedReasons[value][0]!
  regions.value = []
  notice.value = ''
  verified.value = false
}

function addText() {
  if (!editable.value) return
  try {
    regions.value = addRegion(regions.value, selectedRegion(window.getSelection(), nodes))
    notice.value = ''
    verified.value = false
  } catch (e) {
    notice.value = (e as Error).message
  }
}

function addAsset(pointer: string) {
  if (!editable.value) return
  try {
    const node = currentNodes.get(pointer)
    if (node?.normalized_type !== 'image' || !node.asset_refs?.length)
      throw new Error('仅能选择有独立素材引用的图片节点。')
    regions.value = addRegion(regions.value, { content_pointer: pointer })
    verified.value = false
  } catch (e) {
    notice.value = (e as Error).message
  }
}

function openPreview() {
  if (!view.value || !editable.value) return
  try {
    makeReview(view.value, decision.value, reason.value, regions.value)
    verified.value = false
    preview.value = true
    notice.value = ''
  } catch (e) {
    notice.value = (e as Error).message
  }
}

async function send(retry = false, thenNext = false) {
  if (busy.value || !view.value || (!retry && !verified.value)) return
  busy.value = true
  preview.value = false
  notice.value = ''
  try {
    const svc = await services()
    if (!retry) {
      const fresh = await svc.reviews.inspect(sid, qid)
      if (
        fresh.expected_revision !== view.value.expected_revision ||
        !sourceMatches(fresh.source, view.value.source)
      )
        throw new ApiError(409, 'review_changed')
      pending.freeze(makeReview(view.value, decision.value, reason.value, regions.value))
      frozen.value = true
    }
    if (!pending.body) throw new Error('没有待确认的原请求。')
    const saved = await svc.reviews.save(sid, qid, pending.body)
    pending.clear()
    frozen.value = false
    historyOffset.value = 0
    await load()
    notice.value = `已确认保存 · 第 ${saved.record.revision} 次审核。这不是成绩发布。`
    if (thenNext) await gotoQuestion(1)
  } catch (e) {
    if (e instanceof ApiError && [403, 404].includes(e.status)) permissionDenied.value = true
    if (e instanceof ApiError && e.status === 409) {
      pending.clear()
      frozen.value = false
      conflict.value = true
      notice.value = e.message
    } else if (e instanceof ApiError && !e.uncertain && e.status !== 401) {
      pending.clear()
      frozen.value = false
      notice.value = e.message
    } else
      notice.value = pending.body
        ? '保存结果待确认，不代表没有提交。请用下方按钮重试完全相同的请求。'
        : e instanceof Error
          ? e.message
          : '保存未完成。'
  } finally {
    busy.value = false
  }
}

async function moreConfirmed() {
  if (!view.value?.confirmed_truncated || view.value.next_confirmed_offset == null) return
  try {
    const next = await (
      await services()
    ).reviews.inspect(sid, qid, 0, view.value.next_confirmed_offset)
    if (
      !sourceMatches(next.source, view.value.source) ||
      next.expected_revision !== view.value.expected_revision
    )
      throw new ApiError(409, 'review_changed')
    view.value = {
      ...view.value,
      confirmed_regions: [
        ...(view.value.confirmed_regions || []),
        ...(next.confirmed_regions || []),
      ],
      confirmed_truncated: next.confirmed_truncated,
      next_confirmed_offset: next.next_confirmed_offset,
    }
  } catch (e) {
    notice.value = e instanceof Error ? e.message : '读取未完成。'
    conflict.value = true
  }
}

async function moreEvidence() {
  if (!view.value?.evidence_truncated || view.value.next_evidence_offset === null) return
  try {
    const next = await (await services()).reviews.inspect(sid, qid, view.value.next_evidence_offset)
    if (
      !sourceMatches(next.source, view.value.source) ||
      next.expected_revision !== view.value.expected_revision
    )
      throw new ApiError(409, 'review_changed')
    view.value = { ...next, evidence: [...view.value.evidence, ...next.evidence] }
  } catch (e) {
    notice.value = e instanceof Error ? e.message : '读取未完成。'
    conflict.value = true
  }
}

async function pageTo(value: number) {
  pageNumber.value = value
  try {
    await loadPage()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取未完成。'
  }
}

async function historyTo(offset: number) {
  historyOffset.value = offset
  try {
    history.value = await (await services()).reviews.history(sid, qid, offset)
  } catch (e) {
    notice.value = e instanceof Error ? e.message : '读取未完成。'
  }
}

async function gotoQuestion(delta: number) {
  const next = currentIndex.value + delta
  if (next < 0 || next >= questions.value.length) return
  const target = questions.value[next]!
  await router.push(`/assignments/${aid}/submissions/${sid}/questions/${target.id}/review`)
}

function snippet(region: Region) {
  const text = nodes.get(region.content_pointer)
  return text && region.text_start != null
    ? Array.from(text).slice(region.text_start, region.text_end!).join('').slice(0, 180)
    : '独立图片节点；请对照原始资料'
}

function regionKind() {
  if (decision.value === 'response_present') return '作答'
  if (decision.value === 'response_absent') return '题干/空白'
  return '不确定'
}

function formatReasons(codes: string[]) {
  return codes.map((code) => reasonLabels[code] || code).join('、')
}

function onKeydown(event: KeyboardEvent) {
  const target = event.target as HTMLElement
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName)) return
  if (event.key === 'ArrowLeft') void gotoQuestion(-1)
  else if (event.key === 'ArrowRight') void gotoQuestion(1)
  else if (event.key === '1') changeDecision('response_present')
  else if (event.key === '2') changeDecision('response_absent')
  else if (event.key === '3') changeDecision('uncertain')
  else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) openPreview()
}

onMounted(() => {
  void load()
  void loadOriginal()
  window.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
  originalController.abort()
  revokeOriginal()
  window.removeEventListener('keydown', onKeydown)
})
</script>
<template>
  <div class="hp-section-gap">
    <PageState :loading="loading" :error="error" @retry="load"
      ><template v-if="view && submission && question"
        ><div class="hp-card" style="padding: 16px 20px">
          <div class="hp-flex" style="justify-content: space-between; align-items: flex-start">
            <div class="hp-grow" style="min-width: 260px">
              <p class="hp-eyebrow">
                {{ workbench?.assignment.title }} · {{ submission.student_ref }} ·
                {{ formatDateTime(submission.submitted_at) }}
              </p>
              <h1 style="font-size: 19px; font-weight: 700; margin-top: 4px">
                第 {{ question.question_number }} 题 · 作答核对
                <span class="hp-badge" style="margin-left: 4px">{{
                  label(questionTypeLabels, question.question_type)
                }}</span>
                <span class="hp-badge">满分 {{ question.max_score }} 分</span>
              </h1>
              <p class="hp-pagehead-desc" style="max-width: 640px">{{ question.stem }}</p>
            </div>
            <div class="hp-pagehead-actions">
              <button
                class="hp-btn hp-btn-secondary hp-btn-sm"
                type="button"
                :disabled="currentIndex <= 0"
                @click="gotoQuestion(-1)"
              >
                <ArrowLeft aria-hidden="true" />上一题
              </button>
              <button
                class="hp-btn hp-btn-secondary hp-btn-sm"
                type="button"
                :disabled="currentIndex < 0 || currentIndex >= questions.length - 1"
                @click="gotoQuestion(1)"
              >
                下一题<ArrowRight aria-hidden="true" />
              </button>
              <FilePreview
                :sid="sid"
                :name="submission.file.original_filename"
                :type="submission.file.content_type"
                @denied="permissionDenied = true"
              />
            </div>
          </div>
        </div>

        <div class="rv-qstrip">
          <div class="rv-qchips">
            <RouterLink
              v-for="item in questions"
              :key="item.id"
              class="rv-qchip"
              :class="{ 'is-current': item.id === qid }"
              :to="`/assignments/${aid}/submissions/${sid}/questions/${item.id}/review`"
              :aria-current="item.id === qid ? 'true' : undefined"
            >
              <span class="qdot" :class="presenceDotClass(presenceOf(item.id))"></span>第
              {{ item.question_number }} 题
            </RouterLink>
          </div>
          <div v-if="progress" class="rv-qlegend">
            <span
              ><span class="qdot qdot-present" style="width: 7px; height: 7px"></span>存在作答</span
            >
            <span
              ><span class="qdot qdot-absent" style="width: 7px; height: 7px"></span>不存在</span
            >
            <span
              ><span class="qdot qdot-uncertain" style="width: 7px; height: 7px"></span>不确定</span
            >
            <span
              ><span class="qdot qdot-unreviewed" style="width: 7px; height: 7px"></span
              >未复核</span
            >
          </div>
        </div>

        <div v-if="permissionDenied" class="hp-note hp-note-warning" role="alert">
          <Info aria-hidden="true" />
          <span>当前资源权限不可用，本页写入已锁定。请返回作业或刷新，服务端将重新校验权限。</span>
        </div>
        <div v-if="conflict" class="hp-note hp-note-warning" role="alert">
          <Info aria-hidden="true" />
          <span
            >来源或他人的核对记录已变化，当前编辑已锁定。刷新后请重新选择证据，系统不会覆盖他人修订。</span
          >
          <button class="hp-btn hp-btn-primary hp-btn-sm" type="button" @click="load">
            刷新来源并重新核对
          </button>
        </div>
        <div v-if="frozen" class="hp-note hp-note-warning" role="alert">
          <Info aria-hidden="true" />
          <span
            >上一笔保存结果待确认，编辑已锁定。重试会发送原 request_id
            与完全相同的正文，不会重复写入。</span
          >
          <button
            class="hp-btn hp-btn-primary hp-btn-sm"
            type="button"
            :disabled="busy"
            @click="send(true)"
          >
            重试原请求
          </button>
        </div>
        <p v-if="notice" class="hp-note hp-note-info" role="status">{{ notice }}</p>

        <div class="rv-work">
          <section class="rv-pane" aria-label="原始试卷">
            <div class="rv-pane-head">
              <span class="rv-pane-title"><FileText aria-hidden="true" />原始试卷</span>
              <div class="rv-pane-tools">
                <button
                  class="rv-mini"
                  type="button"
                  aria-label="缩小"
                  @click="zoom = Math.max(0.5, zoom - 0.25)"
                >
                  <ZoomOut aria-hidden="true" />
                </button>
                <span class="rv-zoom">{{ Math.round(zoom * 100) }}%</span>
                <button
                  class="rv-mini"
                  type="button"
                  aria-label="放大"
                  @click="zoom = Math.min(2, zoom + 0.25)"
                >
                  <ZoomIn aria-hidden="true" />
                </button>
              </div>
            </div>
            <div class="rv-pane-body">
              <div v-if="originalBusy" class="hp-state-card">
                <span class="hp-spin" />正在读取原始文件…
              </div>
              <div v-else-if="originalError" class="hp-state-card">
                <h3>原始文件暂时无法预览</h3>
                <p>{{ originalError }}</p>
                <button
                  class="hp-btn hp-btn-secondary hp-btn-sm"
                  type="button"
                  @click="((originalError = ''), loadOriginal())"
                >
                  重新加载
                </button>
              </div>
              <div v-else-if="originalUrl" class="rv-paper-wrap">
                <iframe
                  v-if="submission.file.content_type === 'application/pdf'"
                  :src="originalUrl"
                  title="受保护的原始 PDF"
                  class="rv-paper"
                  :style="{ width: `${zoom * 100}%`, height: '70vh' }"
                  style="border: none"
                />
                <img
                  v-else
                  :src="originalUrl"
                  :alt="submission.file.original_filename"
                  class="rv-paper"
                  :style="{ width: `${zoom * 100}%` }"
                />
              </div>
              <div v-else class="hp-empty">
                <div class="hp-empty-title">原始试卷</div>
                <p class="hp-empty-desc">通过认证请求读取的受保护预览，仅在当前页面临时加载。</p>
              </div>
            </div>
            <div class="rv-pane-foot">
              <ShieldCheck aria-hidden="true" style="width: 12px; height: 12px" />
              受保护预览 · 不外链原始文件 · {{ submission.file.original_filename }}
            </div>
          </section>

          <section class="rv-pane" aria-label="作答内容">
            <div class="rv-pane-head">
              <span class="rv-pane-title"><ClipboardPen aria-hidden="true" />作答内容</span>
              <span class="hp-badge hp-badge-primary" style="height: 22px; font-size: 11px"
                >拖选文字即可选取证据</span
              >
              <div class="rv-pane-tools">
                <span class="rv-pagenav">
                  <button
                    class="rv-mini"
                    type="button"
                    aria-label="上一页"
                    :disabled="pageNumber <= 1"
                    @click="pageTo(pageNumber - 1)"
                  >
                    <ChevronLeft aria-hidden="true" />
                  </button>
                  <b>{{ pageNumber }}</b
                  >/ {{ pageCount }}
                  <button
                    class="rv-mini"
                    type="button"
                    aria-label="下一页"
                    :disabled="pageNumber >= pageCount"
                    @click="pageTo(pageNumber + 1)"
                  >
                    <ChevronRight aria-hidden="true" />
                  </button>
                </span>
              </div>
            </div>
            <div class="rv-pane-body">
              <div v-if="page" data-testid="canonical-paper">
                <div
                  v-for="(block, index) in page.page.blocks"
                  :key="block.block_id"
                  class="rv-block"
                  :class="{
                    'is-related': related.has(
                      `/pages/${page.page.page_index}/blocks/${index}/content`,
                    ),
                  }"
                >
                  <span class="rv-block-tag">
                    {{ index + 1 }} · {{ contentTypeLabels[block.normalized_type] ?? '内容' }}
                    {{
                      related.has(`/pages/${page.page.page_index}/blocks/${index}/content`)
                        ? '· 与本题关联'
                        : ''
                    }}
                  </span>
                  <SourceNode
                    :node="block.content"
                    :pointer="`/pages/${page.page.page_index}/blocks/${index}/content`"
                    :related="related"
                    :disabled="!editable"
                    :confirmed="view.confirmed_regions || []"
                    @asset="addAsset"
                  />
                </div>
              </div>
              <button
                class="hp-btn hp-btn-secondary hp-btn-sm"
                type="button"
                style="margin-top: 4px"
                :disabled="!editable"
                @click="addText"
              >
                <ListChecks aria-hidden="true" />将当前文字选区加入证据
              </button>
            </div>
            <div class="rv-legend">
              <span
                ><i style="background: #d8f0e1; box-shadow: 0 0 0 1px #c5e5d2"></i
                >此前确认作答（只读）</span
              >
              <span
                ><i style="background: #e8ecf2; box-shadow: 0 0 0 1px #dde2eb"></i>排除题干</span
              >
              <span><i style="background: #fbefd5; box-shadow: 0 0 0 1px #f0dcb0"></i>不确定</span>
              <span>浅蓝边框 = 与本题关联 · 不代表已作答</span>
            </div>
            <div v-if="view.confirmed_regions?.length" class="rv-pane-foot" style="display: block">
              <b style="color: var(--hp-ink-700)">本次来源的人工记录（只读）：</b>
              <span
                v-for="(r, i) in view.confirmed_regions"
                :key="i"
                style="display: inline-block; margin: 2px 4px 2px 0"
                class="hp-badge"
                :class="{
                  'hp-badge-success': r.role === 'response_regions',
                  'hp-badge-warning': r.role === 'uncertain_regions',
                }"
              >
                第 {{ r.page_number }} 页 ·
                {{
                  r.role === 'response_regions'
                    ? '确认作答'
                    : r.role === 'excluded_prompt_regions'
                      ? '排除题干'
                      : '不确定'
                }}
              </span>
              <button
                v-if="view.confirmed_truncated"
                class="hp-linkbtn"
                type="button"
                @click="moreConfirmed"
              >
                继续读取人工范围
              </button>
            </div>
            <details class="hp-details" style="margin: 0 14px 10px; border-top: none">
              <summary>
                <ChevronDown aria-hidden="true" class="chev" />已关联的来源范围（{{
                  view.evidence.length
                }}）
              </summary>
              <p
                v-for="(e, index) in view.evidence"
                :key="index"
                class="hp-fine"
                style="padding: 2px 4px"
              >
                第 {{ e.page_number }} 页 · {{ contentTypeLabels[e.normalized_type] ?? '内容' }}
                {{
                  e.text_start != null ? `· 文本范围 [${e.text_start}, ${e.text_end})` : '· 整节点'
                }}
              </p>
              <button
                v-if="view.evidence_truncated"
                class="hp-linkbtn"
                type="button"
                @click="moreEvidence"
              >
                继续读取关联来源
              </button>
            </details>
          </section>

          <aside class="rv-pane rv-pane-decision" aria-label="人工判断">
            <div class="rv-status">
              <span
                >最新判断 <b>{{ label(presenceLabels, view.answer_presence) }}</b></span
              >
              <span
                >审核记录 <b>R{{ view.expected_revision }}</b></span
              >
              <span
                v-if="view.latest"
                :class="view.latest_source_matches ? '' : 'hp-error-text'"
                style="margin-left: auto"
              >
                {{
                  view.latest_source_matches
                    ? '最新决定与本次来源匹配'
                    : '历史决定来源已过期，需重新核对'
                }}
              </span>
              <span v-else style="margin-left: auto">尚无人工决定</span>
            </div>
            <div class="rv-form">
              <div v-if="!workbench?.can_write" class="hp-note hp-note-info" role="note">
                此作业为只读授权，不能保存复核。
              </div>
              <div>
                <span class="hp-field-label" style="display: block; margin-bottom: 7px"
                  >本次判断</span
                >
                <div class="rv-decision" role="radiogroup" aria-label="审核决定">
                  <label
                    v-for="option in decisionOptions"
                    :key="option.key"
                    class="rv-option"
                    :class="{ 'is-selected': decision === option.key }"
                  >
                    <input
                      type="radio"
                      name="decision"
                      :value="option.key"
                      :checked="decision === option.key"
                      style="position: absolute; opacity: 0; width: 1px; height: 1px"
                      @change="changeDecision(option.key)"
                    />
                    <span class="rv-radio" aria-hidden="true"></span>
                    <span>
                      <span class="rv-option-name">{{ option.name }}</span
                      ><br />
                      <span class="rv-option-hint">{{ option.hint }}</span>
                    </span>
                  </label>
                </div>
              </div>
              <label class="hp-field">
                <span class="hp-field-label">判断原因</span>
                <select v-model="reason" class="hp-select" :disabled="!editable">
                  <option v-for="code in allowedReasons[decision]" :key="code" :value="code">
                    {{ reasonLabels[code] }}
                  </option>
                </select>
              </label>
              <div>
                <div class="hp-flex" style="margin-bottom: 8px">
                  <span class="hp-field-label">已选证据</span>
                  <span class="hp-badge" style="height: 20px; font-size: 11px"
                    >{{ regions.length }} 个区域</span
                  >
                </div>
                <p v-if="!regions.length" class="hp-fine">
                  请在左侧原文选择范围后点击“将当前文字选区加入证据”。未选择不等于学生没有作答。
                </p>
                <ol v-else class="selection-list rv-evi-list">
                  <li v-for="(r, i) in regions" :key="i" class="rv-evi">
                    <div class="hp-grow" style="min-width: 0">
                      <div class="rv-evi-text">{{ snippet(r) }}</div>
                      <div class="rv-evi-meta">
                        {{ regionKind() }}证据{{
                          nodes.has(r.content_pointer)
                            ? ` · 第 ${pageNumber} 页 · 文本`
                            : ' · 图片整节点'
                        }}
                      </div>
                    </div>
                    <button
                      class="rv-evi-x"
                      type="button"
                      :disabled="!editable"
                      aria-label="移除此证据"
                      @click="regions.splice(i, 1)"
                    >
                      ✕
                    </button>
                  </li>
                </ol>
              </div>
              <div style="display: flex; flex-direction: column; gap: 8px">
                <el-button
                  type="primary"
                  size="large"
                  class="full"
                  :disabled="!editable"
                  @click="openPreview"
                  >预览并保存复核</el-button
                >
                <button
                  class="hp-btn hp-btn-secondary"
                  type="button"
                  :disabled="!editable"
                  @click="send(false, true)"
                >
                  保存并核对下一题<ArrowRight aria-hidden="true" />
                </button>
                <p class="hp-fine">
                  保存后生成第
                  {{ view.expected_revision + 1 }} 次审核记录；此操作不产生、也不发布成绩。
                </p>
              </div>
              <div class="rv-keys">
                <span><span class="hp-kbd">←</span><span class="hp-kbd">→</span> 切换题目</span>
                <span
                  ><span class="hp-kbd">1</span><span class="hp-kbd">2</span
                  ><span class="hp-kbd">3</span> 选择判断</span
                >
                <span
                  ><span class="hp-kbd">Ctrl</span>+<span class="hp-kbd">Enter</span>
                  打开保存预览</span
                >
              </div>
            </div>

            <details class="hp-details" open style="margin: 0">
              <summary>
                <ChevronDown aria-hidden="true" class="chev" />本题审核历史
                <span class="hp-details-count"
                  >（{{ history?.records.length ?? 0 }} 条 · 只读）</span
                >
              </summary>
              <div class="rv-history history-panel">
                <p v-if="!history?.records.length" class="hp-fine" style="padding: 8px 0">
                  尚无复核记录。
                </p>
                <article
                  v-for="record in history?.records"
                  :key="record.decision_id"
                  class="history-row rv-hrow"
                >
                  <span class="rv-hrev">R{{ record.revision }}</span>
                  <div class="hp-grow" style="min-width: 0">
                    <div class="rv-hline">
                      <b>{{ label(presenceLabels, record.decision) }}</b>
                      <span
                        class="hp-badge"
                        :class="{
                          'hp-badge-success': record.decision === 'response_present',
                          'hp-badge-warning': record.decision === 'uncertain',
                        }"
                        style="height: 20px; font-size: 11px"
                        >{{ formatReasons(record.reason_codes) }}</span
                      >
                    </div>
                    <div class="rv-hmeta">
                      {{ formatRelative(record.created_at) }} ·
                      {{
                        record.reviewer_kind === 'authenticated_teacher'
                          ? '认证教师'
                          : '历史自声明身份（未认证）'
                      }}
                    </div>
                    <div
                      class="rv-hnote"
                      :class="sourceMatches(record.source, view.source) ? 'ok' : 'stale'"
                    >
                      {{
                        sourceMatches(record.source, view.source)
                          ? '与本次读取来源一致'
                          : '来源版本不同，仅供历史审计'
                      }}
                      · 前序 {{ record.supersedes_decision_id ? '已关联' : '无' }}
                    </div>
                  </div>
                </article>
                <div class="hp-flex" style="justify-content: center; padding-top: 8px">
                  <button
                    class="hp-btn hp-btn-ghost hp-btn-sm"
                    type="button"
                    :disabled="historyOffset === 0"
                    @click="historyTo(Math.max(0, historyOffset - 10))"
                  >
                    上一页历史
                  </button>
                  <span class="hp-fine">仅显示本题记录</span>
                  <button
                    class="hp-btn hp-btn-ghost hp-btn-sm"
                    type="button"
                    :disabled="!history?.has_next"
                    @click="historyTo(Number(history?.next_offset || 0))"
                  >
                    下一页历史
                  </button>
                </div>
              </div>
            </details>

            <details class="hp-details" style="margin: 0 0 12px">
              <summary><ChevronDown aria-hidden="true" class="chev" />来源版本校验（诊断）</summary>
              <div class="rv-diag">
                <div class="rv-diag-row">
                  <span class="rv-diag-key">Canonical</span>
                  <span class="rv-diag-val">{{ view.source.canonical_sha256.slice(0, 12) }}…</span>
                </div>
                <div class="rv-diag-row">
                  <span class="rv-diag-key">对齐结果</span>
                  <span class="rv-diag-val">{{ view.source.alignment_sha256.slice(0, 12) }}…</span>
                </div>
                <div class="rv-diag-row">
                  <span class="rv-diag-key">对齐器版本</span>
                  <span class="rv-diag-val">{{ view.source.aligner_version }}</span>
                </div>
                <div class="rv-diag-row">
                  <span class="rv-diag-key">修订基准</span>
                  <span class="rv-diag-val">expected_revision = {{ view.expected_revision }}</span>
                </div>
                <div class="rv-diag-row">
                  <span class="rv-diag-key">关联状态</span>
                  <span class="rv-diag-val">{{ view.alignment_status }}</span>
                </div>
                <p class="hp-fine" style="margin-top: 4px">
                  保存时自动校验来源一致性；不一致将阻止提交并提示刷新。日常批阅无需关注。
                </p>
              </div>
            </details>
          </aside>
        </div>

        <el-dialog
          v-model="preview"
          title="确认本次人工复核"
          width="560"
          :close-on-click-modal="false"
        >
          <p style="margin-bottom: 8px">
            决定：<b>{{ label(presenceLabels, decision) }}</b>
          </p>
          <p style="margin-bottom: 8px">
            原因：{{ reasonLabels[reason] }} · 已选 {{ regions.length }} 个证据范围
          </p>
          <p class="hp-fine" style="margin-bottom: 10px">
            基于当前第
            {{ view.expected_revision }}
            次审核及本次读取的作答来源提交；保存前会再次检查是否有人修改。
          </p>
          <ol class="selection-list rv-evi-list">
            <li v-for="(r, index) in regions" :key="index" class="rv-evi">{{ snippet(r) }}</li>
          </ol>
          <label class="hp-check"
            ><input
              v-model="verified"
              type="checkbox"
            />我已对照来源核对，本次仅确认作答证据，不发布成绩。</label
          >
          <template #footer>
            <el-button @click="preview = false">继续核对</el-button>
            <el-button type="primary" :disabled="!verified" :loading="busy" @click="send()"
              >确认保存</el-button
            >
          </template>
        </el-dialog>
      </template></PageState
    >
  </div>
</template>
