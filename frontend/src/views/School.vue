<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ApiError, type Schema } from '../api/client'
import { school, schoolAccess as access, loadSchoolAccess, type Kind } from '../api/school'
import Pagination from '../components/Pagination.vue'

type Tab = 'basic' | 'students' | 'import' | 'history'
const tab = ref<Tab>('basic'),
  busy = ref(false),
  ready = ref(false),
  error = ref(''),
  notice = ref('')
const locked = computed(() => busy.value || !!pending.value)
const years = ref<Schema['AcademicYearPage']>(),
  cohorts = ref<Schema['GradeCohortPage']>(),
  subjects = ref<Schema['SubjectPage']>(),
  terms = ref<Schema['AcademicTermPage']>()
const classes = ref<Schema['SchoolClassPage']>(),
  students = ref<Schema['StudentPage']>(),
  history = ref<Schema['StudentImportBatchPage']>()
const year = ref(''),
  cohort = ref(''),
  cls = ref(''),
  search = ref('')
function today() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
const asOf = ref(today()),
  studentOffset = ref(0),
  classOffset = ref(0)
const enrollment = ref<Schema['ClassEnrollmentPage']>(),
  selectedStudent = ref<Schema['StudentSummary']>()
const dialog = ref(false),
  kind = ref<Kind>('academic-years'),
  form = ref<Record<string, string | number>>({})
const kindNames: Record<Kind, string> = {
  'academic-years': '学年',
  'academic-terms': '学期',
  'grade-cohorts': '入学届别',
  subjects: '学科',
  classes: '行政班',
}
const tabs: [Tab, string][] = [
  ['basic', '基础组织'],
  ['students', '学生名册'],
  ['import', '学生导入'],
  ['history', '导入记录'],
]
const mode = ref<Schema['StudentImportPreviewResult']['mode']>('student_with_enrollment'),
  file = ref<File>(),
  fileInput = ref<HTMLInputElement>()
const modeNames = {
  student_only: '仅建立学生',
  enrollment_only: '仅建立班级归属',
  student_with_enrollment: '学生及班级归属',
}
const preview = ref<Schema['StudentImportPreviewResult']>(),
  rows = ref<Schema['StudentImportRowPage']>(),
  result = ref<Schema['StudentImportConfirmResult']>(),
  invalidated = ref(false)
type Pending =
  | { type: 'create'; kind: Kind; body: string }
  | { type: 'preview'; body: FormData }
  | { type: 'confirm'; batch: string; body: string }
const pending = ref<Pending>()
const controllers = new Set<AbortController>(),
  urls = new Set<string>()
let disposed = false
function clearFile() {
  file.value = undefined
  if (fileInput.value) fileInput.value.value = ''
}
function clearImport() {
  preview.value = undefined
  rows.value = undefined
  result.value = undefined
  invalidated.value = false
  clearFile()
}
function clearSensitive() {
  students.value = undefined
  selectedStudent.value = undefined
  enrollment.value = undefined
  history.value = undefined
  clearImport()
}
const messages: Record<string, string> = {
  student_import_preview_stale: '预检后的资料已变化，请重新上传并预检，不会自动修改本次名单。',
  student_import_has_blocking_errors: '文件仍有阻塞错误，请修正CSV后重新预检。',
  student_import_file_encoding_unsupported: '请将文件另存为UTF-8 CSV，不支持GBK或UTF-16。',
  student_import_file_encoding_invalid: '文件不是有效的UTF-8文本，请检查编码。',
  student_import_columns_invalid: '表头与所选模式不一致，请使用对应模板。',
  student_import_row_limit_exceeded: '每次最多导入5000行，请拆分文件。',
  student_import_file_too_large: '文件不能超过2MiB。',
  organization_code_conflict: '编码已存在，请核对现有记录，不要重复创建。',
  organization_request_conflict: '此请求编号已用于不同内容，请先核对已有记录。',
  academic_term_overlap: '学期日期与已有学期重叠，请重新核对。',
  academic_term_outside_academic_year: '学期日期必须在所属学年内。',
  school_date_outside_academic_year: '名册日期必须在所选学年内。',
  student_import_already_confirmed: '此批次已确认，请在导入记录查看结果。',
}
async function run(action: (signal: AbortSignal) => Promise<void>) {
  if (busy.value) return
  const c = new AbortController()
  controllers.add(c)
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await action(c.signal)
  } catch (e) {
    if (!disposed && !c.signal.aborted) {
      error.value =
        e instanceof ApiError
          ? messages[e.code] || e.message
          : e instanceof Error
            ? e.message
            : '操作未完成，请重试。'
      if (e instanceof ApiError && [401, 403, 404].includes(e.status)) {
        clearSensitive()
        Object.assign(
          access,
          await loadSchoolAccess(c.signal).catch(() => ({
            can_read: false,
            can_manage: false,
            can_preview: false,
            can_confirm: false,
          })),
        )
      }
    }
  } finally {
    controllers.delete(c)
    if (!disposed) busy.value = false
  }
}
async function dictionaries(signal: AbortSignal) {
  const [y, c, s] = await Promise.all([
    school.years(0, signal),
    school.cohorts(0, signal),
    school.subjects(0, signal),
  ])
  if (disposed) return
  years.value = y
  cohorts.value = c
  subjects.value = s
  if (!year.value || !y.records.some((r) => r.id === year.value))
    year.value = y.records[0]?.id || ''
  const selected = y.records.find((r) => r.id === year.value)
  if (selected && (asOf.value < selected.start_date || asOf.value > selected.end_date))
    asOf.value = selected.start_date
  await refreshClasses(signal)
  if (year.value) terms.value = await school.terms(year.value, signal)
}
async function refreshClasses(signal: AbortSignal, offset = 0) {
  classes.value = await school.classes(year.value, cohort.value, asOf.value, offset, signal)
  classOffset.value = offset
}
async function refreshDate(signal: AbortSignal) {
  await refreshClasses(signal)
  if (tab.value === 'students') await studentPage(0, signal)
}
async function classOptions(offset: number, signal: AbortSignal) {
  cls.value = ''
  await refreshClasses(signal, offset)
  await studentPage(0, signal)
}
async function selectCohort(signal: AbortSignal) {
  cls.value = ''
  await refreshClasses(signal)
  if (tab.value === 'students') await studentPage(0, signal)
}
async function selectYear(signal: AbortSignal) {
  const y = years.value?.records.find((y) => y.id === year.value)
  if (y && (asOf.value < y.start_date || asOf.value > y.end_date)) asOf.value = y.start_date
  cls.value = ''
  terms.value = year.value ? await school.terms(year.value, signal) : undefined
  await refreshClasses(signal)
  if (tab.value === 'students') await studentPage(0, signal)
}
function chooseYear(id: string) {
  year.value = id
  void run(selectYear)
}
async function yearPage(offset: number, signal: AbortSignal) {
  years.value = await school.years(offset, signal)
  year.value = years.value.records[0]?.id || ''
  await selectYear(signal)
}
async function studentPage(offset: number, signal: AbortSignal) {
  students.value = await school.students(
    cls.value,
    search.value,
    asOf.value,
    offset,
    signal,
    year.value,
    cohort.value,
  )
  studentOffset.value = offset
  enrollment.value = undefined
  selectedStudent.value = undefined
}
async function switchTab(value: Tab, signal: AbortSignal) {
  tab.value = value
  if (value === 'basic') await refreshClasses(signal)
  if (value === 'students') await studentPage(0, signal)
  if (value === 'history') history.value = await school.history(0, signal)
}
async function showClass(id: string, signal: AbortSignal) {
  cls.value = id
  tab.value = 'students'
  await studentPage(0, signal)
}
async function showStudent(s: Schema['StudentSummary'], offset: number, signal: AbortSignal) {
  selectedStudent.value = s
  enrollment.value = await school.enrollments(s.id, offset, signal)
}
function openCreate() {
  form.value = {
    code: '',
    display_name: '',
    academic_year_id: year.value,
    grade_cohort_id: cohorts.value?.records[0]?.id || '',
    start_date: '',
    end_date: '',
    status: 'planned',
    term_no: 1,
    intake_year: new Date().getFullYear(),
  }
  dialog.value = true
}
function createBody() {
  const f = form.value
  const body: Record<string, unknown> = { request_id: crypto.randomUUID(), code: f.code }
  if (kind.value !== 'academic-terms') body.display_name = f.display_name
  if (['academic-years', 'academic-terms'].includes(kind.value)) {
    body.start_date = f.start_date
    body.end_date = f.end_date
  }
  if (kind.value === 'academic-years') body.status = f.status
  if (kind.value === 'academic-terms') {
    body.academic_year_id = f.academic_year_id
    body.term_no = Number(f.term_no)
  }
  if (kind.value === 'grade-cohorts') body.intake_year = Number(f.intake_year)
  if (kind.value === 'classes') body.grade_cohort_id = f.grade_cohort_id
  return JSON.stringify(body)
}
async function execute(value: Pending, signal: AbortSignal) {
  pending.value = value
  try {
    if (value.type === 'create') {
      const saved = await school.create(value.kind, value.body, signal)
      pending.value = undefined
      dialog.value = false
      if (value.kind === 'academic-years') year.value = saved.id
      await dictionaries(signal)
      notice.value = '组织信息已保存。'
    } else if (value.type === 'preview') {
      const data = await school.preview(value.body, signal)
      pending.value = undefined
      preview.value = data
      result.value = undefined
      invalidated.value = false
      rows.value = await school.rows(data.batch_id, 0, signal)
      notice.value = '预检完成，尚未写入正式学生与归属。'
    } else {
      result.value = await school.confirm(value.batch, value.body, signal)
      pending.value = undefined
      preview.value = await school.batch(value.batch, signal)
      clearFile()
      notice.value = '学生导入已确认，正式数据已保存。'
      await refreshClasses(signal)
    }
  } catch (e) {
    if (!(e instanceof ApiError) || !e.uncertain) pending.value = undefined
    if (e instanceof ApiError && e.status === 409 && value.type === 'confirm')
      invalidated.value = true
    throw e
  }
}
async function cohortPage(offset: number, signal: AbortSignal) {
  cohorts.value = await school.cohorts(offset, signal)
}
async function subjectPage(offset: number, signal: AbortSignal) {
  subjects.value = await school.subjects(offset, signal)
}
async function historyPage(offset: number, signal: AbortSignal) {
  history.value = await school.history(offset, signal)
}
async function rowPage(offset: number, signal: AbortSignal) {
  if (preview.value) rows.value = await school.rows(preview.value.batch_id, offset, signal)
}
function chooseFile(event: Event) {
  preview.value = undefined
  rows.value = undefined
  result.value = undefined
  invalidated.value = false
  const input = event.target as HTMLInputElement
  const next = input.files?.[0]
  if (next && next.size > 2 * 1024 * 1024) {
    error.value = '文件不能超过2MiB。'
    input.value = ''
    file.value = undefined
    return
  }
  file.value = next
}
async function preflight(signal: AbortSignal) {
  if (!file.value || !year.value) throw new Error('请选择学年和CSV文件。')
  const body = new FormData()
  body.append('request_id', crypto.randomUUID())
  body.append('mode', mode.value)
  body.append('academic_year_id', year.value)
  body.append('file', file.value)
  await execute({ type: 'preview', body }, signal)
}
async function confirmImport(signal: AbortSignal) {
  if (!preview.value?.can_confirm || invalidated.value || !access.can_confirm) return
  if (!window.confirm('确认将预检内容写入正式学生和班级归属？这不是考试名单冻结。')) return
  await execute(
    {
      type: 'confirm',
      batch: preview.value.batch_id,
      body: JSON.stringify({
        request_id: crypto.randomUUID(),
        expected_input_sha256: preview.value.input_sha256,
      }),
    },
    signal,
  )
}
async function openBatch(batch: string, signal: AbortSignal) {
  clearImport()
  preview.value = await school.batch(batch, signal)
  rows.value = await school.rows(batch, 0, signal)
}
async function download(signal: AbortSignal) {
  const blob = await school.template(mode.value, signal),
    url = URL.createObjectURL(blob)
  urls.add(url)
  const a = document.createElement('a')
  a.href = url
  a.download = `学生导入-${mode.value}.csv`
  a.click()
  setTimeout(() => {
    URL.revokeObjectURL(url)
    urls.delete(url)
  }, 1000)
}
function dispose() {
  disposed = true
  for (const c of controllers) c.abort()
  controllers.clear()
  clearSensitive()
  pending.value = undefined
  for (const u of urls) URL.revokeObjectURL(u)
  urls.clear()
}
onBeforeRouteLeave(
  () =>
    !pending.value ||
    window.confirm(
      '保存结果尚未核实，离开将清除本页文件与重试信息。请稍后到记录中核对，确定离开？',
    ),
)
onBeforeUnmount(dispose)
onMounted(() =>
  run(async (signal) => {
    Object.assign(access, await loadSchoolAccess(signal))
    ready.value = true
    if (access.can_read) await dictionaries(signal)
  }),
)
</script>

<template>
  <div class="school-heading">
    <div>
      <p class="eyebrow">学校管理 · 基础资料</p>
      <h1>学校基础数据</h1>
      <p class="muted">维护教学日历、班级和学生，为考试准备一致的名册。</p>
    </div>
    <RouterLink to="/exams">前往考试配置 →</RouterLink>
  </div>
  <p v-if="error" role="alert" class="error">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="!ready || busy" role="status">正在处理，请稍候…</p>
  <p v-if="ready && !access.can_read" class="notice">
    当前没有学校组织读取权限。请联系授权人员；普通建卷权限不能查看全校学生。
  </p>
  <aside v-if="pending" class="school-warning" role="alert">
    保存结果尚未核实，请勿重复提交。<button
      :disabled="busy"
      @click="run((s) => execute(pending!, s))"
    >
      重试原请求
    </button>
  </aside>
  <template v-if="ready && access.can_read">
    <nav class="school-tabs" aria-label="学校管理栏目">
      <button
        v-for="[key, name] in tabs"
        :key="key"
        :aria-pressed="tab === key"
        :disabled="locked"
        @click="run((s) => switchTab(key, s))"
      >
        {{ name }}
      </button>
    </nav>
    <section
      v-if="tab === 'basic' || tab === 'students'"
      class="school-filters"
      aria-label="组织筛选"
    >
      <label
        >学年<select v-model="year" :disabled="locked" @change="run(selectYear)">
          <option value="">全部学年</option>
          <option v-for="y in years?.records" :key="y.id" :value="y.id">
            {{ y.display_name }}
          </option>
        </select></label
      >
      <label
        >届别<select v-model="cohort" :disabled="locked" @change="run(selectCohort)">
          <option value="">全部届别</option>
          <option v-for="c in cohorts?.records" :key="c.id" :value="c.id">
            {{ c.display_name }}
          </option>
        </select></label
      >
      <label
        >名册日期<input v-model="asOf" type="date" :disabled="locked" @change="run(refreshDate)"
      /></label>
      <p class="muted">人数按该日期的有效归属计算，历史名册不会随转班改写。</p>
    </section>
    <section v-if="tab === 'basic'" aria-label="基础组织" class="school-basic">
      <div class="school-create" v-if="access.can_manage">
        <label
          >新增类型<select v-model="kind" :disabled="locked">
            <option v-for="(name, key) in kindNames" :key="key" :value="key">{{ name }}</option>
          </select></label
        ><el-button type="primary" :disabled="locked" @click="openCreate"
          >新建{{ kindNames[kind] }}</el-button
        >
      </div>
      <div class="school-dictionaries">
        <section>
          <h2>学年与学期</h2>
          <ul>
            <li v-for="y in years?.records" :key="y.id">
              <button :disabled="locked" @click="chooseYear(y.id)">
                {{ y.display_name }}
              </button>
              <small>{{ y.code }} · {{ y.start_date }}—{{ y.end_date }}</small>
            </li>
          </ul>
          <p v-if="!years?.records.length" class="muted">尚未建立学年</p>
          <Pagination
            v-if="years"
            label="学年"
            :offset="years.offset"
            :has-next="years.has_next"
            :disabled="locked"
            @change="(o) => run((s) => yearPage(o, s))"
          />
          <ul>
            <li v-for="t in terms?.records" :key="t.id">
              第{{ t.term_no }}学期 · {{ t.code }}<small>{{ t.start_date }}—{{ t.end_date }}</small>
            </li>
          </ul>
        </section>
        <section>
          <h2>入学届别</h2>
          <ul>
            <li v-for="c in cohorts?.records" :key="c.id">
              {{ c.display_name }}<small>{{ c.code }} · {{ c.intake_year }}年入学</small>
            </li>
          </ul>
          <p v-if="!cohorts?.records.length" class="muted">尚未建立届别</p>
          <Pagination
            v-if="cohorts"
            label="届别"
            :offset="cohorts.offset"
            :has-next="cohorts.has_next"
            :disabled="locked"
            @change="(o) => run((s) => cohortPage(o, s))"
          />
        </section>
        <section>
          <h2>学科</h2>
          <ul>
            <li v-for="s in subjects?.records" :key="s.id">
              {{ s.display_name }}<small>{{ s.code }}</small>
            </li>
          </ul>
          <p v-if="!subjects?.records.length" class="muted">尚未建立学科</p>
          <Pagination
            v-if="subjects"
            label="学科"
            :offset="subjects.offset"
            :has-next="subjects.has_next"
            :disabled="locked"
            @change="(o) => run((s) => subjectPage(o, s))"
          />
        </section>
      </div>
      <h2>行政班</h2>
      <div class="school-table">
        <table>
          <thead>
            <tr>
              <th>班级</th>
              <th>编码</th>
              <th>年级</th>
              <th>在班人数</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in classes?.records" :key="c.id">
              <td>{{ c.display_name }}</td>
              <td>{{ c.code }}</td>
              <td>{{ c.grade_label || '未选学年' }}</td>
              <td>{{ c.student_count }}</td>
              <td>{{ c.status === 'active' ? '使用中' : '已停用' }}</td>
              <td>
                <button :disabled="locked" @click="run((s) => showClass(c.id, s))">查看学生</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!classes?.records.length" class="muted">
        当前筛选下没有班级。可先创建届别和行政班，再导入学生。
      </p>
      <Pagination
        v-if="classes"
        label="班级"
        :offset="classes.offset"
        :has-next="classes.has_next"
        :disabled="locked"
        @change="(o) => run((s) => refreshClasses(s, o))"
      />
    </section>
    <section v-if="tab === 'students'" aria-label="学生名册">
      <form class="school-filters" @submit.prevent="run((s) => studentPage(0, s))">
        <label
          >本页班级<select v-model="cls">
            <option value="">当前条件下的全部学生</option>
            <option v-for="c in classes?.records" :key="c.id" :value="c.id">
              {{ c.display_name }}
            </option>
          </select></label
        ><label>学号或姓名<input v-model="search" maxlength="64" /></label
        ><el-button native-type="submit" :disabled="locked">查询学生</el-button>
      </form>
      <Pagination
        v-if="classes"
        :offset="classOffset"
        :has-next="classes.has_next"
        :disabled="locked"
        label="可选班级"
        @change="(offset) => run((s) => classOptions(offset, s))"
      />
      <p class="muted">未分班学生请清除学年与届别筛选后查询。</p>
      <div class="school-table">
        <table>
          <thead>
            <tr>
              <th>学号</th>
              <th>姓名</th>
              <th>状态</th>
              <th>班级归属</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in students?.records" :key="s.id">
              <td>{{ s.school_student_code }}</td>
              <td>{{ s.name }}</td>
              <td>{{ s.is_active ? '启用' : '停用' }}</td>
              <td>
                <button :disabled="locked" @click="run((signal) => showStudent(s, 0, signal))">
                  归属历史
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!students?.records.length" class="muted">没有符合条件的学生。未扫描不表示缺考。</p>
      <Pagination
        v-if="students"
        label="学生"
        :offset="students.offset"
        :has-next="students.has_next"
        :disabled="locked"
        @change="(o) => run((s) => studentPage(o, s))"
      />
      <section v-if="enrollment && selectedStudent" class="school-history">
        <h2>{{ selectedStudent.name }} · {{ selectedStudent.school_student_code }} 的班级归属</h2>
        <ul>
          <li v-for="e in enrollment.records" :key="e.id">
            {{ e.administrative_class_code }}：{{ e.valid_from }} 至
            {{ e.valid_to || '当前未结束' }}（结束日不含）
          </li>
        </ul>
        <Pagination
          label="归属"
          :offset="enrollment.offset"
          :has-next="enrollment.has_next"
          :disabled="locked"
          @change="(o) => run((s) => showStudent(selectedStudent!, o, s))"
        />
      </section>
    </section>
    <section v-if="tab === 'import'" aria-label="学生导入">
      <h2>学生导入</h2>
      <p>先预检、再确认。支持UTF-8 / UTF-8 BOM CSV，每批最多2MiB、5000行。不接受GBK。</p>
      <fieldset :disabled="locked || !!preview" class="school-filters">
        <label
          >导入学年<select v-model="year">
            <option value="">请选择学年</option>
            <option v-for="y in years?.records" :key="y.id" :value="y.id">
              {{ y.display_name }}
            </option>
          </select></label
        ><label
          >导入模式<select v-model="mode">
            <option v-for="(text, key) in modeNames" :key="key" :value="key">{{ text }}</option>
          </select></label
        ><label
          >CSV文件<input ref="fileInput" type="file" accept=".csv,text/csv" @change="chooseFile"
        /></label>
      </fieldset>
      <div class="school-filters">
        <el-button v-if="access.can_preview" :disabled="locked" @click="run(download)"
          >下载CSV模板</el-button
        ><el-button
          v-if="access.can_preview && !preview"
          type="primary"
          :disabled="locked || !file || !year"
          @click="run(preflight)"
          >预检CSV</el-button
        ><button v-if="preview" :disabled="locked" @click="clearImport">重新选择并预检</button>
      </div>
      <p v-if="!access.can_preview" class="notice">
        当前没有预检权限。已有批次可在导入记录中查看。
      </p>
      <p class="muted">模板仅含表头，请填写本校授权名单。同名学生按学号区分，不能仅凭姓名合并。</p>
    </section>
    <section v-if="tab === 'history'" aria-label="导入记录">
      <h2>导入记录</h2>
      <div class="school-table">
        <table>
          <thead>
            <tr>
              <th>创建时间</th>
              <th>模式</th>
              <th>行数</th>
              <th>错误 / 警告</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in history?.records" :key="b.batch_id">
              <td>{{ new Date(b.created_at).toLocaleString('zh-CN') }}</td>
              <td>{{ modeNames[b.mode] }}</td>
              <td>{{ b.row_count }}</td>
              <td>{{ b.counts.error_rows || 0 }} / {{ b.counts.warning_rows || 0 }}</td>
              <td>{{ b.status === 'confirmed' ? '已正式导入' : '仅预检，未导入' }}</td>
              <td>
                <button :disabled="locked" @click="run((s) => openBatch(b.batch_id, s))">
                  查看批次
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <Pagination
        v-if="history"
        label="导入批次"
        :offset="history.offset"
        :has-next="history.has_next"
        :disabled="locked"
        @change="(o) => run((s) => historyPage(o, s))"
      />
    </section>
    <section
      v-if="preview && (tab === 'import' || tab === 'history')"
      aria-label="导入预检结果"
      class="school-preview"
    >
      <h2>{{ preview.status === 'confirmed' ? '已正式导入' : '预检结果 · 尚未导入' }}</h2>
      <div class="school-counts">
        <span>共{{ preview.row_count }}行</span
        ><span>有效{{ preview.counts.valid_rows || 0 }}行</span
        ><span>警告{{ preview.counts.warning_rows || 0 }}行</span
        ><span :class="{ error: preview.counts.error_rows }"
          >错误{{ preview.counts.error_rows || 0 }}行</span
        >
      </div>
      <p v-if="result" role="status">
        新增{{ result.created_students }}名学生、{{ result.created_enrollments }}条归属，未变{{
          result.unchanged_rows
        }}行。
      </p>
      <p v-if="invalidated" class="school-warning">预检已失效，必须重新预检后确认。</p>
      <p v-if="preview.truncated" class="school-warning">
        摘要只保留前500条错误及警告；逐行信息请翻页核对。
      </p>
      <ul class="school-issues">
        <li v-for="(issue, i) in [...preview.errors, ...preview.warnings]" :key="i">
          第{{ issue.row_number }}行：{{ issue.message }}
        </li>
      </ul>
      <div class="school-table">
        <table>
          <thead>
            <tr>
              <th>行</th>
              <th>学号</th>
              <th>姓名</th>
              <th>班级编码</th>
              <th>生效日期</th>
              <th>检查</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rows?.records" :key="r.row_number">
              <td>{{ r.row_number }}</td>
              <td>{{ r.student_code }}</td>
              <td>{{ r.name }}</td>
              <td>{{ r.class_code || '—' }}</td>
              <td>{{ r.valid_from || '—' }}</td>
              <td>
                {{ r.row_state === 'error' ? '错误' : r.row_state === 'warning' ? '警告' : '通过' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <Pagination
        v-if="rows"
        label="预检行"
        :offset="rows.offset"
        :has-next="rows.has_next"
        :disabled="locked"
        @change="(o) => run((s) => rowPage(o, s))"
      />
      <el-button
        v-if="access.can_confirm && preview.status === 'previewed'"
        type="primary"
        :disabled="locked || !preview.can_confirm || invalidated"
        @click="run(confirmImport)"
        >确认正式导入</el-button
      >
      <p v-if="!access.can_confirm && preview.status === 'previewed'" class="notice">
        当前没有确认入库权限，不能正式导入。
      </p>
    </section>
  </template>
  <el-dialog
    v-model="dialog"
    :title="'新建' + kindNames[kind]"
    width="540px"
    :close-on-click-modal="false"
    :before-close="
      (done: () => void) => {
        if (!locked) done()
      }
    "
  >
    <form
      class="school-form"
      @submit.prevent="run((s) => execute({ type: 'create', kind, body: createBody() }, s))"
    >
      <fieldset :disabled="locked">
        <label v-if="kind === 'academic-terms'"
          >所属学年<select v-model="form.academic_year_id" required>
            <option v-for="y in years?.records" :key="y.id" :value="y.id">
              {{ y.display_name }}
            </option>
          </select></label
        >
        <label v-if="kind === 'classes'"
          >所属届别<select v-model="form.grade_cohort_id" required>
            <option v-for="c in cohorts?.records" :key="c.id" :value="c.id">
              {{ c.display_name }}
            </option>
          </select></label
        >
        <label>编码<input v-model="form.code" required maxlength="32" /></label
        ><label v-if="kind !== 'academic-terms'"
          >名称<input v-model="form.display_name" required maxlength="100"
        /></label>
        <template v-if="kind === 'academic-years' || kind === 'academic-terms'"
          ><label>开始日期<input v-model="form.start_date" type="date" required /></label
          ><label>结束日期<input v-model="form.end_date" type="date" required /></label
        ></template>
        <label v-if="kind === 'academic-years'"
          >学年状态<select v-model="form.status">
            <option value="planned">计划中</option>
            <option value="active">使用中</option>
            <option value="closed">已结束</option>
          </select></label
        >
        <label v-if="kind === 'academic-terms'"
          >学期序号<input v-model="form.term_no" type="number" min="1" max="4" required /></label
        ><label v-if="kind === 'grade-cohorts'"
          >入学年份<input v-model="form.intake_year" type="number" min="2000" max="2100" required
        /></label>
        <el-button native-type="submit" type="primary" :disabled="!access.can_manage"
          >保存{{ kindNames[kind] }}</el-button
        >
      </fieldset>
    </form>
  </el-dialog>
</template>

<style scoped>
.school-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 24px;
  margin-bottom: 24px;
}
.school-heading h1 {
  font-size: 26px;
  margin: 8px 0;
}
.school-tabs {
  display: flex;
  gap: 12px;
  border-bottom: 1px solid #dbe3e8;
  padding-bottom: 12px;
  margin: 20px 0;
}
.school-tabs button {
  background: transparent;
  border: 0;
  padding: 10px 18px;
  color: #45616b;
  font-weight: 600;
}
.school-tabs button[aria-pressed='true'] {
  color: #146f64;
  background: #e7f2f0;
  border-radius: 7px;
}
.school-filters,
.school-create {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 16px;
  margin: 18px 0;
}
.school-filters label,
.school-create label,
.school-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 14px;
}
.school-filters select,
.school-filters input,
.school-create select,
.school-form input,
.school-form select {
  padding: 10px;
  border: 1px solid #ccd8df;
  border-radius: 6px;
  background: #fff;
  min-height: 40px;
}
.school-dictionaries {
  display: grid;
  grid-template-columns: 1.4fr 1fr 1fr;
  gap: 28px;
  border-top: 1px solid #dbe3e8;
  border-bottom: 1px solid #dbe3e8;
  padding: 16px 0;
  margin-bottom: 26px;
}
.school-dictionaries h2,
.school-history h2 {
  font-size: 18px;
}
.school-dictionaries ul,
.school-history ul {
  padding: 0;
  list-style: none;
}
.school-dictionaries li {
  margin: 12px 0;
}
.school-dictionaries button {
  padding: 0;
  border: 0;
  background: transparent;
  color: #176c72;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.school-table button,
.school-filters > button {
  padding: 7px 12px;
  border: 1px solid #cbd8df;
  border-radius: 6px;
  background: #fff;
  color: #254957;
  font: inherit;
  cursor: pointer;
}
.school-dictionaries small {
  display: block;
  color: #657a85;
  font-size: 13px;
  margin-top: 4px;
}
.school-pages {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 16px 0;
  font-size: 13px;
}
.school-pages button,
.school-table button {
  padding: 6px 10px;
}
.school-table {
  overflow: auto;
}
.school-table table {
  width: 100%;
  border-collapse: collapse;
  font-size: 15px;
  background: #fff;
}
.school-table th,
.school-table td {
  padding: 14px 12px;
  border-bottom: 1px solid #e2e8ed;
  text-align: left;
  white-space: nowrap;
}
.school-table th {
  background: #edf2f5;
  color: #435b66;
  font-size: 14px;
}
.school-warning {
  border-left: 3px solid #d49324;
  background: #fff6e6;
  padding: 14px;
}
.school-preview {
  border-top: 1px solid #d7e2e6;
  margin-top: 24px;
  padding-top: 16px;
}
.school-counts {
  display: flex;
  gap: 24px;
  font-weight: 600;
  margin: 16px 0;
}
.school-issues {
  max-height: 220px;
  overflow: auto;
  font-size: 14px;
}
.school-form fieldset {
  display: grid;
  gap: 16px;
  border: 0;
  padding: 0;
}
.school-form fieldset:disabled {
  opacity: 0.6;
}
.school-filters {
  border: 0;
  padding: 0;
}
@media (max-width: 1100px) {
  .school-dictionaries {
    grid-template-columns: 1fr;
  }
  .school-heading {
    align-items: start;
  }
  .school-counts {
    gap: 12px;
    flex-wrap: wrap;
  }
}
</style>
