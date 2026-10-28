<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { examResults } from '../api/exam-results'
import { ApiError, type Schema } from '../api/client'
import { finalResults, type AttendanceRow, type FinalRead, type Version } from '../api/final-results'

const sessions = ref<Schema['SessionSummary'][]>([])
const sid = ref(''), classId = ref(''), error = ref(''), note = ref('')
const attendance = ref<AttendanceRow[]>([]), preview = ref<Schema['StudentResult'][]>([])
const attendanceDraft = ref<Record<string, AttendanceRow['status']>>({})
const versions = ref<Version[]>([]), selected = ref<FinalRead>(), reason = ref('教师核对后确认')
const busy = ref(false)
const pending = ref<{ path: string; body: object }>()
const sessionOffset = ref(0), sessionsMore = ref(false)
let loadGeneration = 0
const session = computed(() => sessions.value.find((value) => value.session_id === sid.value))
const latest = computed(() => versions.value.find((value) => value.status === 'published'))
const statusText: Record<string, string> = { present: '到考', absent: '缺考', unverified: '待核验', pending: '待审批', approved: '已批准待发布', published: '已发布', cancelled: '已作废', complete: '全部复核完成', incomplete: '尚未完成', identity_pending: '身份待确认', source_stale: '来源已失效', variant_unverified: '卷别待核验' }
const errorText: Record<string, string> = {
  final_attendance_unverified: '仍有学生的到考情况待核验。',
  final_absent_has_confirmed_script: '该学生已有确认答卷，请先核对身份和到考情况。',
  final_results_incomplete: '仍有题目缺评、身份待确认或来源失效，请先完成批改。',
  final_source_changed_reprepare: '名单、答卷或复核已变化，请作废此版本并重新生成。',
  final_previous_version_changed: '已发布版本发生变化，请刷新后重试。',
  final_no_changes: '与已发布版本没有变化，无需创建更正版本。',
  final_roster_limit: '本次成绩管理最多支持 100 名学生。',
}
function message(e: unknown) { return e instanceof ApiError ? errorText[e.code] || e.message : e instanceof Error ? e.message : '请求未完成' }
async function load() {
  const generation = ++loadGeneration
  const sessionId = sid.value, selectedClass = classId.value
  error.value = ''
  selected.value = undefined
  if (!sessionId) {
    attendance.value = []; preview.value = []; versions.value = []
    return
  }
  busy.value = true
  try {
    const [a, v] = await Promise.all([finalResults.attendance(sessionId, selectedClass), finalResults.versions(sessionId)])
    const collected: Schema['StudentResult'][] = []
    let offset = 0
    for (;;) {
      const page = await examResults.preview(sessionId, selectedClass, offset)
      collected.push(...page.students)
      if (!page.has_more || collected.length >= 100) break
      offset += page.limit
    }
    if (generation !== loadGeneration) return
    attendance.value = a.items
    attendanceDraft.value = Object.fromEntries(a.items.map((item) => [item.participation_id, item.status]))
    versions.value = v.items
    preview.value = collected
  } catch (e) { if (generation === loadGeneration) error.value = message(e) }
  finally { if (generation === loadGeneration) busy.value = false }
}
async function loadSessions(offset = 0) {
  if (offset < 0 || offset > 2000 || busy.value || pending.value) return
  busy.value = true; error.value = ''
  try {
    const value = await examResults.sessions('', offset)
    sessionOffset.value = offset
    sessionsMore.value = value.truncated
    sessions.value = value.items
    sid.value = value.items[0]?.session_id || ''
    classId.value = ''
    busy.value = false
    await load()
  } catch (e) { error.value = message(e) } finally { busy.value = false }
}
function changeSession() { classId.value = ''; void load() }
async function submit() {
  if (!pending.value || busy.value) return
  busy.value = true; error.value = ''; note.value = ''
  try {
    await finalResults.mutate(pending.value.path, pending.value.body)
    pending.value = undefined
    note.value = '操作已保存。'
    busy.value = false
    await load()
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
    error.value = message(e)
  } finally { busy.value = false }
}
function attendanceAction(row: AttendanceRow) {
  pending.value = { path: `${finalResults.root(sid.value)}/attendance`, body: {
    request_id: crypto.randomUUID(), participation_id: row.participation_id,
    expected_revision: row.revision, status: attendanceDraft.value[row.participation_id], reason: reason.value.trim(),
  } }
  void submit()
}
function prepare() {
  pending.value = { path: `${finalResults.root(sid.value)}/versions`, body: {
    request_id: crypto.randomUUID(), previous_version_id: latest.value?.id || null, reason: reason.value.trim(),
  } }
  void submit()
}
function transition(version: Version, action: 'approve' | 'publish' | 'cancel') {
  pending.value = { path: `${finalResults.root(sid.value)}/versions/${version.id}/transition`, body: {
    request_id: crypto.randomUUID(), expected_revision: version.revision, action, reason: reason.value.trim(),
  } }
  void submit()
}
async function view(version: Version) {
  error.value = ''; busy.value = true
  try { selected.value = await finalResults.read(sid.value, version.id, classId.value) }
  catch (e) { error.value = message(e) } finally { busy.value = false }
}
async function download() {
  if (!selected.value) return
  try {
    const blob = await finalResults.export(sid.value, selected.value.version.id, classId.value)
    const url = URL.createObjectURL(blob), a = document.createElement('a')
    a.href = url; a.download = `正式成绩-v${selected.value.version.version_no}.xlsx`; a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) { error.value = message(e) }
}
const detail = (id: string) => preview.value.find((value) => value.participation_id === id)
onMounted(() => { void loadSessions() })
onUnmounted(() => { loadGeneration++; attendance.value = []; preview.value = []; selected.value = undefined })
</script>

<template>
  <div class="page-heading"><div><p class="eyebrow">考试管理 · 教师确认</p><h1>成绩管理</h1><p class="muted">人工确认到考情况，审批后发布；未完成批改不会成为正式成绩。</p></div></div>
  <p v-if="error" role="alert">{{ error }}</p><p v-if="note" role="status">{{ note }}</p>
  <p v-if="pending" role="alert">保存结果尚未确认。<button :disabled="busy" @click="submit">重试原请求</button></p>
  <div class="result-filters">
    <label>考试场次<select v-model="sid" :disabled="busy" @change="changeSession"><option v-for="s in sessions" :key="s.session_id" :value="s.session_id">{{ s.event_title }} · {{ s.title }}</option></select></label>
    <label>班级<select v-model="classId" :disabled="busy" @change="load"><option value="">全部已授权班级</option><option v-for="c in session?.classes || []" :key="c.class_id" :value="c.class_id">{{ c.class_name }}</option></select></label>
    <button :disabled="busy || !sid" @click="load">刷新</button>
  </div>
  <div v-if="sessionOffset || sessionsMore" class="result-filters">
    <button :disabled="busy || !!pending || sessionOffset === 0" @click="loadSessions(sessionOffset - 20)">上一批场次</button>
    <button :disabled="busy || !!pending || !sessionsMore || sessionOffset >= 2000" @click="loadSessions(sessionOffset + 20)">下一批场次</button>
    <span>第 {{ sessionOffset / 20 + 1 }} 批</span>
  </div>
  <section><h2>到考核对与批改进度</h2><p>没有答卷不代表缺考。考务负责人应逐人核验；生成版本时会检查全部班级与题目。</p>
    <label>本次操作说明 <input v-model="reason" maxlength="300" /></label>
    <div class="table-wrap"><table class="result-table"><thead><tr><th>班级</th><th>学生</th><th>学号 / 考号</th><th>到考状态</th><th>批改进度</th><th>操作</th></tr></thead>
      <tbody><tr v-for="row in attendance" :key="row.participation_id"><td>{{ row.class_name }}</td><td>{{ row.student_name }}</td><td>{{ row.student_code }} / {{ row.exam_number || '未设置' }}</td><td><span>已保存：{{ statusText[row.status] }}</span> <select v-model="attendanceDraft[row.participation_id]" :disabled="busy || !!pending"><option value="unverified">待核验</option><option value="present">到考</option><option value="absent">缺考</option></select></td><td>{{ statusText[detail(row.participation_id)?.status || ''] || '未加载' }} · {{ detail(row.participation_id)?.reviewed_count ?? 0 }} / {{ detail(row.participation_id)?.required_count ?? '—' }}</td><td><button :disabled="busy || !!pending || !reason.trim()" @click="attendanceAction(row)">确认</button></td></tr></tbody>
    </table></div>
    <p v-if="!attendance.length && !busy">暂无可查看的参试名单。</p>
    <button :disabled="busy || !!pending || !sid || !reason.trim()" @click="prepare">{{ latest ? '生成更正版本' : '生成待审批版本' }}</button>
  </section>
  <section><h2>成绩版本</h2><p>审批与发布由有权的考务负责人完成。变更来源后需作废旧待审批版本并重新生成。</p>
    <div class="table-wrap"><table class="result-table"><thead><tr><th>版本</th><th>状态</th><th>说明</th><th>操作</th></tr></thead><tbody>
      <tr v-for="v in versions" :key="v.id"><td>第 {{ v.version_no }} 版</td><td>{{ statusText[v.status] }}</td><td>{{ v.reason }}</td><td>
        <button v-if="v.status === 'pending'" :disabled="busy || !!pending" @click="transition(v, 'approve')">批准</button>
        <button v-if="v.status === 'approved'" :disabled="busy || !!pending" @click="transition(v, 'publish')">发布</button>
        <button v-if="v.status === 'pending' || v.status === 'approved'" :disabled="busy || !!pending" @click="transition(v, 'cancel')">作废</button>
        <button v-if="v.status !== 'cancelled'" :disabled="busy" @click="view(v)">{{ v.status === 'published' ? '查看正式成绩' : '查看待审批快照' }}</button>
      </td></tr></tbody></table></div>
  </section>
  <section v-if="selected"><div class="page-heading"><div><h2>{{ selected.result.published ? '已发布' : '待审批快照' }} · 第 {{ selected.version.version_no }} 版</h2><p>该版本为确认时快照；历史版本不会被后续更正覆盖。</p></div><button v-if="selected.result.published" @click="download">导出正式成绩 Excel</button></div>
    <div class="table-wrap"><table class="result-table"><thead><tr><th>班级</th><th>学生</th><th>学号</th><th>卷别</th><th>到考</th><th>正式总分</th><th>逐题分数</th></tr></thead><tbody>
      <tr v-for="s in selected.result.students" :key="s.participation_id"><td>{{ s.class_name }}</td><td>{{ s.student_name }}</td><td>{{ s.student_code }} / {{ s.exam_number || '未设置' }}</td><td>{{ s.variant || '—' }}</td><td>{{ statusText[s.attendance_status] }}</td><td>{{ s.total ?? '—' }}</td><td><span v-for="q in s.questions" :key="q.mapping_id">第{{ q.number }}题 {{ q.points }} / {{ q.max_points }} </span><RouterLink v-if="s.script_id" :to="`/scans/sessions/${sid}/scripts?script=${s.script_id}`">进入批改</RouterLink></td></tr>
    </tbody></table></div>
    <template v-if="selected.statistics"><h3>基础统计</h3><p>{{ selected.statistics.completion_definition }}；仅统计本次显示的已授权班级与该发布版本。</p>
    <p v-for="c in selected.statistics.classes" :key="c.class_id">{{ c.class_name }}：到考 {{ c.present }} / {{ c.total }}，缺考 {{ c.absent }}，完成率 {{ c.completion_rate }}%，缺评 {{ c.missing_reviews }}</p>
    <article v-for="p in selected.statistics.papers" :key="p.class_id + p.paper_version_id"><strong>{{ p.class_name }} · {{ p.variant }}卷（满分 {{ p.max_points }}）</strong><p>平均 {{ p.average }}，最高 {{ p.highest }}，最低 {{ p.lowest }}；{{ p.count }} 人</p><p><span v-for="(count, label) in p.distribution" :key="label">{{ label }}：{{ count }} 人 </span></p><p><span v-for="q in p.questions" :key="q.mapping_id">第{{ q.number }}题得分率 {{ q.score_rate }}% </span></p></article></template>
  </section>
</template>

<style scoped>
.result-filters { display: flex; gap: 16px; align-items: end; flex-wrap: wrap; margin: 24px 0; }
.result-filters label { display: grid; gap: 6px; }
section { margin: 30px 0; }
section h2 { margin-bottom: 8px; }
section p { color: #526578; }
.table-wrap { overflow-x: auto; border: 1px solid #e1e6ef; border-radius: 8px; margin: 16px 0; }
table { width: 100%; min-width: 900px; border-collapse: collapse; text-align: left; font-size: 14px; }
th, td { padding: 12px 14px; border-bottom: 1px solid #e6ebf2; vertical-align: top; }
th { background: #f2f5fa; color: #3d5066; }
select, input { padding: 8px; border: 1px solid #ccd6e2; border-radius: 5px; background: white; font: inherit; }
button { padding: 8px 12px; border: 1px solid #bac9d8; border-radius: 5px; background: white; color: #245a99; font: inherit; cursor: pointer; margin-right: 6px; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
[role='alert'] { color: #a52b26; }
</style>
