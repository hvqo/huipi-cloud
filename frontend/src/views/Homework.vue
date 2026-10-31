<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, ApiError, id, request, type Schema } from '../api/client'

interface Teaching {
  class_id: string
  class_name: string
  term_id: string
  term_name: string
  subject_id: string
  subject_name: string
}
interface Student {
  participant_id: string
  student_name: string
  student_code: string
  class_id: string
  status: string
  current_submission_id: string | null
  version: number | null
  parsing_status: string | null
  reviewed_count: number
  question_count: number
}
interface Homework {
  offering_id: string
  due_at: string
  classes: { class_id: string; class_name: string }[]
  counts: {
    class_id: string
    expected: number
    submitted: number
    late: number
    reviewed_records: number
    submitted_question_count: number
  }[]
  students: Student[]
  offset: number
  limit: number
  has_next: boolean
}
interface Attempt {
  submission_id: string
  version: number
  is_current: boolean
  submitted_at: string
}
const aid = String(useRoute().params.aid)
const view = ref<Schema['AssignmentWorkbench']>(),
  teaching = ref<Teaching[]>([]),
  homework = ref<Homework>()
const error = ref(''),
  notice = ref(''),
  busy = ref(false),
  classId = ref(''),
  offset = ref(0)
const pageStatus = ref('all')
const shownStudentCodes = ref(new Set<string>())
function toggleStudentCode(participantId: string) {
  if (shownStudentCodes.value.has(participantId)) shownStudentCodes.value.delete(participantId)
  else shownStudentCodes.value.add(participantId)
}
const visibleStudents = computed(() => {
  const statusOrder: Record<string, number> = { not_submitted: 0, submitted: 1, late: 2 }
  return (homework.value?.students || [])
    .filter((student) => pageStatus.value === 'all' || student.status === pageStatus.value)
    .slice()
    .sort((a, b) => (statusOrder[a.status] ?? 3) - (statusOrder[b.status] ?? 3))
})
const classProgress = computed(() =>
  (homework.value?.classes || []).map((item) => ({
    ...item,
    count: homework.value?.counts.find((count) => count.class_id === item.class_id),
  })),
)
const group = ref(''),
  selected = ref<string[]>([]),
  deadline = ref('')
const q = ref({ number: 1, stem: '', max: '10.00', answer: '', rubric: '' })
const history = ref<{ name: string; items: Attempt[]; truncated: boolean }>()
const pending = ref<{ participant: string; requestId: string; file: File }>()
const options = computed(() => [
  ...new Map(
    teaching.value
      .filter((t) => t.subject_name === view.value?.assignment.subject)
      .map((t) => [`${t.term_id}:${t.subject_id}`, t]),
  ).values(),
])
const available = computed(() =>
  teaching.value.filter((t) => `${t.term_id}:${t.subject_id}` === group.value),
)
const text: Record<string, string> = {
  not_submitted: '未交',
  submitted: '已交',
  late: '迟交',
  pending: '等待解析',
  running: '解析中',
  succeeded: '解析完成',
  failed: '解析失败',
  retry_wait: '等待重试',
}
function show(e: unknown) {
  return e instanceof ApiError && e.code === 'homework_answer_not_ready'
    ? '请先核对并确认本题学生作答。'
    : e instanceof Error
      ? e.message
      : '操作未完成，请刷新后重试。'
}
async function refresh() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const [assignment, teachers] = await Promise.all([
      api.workbench(aid),
      request<{ items: Teaching[] }>('/api/v1/homework/teaching-assignments'),
    ])
    view.value = assignment
    teaching.value = teachers.items
    try {
      homework.value = await request<Homework>(
        `/api/v1/assignments/${id(aid)}/homework?offset=${offset.value}&limit=50${classId.value ? `&class_id=${id(classId.value)}` : ''}`,
      )
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) homework.value = undefined
      else throw e
    }
  } catch (e) {
    error.value = show(e)
  } finally {
    busy.value = false
  }
}
async function addQuestion() {
  busy.value = true
  error.value = ''
  try {
    await request(`/api/v1/assignments/${id(aid)}/homework-questions`, {
      body: JSON.stringify({
        question: {
          question_number: q.value.number,
          question_type: 'short_answer',
          stem: q.value.stem,
          max_score: q.value.max,
        },
        answer: { answer_content: q.value.answer },
        rubric: { criteria: [{ description: q.value.rubric, points: q.value.max, sort_order: 1 }] },
      }),
      errorCode: true,
    })
    q.value = { number: q.value.number + 1, stem: '', max: '10.00', answer: '', rubric: '' }
    notice.value = '题目、标准答案和评分项已保存。'
  } catch (e) {
    error.value = show(e)
  } finally {
    busy.value = false
    await refresh()
  }
}
async function publish() {
  busy.value = true
  error.value = ''
  try {
    await request(`/api/v1/assignments/${id(aid)}/publish`, { body: '{}' })
    notice.value = '作业内容已发布，可以选择班级布置。'
  } catch (e) {
    error.value = show(e)
  } finally {
    busy.value = false
    await refresh()
  }
}
async function assign() {
  const [termId, subjectId] = group.value.split(':')
  if (!termId || !subjectId || !selected.value.length || !deadline.value) return
  busy.value = true
  error.value = ''
  try {
    await request(`/api/v1/assignments/${id(aid)}/homework`, {
      body: JSON.stringify({
        term_id: termId,
        subject_id: subjectId,
        class_ids: selected.value,
        due_at: new Date(deadline.value).toISOString(),
      }),
      errorCode: true,
    })
    notice.value = '已布置，并按当前学籍生成应交名单。'
  } catch (e) {
    error.value = show(e)
  } finally {
    busy.value = false
    await refresh()
  }
}
function choose(event: Event, student: Student) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  input.value = ''
  pending.value = { participant: student.participant_id, requestId: crypto.randomUUID(), file }
}
async function upload() {
  if (!pending.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const data = new FormData()
    data.set('request_id', pending.value.requestId)
    data.set('file', pending.value.file)
    await request(`/api/v1/homework/participants/${id(pending.value.participant)}/attempts`, {
      body: data,
      timeoutMs: 120000,
      errorCode: true,
    })
    pending.value = undefined
    notice.value = '提交已保存，新的版本已设为当前有效版本。'
  } catch (e) {
    error.value = show(e)
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
  } finally {
    busy.value = false
    await refresh()
  }
}
async function loadHistory(student: Student) {
  try {
    const data = await request<{ student_name: string; items: Attempt[]; truncated: boolean }>(
      `/api/v1/homework/participants/${id(student.participant_id)}/attempts`,
    )
    history.value = { name: data.student_name, items: data.items, truncated: data.truncated }
  } catch (e) {
    error.value = show(e)
  }
}
function filter() {
  offset.value = 0
  void refresh()
}
function page(delta: number) {
  offset.value += delta
  void refresh()
}
onMounted(refresh)
onUnmounted(() => {
  pending.value = undefined
  homework.value = undefined
  history.value = undefined
})
</script>

<template>
  <RouterLink class="back-link" :to="`/assignments/${aid}`">← 返回作业</RouterLink>
  <div class="page-heading">
    <div>
      <p class="eyebrow">日常作业</p>
      <h1>{{ view?.assignment.title || '作业管理' }}</h1>
      <p class="muted">布置班级、代收作业并跟进批改。</p>
    </div>
  </div>
  <p v-if="error" role="alert" class="error">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <section v-if="view?.assignment.status === 'draft'" class="panel">
    <h2>添加题目与评分标准</h2>
    <p>先保存题目和标准答案，发布后再选择班级。</p>
    <form @submit.prevent="addQuestion">
      <label>题号 <input v-model.number="q.number" type="number" min="1" required /></label>
      <label>题目 <textarea v-model="q.stem" required /></label>
      <label>满分 <input v-model="q.max" inputmode="decimal" required /></label>
      <label>标准答案 <textarea v-model="q.answer" required /></label>
      <label>评分标准 <textarea v-model="q.rubric" required /></label>
      <button :disabled="busy">保存题目</button>
    </form>
    <p>当前已配置 {{ view.questions.length }} 题。</p>
    <button :disabled="busy || !view.questions.length" @click="publish">发布作业内容</button>
  </section>
  <section v-else-if="view && !homework" class="panel">
    <h2>选择任教班级</h2>
    <p>仅显示管理员登记的本学期任教关系；名单按布置当天的学籍生成。</p>
    <label
      >学期与学科
      <select v-model="group" @change="selected = []">
        <option value="">请选择</option>
        <option
          v-for="t in options"
          :key="`${t.term_id}:${t.subject_id}`"
          :value="`${t.term_id}:${t.subject_id}`"
        >
          {{ t.term_name }} · {{ t.subject_name }}
        </option>
      </select></label
    >
    <p v-if="!options.length">暂无可用任教班级，请联系学校管理员配置。</p>
    <div v-for="t in available" :key="t.class_id">
      <label
        ><input v-model="selected" type="checkbox" :value="t.class_id" /> {{ t.class_name }}</label
      >
    </div>
    <label>截止时间 <input v-model="deadline" type="datetime-local" required /></label>
    <button :disabled="busy || !selected.length || !deadline" @click="assign">布置作业</button>
  </section>
  <template v-if="homework">
    <section class="panel">
      <h2>班级完成情况</h2>
      <p>截止 {{ new Date(homework.due_at).toLocaleString('zh-CN') }}；未交不计零分。</p>
      <div class="homework-class-grid">
        <article v-for="c in classProgress" :key="c.class_id" class="homework-class">
          <header>
            <strong>{{ c.class_name }}</strong
            ><span v-if="c.count">已交 {{ c.count.submitted }} / 应交 {{ c.count.expected }}</span>
          </header>
          <template v-if="c.count">
            <progress
              v-if="
                c.count.expected > 0 &&
                c.count.submitted >= 0 &&
                c.count.submitted <= c.count.expected
              "
              :value="c.count.submitted"
              :max="c.count.expected"
              :aria-label="`${c.class_name}提交进度`"
            />
            <p v-else class="homework-progress-note">
              {{
                c.count.expected === 0 && c.count.submitted === 0
                  ? '暂无应交学生'
                  : '进度暂不可用，请刷新。'
              }}
            </p>
            <p class="homework-class-meta">
              <span
                >未交
                {{
                  c.count.submitted <= c.count.expected ? c.count.expected - c.count.submitted : '—'
                }}</span
              >
              <span>迟交 {{ c.count.late }}</span>
              <span
                >教师复核记录 {{ c.count.reviewed_records }} /
                {{ c.count.submitted_question_count }}</span
              >
            </p>
          </template>
          <p v-else class="homework-progress-note">统计暂不可用，请刷新。</p>
        </article>
      </div>
      <div class="homework-class-controls">
        <label
          >筛选班级
          <select v-model="classId" :disabled="busy || !!pending" @change="filter">
            <option value="">全部任教班级</option>
            <option v-for="c in homework.classes" :key="c.class_id" :value="c.class_id">
              {{ c.class_name }}
            </option>
          </select></label
        >
        <button :disabled="busy || !!pending" @click="refresh">刷新</button>
      </div>
    </section>
    <section class="panel">
      <h2>学生作业</h2>
      <p>复核数量仅表示已有记录；请进入批改页检查来源是否仍有效。</p>
      <label
        >筛选本页提交状态
        <select v-model="pageStatus" :disabled="busy || !!pending">
          <option value="all">全部状态</option>
          <option value="not_submitted">未交</option>
          <option value="submitted">已交（准时）</option>
          <option value="late">迟交</option>
        </select>
      </label>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>学生</th>
              <th>学号</th>
              <th>状态</th>
              <th>解析与复核记录</th>
              <th>代收 / 补交</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in visibleStudents" :key="s.participant_id">
              <td>{{ s.student_name }}</td>
              <td class="student-code-cell">
                <button
                  type="button"
                  class="student-code-toggle"
                  :title="s.student_code"
                  :aria-label="`查看${s.student_name}的完整学号`"
                  :aria-expanded="shownStudentCodes.has(s.participant_id)"
                  @click="toggleStudentCode(s.participant_id)"
                >
                  <span class="student-code-short">{{ s.student_code }}</span
                  ><span class="student-code-hint">查看</span>
                </button>
                <span v-if="shownStudentCodes.has(s.participant_id)" class="student-code-full">{{
                  s.student_code
                }}</span>
              </td>
              <td>
                {{ text[s.status] }} <span v-if="s.version">· 第 {{ s.version }} 次</span>
              </td>
              <td>
                {{ s.parsing_status ? text[s.parsing_status] : '—' }} · {{ s.reviewed_count }} /
                {{ s.question_count }}
              </td>
              <td>
                <label class="upload-picker"
                  >选择文件<input
                    :aria-label="`为${s.student_code}选择文件`"
                    type="file"
                    accept="application/pdf,image/jpeg,image/png"
                    :disabled="busy || !!pending"
                    @change="choose($event, s)"
                /></label>
              </td>
              <td>
                <RouterLink
                  v-if="s.current_submission_id"
                  :to="`/assignments/${aid}/submissions/${s.current_submission_id}`"
                  >进入批改</RouterLink
                >
                <button v-if="s.version" @click="loadHistory(s)">提交历史</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!homework.students.length">当前班级没有应交学生。</p>
      <p v-else-if="!visibleStudents.length">本页没有此状态的学生，可切换筛选或查看其他页。</p>
      <button :disabled="busy || !!pending || offset === 0" @click="page(-50)">上一页</button
      ><button :disabled="busy || !!pending || !homework.has_next" @click="page(50)">下一页</button>
      <div v-if="pending" class="notice">
        已选择 {{ pending.file.name }}。如保存结果未知，请保留此页面并重试原请求。
        <button :disabled="busy" @click="upload">{{ error ? '重试原请求' : '确认代收' }}</button>
      </div>
    </section>
  </template>
  <section v-if="history" class="panel">
    <h2>{{ history.name }} · 提交历史</h2>
    <p>历史版本仅供查看，新的批改应使用当前版本。</p>
    <p v-if="history.truncated">仅显示最近 100 次，较早的提交仍被保留。</p>
    <ul>
      <li v-for="item in history.items" :key="item.submission_id">
        第 {{ item.version }} 次 · {{ item.is_current ? '当前有效' : '历史版本' }} ·
        {{ new Date(item.submitted_at).toLocaleString('zh-CN') }}
        <RouterLink :to="`/assignments/${aid}/submissions/${item.submission_id}`">查看</RouterLink>
      </li>
    </ul>
    <button @click="history = undefined">关闭</button>
  </section>
</template>
<style scoped>
.homework-class-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  gap: 12px;
  margin: 16px 0;
}
.homework-class {
  background: #fff;
  border: 1px solid #e0e7f0;
  border-radius: 8px;
  padding: 14px 16px;
  line-height: 1.5;
  color: #52667c;
}
.homework-class header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.homework-class strong {
  color: #203a5d;
}
.homework-class header span,
.homework-class-meta,
.homework-progress-note {
  font-size: 13px;
}
.homework-class-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.homework-class-controls label,
.homework-class-controls button {
  margin: 0;
}
.homework-class progress {
  display: block;
  width: 100%;
  height: 6px;
  margin: 10px 0;
  border: 0;
  border-radius: 4px;
  overflow: hidden;
}
.homework-class progress::-webkit-progress-bar {
  background: #e8eef7;
}
.homework-class progress::-webkit-progress-value {
  background: #3a71cc;
}
.homework-class progress::-moz-progress-bar {
  background: #3a71cc;
}
.homework-class-meta {
  display: flex;
  gap: 8px 14px;
  flex-wrap: wrap;
  margin: 0;
}
.homework-progress-note {
  color: #657990;
  margin: 10px 0;
}
.student-code-cell {
  min-width: 165px;
}
#main-content .student-code-toggle:not(.el-button):not(.text-button) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  padding: 2px 0;
  margin: 0;
  border: 0;
  background: transparent;
  text-align: left;
}
.student-code-short {
  display: inline-block;
  max-width: 14ch;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  vertical-align: middle;
}
.student-code-hint {
  color: #3768ac;
  font-size: 12px;
  white-space: nowrap;
}
.student-code-full {
  display: block;
  max-width: 240px;
  margin-top: 6px;
  overflow-wrap: anywhere;
  color: #334d6b;
}
label {
  display: block;
  margin: 12px 0;
}
input,
select,
textarea {
  font: inherit;
  padding: 7px;
}
textarea {
  display: block;
  width: min(100%, 720px);
  min-height: 70px;
}
button {
  margin: 8px 8px 8px 0;
  padding: 8px 12px;
  cursor: pointer;
}
.upload-picker {
  display: inline-block;
  margin: 0;
  padding: 7px 11px;
  border: 1px solid #c8d5dc;
  border-radius: 4px;
  cursor: pointer;
}
.upload-picker input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}
.upload-picker:focus-within {
  outline: 2px solid #2c7484;
}
.table-wrap {
  overflow-x: auto;
}
table {
  width: 100%;
  min-width: 900px;
  border-collapse: collapse;
}
th,
td {
  padding: 10px;
  text-align: left;
  border-bottom: 1px solid #e1e7ef;
}
</style>
