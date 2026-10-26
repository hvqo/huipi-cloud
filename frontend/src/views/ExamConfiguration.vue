<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { configuration as api } from '../api/exam-configuration'
import { ApiError, type Schema } from '../api/client'
import PageState from '../components/PageState.vue'

type Session = Schema['ConfigurationSessionView']
type Roster = Schema['ConfigurationRosterSummary']
type Entry = Schema['ConfigurationCandidateStudent'] & { included: boolean }
const route = useRoute(),
  router = useRouter()
const options = ref<Schema['ConfigurationOptions']>()
const events = ref<Schema['ConfigurationEventView'][]>([]),
  sessions = ref<Session[]>([]),
  papers = ref<Schema['ConfigurationPaperChoice'][]>([])
const eventId = ref(''),
  current = ref<Session>(),
  history = ref<Roster[]>([]),
  roster = ref<Roster>()
const entries = ref<Entry[]>([]),
  businessDate = ref(''),
  classes = ref<string[]>([])
const busy = ref(false),
  error = ref(''),
  notice = ref(''),
  eventDialog = ref(false),
  sessionDialog = ref(false)
const pending = ref<{ path: string; body: unknown; kind: string }>(),
  moreEvents = ref(false),
  moreSessions = ref(false),
  morePapers = ref(false)
const eventOffset = ref(0),
  sessionOffset = ref(0),
  paperOffset = ref(0),
  entryPage = ref(0)
const draft = ref({ title: '', category: 'midterm', year: '', term: '' })
const schedule = ref({ title: '', subject: '', starts: '', ends: '', policy: 'single' })
const bind = ref({ paper: '', variant: 'A' }),
  reason = ref('核对本次参试名单'),
  savedEntries = ref('')
const selectedEvent = computed(() => events.value.find((x) => x.id === eventId.value))
const locked = computed(() => busy.value || !!pending.value)
const actions = computed(() => new Set(current.value?.actions || []))
const canRoster = computed(
  () => actions.value.has('exam.roster.manage') && actions.value.has('exam.roster.read'),
)
const frozen = computed(() => roster.value?.status === 'frozen')
const picked = computed(() => entries.value.filter((e) => e.included))
const dirty = computed(
  () => !!roster.value && JSON.stringify(entryPayload()) !== savedEntries.value,
)
const visibleEntries = computed(() =>
  entries.value.slice(entryPage.value * 100, (entryPage.value + 1) * 100),
)
const numbersMissing = computed(() => picked.value.filter((e) => !e.exam_number).length)
const numberDuplicate = computed(() => {
  const n = picked.value.map((e) => e.exam_number?.trim().toUpperCase()).filter(Boolean)
  return n.length !== new Set(n).size
})
const issues = computed(() => picked.value.some((e) => e.issues.length > 0))
const activeBindings = computed(
  () => current.value?.papers?.filter((p) => p.status === 'active') || [],
)
const readyPapers = computed(() => {
  const expected = current.value?.paper_policy === 'ab' ? ['A', 'B'] : ['A']
  return (
    expected.every((code) =>
      activeBindings.value.some(
        (p) =>
          p.variant_code === code &&
          p.paper_status === 'locked' &&
          p.content_current &&
          p.rubric_complete,
      ),
    ) && activeBindings.value.length === expected.length
  )
})
const stateLabels: Record<string, string> = {
  draft: '草稿',
  ready: '用卷已就绪',
  active: '已启用',
  closed: '已关闭',
  cancelled: '已取消',
  frozen: '已冻结',
  retired: '已退役',
  locked: '已锁定',
  revoked: '已撤销',
}
function state(value: string) {
  return stateLabels[value] || '待核对'
}
function time(value: string) {
  return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
}
function capable(action: string) {
  return actions.value.has(action)
}
function schoolTime(value: string) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) throw new Error('请填写完整的北京时间。')
  return value + ':00+08:00'
}
async function run(work: () => Promise<unknown>) {
  if (locked.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await work()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
async function loadLists() {
  const [ev, ss] = await Promise.all([
    api.events(eventOffset.value),
    api.sessions(eventId.value, sessionOffset.value),
  ])
  events.value = ev.items
  moreEvents.value = ev.has_more
  sessions.value = ss.items
  moreSessions.value = ss.has_more
}
async function loadPapers() {
  const value = await api.papers(paperOffset.value)
  papers.value = value.items
  morePapers.value = value.has_more
}
async function loadRoster(id: string) {
  const first = await api.roster(id)
  const values = [...first.entries]
  for (let offset = 100; offset < first.total; offset += 100) {
    if (offset >= 2000) throw new Error('名单超过网页安全读取上限。')
    values.push(...(await api.roster(id, offset)).entries)
  }
  roster.value = first.roster
  businessDate.value = first.roster.business_date
  entries.value = values.map((e) => ({
    student_id: e.student_id,
    student_name: e.student_name,
    student_code: e.student_code,
    class_id: e.administrative_class_id,
    class_name: e.class_name,
    exam_number: e.exam_number,
    session_paper_id: e.session_paper_id,
    issues: [],
    included: true,
  }))
  entryPage.value = 0
  savedEntries.value = JSON.stringify(entryPayload())
}
async function refreshSession(sid: string, preferredRoster = '') {
  current.value = await api.session(sid)
  sessions.value = sessions.value.map((s) => (s.id === sid ? current.value! : s))
  bind.value.variant = 'A'
  await loadPapers()
  history.value = []
  roster.value = undefined
  entries.value = []
  if (capable('exam.roster.read')) {
    history.value = (await api.rosters(sid)).revisions
    const selected =
      preferredRoster ||
      history.value.find((r) => r.status === 'draft')?.id ||
      history.value.find((r) => r.is_current)?.id ||
      ''
    if (selected) await loadRoster(selected)
  }
}
async function selectSession(sid: string) {
  await router.push('/exams/sessions/' + sid)
}
async function refresh() {
  await loadLists()
  if (current.value) await refreshSession(current.value.id, roster.value?.id)
}
async function mutate(path: string, body: unknown, kind: string) {
  pending.value = { path, body: JSON.parse(JSON.stringify(body)), kind }
  await send()
}
async function send() {
  const intent = pending.value
  if (!intent) return
  try {
    const result = await api.write<Schema['ConfigurationWriteResult'] | Roster>(
      intent.path,
      intent.body,
    )
    pending.value = undefined
    notice.value = '操作已保存。'
    if (intent.kind === 'event') {
      eventDialog.value = false
      eventId.value = result.id
      eventOffset.value = 0
      sessionOffset.value = 0
      await loadLists()
    } else if (intent.kind === 'session') {
      sessionDialog.value = false
      await loadLists()
      await selectSession(result.id)
      await refreshSession(result.id)
    } else if (intent.kind === 'roster') {
      await refreshSession(current.value!.id, result.id)
    } else {
      await refreshSession(current.value!.id)
    }
  } catch (e) {
    if (!(e instanceof ApiError && e.uncertain)) pending.value = undefined
    throw e
  }
}
async function retry() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await send()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
async function createEvent() {
  if (!draft.value.term) throw new Error('请选择学年和学期。')
  await mutate(
    '/events',
    {
      request_id: crypto.randomUUID(),
      academic_term_id: draft.value.term,
      title: draft.value.title,
      category: draft.value.category,
    },
    'event',
  )
}
async function createSession() {
  const ev = selectedEvent.value
  if (!ev) throw new Error('请选择当前考试。')
  await mutate(
    `/events/${ev.id}/sessions`,
    {
      request_id: crypto.randomUUID(),
      expected_event_revision: ev.revision,
      subject_id: schedule.value.subject,
      title: schedule.value.title,
      starts_at: schoolTime(schedule.value.starts),
      ends_at: schoolTime(schedule.value.ends),
      paper_policy: schedule.value.policy,
    },
    'session',
  )
}
async function bindPaper() {
  await mutate(
    `/sessions/${current.value!.id}/papers`,
    {
      request_id: crypto.randomUUID(),
      expected_revision: current.value!.revision,
      paper_version_id: bind.value.paper,
      variant_code: bind.value.variant,
    },
    'session-update',
  )
}
async function ready() {
  if (!window.confirm('确认本场次用卷配置已完成？名单仍需单独核对和冻结。')) return
  await mutate(
    `/sessions/${current.value!.id}/state`,
    {
      request_id: crypto.randomUUID(),
      expected_revision: current.value!.revision,
      lifecycle: 'ready',
    },
    'session-update',
  )
}
async function revokeBinding(id: string) {
  if (!window.confirm('撤销此用卷关联？历史关联仍会保留。')) return
  await mutate(
    `/sessions/${current.value!.id}/papers/revoke`,
    {
      request_id: crypto.randomUUID(),
      expected_revision: current.value!.revision,
      session_paper_id: id,
    },
    'session-update',
  )
}
async function previewStudents() {
  if (!classes.value.length) throw new Error('请先选择参试班级。')
  const sid = current.value!.id,
    revision = current.value!.revision
  const collected: Entry[] = []
  let offset = 0
  while (true) {
    const page = await api.candidates(sid, {
      request_id: crypto.randomUUID(),
      class_ids: classes.value,
      expected_session_revision: revision,
      offset,
      limit: 100,
    })
    businessDate.value = page.business_date
    collected.push(...page.students.map((s) => ({ ...s, included: true })))
    if (page.warnings.length) notice.value = '部分班级在考试日期没有可用学生，请核对。'
    if (!page.has_more) break
    offset += 100
    if (offset >= 2000) throw new Error('名单超过2000人安全上限。')
  }
  entries.value = collected
  entryPage.value = 0
  notice.value = `已按考试日期预览 ${collected.length} 人，请逐项核对考号和卷别。`
}
function entryPayload() {
  return picked.value.map((e) => ({
    student_id: e.student_id,
    exam_number: e.exam_number?.trim() || null,
    session_paper_id: e.session_paper_id || null,
  }))
}
async function saveRoster() {
  const body = { request_id: crypto.randomUUID(), entries: entryPayload() }
  if (roster.value) {
    await mutate(
      `/rosters/${roster.value.id}/entries`,
      { ...body, expected_revision: roster.value.revision },
      'roster',
    )
  } else {
    await mutate(
      `/sessions/${current.value!.id}/rosters`,
      { ...body, business_date: businessDate.value, reason: reason.value },
      'roster',
    )
  }
}
async function correctRoster() {
  if (!roster.value?.is_current) throw new Error('请选择当前冻结名单。')
  await mutate(
    `/rosters/${roster.value.id}/corrections`,
    {
      request_id: crypto.randomUUID(),
      expected_revision: roster.value.revision,
      entries: entryPayload(),
      reason: reason.value,
    },
    'roster',
  )
}
async function freezeRoster() {
  if (dirty.value) throw new Error('名单修改尚未保存，请先保存草稿。')
  if (
    !window.confirm(
      '确认冻结本版名单？冻结后只能通过新版本更正。未知卷别仍需后续核验，不代表已到考。',
    )
  )
    return
  await mutate(
    `/rosters/${roster.value!.id}/freeze`,
    { request_id: crypto.randomUUID(), expected_revision: roster.value!.revision },
    'roster',
  )
}
function generateNumbers() {
  picked.value.forEach((e, i) => {
    e.exam_number = `N${String(i + 1).padStart(4, '0')}`
  })
}
function bulkVariant(id: string) {
  picked.value.forEach((e) => {
    e.session_paper_id = id || null
  })
}
async function loadOptions() {
  let value = await api.options()
  for (let offset = 100; value.truncated && offset < 1000; offset += 100) {
    const next = await api.options(offset)
    if (!next.organization_available) {
      options.value = next
      return
    }
    const merge = <T extends { id: string }>(a: T[], b: T[]) => [
      ...new Map([...a, ...b].map((x) => [x.id, x])).values(),
    ]
    value = {
      ...next,
      years: merge(value.years, next.years),
      terms: merge(value.terms, next.terms),
      subjects: merge(value.subjects, next.subjects),
      classes: merge(value.classes, next.classes),
    }
  }
  options.value = value
}
async function load() {
  await loadOptions()
  await loadLists()
  const sid = String(route.params.sid || '')
  if (sid) await refreshSession(sid)
}
onMounted(() => run(load))
watch(
  () => route.params.sid,
  (value, old) => {
    if (value !== old && value) void run(() => refreshSession(String(value)))
  },
)
</script>

<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">考试组织 · 可信配置</p>
      <h1>考试工作台</h1>
      <p class="muted">配置考试、用卷与参试名单，再进入扫描和教师复核。</p>
    </div>
    <el-button
      type="primary"
      :disabled="locked || !options?.can_create_event || !options?.organization_available"
      @click="eventDialog = true"
      >新建考试</el-button
    >
  </div>
  <p v-if="error && !eventDialog && !sessionDialog" role="alert" class="error">
    {{ error }} <button :disabled="locked" @click="run(refresh)">刷新数据</button>
  </p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="pending && !eventDialog && !sessionDialog" role="alert" class="config-warning">
    保存结果尚未核实，暂时锁定编辑。请勿新建重复请求。<button :disabled="busy" @click="retry">
      重试原请求
    </button>
  </p>
  <p v-if="options && !options.organization_available" class="muted">
    当前身份未获组织资料读取权限，可查看已授权场次。新建和班级选择需由考务负责人授权。
  </p>
  <p v-if="options?.truncated" role="alert">
    组织选项超过网页1000项读取上限，请联系考务负责人；这里不代表已加载全部选项。
  </p>
  <fieldset :disabled="locked" class="config-toolbar">
    <label
      >考试<select
        v-model="eventId"
        @change="
          run(async () => {
            sessionOffset = 0
            await loadLists()
          })
        "
      >
        <option value="">全部可访问考试</option>
        <option v-for="ev in events" :key="ev.id" :value="ev.id">
          {{ ev.title }} · {{ state(ev.lifecycle) }}
        </option>
      </select></label
    ><el-button
      :disabled="
        !selectedEvent?.actions?.includes('exam.event.manage') ||
        selectedEvent?.lifecycle !== 'draft' ||
        !options?.organization_available
      "
      @click="sessionDialog = true"
      >新建科目场次</el-button
    ><el-button @click="run(refresh)">刷新考试状态</el-button>
  </fieldset>
  <div class="config-sessions">
    <article
      v-for="item in sessions"
      :key="item.id"
      :class="{ 'config-selected': current?.id === item.id }"
    >
      <small>{{ item.event_title }}</small>
      <h2>{{ item.title }}</h2>
      <p>{{ item.subject_name }} · {{ state(item.lifecycle) }}</p>
      <p class="muted">{{ time(item.starts_at) }}<br />{{ item.term_label }}</p>
      <button class="link" :disabled="locked" @click="selectSession(item.id)">配置场次 →</button>
    </article>
  </div>
  <PageState v-if="!sessions.length && !busy" empty />
  <div class="config-pages">
    <button
      v-if="sessionOffset"
      :disabled="locked"
      @click="
        run(async () => {
          sessionOffset -= 20
          await loadLists()
        })
      "
    >
      上一批场次</button
    ><button
      v-if="moreSessions"
      :disabled="locked"
      @click="
        run(async () => {
          sessionOffset += 20
          await loadLists()
        })
      "
    >
      下一批场次</button
    ><button
      v-if="eventOffset"
      :disabled="locked"
      @click="
        run(async () => {
          eventOffset -= 20
          await loadLists()
        })
      "
    >
      上一批考试</button
    ><button
      v-if="moreEvents"
      :disabled="locked"
      @click="
        run(async () => {
          eventOffset += 20
          await loadLists()
        })
      "
    >
      下一批考试
    </button>
  </div>
  <section v-if="current" class="config-detail" aria-label="考试场次配置">
    <header>
      <div>
        <p class="eyebrow">{{ current.event_title }}</p>
        <h2>{{ current.title }} · {{ state(current.lifecycle) }}</h2>
        <p>{{ time(current.starts_at) }} — {{ time(current.ends_at) }}（北京时间）</p>
      </div>
      <span>{{ current.paper_policy === 'ab' ? 'A/B 双卷' : '单卷' }}</span>
    </header>
    <section class="config-section" aria-label="场次用卷">
      <h2>1 · 选择已锁定试卷</h2>
      <RouterLink to="/papers">管理试卷、标准答案与评分标准 →</RouterLink>
      <p class="muted">
        共享用卷不复制题目。锁定内容不能在这里修改；可在试卷管理中创建草稿、核对并锁定。
      </p>
      <div class="config-overflow">
        <table>
          <thead>
            <tr>
              <th>卷别</th>
              <th>试卷版本</th>
              <th>题量 / 总分</th>
              <th>配置状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in current.papers" :key="p.id">
              <td>{{ p.variant_code }}</td>
              <td>{{ p.title }} · 第{{ p.version_no }}版</td>
              <td>{{ p.question_count }}题 / {{ p.max_points }}分</td>
              <td>
                {{ state(p.status) }} · {{ state(p.paper_status)
                }}<small v-if="!p.rubric_complete || !p.content_current" class="error"
                  >评分配置或来源异常</small
                >
              </td>
              <td>
                <button
                  v-if="
                    p.status === 'active' &&
                    current.lifecycle === 'draft' &&
                    capable('exam.session.paper.bind')
                  "
                  :disabled="locked"
                  @click="run(() => revokeBinding(p.id))"
                >
                  撤销关联
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <fieldset
        v-if="current.lifecycle === 'draft' && capable('exam.session.paper.bind')"
        :disabled="locked"
        class="config-toolbar"
      >
        <label
          >卷别<select v-model="bind.variant">
            <option>A</option>
            <option v-if="current.paper_policy === 'ab'">B</option>
          </select></label
        ><label
          >可用试卷<select v-model="bind.paper">
            <option value="">请选择已锁定试卷</option>
            <option
              v-for="p in papers"
              :key="p.paper_version_id"
              :value="p.paper_version_id"
              :disabled="!p.rubric_complete || !p.content_current"
            >
              {{ p.title }} · 第{{ p.version_no }}版 · {{ p.question_count }}题 /
              {{ p.max_points }}分
            </option>
          </select></label
        ><button :disabled="!bind.paper" @click="run(bindPaper)">绑定试卷</button
        ><button
          v-if="paperOffset"
          @click="
            run(async () => {
              paperOffset -= 20
              await loadPapers()
            })
          "
        >
          上一批试卷</button
        ><button
          v-if="morePapers"
          @click="
            run(async () => {
              paperOffset += 20
              await loadPapers()
            })
          "
        >
          下一批试卷
        </button>
      </fieldset>
      <el-button
        v-if="current.lifecycle === 'draft' && capable('exam.session.manage')"
        :disabled="locked || !readyPapers"
        type="primary"
        @click="run(ready)"
        >标记用卷准备就绪</el-button
      >
      <p v-if="current.lifecycle === 'ready'" class="config-positive">
        用卷已就绪。仍需单独核对并冻结参试名单，不代表已到考或已批改。
      </p>
    </section>
    <section class="config-section" aria-label="参试名单配置">
      <h2>2 · 核对参试名单</h2>
      <p v-if="!capable('exam.roster.read')">当前身份不能读取名单，请联系考务负责人。</p>
      <template v-else
        ><fieldset :disabled="locked" class="config-toolbar">
          <label
            >名单版本<select
              :value="roster?.id || ''"
              @change="run(() => loadRoster(($event.target as HTMLSelectElement).value))"
            >
              <option v-if="!history.length" value="">尚未建立名单</option>
              <option v-for="r in history" :key="r.id" :value="r.id">
                第{{ r.version_no }}版 · {{ state(r.status)
                }}{{ r.is_current ? ' · 当前有效' : '' }}
              </option>
            </select></label
          ><span v-if="businessDate">考试业务日期：{{ businessDate }}</span>
        </fieldset>
        <p v-if="frozen" class="config-positive">
          本版已冻结，只读保留；更正必须建立新版本，旧版不会被覆盖。
        </p>
        <p v-if="roster?.previous_revision_id && !frozen" class="config-warning">
          当前是更正草稿；冻结前仍以原冻结版为当前名单。
        </p>
        <fieldset
          v-if="!frozen && canRoster && options?.organization_available"
          :disabled="locked"
          class="config-class-select"
        >
          <legend>选择参试行政班</legend>
          <label v-for="c in options.classes" :key="c.id"
            ><input v-model="classes" type="checkbox" :value="c.id" />{{ c.display_name }}</label
          ><button @click="run(previewStudents)">按考试日期预览学生</button>
        </fieldset>
        <fieldset v-if="entries.length" :disabled="locked || frozen" class="config-toolbar">
          <button @click="generateNumbers">按当前顺序编排考号</button
          ><label
            >统一设置卷别<select @change="bulkVariant(($event.target as HTMLSelectElement).value)">
              <option value="">待核验</option>
              <option v-for="p in activeBindings" :key="p.id" :value="p.id">
                {{ p.variant_code }}卷
              </option>
            </select></label
          >
        </fieldset>
        <p v-if="entries.length">
          已选择{{ picked.length }}人 · {{ new Set(picked.map((e) => e.class_id)).size }}个班 ·
          考号缺失{{ numbersMissing }}人 · 卷别待核验{{
            picked.filter((e) => !e.session_paper_id).length
          }}人
        </p>
        <p v-if="numberDuplicate" role="alert" class="error">考号重复，请更正后保存。</p>
        <div v-if="entries.length" class="config-overflow">
          <table>
            <thead>
              <tr>
                <th>参试</th>
                <th>学生 / 学号</th>
                <th>考试时班级</th>
                <th>考号</th>
                <th>实际卷别</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="entry in visibleEntries" :key="entry.student_id">
                <td>
                  <input
                    v-model="entry.included"
                    type="checkbox"
                    :disabled="locked || frozen || !canRoster"
                    :aria-label="`选择学生 ${entry.student_code}`"
                  />
                </td>
                <td>
                  {{ entry.student_name }}<small>{{ entry.student_code }}</small
                  ><small v-if="entry.issues.length" class="error">学生已停用，不能冻结</small>
                </td>
                <td>{{ entry.class_name }}</td>
                <td>
                  <input
                    v-model="entry.exam_number"
                    :disabled="locked || frozen || !canRoster"
                    :aria-label="`考号 ${entry.student_code}`"
                    maxlength="32"
                  />
                </td>
                <td>
                  <select
                    v-model="entry.session_paper_id"
                    :disabled="locked || frozen || !canRoster"
                    :aria-label="`卷别 ${entry.student_code}`"
                  >
                    <option :value="null">待核验</option>
                    <option v-for="p in activeBindings" :key="p.id" :value="p.id">
                      {{ p.variant_code }}卷
                    </option>
                    <option
                      v-if="
                        entry.session_paper_id &&
                        !activeBindings.some((p) => p.id === entry.session_paper_id)
                      "
                      :value="entry.session_paper_id"
                      disabled
                    >
                      历史关联，现已不可用
                    </option>
                  </select>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="entries.length > 100" class="config-pages">
          <button :disabled="entryPage === 0" @click="entryPage--">上一页学生</button
          ><span>{{ entryPage + 1 }} / {{ Math.ceil(entries.length / 100) }}</span
          ><button :disabled="(entryPage + 1) * 100 >= entries.length" @click="entryPage++">
            下一页学生
          </button>
        </div>
        <p v-if="dirty && !frozen" class="config-warning">名单修改尚未保存，请先保存草稿再冻结。</p>
        <p class="muted">
          同名学生以学号与考号区分。未知卷别不会自动选A卷，可保留待核验；冻结名单不表示到考、缺考或零分。
        </p>
        <fieldset :disabled="locked" class="config-toolbar">
          <label>名单操作说明<input v-model="reason" maxlength="300" /></label
          ><el-button
            v-if="!frozen && canRoster"
            :disabled="!picked.length || numberDuplicate || issues"
            @click="run(saveRoster)"
            >保存名单草稿</el-button
          ><el-button
            v-if="roster && !frozen && capable('exam.roster.freeze')"
            type="primary"
            :disabled="
              dirty ||
              numbersMissing > 0 ||
              numberDuplicate ||
              !picked.length ||
              current.lifecycle !== 'ready'
            "
            @click="run(freezeRoster)"
            >冻结当前名单</el-button
          ><el-button
            v-if="frozen && roster?.is_current && capable('exam.roster.correct') && canRoster"
            :disabled="history.some((r) => r.status === 'draft')"
            @click="run(correctRoster)"
            >创建更正草稿</el-button
          >
        </fieldset>
      </template>
    </section>
    <section class="config-section" aria-label="后续处理入口">
      <h2>3 · 进入扫描与教师复核</h2>
      <p class="muted">
        以下入口继续独立校验授权和当前来源。配置权限不会自动授予扫描、批改或班级成绩权限。
      </p>
      <div class="config-next">
        <RouterLink :to="`/scans?session=${current.id}`">扫描接收与页面预览 →</RouterLink
        ><RouterLink :to="`/scans/sessions/${current.id}/scripts`"
          >人工分卷、身份确认与批改 →</RouterLink
        ><RouterLink :to="`/scans/sessions/${current.id}/results`"
          >整卷预览与Excel导出（需班级授权） →</RouterLink
        >
      </div>
    </section>
  </section>
  <el-dialog
    v-model="eventDialog"
    title="新建考试"
    width="min(600px,94vw)"
    :show-close="!pending"
    :close-on-click-modal="!pending"
    :close-on-press-escape="!pending"
    ><p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="pending" role="alert" class="config-warning">
      保存结果尚未核实，暂时锁定编辑。<button :disabled="busy" @click="retry">重试原请求</button>
    </p>
    <fieldset :disabled="locked" class="config-form">
      <label
        >考试名称<input
          v-model="draft.title"
          maxlength="200"
          placeholder="例如：高二年级期中考试" /></label
      ><label
        >考试类型<select v-model="draft.category">
          <option value="midterm">期中考试</option>
          <option value="final">期末考试</option>
          <option value="monthly">月考</option>
          <option value="other">其他考试</option>
        </select></label
      ><label
        >学年<select aria-label="学年" v-model="draft.year" @change="draft.term = ''">
          <option value="">请选择学年</option>
          <option v-for="y in options?.years" :key="y.id" :value="y.id">
            {{ y.display_name }}
          </option>
        </select></label
      ><label
        >学期<select aria-label="学期" v-model="draft.term">
          <option value="">请选择学期</option>
          <option
            v-for="t in options?.terms.filter((t) => t.academic_year_id === draft.year)"
            :key="t.id"
            :value="t.id"
          >
            {{ t.code }} · {{ t.start_date }} 至 {{ t.end_date }}
          </option>
        </select></label
      ><el-button
        type="primary"
        :disabled="!draft.title.trim() || !draft.term"
        @click="run(createEvent)"
        >创建考试</el-button
      >
    </fieldset></el-dialog
  >
  <el-dialog
    v-model="sessionDialog"
    title="新建科目场次"
    width="min(600px,94vw)"
    :show-close="!pending"
    :close-on-click-modal="!pending"
    :close-on-press-escape="!pending"
    ><p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="pending" role="alert" class="config-warning">
      保存结果尚未核实，暂时锁定编辑。<button :disabled="busy" @click="retry">重试原请求</button>
    </p>
    <fieldset :disabled="locked" class="config-form">
      <p>{{ selectedEvent?.title }} · {{ selectedEvent?.term_label }}</p>
      <label>场次名称<input v-model="schedule.title" maxlength="200" /></label
      ><label
        >学科<select aria-label="学科" v-model="schedule.subject">
          <option value="">请选择学科</option>
          <option v-for="s in options?.subjects" :key="s.id" :value="s.id">
            {{ s.display_name }}
          </option>
        </select></label
      ><label>开始时间（北京时间）<input v-model="schedule.starts" type="datetime-local" /></label
      ><label>结束时间（北京时间）<input v-model="schedule.ends" type="datetime-local" /></label
      ><label
        >用卷方式<select v-model="schedule.policy">
          <option value="single">单卷</option>
          <option value="ab">A/B 双卷</option>
        </select></label
      ><el-button
        type="primary"
        :disabled="
          !schedule.title.trim() || !schedule.subject || !schedule.starts || !schedule.ends
        "
        @click="run(createSession)"
        >创建场次</el-button
      >
    </fieldset></el-dialog
  >
</template>

<style scoped>
fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
label {
  display: grid;
  gap: 6px;
  font-size: 14px;
}
input:not([type='checkbox']),
select {
  font: inherit;
  border: 1px solid #ccd9dc;
  border-radius: 6px;
  padding: 9px;
  background: #fff;
  max-width: 100%;
  min-width: 0;
}
input:disabled,
select:disabled {
  color: #51676e;
  background: #f1f4f5;
}
.config-toolbar {
  display: flex;
  gap: 14px;
  align-items: end;
  flex-wrap: wrap;
  margin: 20px 0;
}
.config-sessions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 16px;
  margin: 20px 0;
}
.config-sessions article {
  padding: 20px;
  border: 1px solid #dbe4e7;
  border-radius: 8px;
  background: white;
}
.config-sessions h2 {
  margin-top: 10px;
}
.config-selected {
  border-color: #146f64 !important;
}
.config-detail {
  margin-top: 32px;
}
.config-detail > header {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  padding-bottom: 12px;
}
.config-section {
  border-top: 1px solid #dbe4e7;
  padding: 24px 0;
}
.config-class-select {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin: 20px 0;
}
.config-class-select label {
  display: flex;
  align-items: center;
  gap: 6px;
}
.config-positive {
  color: #146f64;
}
.config-warning {
  color: #8c5b17;
}
.config-overflow {
  overflow: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 14px;
}
th,
td {
  padding: 12px;
  border-bottom: 1px solid #e0e8eb;
  vertical-align: top;
}
th {
  background: #edf2f6;
}
small {
  display: block;
  color: #657981;
  overflow-wrap: anywhere;
  margin: 5px 0;
}
td input:not([type='checkbox']) {
  max-width: 160px;
}
.config-form {
  display: grid;
  gap: 16px;
}
.config-next {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
}
.config-next a {
  color: #146f64;
  text-decoration: underline;
}
.config-pages {
  display: flex;
  gap: 16px;
  align-items: center;
  margin: 16px 0;
}
.link {
  color: #146f64;
  background: none;
  border: none;
  font-weight: 600;
}
@media (max-width: 800px) {
  .config-toolbar {
    align-items: stretch;
  }
  .config-toolbar label {
    width: 100%;
  }
  table {
    min-width: 700px;
  }
  .config-next {
    display: grid;
    gap: 16px;
  }
}
</style>
