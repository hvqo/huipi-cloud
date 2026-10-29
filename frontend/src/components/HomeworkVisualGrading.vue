<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, id, request } from '../api/client'

const props = defineProps<{ submissionId: string; questionId: string }>()
type Rect = { page_index: number; x0: number; y0: number; x1: number; y1: number }
type StoredRegion = {
  source_page_index: number
  x0: number
  y0: number
  x1: number
  y1: number
  width: number
  height: number
}
type Evidence = { kind: 'image_region'; region_index: number; description: string }
type Score = {
  criterion_id: string
  awarded_points: string
  max_points: string
  rationale: string
  evidence_references: Evidence[]
}
type Proposal = {
  grading_status: string
  total_score: string | null
  abstention_reason: string | null
  criterion_scores: Score[]
}
type Detail = {
  selection: { revision: number; regions: StoredRegion[] } | null
  job: { status: string; failure_code: string | null } | null
  proposal: Proposal | null
  review: { revision: number; status: string } | null
}
const base = `/api/v1/homework/submissions/${id(props.submissionId)}/questions/${id(props.questionId)}/visual`
const page = ref(1),
  picture = ref(''),
  details = ref<Detail>(),
  regions = ref<Rect[]>([])
const error = ref(''),
  notice = ref(''),
  busy = ref(false),
  confirmedText = ref(false)
const points = ref<Record<string, string>>({}),
  comment = ref(''),
  pending = ref<string>()
const pendingKind = ref<'save' | 'score' | 'review'>(),
  dirty = ref(false)
const stage = ref<HTMLElement>(),
  start = ref<{ x: number; y: number }>(),
  draft = ref<Rect>()
const labels: Record<string, string> = {
  accepted: '已接受',
  revised: '已修订',
  rejected: '已拒绝',
  regrading_required: '需要重新评分',
}
function disposeImage() {
  if (picture.value) URL.revokeObjectURL(picture.value)
  picture.value = ''
}
async function loadPage() {
  try {
    const blob = await request<Blob>(
      `/api/v1/homework/submissions/${id(props.submissionId)}/visual-pages/${page.value - 1}`,
      { blob: true, maxBytes: 2 * 1024 * 1024, errorCode: true },
    )
    disposeImage()
    picture.value = URL.createObjectURL(blob)
    error.value = ''
  } catch (e) {
    disposeImage()
    error.value = e instanceof Error ? e.message : '原图读取失败。'
  }
}
async function load() {
  try {
    const value = await request<Detail>(base, { errorCode: true })
    details.value = value
    if (value.selection)
      regions.value = value.selection.regions.map((r) => ({
        page_index: r.source_page_index,
        x0: Math.round((r.x0 / r.width) * 10000),
        y0: Math.round((r.y0 / r.height) * 10000),
        x1: Math.round((r.x1 / r.width) * 10000),
        y1: Math.round((r.y1 / r.height) * 10000),
      }))
    dirty.value = false
    points.value = Object.fromEntries(
      (value.proposal?.criterion_scores || []).map((s) => [s.criterion_id, s.awarded_points]),
    )
  } catch (e) {
    error.value = e instanceof Error ? e.message : '评分状态读取失败。'
  }
}
function position(event: PointerEvent) {
  const box = stage.value?.getBoundingClientRect()
  if (!box || !box.width || !box.height) return { x: 0, y: 0 }
  return {
    x: Math.max(0, Math.min(10000, Math.round(((event.clientX - box.left) / box.width) * 10000))),
    y: Math.max(0, Math.min(10000, Math.round(((event.clientY - box.top) / box.height) * 10000))),
  }
}
function down(event: PointerEvent) {
  if (busy.value || regions.value.length >= 8) return
  start.value = position(event)
  stage.value?.setPointerCapture(event.pointerId)
}
function move(event: PointerEvent) {
  if (!start.value) return
  const p = position(event)
  draft.value = {
    page_index: page.value - 1,
    x0: Math.min(start.value.x, p.x),
    y0: Math.min(start.value.y, p.y),
    x1: Math.max(start.value.x, p.x),
    y1: Math.max(start.value.y, p.y),
  }
}
function up(event: PointerEvent) {
  move(event)
  if (
    draft.value &&
    draft.value.x1 - draft.value.x0 > 100 &&
    draft.value.y1 - draft.value.y0 > 100
  ) {
    regions.value.push(draft.value)
    dirty.value = true
  }
  start.value = undefined
  draft.value = undefined
}
function cancelSelection() {
  start.value = undefined
  draft.value = undefined
}
function removeRegion(index: number) {
  regions.value.splice(index, 1)
  dirty.value = true
}
function style(r: Rect) {
  return {
    left: `${r.x0 / 100}%`,
    top: `${r.y0 / 100}%`,
    width: `${(r.x1 - r.x0) / 100}%`,
    height: `${(r.y1 - r.y0) / 100}%`,
  }
}
async function showEvidence(index: number) {
  const region = regions.value[index]
  if (!region || dirty.value) return
  page.value = region.page_index + 1
  await loadPage()
  stage.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
async function save() {
  if (!regions.value.length || busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  pendingKind.value = 'save'
  pending.value ||= JSON.stringify({
    request_id: crypto.randomUUID(),
    expected_revision: details.value?.selection?.revision || 0,
    regions: regions.value,
  })
  try {
    await request(`${base}/selection`, { body: pending.value, errorCode: true, timeoutMs: 120000 })
    pending.value = undefined
    notice.value = '图片作答区域已确认，请核对后请求评分。'
    await load()
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
    error.value = pending.value
      ? '保存结果暂未确认，请重试原请求。'
      : e instanceof Error
        ? e.message
        : '保存失败。'
  } finally {
    busy.value = false
  }
}
async function score() {
  if (!details.value?.selection || busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  pendingKind.value = 'score'
  pending.value ||= JSON.stringify({
    request_id: crypto.randomUUID(),
    expected_selection_revision: details.value.selection.revision,
    include_confirmed_text: confirmedText.value,
  })
  try {
    const result = await request<{ status: string }>(`${base}/score`, {
      body: pending.value, errorCode: true, timeoutMs: 180000,
    })
    pending.value = undefined
    notice.value = result.status === 'succeeded'
      ? '评分请求已结束，请核对建议和图片证据。'
      : result.status === 'running'
        ? '评分仍在处理，请稍后刷新查看。'
        : '评分未完成，请重新确认区域后处理。'
    await load()
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
    error.value =
      e instanceof ApiError && e.code === 'homework_visual_grading_disabled'
        ? '当前未启用图片评分，请联系管理员。'
        : pending.value
          ? '评分结果暂未确认，请刷新或重试原请求。'
          : e instanceof Error
            ? e.message
            : '评分未完成。'
    await load()
  } finally {
    busy.value = false
  }
}
async function review(status: 'accepted' | 'revised' | 'rejected' | 'regrading_required') {
  if (!details.value?.proposal || busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  pendingKind.value = 'review'
  try {
    const criteria =
      status === 'revised'
        ? details.value.proposal.criterion_scores.map((s) => {
            const updated = points.value[s.criterion_id]?.trim()
            if (!updated) throw new Error('请填写所有评分项；空白不会自动计零。')
            const changed = Number(updated) !== Number(s.awarded_points)
            return {
              criterion_id: s.criterion_id,
              reviewed_points: updated,
              ...(changed
                ? {
                    rationale: comment.value || '教师核对图片后修订',
                    evidence_references: s.evidence_references,
                  }
                : {}),
            }
          })
        : []
    pending.value ||= JSON.stringify({
      request_id: crypto.randomUUID(),
      expected_revision: details.value.review?.revision || 0,
      review_status: status,
      review_comment: comment.value || null,
      criterion_reviews: criteria,
    })
    await request(`${base}/review`, { body: pending.value, errorCode: true })
    pending.value = undefined
    notice.value = '教师复核已保存。'
    await load()
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
    error.value = pending.value
      ? '复核结果暂未确认，请重试原请求。'
      : e instanceof Error
        ? e.message
        : '复核失败。'
  } finally {
    busy.value = false
  }
}
onMounted(async () => {
  await load()
  await loadPage()
})
onBeforeUnmount(disposeImage)
</script>

<template>
  <section class="panel homework-visual" aria-label="图片作答批改">
    <h2>图片作答批改</h2>
    <p>在原图上框选本题作答，可跨页选择多个区域。图片评分建议需要教师复核，不会自动记分。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <div class="page-tools">
      <label
        >页码
        <input v-model.number="page" type="number" min="1" max="50" aria-label="图片页码" /></label
      ><button :disabled="busy || page < 1 || page > 50" @click="loadPage">查看原页</button>
    </div>
    <div
      v-if="picture"
      ref="stage"
      class="image-stage"
      @pointerdown="down"
      @pointermove="move"
      @pointerup="up"
      @pointercancel="cancelSelection"
    >
      <img :src="picture" alt="学生作答原页，在图上拖动选择本题作答区域" draggable="false" />
      <div
        v-for="(r, index) in regions"
        v-show="r.page_index === page - 1"
        :key="index"
        class="rectangle"
        :style="style(r)"
      >
        <span>{{ index + 1 }}</span>
      </div>
      <div v-if="draft" class="rectangle draft" :style="style(draft)" />
    </div>
    <ol>
      <li v-for="(r, index) in regions" :key="index">
        区域 {{ index + 1 }} · 第 {{ r.page_index + 1 }} 页
        <button :disabled="busy" @click="removeRegion(index)">
          移除
        </button>
      </li>
    </ol>
    <button :disabled="busy || !regions.length || !!pending" @click="save">确认图片区域</button>
    <template v-if="details?.selection"
      ><label class="text-option"
        ><input
          v-model="confirmedText"
          type="checkbox"
          :disabled="busy"
        />同时使用已人工确认的文本作答</label
      ><button :disabled="busy || !!pending || !!details.job || dirty" @click="score">
        请求图片 AI 评分</button
      ><span v-if="details.job">
        评分状态：{{
          details.job.status === 'succeeded'
            ? '已生成'
            : details.job.status === 'running'
              ? '处理中'
              : '未完成，请重新确认区域后处理'
        }}</span
      ></template
    >
    <button
      v-if="pending && !busy"
      @click="
        pendingKind === 'save' ? save() : pendingKind === 'score' ? score() : review('accepted')
      "
    >
      重试原请求
    </button>
    <template v-if="details?.proposal"
      ><div v-if="details.proposal.grading_status === 'grading_abstained'">
        <p>模型弃权：{{ details.proposal.abstention_reason }}，请人工判读。</p>
        <button :disabled="busy || !!pending || dirty" @click="review('regrading_required')">
          需要人工判读
        </button>
      </div>
      <template v-else
        ><p>
          建议总分 {{ details.proposal.total_score }} · 教师复核：{{
            labels[details.review?.status || ''] || '未复核'
          }}
        </p>
        <table>
          <thead>
            <tr>
              <th>建议 / 满分</th>
              <th>理由与图片证据</th>
              <th>教师分</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in details.proposal.criterion_scores" :key="s.criterion_id">
              <td>{{ s.awarded_points }} / {{ s.max_points }}</td>
              <td>
                {{ s.rationale }}
                <p v-for="e in s.evidence_references" :key="e.region_index">
                  <button :disabled="dirty" @click="showEvidence(e.region_index)">
                    查看区域 {{ e.region_index + 1 }}</button
                  >：{{ e.description }}
                </p>
              </td>
              <td>
                <input
                  v-model="points[s.criterion_id]"
                  :aria-label="`图片教师分 ${s.criterion_id}`"
                  inputmode="decimal"
                />
              </td>
            </tr>
          </tbody>
        </table>
        <label>教师说明 <input v-model="comment" /></label>
        <div>
          <button :disabled="busy || !!pending || dirty" @click="review('accepted')">
            接受图片建议</button
          ><button :disabled="busy || !!pending || dirty" @click="review('revised')">
            提交图片评分修订</button
          ><button :disabled="busy || !!pending || dirty" @click="review('rejected')">
            拒绝图片建议</button
          ><button :disabled="busy || !!pending || dirty" @click="review('regrading_required')">
            需要人工判读
          </button>
        </div></template
      ></template
    >
  </section>
</template>

<style scoped>
.page-tools {
  display: flex;
  gap: 12px;
  align-items: center;
  margin: 12px 0;
}
.page-tools input {
  width: 5em;
}
.image-stage {
  position: relative;
  display: inline-block;
  max-width: min(100%, 760px);
  line-height: 0;
  touch-action: none;
  cursor: crosshair;
}
.image-stage img {
  display: block;
  max-width: 100%;
  max-height: 760px;
  user-select: none;
}
.rectangle {
  position: absolute;
  border: 2px solid #2468c9;
  background: rgba(36, 104, 201, 0.12);
  pointer-events: none;
  line-height: 1.4;
  box-sizing: border-box;
}
.rectangle span {
  background: #2468c9;
  color: white;
  font-size: 12px;
  padding: 1px 4px;
}
.rectangle.draft {
  border-style: dashed;
}
.text-option {
  display: inline-block;
  margin: 12px;
}
.homework-visual table {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
}
.homework-visual td,
.homework-visual th {
  text-align: left;
  border-bottom: 1px solid #d9e2ed;
  padding: 8px;
}
</style>
