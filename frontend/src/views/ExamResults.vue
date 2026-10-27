<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { examResults } from '../api/exam-results'
import { ApiError, type Schema } from '../api/client'
import PageState from '../components/PageState.vue'

const route = useRoute()
const sessions = ref<Schema['SessionSummary'][]>([])
const sessionId = ref(String(route.params.sid || ''))
const eventId = ref(''),
  classId = ref(''),
  offset = ref(0),
  sessionOffset = ref(0)
const moreSessions = ref(false),
  loading = ref(false),
  exporting = ref(false),
  error = ref('')
const result = ref<Schema['Preview']>(),
  selected = ref<Schema['StudentResult']>()
const events = computed(() => [
  ...new Map(
    sessions.value.map((s) => [s.event_id, { id: s.event_id, title: s.event_title }]),
  ).values(),
])
const labels: Record<string, string> = {
  complete: '全部复核完成',
  incomplete: '未完成',
  identity_pending: '身份待确认',
  source_stale: '来源失效',
  variant_unverified: '卷别待核验',
  reviewed: '已完成复核',
  not_scored: '尚未评分',
  awaiting_review: '待教师复核',
  regrading_required: '需要重新评分',
  rejected: '候选已拒绝',
  abstained: '模型弃权',
  accepted: '教师接受',
  revised: '教师修订',
}
const reasons: Record<string, string> = {
  identity_not_confirmed: '尚未确认学生身份',
  attendance_not_determined: '尚未确认是否缺考',
  variant_unverified: '请先核验实际卷别',
  parsing_not_complete: '解析尚未完成',
  marking_task_not_submitted_or_changed: '阅卷任务尚未提交或已调整',
  script_or_roster_changed: '答卷或名单已变更，请重新核对',
  paper_unavailable: '试卷已失效',
  source_or_authority_changed: '来源或相关授权已变更',
}
let generation = 0,
  alive = true
function text(code: string) {
  return labels[code] || reasons[code] || '需要人工核对'
}
async function load() {
  const current = ++generation
  result.value = undefined
  selected.value = undefined
  error.value = ''
  loading.value = true
  try {
    if (!sessionId.value) return
    const value = await examResults.preview(sessionId.value, classId.value, offset.value)
    if (alive && current === generation) result.value = value
  } catch (e) {
    if (alive && current === generation) error.value = (e as Error).message
  } finally {
    if (alive && current === generation) loading.value = false
  }
}
async function loadSessions() {
  error.value = ''
  try {
    const value = await examResults.sessions(eventId.value, sessionOffset.value)
    if (!alive) return
    sessions.value = value.items
    moreSessions.value = value.truncated
    if (!sessionId.value || !sessions.value.some((s) => s.session_id === sessionId.value))
      sessionId.value = value.items[0]?.session_id || ''
    classId.value = ''
    offset.value = 0
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}
async function changeEvent() {
  sessionOffset.value = 0
  await loadSessions()
}
async function sessionPage(delta: number) {
  sessionOffset.value += delta
  await loadSessions()
}
async function changeSession() {
  classId.value = ''
  offset.value = 0
  await load()
}
async function filterClass() {
  offset.value = 0
  await load()
}
async function page(delta: number) {
  offset.value += delta
  await load()
}
async function exportFile() {
  error.value = ''
  exporting.value = true
  try {
    const blob = await examResults.export(sessionId.value, classId.value)
    const url = URL.createObjectURL(blob),
      a = document.createElement('a')
    a.href = url
    a.download = '整卷成绩预览.xlsx'
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) {
    error.value =
      e instanceof ApiError && e.status === 403
        ? '导出未获授权，请选择已授权导出的班级，或联系考务负责人。'
        : (e as Error).message
  } finally {
    exporting.value = false
  }
}
onMounted(loadSessions)
onUnmounted(() => {
  alive = false
  generation++
})
</script>

<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">教师核对 · 内部预览</p>
      <h1>整卷成绩预览</h1>
      <p class="muted">仅汇总教师已复核或确认的分数。未完成不补零，预览与导出均不发布成绩。</p>
    </div>
    <el-button :disabled="!result || exporting || loading" @click="exportFile">{{
      exporting ? '正在核验并导出…' : '导出 Excel 预览'
    }}</el-button>
  </div>
  <div class="result-filters">
    <label
      >考试<select v-model="eventId" @change="changeEvent">
        <option value="">全部可访问考试</option>
        <option v-for="e in events" :key="e.id" :value="e.id">{{ e.title }}</option>
      </select></label
    >
    <label
      >场次<select v-model="sessionId" :disabled="loading" @change="changeSession">
        <option v-for="s in sessions" :key="s.session_id" :value="s.session_id">
          {{ s.title }}
        </option>
      </select></label
    >
    <label
      >班级<select v-model="classId" :disabled="loading" @change="filterClass">
        <option value="">全部授权班级</option>
        <option
          v-for="c in sessions.find((s) => s.session_id === sessionId)?.classes ||
          result?.classes ||
          []"
          :key="c.class_id"
          :value="c.class_id"
        >
          {{ c.class_name }}
        </option>
      </select></label
    >
    <el-button :disabled="loading || !sessionId" @click="load">刷新当前结果</el-button>
  </div>
  <p v-if="error" role="alert" class="error">{{ error }}</p>
  <p v-if="!sessions.length && !loading && !error" class="muted">
    暂无已授权班级的考试场次，请联系考务负责人。
  </p>
  <PageState :loading="loading" :empty="!result?.students.length" @retry="load">
    <template v-if="result">
      <p class="muted">
        {{ result.event_title }} · {{ result.session_title }} · 共
        {{ result.total_students }} 位授权参试者
      </p>
      <div class="result-table">
        <table>
          <thead>
            <tr>
              <th>学生</th>
              <th>班级 / 考号</th>
              <th>卷别</th>
              <th>已复核 / 应复核</th>
              <th>已确认小计</th>
              <th>完整总分</th>
              <th>状态与操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="s in result.students"
              :key="s.participation_id"
              :data-participation="s.participation_id"
            >
              <td>
                <strong>{{ s.student_name }}</strong
                ><small>{{ s.student_code }}</small>
              </td>
              <td>
                {{ s.class_name }}<small>{{ s.exam_number || '考号未设置' }}</small>
              </td>
              <td>{{ s.variant || '待核验' }}</td>
              <td>{{ s.reviewed_count }} / {{ s.required_count ?? '待核验' }}</td>
              <td>{{ s.subtotal ?? '—' }}</td>
              <td>
                <strong>{{ s.total ?? '未完成' }}</strong
                ><small v-if="s.max_points">满分 {{ s.max_points }}</small>
              </td>
              <td>
                <span :class="['result-state', s.status]">{{ text(s.status) }}</span
                ><small v-for="reason in s.reasons" :key="reason">{{ text(reason) }}</small>
                <button class="link" @click="selected = s">查看各题与复核</button>
                <RouterLink
                  v-if="s.script_id"
                  :to="`/scans/sessions/${result.session_id}/scripts?script=${s.script_id}`"
                  >进入答卷批改</RouterLink
                >
                <RouterLink
                  v-if="(s.questions || []).some((q) => q.evidence_source === 'omr')"
                  :to="{ path: '/omr', query: { session: result.session_id } }"
                  >查看答题卡确认</RouterLink
                >
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="result-pager">
        <el-button :disabled="offset === 0 || loading" @click="page(-20)">上一页</el-button
        ><span
          >{{ offset + 1 }}–{{ offset + result.students.length }} /
          {{ result.total_students }}</span
        ><el-button :disabled="!result.has_more || loading" @click="page(20)">下一页</el-button>
      </div>
      <p class="muted">
        生成于
        {{
          new Date(result.generated_at).toLocaleString('zh-CN')
        }}。名单与来源变更后请刷新；无答卷不代表缺考。最多导出100人、2000个题目单元。
      </p>
    </template>
  </PageState>
  <div v-if="moreSessions || sessionOffset" class="result-pager">
    <el-button :disabled="sessionOffset === 0" @click="sessionPage(-20)">上一批场次</el-button
    ><el-button :disabled="!moreSessions" @click="sessionPage(20)">下一批场次</el-button>
  </div>
  <el-dialog
    :model-value="!!selected"
    title="逐题教师复核"
    width="min(850px, 94vw)"
    @close="selected = undefined"
  >
    <template v-if="selected"
      ><p>
        {{ selected.student_name }} · {{ selected.class_name }} · 考号
        {{ selected.exam_number || '未设置' }} · {{ selected.variant || '待核验' }}卷
      </p>
      <table class="result-question-table">
        <thead>
          <tr>
            <th>题目</th>
            <th>满分</th>
            <th>教师确认分</th>
            <th>复核记录</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="q in selected.questions" :key="q.mapping_id">
            <td>第{{ q.number }}题</td>
            <td>{{ q.max_points }}</td>
            <td>{{ q.points ?? '—' }}</td>
            <td>
              {{ text(q.status)
              }}<small v-if="q.review_id"
                >{{ text(q.review_status || '') }} · 修订{{ q.review_revision }}<br />记录
                {{ q.review_id }}</small
              >
              <small v-else-if="q.evidence_source === 'omr'">
                答题卡填涂 · {{ q.omr_decision_id ? '教师已确认' : '等待教师确认' }}
              </small>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!selected.questions?.length">身份或实际卷别尚未确认，暂不能确定应批题目集合。</p>
      <p class="muted">这里显示当前来源的复核记录；不是已发布成绩。</p></template
    >
  </el-dialog>
</template>

<style scoped>
.result-filters {
  display: flex;
  gap: 16px;
  align-items: end;
  flex-wrap: wrap;
  margin: 24px 0;
}
.result-filters label {
  display: grid;
  gap: 6px;
  font-size: 14px;
}
.result-filters select {
  min-width: 180px;
  padding: 9px;
  border: 1px solid var(--border, #dce2eb);
  border-radius: 6px;
  background: white;
  font: inherit;
}
.result-table {
  overflow: auto;
  border: 1px solid #e1e6ef;
  border-radius: 8px;
}
table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 14px;
}
th {
  background: #f2f5fa;
  font-weight: 600;
  color: #3d5066;
}
th,
td {
  padding: 14px 16px;
  border-bottom: 1px solid #e6ebf2;
  vertical-align: top;
}
small {
  display: block;
  color: #64748b;
  margin-top: 6px;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.result-table table {
  min-width: 900px;
}
.result-table .link {
  display: block;
  margin: 8px 0;
}
.result-state {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: #8c5b17;
}
.result-state.complete {
  color: #14766a;
}
.result-state.source_stale {
  color: #ad3737;
}
.result-pager {
  display: flex;
  align-items: center;
  gap: 16px;
  justify-content: flex-end;
  margin: 20px 0;
}
.link {
  background: none;
  border: 0;
  padding: 8px 8px 8px 0;
  color: #265da9;
  cursor: pointer;
  font: inherit;
}
.result-table a {
  display: block;
  margin-top: 6px;
}
.error {
  color: #b42318;
}
</style>
