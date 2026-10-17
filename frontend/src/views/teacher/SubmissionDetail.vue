<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  ArrowLeft,
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  ClipboardPen,
  FileText,
  Sparkles,
  TriangleAlert,
} from 'lucide-vue-next'
import { services, type S } from '../../services'
import { UnsupportedCapabilityError, type SubmissionReviewProgress } from '../../services/types'
import {
  contentTypeLabels,
  alignmentLabels,
  formatBytes,
  formatDateTime,
  label,
  parsingLabels,
  presenceLabels,
  questionTypeLabels,
} from '../../lib/labels'
import EmptyState from '../../components/EmptyState.vue'
import FilePreview from '../../components/FilePreview.vue'
import PageState from '../../components/PageState.vue'
import SourceNode from '../../components/SourceNode.vue'

/**
 * 提交详情：文件与解析状态 → 作答内容（Canonical）→ 逐题进入核对。
 * 受保护文件预览复用真实 Blob 管线；真实模式无逐题进度聚合时如实说明。
 */
const route = useRoute(),
  aid = String(route.params.aid),
  sid = String(route.params.sid)

const state = reactive({
  loading: false,
  error: '',
  submission: null as S['SubmissionRead'] | null,
  parsing: null as S['ParsingTaskStatusRead'] | null,
  canonical: null as S['CanonicalDocumentStatusRead'] | null,
  alignment: null as S['AlignmentSummaryRead'] | null,
  workbench: null as S['AssignmentWorkbench'] | null,
  page: null as S['CanonicalPageRead'] | null,
  pageNumber: 1,
})
const progress = ref<SubmissionReviewProgress | null>(null)
const progressUnsupported = ref(false)
const notice = ref('')

const pageCount = computed(() => state.canonical?.canonical_document?.page_count ?? 1)
const canonicalReady = computed(
  () =>
    state.canonical?.status === 'available' && (state.alignment?.status ?? '') !== 'not_generated',
)
const questions = computed(() =>
  (state.workbench?.questions ?? []).slice().sort((a, b) => a.question_number - b.question_number),
)
const denied = ref(false)

async function load() {
  state.loading = true
  state.error = ''
  denied.value = false
  try {
    const svc = await services()
    const [submission, workbench, canonical, parsing, alignment] = await Promise.all([
      svc.submissions.detail(sid),
      svc.assignments.workbench(aid, 0),
      svc.submissions.canonicalStatus(sid),
      svc.submissions.parsingStatus(sid),
      svc.submissions.alignment(sid),
    ])
    if (submission.assignment_id !== aid) throw new Error('提交与当前作业不匹配。')
    state.submission = submission
    state.workbench = workbench
    state.canonical = canonical
    state.parsing = parsing
    state.alignment = alignment
    state.pageNumber = 1
    await loadPage()
    try {
      progress.value = await svc.reviews.progress(sid)
      progressUnsupported.value = false
    } catch (e) {
      progress.value = null
      progressUnsupported.value = e instanceof UnsupportedCapabilityError
    }
  } catch (e) {
    state.error = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    state.loading = false
  }
}

async function loadPage() {
  if (!canonicalReady.value) return
  const current = await (await services()).submissions.page(sid, state.pageNumber)
  const document = state.canonical?.canonical_document
  if (document && current.document_id !== document.document_id)
    throw new Error('作答内容版本已变化，请刷新。')
  state.page = current
}

async function pageTo(delta: number) {
  const next = state.pageNumber + delta
  if (next < 1 || next > pageCount.value) return
  state.pageNumber = next
  await loadPage()
}

function parseBadgeClass(status: string) {
  if (status === 'succeeded') return 'hp-badge-success'
  if (status === 'failed') return 'hp-badge-danger'
  if (status === 'running' || status === 'pending') return 'hp-badge-primary'
  return 'hp-badge-warning'
}
function presenceBadgeClass(presence: string) {
  if (presence === 'response_present') return 'hp-badge-success'
  if (presence === 'uncertain') return 'hp-badge-warning'
  if (presence === 'response_absent') return ''
  return ''
}

onMounted(load)
</script>
<template>
  <div class="hp-section-gap">
    <RouterLink class="hp-linkbtn" :to="`/assignments/${aid}`"
      ><ArrowLeft aria-hidden="true" style="width: 14px; height: 14px" />返回作业</RouterLink
    >
    <PageState :loading="state.loading" :error="state.error" @retry="load">
      <div v-if="state.submission" class="hp-section-gap">
        <div class="hp-pagehead hp-card" style="padding: 18px 20px">
          <div class="hp-grow" style="min-width: 260px">
            <p class="hp-eyebrow">{{ state.workbench?.assignment.title }}</p>
            <h1 style="font-size: 21px; font-weight: 700; margin-top: 4px">
              提交 · {{ state.submission.student_ref }}
            </h1>
            <p class="hp-pagehead-desc">
              提交于 {{ formatDateTime(state.submission.submitted_at) }} ·
              {{ state.submission.file.original_filename }}（{{
                formatBytes(state.submission.file.size_bytes)
              }}）· 学生标识，非认证身份
            </p>
          </div>
          <div class="hp-pagehead-actions">
            <span class="hp-badge" :class="parseBadgeClass(state.submission.parsing_task.status)">
              <span
                class="dot"
                :class="{ 'hp-dot-pulse': state.submission.parsing_task.status === 'running' }"
              />
              {{ label(parsingLabels, state.submission.parsing_task.status) }}
            </span>
            <FilePreview
              :sid="sid"
              :name="state.submission.file.original_filename"
              :type="state.submission.file.content_type"
              @denied="denied = true"
            />
          </div>
        </div>

        <div v-if="denied" class="hp-note hp-note-warning" role="alert">
          <TriangleAlert aria-hidden="true" />
          <span
            ><b>资源权限不可用。</b
            >当前账号无法读取该文件，请返回作业列表或刷新，服务端会重新校验权限。</span
          >
        </div>

        <div v-if="notice" class="hp-note hp-note-success" role="status">{{ notice }}</div>

        <div
          v-if="state.parsing && ['failed', 'retry_wait'].includes(state.parsing.status)"
          class="hp-note hp-note-warning"
          role="alert"
        >
          <TriangleAlert aria-hidden="true" />
          <span>
            <b>{{ state.parsing.status === 'failed' ? '解析失败' : '解析等待重试' }}：</b
            >{{ state.parsing.last_error_message || '原因未知' }}（已尝试
            {{ state.parsing.attempt_count }}/{{ state.parsing.max_attempts }}
            次）。可稍后刷新查看；系统不会把解析失败的提交当作学生未作答。
          </span>
        </div>

        <div class="hp-stats">
          <div class="hp-stat">
            <div class="hp-stat-icon"><FileText aria-hidden="true" /></div>
            <div>
              <div class="hp-stat-num">
                {{ state.canonical?.canonical_document?.page_count ?? 0 }}<small>页</small>
              </div>
              <div class="hp-stat-label">
                {{ label(alignmentLabels, state.alignment?.status ?? 'not_generated') }}
              </div>
            </div>
          </div>
          <div class="hp-stat">
            <div class="hp-stat-icon is-teal"><ClipboardPen aria-hidden="true" /></div>
            <div>
              <div class="hp-stat-num">
                {{ state.alignment?.aligned_count ?? 0 }}<small>题</small>
              </div>
              <div class="hp-stat-label">来源已关联 · 不代表已作答</div>
            </div>
          </div>
          <div v-if="progress" class="hp-stat">
            <div class="hp-stat-icon is-amber"><ClipboardPen aria-hidden="true" /></div>
            <div>
              <div class="hp-stat-num">
                {{ progress.questions.filter((q) => q.answer_presence !== 'unreviewed').length
                }}<small>/{{ progress.questions.length }}</small>
              </div>
              <div class="hp-stat-label">已人工核对题数</div>
            </div>
          </div>
        </div>

        <section class="hp-card">
          <div class="hp-card-head">
            <h2 class="hp-card-title"><FileText aria-hidden="true" />作答内容（解析结果）</h2>
            <template v-if="canonicalReady">
              <span class="hp-card-note">按页显示 · 不执行外部内容</span>
              <span class="hp-card-side hp-flex hp-gap-sm">
                <button
                  class="hp-btn hp-btn-secondary hp-btn-sm"
                  type="button"
                  :disabled="state.pageNumber <= 1"
                  @click="pageTo(-1)"
                >
                  <ChevronLeft aria-hidden="true" />上一页
                </button>
                <span class="hp-fine num">第 {{ state.pageNumber }} / {{ pageCount }} 页</span>
                <button
                  class="hp-btn hp-btn-secondary hp-btn-sm"
                  type="button"
                  :disabled="state.pageNumber >= pageCount"
                  @click="pageTo(1)"
                >
                  下一页<ChevronRight aria-hidden="true" />
                </button>
              </span>
            </template>
          </div>
          <div class="hp-card-body">
            <EmptyState
              v-if="!canonicalReady"
              :title="
                state.submission.parsing_task.status === 'succeeded'
                  ? '作答内容尚未生成'
                  : '等待解析完成'
              "
              :desc="
                state.submission.parsing_task.status === 'succeeded'
                  ? '解析已成功，标准化文档生成中，稍后刷新即可查看。'
                  : '解析完成后会自动生成分页的作答内容，届时可逐题核对证据。'
              "
            />
            <div v-else-if="state.page" class="rv-block" style="margin: 0">
              <div
                v-for="(block, index) in state.page.page.blocks"
                :key="block.block_id"
                style="margin-bottom: 10px"
              >
                <div class="hp-fine" style="margin-bottom: 3px">
                  {{ index + 1 }} · {{ contentTypeLabels[block.normalized_type] ?? '内容' }}
                </div>
                <SourceNode
                  :node="block.content"
                  :pointer="`/pages/${state.page.page.page_index}/blocks/${index}/content`"
                  :related="new Set()"
                  :disabled="true"
                  :confirmed="[]"
                />
              </div>
            </div>
          </div>
        </section>

        <section class="hp-card">
          <div class="hp-card-head">
            <h2 class="hp-card-title"><ClipboardPen aria-hidden="true" />逐题核对</h2>
            <span class="hp-card-note">题号关联仅表示来源映射，不代表学生已作答</span>
          </div>
          <div class="hp-table-wrap">
            <table v-if="questions.length" class="hp-table">
              <thead>
                <tr>
                  <th style="width: 90px">题号</th>
                  <th>题干</th>
                  <th v-if="state.alignment">来源关联</th>
                  <th v-if="progress">人工判断</th>
                  <th style="text-align: right">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="question in questions" :key="question.id">
                  <td>
                    <span class="hp-cell-title">第 {{ question.question_number }} 题</span>
                    <div class="hp-cell-sub">
                      {{ label(questionTypeLabels, question.question_type) }} ·
                      {{ question.max_score }} 分
                    </div>
                  </td>
                  <td style="max-width: 380px">
                    <span style="font-size: 13px; color: var(--hp-ink-700)"
                      >{{ question.stem.slice(0, 60)
                      }}{{ question.stem.length > 60 ? '…' : '' }}</span
                    >
                  </td>
                  <td v-if="state.alignment">
                    <span
                      v-if="
                        state.alignment.status === 'complete' ||
                        state.alignment.status === 'review_required'
                      "
                      class="hp-badge"
                      :class="{ 'hp-badge-warning': state.alignment.review_required_count > 0 }"
                      >已对齐</span
                    >
                    <span v-else class="hp-badge">尚未对齐</span>
                  </td>
                  <td v-if="progress">
                    <span
                      class="hp-badge"
                      :class="
                        presenceBadgeClass(
                          progress.questions.find((q) => q.question_id === question.id)
                            ?.answer_presence ?? 'unreviewed',
                        )
                      "
                    >
                      {{
                        label(
                          presenceLabels,
                          progress.questions.find((q) => q.question_id === question.id)
                            ?.answer_presence ?? 'unreviewed',
                        )
                      }}
                    </span>
                  </td>
                  <td class="hp-cell-actions">
                    <RouterLink
                      v-if="canonicalReady"
                      class="hp-linkbtn"
                      :to="`/assignments/${aid}/submissions/${sid}/questions/${question.id}/review`"
                      >核对作答<ArrowRight aria-hidden="true" style="width: 13px; height: 13px"
                    /></RouterLink>
                    <span v-else class="hp-fine">等待来源就绪</span>
                  </td>
                </tr>
              </tbody>
            </table>
            <EmptyState
              v-else
              title="作业还没有题目"
              desc="先在作业页添加题目，才能逐题核对作答。"
            />
          </div>
          <p v-if="progressUnsupported" class="hp-fine" style="padding: 10px 16px">
            逐题人工判断进度需要后端聚合接口（开发中）；当前仍可逐题进入核对页面查看与保存判断。
          </p>
        </section>

        <section class="hp-card">
          <div class="hp-card-head">
            <h2 class="hp-card-title"><Sparkles aria-hidden="true" />AI 候选评分</h2>
            <span class="hp-card-note">候选评分仅作参考，不代表正式成绩</span>
          </div>
          <div class="hp-card-body">
            <p class="hp-fine" style="margin-bottom: 10px">
              解析完成且生成候选评分后，可从题目进入候选评分审核页面进行接受、修订或拒绝。
            </p>
            <div class="hp-flex">
              <RouterLink
                v-for="question in questions.slice(0, 3)"
                :key="'g' + question.id"
                class="hp-btn hp-btn-secondary hp-btn-sm"
                :to="`/assignments/${aid}/submissions/${sid}/questions/${question.id}/grading`"
                >第 {{ question.question_number }} 题候选<ArrowRight aria-hidden="true"
              /></RouterLink>
            </div>
          </div>
        </section>
      </div>
    </PageState>
  </div>
</template>
