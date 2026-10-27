<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ApiError, id, request, type Schema } from '../api/client'

type Rectangle = Schema['VisualRectangle']
type Mutation =
  Schema['VisualSelectionRequest'] | Schema['VisualScoreRequest'] | Schema['VisualScoreReview']
const props = defineProps<{
  details: Schema['ExamMarkDetails']
  userId: string
  disabled?: boolean
  loadPreview: (page: string) => Promise<Blob>
}>()
const emit = defineEmits<{ changed: [] }>()
const draft = ref<Rectangle[]>([])
const savedAreas = ref('[]')
const page = ref(''),
  imageUrl = ref(''),
  busy = ref(false),
  error = ref(''),
  note = ref('')
const zoom = ref(100),
  start = ref<{ x: number; y: number } | null>(null)
const currentRect = ref<Rectangle | null>(null),
  overlay = ref<HTMLElement | null>(null)
const scores = ref<Record<string, string>>({}),
  reviewReason = ref('')
const pending = ref<{ action: string; body: Mutation }>()
let requestSerial = 0
const selection = computed(() => props.details.visual_selection)
const proposal = computed(() => props.details.visual_proposal)
const visualInput = computed(() => props.details.visual_grade_input)
const unsavedAreas = computed(() => JSON.stringify(draft.value) !== savedAreas.value)
const pageAreas = computed(() => draft.value.filter((r) => r.scan_page_id === page.value))
const stateLabels: Record<string, string> = {
  not_started: '尚未评分', pending: '等待评分', running: '正在评分',
  succeeded: '建议已生成', failed: '评分失败，需人工处理', invalidated: '来源已变化',
}
const reviewLabels: Record<string, string> = {
  accepted: '接受建议', revised: '教师修订', rejected: '拒绝建议',
  regrading_required: '需要人工判读',
}
const pageLabel = (pageId: string) => {
  const source = props.details.source_pages.find((p) => p.scan_page_id === pageId)
  return source ? `原文件第${source.source_page_index + 1}页` : '来源页'
}
watch(
  () => [props.details.workspace_id, selection.value?.revision] as const,
  () => {
    draft.value = (selection.value?.active ? selection.value.regions : []).map((r) => ({
      scan_page_id: r.scan_page_id,
      x0: Math.round((r.x0 * 10000) / r.width),
      y0: Math.round((r.y0 * 10000) / r.height),
      x1: Math.round((r.x1 * 10000) / r.width),
      y1: Math.round((r.y1 * 10000) / r.height),
    }))
    savedAreas.value = JSON.stringify(draft.value)
    scores.value = {}
  },
  { immediate: true },
)
watch(
  () => props.details.workspace_id,
  () => {
    page.value = ''
    requestSerial++
    if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
    imageUrl.value = ''
  },
)
async function selectPage(pageId: string) {
  const serial = ++requestSerial
  error.value = ''
  try {
    const blob = await props.loadPreview(pageId)
    if (serial !== requestSerial) return
    if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
    imageUrl.value = URL.createObjectURL(blob)
    page.value = pageId
  } catch (e) {
    error.value = e instanceof Error ? e.message : '无法读取扫描页。'
  }
}
function point(event: PointerEvent) {
  const rect = overlay.value?.getBoundingClientRect()
  if (!rect) return null
  return {
    x: Math.max(0, Math.min(10000, Math.round(((event.clientX - rect.left) / rect.width) * 10000))),
    y: Math.max(0, Math.min(10000, Math.round(((event.clientY - rect.top) / rect.height) * 10000))),
  }
}
function down(event: PointerEvent) {
  if (busy.value || props.disabled || pending.value || !page.value) return
  start.value = point(event)
  overlay.value?.setPointerCapture(event.pointerId)
}
function move(event: PointerEvent) {
  if (!start.value) return
  const end = point(event)
  if (!end) return
  currentRect.value = {
    scan_page_id: page.value,
    x0: Math.min(start.value.x, end.x),
    y0: Math.min(start.value.y, end.y),
    x1: Math.max(start.value.x, end.x),
    y1: Math.max(start.value.y, end.y),
  }
}
function up(event: PointerEvent) {
  if (!start.value) return
  move(event)
  const r = currentRect.value
  if (r && r.x1 - r.x0 >= 100 && r.y1 - r.y0 >= 100) {
    if (draft.value.length >= 8) error.value = '最多选择 8 个图像区域。'
    else draft.value = [...draft.value, r]
  }
  start.value = null
  currentRect.value = null
}
async function sendPending() {
  const value = pending.value
  if (!value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await request(
      `/api/v1/exam-grading-workspaces/${id(props.details.workspace_id)}/${value.action}`,
      {
        body: JSON.stringify(value.body),
        timeoutMs: 100000,
        errorCode: true,
      },
    )
    pending.value = undefined
    note.value = '已保存。模型建议须经教师复核，尚未发布成绩。'
    emit('changed')
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
    error.value = e instanceof ApiError && e.code === 'exam_visual_grading_disabled'
      ? '图文评分尚未启用，请联系管理员。'
      : e instanceof Error ? e.message : '操作失败，请重新核对。'
  } finally {
    busy.value = false
  }
}
async function save(action: 'select' | 'clear') {
  pending.value = {
    action: 'visual-selection',
    body: {
      request_id: crypto.randomUUID(),
      expected_revision: selection.value?.revision || 0,
      action,
      regions: action === 'select' ? draft.value : [],
    },
  }
  await sendPending()
}
async function score() {
  if (!selection.value?.active) return
  pending.value = {
    action: 'visual-scoring',
    body: {
      request_id: crypto.randomUUID(),
      expected_selection_revision: selection.value.revision,
    },
  }
  await sendPending()
}
async function review(status: 'accepted' | 'revised' | 'rejected' | 'regrading_required') {
  const candidate = proposal.value
  if (!candidate) return
  const criteria: Schema['VisualReviewCriterionInput'][] = []
  if (status === 'revised') {
    for (const c of candidate.criterion_scores || []) {
      const value = scores.value[c.criterion_id] || String(c.awarded_points)
      if (!/^\d+(\.\d{1,2})?$/.test(value)) {
        error.value = '请输入最多两位小数的分值。'
        return
      }
      const cents = (v: string) => {
        const [a, b = ''] = v.split('.')
        return BigInt(a!) * 100n + BigInt((b + '00').slice(0, 2))
      }
      const changed = cents(value) !== cents(String(c.awarded_points))
      criteria.push({
        criterion_id: c.criterion_id,
        reviewed_points: value,
        ...(changed
          ? { rationale: reviewReason.value, evidence_references: c.evidence_references }
          : {}),
      })
    }
  }
  pending.value = {
    action: 'visual-score-review',
    body: {
      request_id: crypto.randomUUID(),
      expected_revision: props.details.visual_history?.at(-1)?.review_revision || 0,
      payload: {
        schema_name: 'huipi.grading.visual-review',
        schema_version: '2.0',
        review_status: status,
        reviewer_ref: `teacher_${props.userId.replaceAll('-', '')}`,
        criterion_reviews: criteria,
        review_reason_codes: status === 'accepted' ? [] : ['manual_follow_up_needed'],
        review_comment: reviewReason.value.trim() || null,
      },
    },
  }
  await sendPending()
}
async function showEvidence(index: number) {
  const area = visualInput.value?.regions[index]
  if (area) await selectPage(area.scan_page_id)
  overlay.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
onUnmounted(() => {
  requestSerial++
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
})
</script>

<template>
  <section class="visual-grading" aria-label="图像作答区域与多模态评分">
    <h3>图像作答与 AI 批改</h3>
    <p>先核对学生和题目，在扫描原页框选作答；支持跨页选择。图像建议须由教师复核。</p>
    <p>请求评分时，所选图像、题目及评分标准会发送至已授权的 DeepSeek 服务。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="note" role="status">{{ note }}</p>
    <p v-if="pending" role="alert">
      保存结果尚未核实。 <button :disabled="busy" @click="sendPending">重试原请求</button>
    </p>
    <div class="visual-pages">
      <button
        v-for="p in details.source_pages"
        :key="p.scan_page_id"
        :disabled="busy"
        @click="selectPage(p.scan_page_id)"
      >
        {{ pageLabel(p.scan_page_id) }}
      </button>
    </div>
    <label v-if="imageUrl"
      >缩放
      <select v-model.number="zoom">
        <option :value="100">100%</option>
        <option :value="150">150%</option>
        <option :value="200">200%</option>
      </select>
    </label>
    <div v-if="imageUrl" class="visual-scroll">
      <div
        ref="overlay"
        class="visual-image"
        :style="{ width: `${zoom}%` }"
        @pointerdown.prevent="down"
        @pointermove.prevent="move"
        @pointerup.prevent="up"
      >
        <img :src="imageUrl" alt="学生答卷扫描原页，拖动鼠标框选作答区域" draggable="false" />
        <svg viewBox="0 0 10000 10000" preserveAspectRatio="none" aria-hidden="true">
          <rect
            v-for="(r, i) in pageAreas"
            :key="i"
            :x="r.x0"
            :y="r.y0"
            :width="r.x1 - r.x0"
            :height="r.y1 - r.y0"
          />
          <rect
            v-if="currentRect"
            :x="currentRect.x0"
            :y="currentRect.y0"
            :width="currentRect.x1 - currentRect.x0"
            :height="currentRect.y1 - currentRect.y0"
          />
        </svg>
      </div>
    </div>
    <ol>
      <li v-for="(r, i) in draft" :key="i">
        区域{{ i + 1 }} · {{ pageLabel(r.scan_page_id) }}
        <button :disabled="busy || disabled || !!pending" @click="selectPage(r.scan_page_id)">
          查看
        </button>
        <button :disabled="busy || disabled || !!pending" @click="draft.splice(i, 1)">移除</button>
      </li>
    </ol>
    <div class="visual-actions">
      <button :disabled="busy || disabled || !!pending || !draft.length" @click="save('select')">
        确认图像作答区域
      </button>
      <button
        :disabled="busy || disabled || !!pending || !selection?.active"
        @click="save('clear')"
      >
        清除已确认图像区域
      </button>
      <button
        :disabled="
          busy ||
          disabled ||
          !!pending ||
          !selection?.active ||
          unsavedAreas ||
          ['pending', 'running', 'succeeded', 'failed', 'invalidated'].includes(details.scoring_status)
        "
        @click="score"
      >
        请求图文 AI 评分
      </button>
    </div>
    <p>
      图像区域：{{
        selection?.active ? `已确认（版本 ${selection.revision}）` : '尚未确认'
      }}。评分状态：{{ stateLabels[details.scoring_status] || '尚未评分' }}
    </p>
    <p v-if="['failed', 'invalidated'].includes(details.scoring_status)" role="alert">
      本次评分未生成有效建议。请核对来源并重新确认图像区域，再按需发起新请求；系统不会自动重试。
    </p>
    <template v-if="proposal">
      <p>
        模型：{{ proposal.model_id }} ·
        {{
          proposal.grading_status === 'grading_abstained'
            ? '模型弃权，需人工判读'
            : '仅为候选，需教师复核'
        }}
      </p>
      <p v-if="proposal.abstention_reason">{{ proposal.abstention_reason }}</p>
      <table v-if="proposal.grading_status === 'completed_candidate'">
        <thead>
          <tr>
            <th>评分项</th>
            <th>建议分</th>
            <th>视觉证据与理由</th>
            <th>教师分</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in proposal.criterion_scores" :key="c.criterion_id">
            <td>
              {{ details.rubric.find((r) => r.criterion_id === c.criterion_id)?.description }}
            </td>
            <td>{{ c.awarded_points }} / {{ c.max_points }}</td>
            <td>
              {{ c.rationale }}
              <p v-for="(e, i) in c.evidence_references" :key="i">
                <button @click="showEvidence(e.region_index)">
                  查看原图区域{{ e.region_index + 1 }}
                </button>
                {{ e.description }}
              </p>
            </td>
            <td>
              <input
                v-model="scores[c.criterion_id]"
                :placeholder="String(c.awarded_points)"
                inputmode="decimal"
                :disabled="busy || disabled"
              />
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="proposal.total_score !== null">
        建议总分 {{ proposal.total_score }} / {{ proposal.max_score }}
      </p>
      <label
        >审核说明<textarea v-model="reviewReason" maxlength="2000" :disabled="busy || disabled" />
      </label>
      <div class="visual-actions">
        <button
          :disabled="
            busy || disabled || !!pending || proposal.grading_status !== 'completed_candidate'
          "
          @click="review('accepted')"
        >
          接受建议
        </button>
        <button
          :disabled="
            busy ||
            disabled ||
            !!pending ||
            !reviewReason.trim() ||
            proposal.grading_status !== 'completed_candidate'
          "
          @click="review('revised')"
        >
          提交修订
        </button>
        <button :disabled="busy || disabled || !!pending" @click="review('rejected')">
          拒绝建议
        </button>
        <button :disabled="busy || disabled || !!pending" @click="review('regrading_required')">
          需要人工判读
        </button>
      </div>
    </template>
    <div v-if="details.visual_history?.length">
      <h4>图像评分审核历史</h4>
      <p v-for="h in details.visual_history" :key="h.review_id">
        修订{{ h.review_revision }} · {{ reviewLabels[h.review_status] || h.review_status }} ·
        {{ h.reviewed_total_score ?? '未确认分数' }}
      </p>
    </div>
  </section>
</template>

<style scoped>
.visual-grading {
  border-top: 1px solid #dce3ed;
  margin-top: 24px;
  padding-top: 18px;
}
.visual-pages,
.visual-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
}
.visual-scroll {
  max-height: 650px;
  max-width: 850px;
  overflow: auto;
  border: 1px solid #cbd5e1;
  margin-top: 10px;
}
.visual-image {
  position: relative;
  line-height: 0;
  cursor: crosshair;
  touch-action: none;
}
.visual-image img {
  display: block;
  width: 100%;
  height: auto;
  user-select: none;
}
.visual-image svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
.visual-image rect {
  fill: #1677ff33;
  stroke: #1677ff;
  stroke-width: 2;
  vector-effect: non-scaling-stroke;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th,
td {
  border-bottom: 1px solid #e2e8f0;
  padding: 8px;
  text-align: left;
  vertical-align: top;
}
textarea {
  display: block;
  width: min(100%, 600px);
}
</style>
