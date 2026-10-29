<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, id, request } from '../api/client'
const props = defineProps<{ submissionId: string; questionId: string }>()
type RefEvidence = { kind: string; quote?: string; [key: string]: unknown }
type Score = { criterion_id: string; awarded_points: string; max_points: string; rationale: string; evidence_references: RefEvidence[] }
type Candidate = { proposal_id: string; grading_status: string; criterion_scores: Score[]; total_score: string | null; abstention_reason: string | null }
const link = ref<{participant_id: string; is_current: boolean}>(), candidate = ref<Candidate>(), revision = ref(0), reviewStatus = ref<string | null>()
const error = ref(''), notice = ref(''), comment = ref(''), busy = ref(false), points = ref<Record<string, string>>({})
const pendingScore = ref<string>(), pendingReview = ref<string>()
const reviewLabels: Record<string, string> = { accepted: '已接受', revised: '已修订', rejected: '已拒绝', regrading_required: '需要重新评分' }
async function load() {
  try {
    const linked = await request<{participant_id: string; is_current: boolean}>(`/api/v1/homework/submissions/${id(props.submissionId)}`)
    link.value = linked
    if (!linked.is_current) return
    const data = await request<{candidate: Candidate | null; review_revision?: number; review_status?: string}>(`/api/v1/homework/participants/${id(linked.participant_id)}/questions/${id(props.questionId)}/candidate`)
    candidate.value = data.candidate || undefined; revision.value = data.review_revision || 0; reviewStatus.value = data.review_status || null
    points.value = Object.fromEntries((candidate.value?.criterion_scores || []).map(s => [s.criterion_id, s.awarded_points]))
  } catch(e) { if (!(e instanceof ApiError && e.status === 404)) error.value = e instanceof Error ? e.message : '读取失败。' }
}
async function grade() {
  if (!link.value || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  pendingScore.value ||= crypto.randomUUID()
  try {
    await request(`/api/v1/homework/participants/${id(link.value.participant_id)}/questions/${id(props.questionId)}/candidate`, { body: JSON.stringify({ request_id: pendingScore.value }), timeoutMs: 180000, errorCode: true })
    pendingScore.value = undefined
    notice.value = '评分建议已生成，请核对证据并由教师确认。'; await load()
  } catch(e) { if (e instanceof ApiError && !e.uncertain) pendingScore.value = undefined; error.value = pendingScore.value ? '评分结果尚未确认，请重试原请求。' : e instanceof ApiError && e.code === 'homework_visual_scoring_not_supported' ? '本次作答包含图片等非文本证据，日常作业暂不支持直接图文评分，请人工处理。' : e instanceof ApiError && e.code === 'homework_answer_not_ready' ? '请先保存本题的学生作答确认。' : e instanceof Error ? e.message : '评分失败。' } finally { busy.value = false }
}
async function review(status: 'accepted' | 'revised' | 'rejected' | 'regrading_required') {
  if (!candidate.value || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const criteria = status === 'revised' ? candidate.value.criterion_scores.map(s => {
      const updated = points.value[s.criterion_id]?.trim()
      if (!updated) throw new Error('请填写每个评分项的教师分值，空白不会自动计零。')
      const changed = Number(updated) !== Number(s.awarded_points)
      return { criterion_id: s.criterion_id, reviewed_points: updated, ...(changed ? { rationale: comment.value || '教师核对后修订', evidence_references: s.evidence_references } : {}) }
    }) : []
    pendingReview.value ||= JSON.stringify({ request_id: crypto.randomUUID(), expected_revision: revision.value, review_status: status, review_reason_codes: status === 'rejected' || status === 'regrading_required' ? ['candidate_not_reliable'] : [], review_comment: comment.value || null, criterion_reviews: criteria })
    await request(`/api/v1/submissions/${id(props.submissionId)}/questions/${id(props.questionId)}/grade-proposals/${id(candidate.value.proposal_id)}/reviews`, { body: pendingReview.value, errorCode: true })
    pendingReview.value = undefined
    notice.value = '教师复核已保存，不代表正式成绩发布。'; await load()
  } catch(e) { if (e instanceof ApiError && !e.uncertain) pendingReview.value = undefined; error.value = pendingReview.value ? '复核结果尚未确认，请重试原请求。' : e instanceof Error ? e.message : '复核未保存。' } finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <section v-if="link" class="panel" aria-label="作业评分">
    <h2>AI 辅助批改</h2><p>请先确认学生实际作答。AI 建议须由教师复核；无法识别时保留人工处理。</p>
    <p v-if="!link.is_current" class="notice">该提交已被新版本替代，请从作业看板进入当前提交。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <button v-if="link.is_current" :disabled="busy || !!pendingReview" @click="grade">{{ pendingScore ? '重试原评分请求' : '请求 AI 评分建议' }}</button>
    <template v-if="candidate"><p v-if="candidate.grading_status === 'grading_abstained'">模型弃权：{{ candidate.abstention_reason }}</p>
      <template v-else><p>建议总分 {{ candidate.total_score }}；当前复核：{{ reviewLabels[reviewStatus || ''] || '未复核' }}</p>
        <table><thead><tr><th>建议 / 满分</th><th>理由与原文证据</th><th>教师分</th></tr></thead><tbody><tr v-for="s in candidate.criterion_scores" :key="s.criterion_id"><td>{{ s.awarded_points }} / {{ s.max_points }}</td><td>{{ s.rationale }}<blockquote v-for="(e, i) in s.evidence_references" :key="i">{{ e.quote || '无可引用证据' }}</blockquote></td><td><input v-model="points[s.criterion_id]" :aria-label="`教师分 ${s.criterion_id}`" inputmode="decimal" /></td></tr></tbody></table>
        <label>教师说明 <input v-model="comment" /></label>
        <div v-if="pendingReview"><button :disabled="busy" @click="review('accepted')">重试原复核请求</button></div>
        <div v-else><button :disabled="busy || !!pendingScore || !link.is_current" @click="review('accepted')">接受建议</button><button :disabled="busy || !!pendingScore || !link.is_current" @click="review('revised')">提交修订</button><button :disabled="busy || !!pendingScore || !link.is_current" @click="review('rejected')">拒绝建议</button><button :disabled="busy || !!pendingScore || !link.is_current" @click="review('regrading_required')">要求重新评分</button></div>
      </template>
    </template>
  </section>
</template>
