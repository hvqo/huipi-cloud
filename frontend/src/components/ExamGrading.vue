<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { examGrading, type MarkQuestionLink } from '../api/exam-grading'
import { request, ApiError, type Schema, type Region } from '../api/client'
import { selectedRegion, addRegion } from '../lib/selection'
import { scans } from '../api/scans'
import ScanThumbnail from './ScanThumbnail.vue'
import VisualGrading from './VisualGrading.vue'

const props = defineProps<{
  jobId: string
  studentLabel?: string
  workspaceId?: string
  taskId?: string
  readOnly?: boolean
}>()
const questions = ref<MarkQuestionLink[]>([]),
  current = ref<Schema['ExamMarkDetails']>()
const workspace = ref(''),
  userId = ref(''),
  busy = ref(false),
  error = ref(''),
  notice = ref('')
const regions = ref<Region[]>([]),
  manual = ref(false),
  preview = ref('')
const previewZoom = ref(100)
const evidenceSegment = ref<Record<string, string>>({})
const revised = ref<Record<string, string>>({}),
  reviewReason = ref('')
const pending = ref<{
  workspace: string
  action: 'answer-review' | 'scoring' | 'score-review'
  body: Schema['AnswerDecision'] | Schema['ScoreRequest'] | Schema['ScoreReview']
}>()
const initRequest = ref('')
let alive = true,
  timer: ReturnType<typeof setTimeout> | undefined
const locked = computed(() => busy.value || !!pending.value || !!props.readOnly)
const labels: Record<string, string> = {
  not_started: '尚未评分',
  pending: '等待评分',
  running: '正在评分',
  succeeded: '建议已生成',
  failed: '评分未完成',
  invalidated: '来源已变更',
  accepted: '接受候选',
  revised: '教师修订',
  rejected: '拒绝候选',
  regrading_required: '需要重新评分',
}
const nodeMap = computed(
  () => new Map((current.value?.nodes || []).map((n) => [n.content_pointer, n.text])),
)
function message(e: unknown) {
  return e instanceof ApiError
    ? e.message
    : e instanceof Error
      ? e.message
      : '操作未完成，请重新核对。'
}
async function run(work: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await work()
  } catch (e) {
    if (alive) error.value = message(e)
  } finally {
    if (alive) busy.value = false
  }
}
async function refresh() {
  clearTimeout(timer)
  if (!workspace.value) return
  const wid = workspace.value
  const result = await examGrading.details(wid)
  if (!alive || workspace.value !== wid) return
  current.value = result
  if (['pending', 'running'].includes(result.scoring_status))
    timer = setTimeout(() => void run(refresh), 3000)
}
async function load() {
  if (props.workspaceId) {
    workspace.value = props.workspaceId
    await refresh()
    if (current.value)
      questions.value = [
        {
          question_id: current.value.question.id,
          question_number: current.value.question.number,
          stem: current.value.question.stem,
          max_score: current.value.question.max_score,
          workspace_id: props.workspaceId,
        },
      ]
    return
  }
  const r = await examGrading.questions(props.jobId)
  if (!alive) return
  questions.value = r.questions
  if (!workspace.value && r.questions[0]?.workspace_id)
    workspace.value = r.questions[0].workspace_id
  await refresh()
}
async function initialize() {
  await run(async () => {
    initRequest.value ||= crypto.randomUUID()
    await examGrading.initialize(props.jobId, initRequest.value)
    initRequest.value = ''
    await load()
  })
}
async function chooseQuestion() {
  current.value = undefined
  regions.value = []
  manual.value = false
  await run(refresh)
}
function addSelected() {
  try {
    regions.value = addRegion(regions.value, selectedRegion(window.getSelection(), nodeMap.value))
    window.getSelection()?.removeAllRanges()
  } catch (e) {
    error.value = message(e)
  }
}
function addWhole(pointer: string) {
  try {
    regions.value = addRegion(regions.value, {
      content_pointer: pointer,
      text_start: 0,
      text_end: Array.from(nodeMap.value.get(pointer) || '').length,
    })
  } catch (e) {
    error.value = message(e)
  }
}
async function send() {
  const attempt = pending.value
  if (!attempt) return
  try {
    await examGrading.mutate(attempt.workspace, attempt.action, attempt.body)
    if (!alive) return
    pending.value = undefined
    regions.value = []
    notice.value = '已保存，请核对当前结果。尚未发布正式成绩。'
    await refresh()
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
    throw e
  }
}
async function confirm(decision: 'response_present' | 'uncertain') {
  if (!current.value) return
  pending.value = {
    workspace: workspace.value,
    action: 'answer-review',
    body: {
      request_id: crypto.randomUUID(),
      expected_revision: current.value.answer_review?.revision || 0,
      manual_attribution: manual.value,
      payload: {
        decision,
        response_regions: decision === 'response_present' ? regions.value : [],
        uncertain_regions: decision === 'uncertain' ? regions.value : [],
        excluded_prompt_regions: [],
        reason_codes:
          decision === 'response_present'
            ? ['student_work_visible']
            : ['source_quality_insufficient'],
        reviewer_ref: `teacher_${userId.value.replaceAll('-', '')}`,
      },
    },
  }
  await run(send)
}
async function score() {
  if (!current.value?.answer_review) return
  pending.value = {
    workspace: workspace.value,
    action: 'scoring',
    body: {
      request_id: crypto.randomUUID(),
      expected_answer_revision: current.value.answer_review.revision,
    },
  }
  await run(send)
}
function editScores() {
  revised.value = Object.fromEntries(
    (current.value?.proposal?.criterion_scores || []).map((c) => [
      c.criterion_id,
      String(c.awarded_points),
    ]),
  )
  reviewReason.value = ''
  evidenceSegment.value = {}
}
async function review(status: 'accepted' | 'revised' | 'rejected' | 'regrading_required') {
  const d = current.value
  if (!d?.proposal) return
  const criteria: Schema['GradeReviewCriterionInput'][] = []
  if (status === 'revised') {
    for (const c of d.proposal.criterion_scores || []) {
      const value = revised.value[c.criterion_id] ?? String(c.awarded_points)
      if (!/^\d+(\.\d{1,2})?$/.test(value)) {
        error.value = '请输入最多两位小数的分值。'
        return
      }
      const cents = (v: string) => {
        const [a, b = ''] = v.split('.')
        return BigInt(a!) * 100n + BigInt((b + '00').slice(0, 2))
      }
      const changed = cents(value) !== cents(String(c.awarded_points))
      const selected = d.grade_input?.selected_answer_segments.find(
        (s) => String(s.segment_index) === evidenceSegment.value[c.criterion_id],
      )
      const evidence = selected
        ? [
            {
              kind: 'quote' as const,
              segment_index: selected.segment_index,
              text_start: 0,
              text_end: Array.from(selected.text).length,
              quote: selected.text,
            },
          ]
        : c.evidence_references
      criteria.push({
        criterion_id: c.criterion_id,
        reviewed_points: value,
        ...(changed ? { rationale: reviewReason.value, evidence_references: evidence } : {}),
      })
    }
  }
  pending.value = {
    workspace: workspace.value,
    action: 'score-review',
    body: {
      request_id: crypto.randomUUID(),
      expected_revision: d.history.at(-1)?.review_revision || 0,
      payload: {
        schema_name: 'huipi.grading.review',
        schema_version: '1.0',
        review_status: status,
        reviewer_ref: `teacher_${userId.value.replaceAll('-', '')}`,
        criterion_reviews: criteria,
        review_reason_codes: status === 'accepted' ? [] : ['manual_follow_up_needed'],
        review_comment: reviewReason.value || null,
      },
    },
  }
  await run(send)
}
async function taskPreview(page: string) {
  if (!props.taskId) return scans.preview(page)
  const { marking } = await import('../api/marking')
  return marking.preview(props.taskId, page)
}
async function showSource(page: string) {
  await run(async () => {
    const blob = await taskPreview(page)
    if (preview.value) URL.revokeObjectURL(preview.value)
    if (alive) {
      preview.value = URL.createObjectURL(blob)
      previewZoom.value = 100
    }
  })
}
onMounted(
  () =>
    void run(async () => {
      const me = await request<Schema['CurrentUser']>('/api/v1/me')
      if (!alive) return
      userId.value = me.user_id
      await load()
    }),
)
onUnmounted(() => {
  alive = false
  clearTimeout(timer)
  if (preview.value) URL.revokeObjectURL(preview.value)
})
</script>
<template>
  <section class="exam-marking" :class="{ 'task-grading': !!taskId }" aria-label="AI 批改与复核">
    <header>
      <h2>AI 批改与复核</h2>
      <p>{{ studentLabel }}</p>
      <span>教师确认后形成审核记录 · 不发布正式成绩</span>
    </header>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="pending" role="alert">
      保存结果尚未核实，编辑已锁定。<button :disabled="busy" @click="run(send)">重试原请求</button>
    </p>
    <button v-if="questions.some((q) => !q.workspace_id)" :disabled="locked" @click="initialize">
      准备题目作答核对
    </button>
    <label
      >题目<select v-model="workspace" :disabled="locked" @change="chooseQuestion">
        <option value="">选择题目</option>
        <option
          v-for="q in questions"
          :key="q.question_id"
          :value="q.workspace_id || ''"
          :disabled="!q.workspace_id"
        >
          第{{ q.question_number }}题 · {{ q.max_score }}分
        </option>
      </select></label
    >
    <button :disabled="busy" @click="run(load)">刷新批改状态</button>
    <template v-if="current">
      <div class="grading-workspace">
        <nav class="grading-page-nav" aria-label="原卷页面导航">
          <h3>原卷页面</h3>
          <div class="mark-thumbs">
            <button
              v-for="p in current.source_pages"
              :key="p.scan_page_id"
              :disabled="busy"
              @click="showSource(p.scan_page_id)"
            >
              <ScanThumbnail
                :page-id="p.scan_page_id"
                :load-preview="props.taskId ? taskPreview : undefined"
              />原第{{ p.source_page_index + 1 }}页
            </button>
          </div>
        </nav>
        <section class="grading-preview" aria-label="原卷预览">
          <div class="grading-preview-toolbar">
            <h3>扫描原页</h3>
            <div v-if="preview" class="grading-zoom">
              <button
                aria-label="缩小批改原卷"
                :disabled="previewZoom <= 60"
                @click="previewZoom -= 20"
              >
                −
              </button>
              <span>{{ previewZoom }}%</span>
              <button
                aria-label="放大批改原卷"
                :disabled="previewZoom >= 200"
                @click="previewZoom += 20"
              >
                ＋
              </button>
            </div>
          </div>
          <div v-if="preview" class="grading-preview-scroll">
            <img
              :src="preview"
              alt="当前答卷扫描原页"
              class="mark-source"
              :style="{ width: previewZoom + '%' }"
            />
          </div>
          <p v-else class="grading-preview-empty">选择左侧页面，对照原卷核对作答。</p>
        </section>
        <div class="grading-review">
          <h3>第{{ current.question.number }}题</h3>
          <p>{{ current.question.stem }}</p>
          <details>
            <summary>标准答案与评分细则</summary>
            <p>{{ current.answer_key || '尚未提供教师答案' }}</p>
            <ul>
              <li v-for="r in current.rubric" :key="r.criterion_id">
                {{ r.description }} · {{ r.points }}分
              </li>
            </ul>
          </details>
          <section v-if="!current.visual_selection?.active">
            <h3>AI 候选评分</h3>
            <p role="status">{{ labels[current.scoring_status] || current.scoring_status }}</p>
            <p
              v-if="current.scoring_status === 'invalidated' || current.scoring_status === 'failed'"
            >
              来源或执行状态已变化。请核对并重新确认作答版本后发起，不会按零分处理。
            </p>
            <button
              :disabled="
                locked ||
                current.answer_review?.decision !== 'response_present' ||
                ['pending', 'running', 'succeeded'].includes(current.scoring_status)
              "
              @click="score"
            >
              请求 AI 评分
            </button>
            <template v-if="current.proposal"
              ><p>
                模型：{{ current.proposal.model_id }} ·
                {{
                  current.proposal.grading_status === 'grading_abstained'
                    ? '模型弃权，需人工处理'
                    : '仅为建议，需教师核对'
                }}
              </p>
              <p v-if="current.proposal.abstention_reason">
                {{ current.proposal.abstention_reason }}
              </p>
              <p v-if="current.proposal.total_score !== null">
                建议总分 {{ current.proposal.total_score }} / {{ current.proposal.max_score }}
              </p>
              <p v-if="current.proposal.grading_status === 'completed_candidate'">
                请核对各评分项与原卷后提交。
              </p>
              <div class="mark-actions score-actions">
                <button
                  class="primary"
                  :disabled="locked || current.proposal.grading_status !== 'completed_candidate'"
                  @click="review('accepted')"
                >
                  接受模型建议</button
                ><button :disabled="locked" @click="editScores">载入完整分数快照</button
                ><button
                  :disabled="
                    locked ||
                    !reviewReason.trim() ||
                    current.proposal.grading_status !== 'completed_candidate'
                  "
                  @click="review('revised')"
                >
                  提交完整修订</button
                ><button :disabled="locked" @click="review('rejected')">拒绝候选</button
                ><button :disabled="locked" @click="review('regrading_required')">
                  要求重新评分
                </button>
              </div>
              <table
                v-if="current.proposal.grading_status === 'completed_candidate'"
                class="criterion-cards"
              >
                <thead>
                  <tr>
                    <th>评分项</th>
                    <th>建议分</th>
                    <th>理由与证据</th>
                    <th>教师分</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="c in current.proposal.criterion_scores" :key="c.criterion_id">
                    <td class="criterion-name">
                      {{
                        current.rubric.find((r) => r.criterion_id === c.criterion_id)?.description
                      }}
                    </td>
                    <td class="criterion-suggestion">
                      <small>建议分</small> {{ c.awarded_points }} / {{ c.max_points }}
                    </td>
                    <td class="criterion-evidence">
                      <small>理由与证据</small><br />
                      {{ c.rationale }}
                      <blockquote v-for="(e, i) in c.evidence_references" :key="i">
                        {{ e.kind === 'quote' ? e.quote : e.reason }}
                      </blockquote>
                    </td>
                    <td class="criterion-review">
                      <span>教师分</span>
                      <input
                        v-model="revised[c.criterion_id]"
                        :aria-label="'教师分 ' + c.criterion_id"
                        :placeholder="String(c.awarded_points)"
                        inputmode="decimal"
                        :disabled="locked"
                      />
                      <label
                        >修订依据<select
                          v-model="evidenceSegment[c.criterion_id]"
                          :disabled="locked"
                        >
                          <option :value="undefined">沿用模型引用</option>
                          <option
                            v-for="s in current.grade_input?.selected_answer_segments.filter(
                              (s) => Array.from(s.text).length <= 2000,
                            )"
                            :key="s.segment_index"
                            :value="String(s.segment_index)"
                          >
                            作答片段{{ s.segment_index + 1 }}：{{ s.text.slice(0, 60) }}
                          </option>
                        </select></label
                      >
                    </td>
                  </tr>
                </tbody>
              </table>
              <label
                >审核说明<textarea v-model="reviewReason" :disabled="locked" maxlength="2000" />
              </label>
            </template>
          </section>
          <section>
            <h3>教师确认作答区域</h3>
            <p>拖选学生实际作答文字后添加，可分次选择多段。无法确定时请保留人工核验。</p>
            <div class="mark-nodes">
              <article v-for="n in current.nodes" :key="n.content_pointer" class="mark-node">
                <small>解析第{{ n.page_index + 1 }}页</small>
                <pre :data-source-pointer="n.content_pointer">{{ n.text }}</pre>
                <button :disabled="locked || !n.text" @click="addWhole(n.content_pointer)">
                  选择此段
                </button>
              </article>
            </div>
            <button :disabled="locked" @mousedown.prevent="addSelected">添加所选文字</button>
            <ol>
              <li v-for="(r, i) in regions" :key="r.content_pointer + ':' + r.text_start">
                {{
                  Array.from(nodeMap.get(r.content_pointer) || '')
                    .slice(r.text_start ?? 0, r.text_end ?? undefined)
                    .join('')
                }}
                <button :disabled="locked" @click="regions.splice(i, 1)">移除</button>
              </li>
            </ol>
            <label
              ><input
                v-model="manual"
                type="checkbox"
                :disabled="locked"
              />我已核对原页，人工指定所选内容属于本题</label
            >
            <div class="mark-actions">
              <button
                class="primary"
                :disabled="locked || !regions.length"
                @click="confirm('response_present')"
              >
                确认学生作答</button
              ><button :disabled="locked" @click="confirm('uncertain')">标记需要人工核验</button>
            </div>
            <p>
              当前核对：{{
                current.answer_review?.decision === 'response_present'
                  ? '已确认作答'
                  : current.answer_review
                    ? '需要人工核验'
                    : '尚未确认'
              }}
              · 修订{{ current.answer_review?.revision || 0 }}
            </p>
          </section>
          <VisualGrading
            :details="current"
            :user-id="userId"
            :disabled="locked"
            :load-preview="taskPreview"
            @changed="run(refresh)"
          />
          <section v-if="!current.visual_selection?.active">
            <h3>教师审核历史</h3>
            <p v-if="current.history_has_more">仅展示最近20次修订，较早记录仍保留于审计历史。</p>
            <p v-if="!current.history.length">尚无审核记录。</p>
            <article v-for="h in current.history" :key="h.review_id">
              <strong
                >修订{{ h.review_revision }} · {{ labels[h.review_status] || h.review_status }} ·
                {{ h.reviewed_total_score ?? '未确认分数' }}</strong
              >
              <p>{{ h.review_comment }}</p>
              <ul>
                <li v-for="c in h.criterion_reviews" :key="c.criterion_id">
                  {{
                    current.rubric.find((r) => r.criterion_id === c.criterion_id)?.description
                  }}：{{ c.proposed_points }} → {{ c.reviewed_points }}
                </li>
              </ul>
            </article>
          </section>
        </div>
      </div>
    </template>
  </section>
</template>
<style scoped>
.exam-marking {
  background: white;
  border-radius: 8px;
  padding: 24px;
  min-width: 0;
  border-top: 1px solid #dce3ed;
  margin-top: 24px;
  padding-top: 20px;
}
.exam-marking header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 16px;
}
.exam-marking header span,
.exam-marking small {
  color: #5c6b80;
  font-size: 13px;
}
.exam-marking h3 {
  font-size: 17px;
  margin: 20px 0 12px;
}
.exam-marking label {
  display: block;
  margin: 12px 0;
}
.exam-marking button {
  margin: 4px 8px 4px 0;
}
.mark-nodes {
  max-height: 240px;
  overflow: auto;
  padding-right: 12px;
}
.exam-marking button {
  border: 1px solid #bccadd;
  background: #fff;
  border-radius: 6px;
  padding: 8px 12px;
  color: #164c93;
  font: inherit;
  cursor: pointer;
}
.exam-marking button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.exam-marking input,
.exam-marking select,
.exam-marking textarea {
  font: inherit;
  padding: 8px;
  border: 1px solid #b5c1d1;
  border-radius: 6px;
}
.grading-workspace {
  display: grid;
  grid-template-columns: minmax(110px, 150px) minmax(0, 1.2fr) minmax(300px, 1fr);
  gap: 14px;
  align-items: start;
}
.grading-page-nav,
.grading-preview {
  position: sticky;
  top: 78px;
  min-width: 0;
}
.task-grading .grading-page-nav,
.task-grading .grading-preview {
  top: 138px;
  max-height: calc(100vh - 155px);
}
.task-grading .grading-preview-scroll {
  max-height: calc(100vh - 225px);
}
.grading-page-nav {
  max-height: calc(100vh - 96px);
  overflow: auto;
}
.grading-page-nav h3,
.grading-preview h3 {
  margin: 0 0 12px;
}
.mark-thumbs {
  display: grid;
  gap: 8px;
}
.mark-thumbs button {
  width: 100%;
  margin: 0;
}
.grading-preview {
  border: 1px solid #e0e6ef;
  border-radius: 8px;
  padding: 12px;
  background: #f7f9fc;
}
.grading-preview-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.grading-zoom {
  display: flex;
  align-items: center;
  white-space: nowrap;
  font-size: 13px;
}
.grading-zoom button {
  padding: 4px 8px;
  margin: 0 4px;
}
.grading-preview-scroll {
  max-height: calc(100vh - 166px);
  overflow: auto;
  background: #e9eef4;
}
.grading-preview-empty {
  min-height: 300px;
  display: grid;
  place-items: center;
  color: #61738a;
  text-align: center;
}
.mark-source {
  display: block;
  max-width: none;
  height: auto;
  margin: auto;
}
.grading-review {
  min-width: 0;
  padding: 0 4px;
}
.grading-review h3:first-child {
  margin-top: 0;
}
.grading-review table {
  display: block;
  overflow-x: auto;
}
.criterion-cards thead {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
.criterion-cards tbody {
  display: grid;
  gap: 10px;
}
.criterion-cards tbody tr {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  padding: 12px;
  border: 1px solid #e0e7f0;
  border-radius: 8px;
}
.criterion-cards tbody td {
  display: block;
  min-width: 0;
  padding: 0;
  border: 0;
  overflow-wrap: anywhere;
}
.criterion-name {
  font-weight: 600;
}
.criterion-suggestion {
  color: #2359ae;
  white-space: nowrap;
}
.criterion-evidence,
.criterion-review {
  grid-column: 1 / -1;
}
.criterion-review > span {
  margin-right: 8px;
}
.criterion-review select {
  display: block;
  max-width: 100%;
}
.mark-node {
  padding: 12px 0;
  border-bottom: 1px solid #e5eaf0;
}
.mark-node pre {
  white-space: pre-wrap;
  font-family: inherit;
  line-height: 1.7;
  font-size: 15px;
  user-select: text;
}
.exam-marking table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
}
.exam-marking th,
.exam-marking td {
  padding: 12px 8px;
  border-bottom: 1px solid #e5eaf0;
  vertical-align: top;
}
.exam-marking td input {
  width: 72px;
}
.exam-marking textarea {
  display: block;
  width: 100%;
  min-height: 72px;
}
.exam-marking blockquote {
  margin: 8px 0;
  padding-left: 12px;
  border-left: 3px solid #83a4cd;
  color: #52647c;
}
.score-actions {
  position: sticky;
  top: 72px;
  z-index: 2;
  padding: 6px 0;
  background: #fff;
  border-bottom: 1px solid #e7edf4;
}
.task-grading .score-actions {
  top: 138px;
}
.mark-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
@media (max-width: 1050px) {
  .grading-workspace {
    grid-template-columns: minmax(100px, 0.4fr) minmax(0, 1fr);
  }
  .grading-review {
    grid-column: 1 / -1;
  }
  .grading-page-nav,
  .grading-preview {
    position: static;
    max-height: none;
  }
  .task-grading .grading-page-nav,
  .task-grading .grading-preview {
    position: static;
    max-height: none;
  }
  .exam-marking header {
    display: block;
  }
}
@media (max-width: 700px) {
  .grading-workspace {
    grid-template-columns: 1fr;
  }
  .grading-review {
    grid-column: auto;
  }
  .mark-thumbs {
    grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
  }
}
</style>
