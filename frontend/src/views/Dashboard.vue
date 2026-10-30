<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ArrowRight,
  Collection,
  Document,
  Notebook,
  Reading,
  Warning,
} from '@element-plus/icons-vue'
import { api, ApiError, id, request, type Schema } from '../api/client'
import { configuration } from '../api/exam-configuration'
import { marking, taskStatus, type MarkTask } from '../api/marking'
import { recovery } from '../api/recovery'
import { identity } from '../auth'
import { navigationAccess as nav } from '../navigation'
import { date, status } from '../lib/load'

type LoadState = 'loading' | 'ready' | 'unavailable'
interface HomeworkProgress {
  counts: { expected: number; submitted: number }[]
}
interface ExceptionSummary {
  id: string
  title: string
  event_title: string
  total: number
}
const assignments = ref<Schema['AssignmentSummary'][]>([])
const tasks = ref<MarkTask[]>([])
const taskPageCount = ref(0),
  taskHasNext = ref(false)
const sessions = ref<Schema['ConfigurationSessionView'][]>([])
const exceptions = ref<ExceptionSummary[]>([])
const hasExceptionSessions = ref(false)
const progress = ref<Record<string, HomeworkProgress | null>>({})
const states = ref<Record<'assignments' | 'tasks' | 'sessions' | 'exceptions', LoadState>>({
  assignments: 'loading',
  tasks: 'loading',
  sessions: 'loading',
  exceptions: 'loading',
})
const greeting = computed(() => {
  const hour = new Date().getHours()
  return hour < 11 ? '早上好' : hour < 14 ? '中午好' : hour < 18 ? '下午好' : '晚上好'
})
const lifecycle: Record<string, string> = {
  draft: '准备中',
  ready: '已准备',
  active: '进行中',
  closed: '已结束',
  cancelled: '已取消',
}
function progressText(row: Schema['AssignmentSummary']) {
  const item = progress.value[row.id]
  if (!item) return row.status === 'draft' ? '待完善作业内容' : '查看作业详情'
  const expected = item.counts.reduce((sum, count) => sum + count.expected, 0)
  const submitted = item.counts.reduce((sum, count) => sum + count.submitted, 0)
  return `已交 ${submitted} / 应交 ${expected}`
}
async function loadAssignments() {
  states.value.assignments = 'loading'
  try {
    const rows = await api.assignments()
    assignments.value = rows.slice(0, 4)
    const details = await Promise.allSettled(
      rows
        .slice(0, 4)
        .map((row) =>
          request<HomeworkProgress>(`/api/v1/assignments/${id(row.id)}/homework?offset=0&limit=1`),
        ),
    )
    const values: Record<string, HomeworkProgress | null> = {}
    details.forEach((result, index) => {
      const row = rows[index]
      if (row) values[row.id] = result.status === 'fulfilled' ? result.value : null
    })
    progress.value = values
    states.value.assignments = 'ready'
  } catch {
    states.value.assignments = 'unavailable'
  }
}
async function loadTasks() {
  states.value.tasks = 'loading'
  try {
    const data = await marking.mine()
    const pending = data.items.filter((task) => ['assigned', 'in_progress'].includes(task.status))
    tasks.value = pending.slice(0, 4)
    taskPageCount.value = pending.length
    taskHasNext.value = data.has_next
    states.value.tasks = 'ready'
  } catch {
    states.value.tasks = 'unavailable'
  }
}
async function loadSessions() {
  states.value.sessions = 'loading'
  try {
    const data = await configuration.sessions()
    sessions.value = [...data.items]
      .sort((a, b) => b.starts_at.localeCompare(a.starts_at))
      .slice(0, 4)
    states.value.sessions = 'ready'
  } catch {
    states.value.sessions = 'unavailable'
  }
}
async function loadExceptions() {
  states.value.exceptions = 'loading'
  try {
    const data = await recovery.sessions()
    const selected = data.items.slice(0, 3)
    hasExceptionSessions.value = selected.length > 0
    const results = await Promise.all(
      selected.map(async (item) => ({
        ...item,
        total: (await recovery.inspect(item.id, 0, '', true)).total,
      })),
    )
    exceptions.value = results.filter((item) => item.total > 0)
    states.value.exceptions = 'ready'
  } catch (error) {
    // Authorization or transient failures are not equivalent to an empty exception list.
    if (error instanceof ApiError && error.status === 401) exceptions.value = []
    states.value.exceptions = 'unavailable'
  }
}
function refresh() {
  void Promise.allSettled([loadAssignments(), loadTasks(), loadSessions(), loadExceptions()])
}
onMounted(refresh)
</script>
<template>
  <div class="dashboard">
    <section class="dashboard-welcome">
      <div>
        <p class="dashboard-kicker">教学工作台</p>
        <h1>{{ greeting }}，{{ identity.name || '老师' }}</h1>
        <p>从这里继续今天的教学与阅卷工作。</p>
      </div>
      <RouterLink to="/assignments" class="dashboard-primary"
        >查看我的作业 <ArrowRight aria-hidden="true"
      /></RouterLink>
    </section>
    <div class="dashboard-layout">
      <div class="dashboard-main">
        <section class="dashboard-section" aria-labelledby="pending-title">
          <div class="section-heading">
            <div>
              <p class="dashboard-kicker">待处理</p>
              <h2 id="pending-title">
                我的阅卷任务
                <span v-if="states.tasks === 'ready'" class="count-label"
                  >本页 {{ taskPageCount }} 项</span
                >
              </h2>
            </div>
            <RouterLink v-if="nav.myMarking" to="/marking/my" class="more-link"
              >查看全部 <ArrowRight aria-hidden="true"
            /></RouterLink>
          </div>
          <p v-if="states.tasks === 'loading'" class="dashboard-empty" role="status">
            正在读取任务…
          </p>
          <p v-else-if="states.tasks === 'unavailable'" class="dashboard-empty">
            暂时无法读取任务，请稍后重试。
            <button class="text-button" @click="loadTasks">重新读取</button>
          </p>
          <p v-else-if="!tasks.length" class="dashboard-empty">
            {{
              taskHasNext ? '本页没有待处理任务，可查看其他页。' : '目前没有待领取或批改中的任务。'
            }}
          </p>
          <div v-else class="dashboard-list">
            <RouterLink
              v-for="task in tasks"
              :key="task.task_id"
              :to="`/marking/my/${task.task_id}`"
              class="dashboard-row"
            >
              <span class="row-icon blue"><Reading aria-hidden="true" /></span
              ><span class="row-content"
                ><strong>第 {{ task.question_number }} 题 · {{ task.student_name }}</strong
                ><small>{{ task.class_name }} · {{ task.student_code }}</small></span
              ><span class="row-status">{{ taskStatus[task.status] }}</span
              ><ArrowRight class="row-arrow" aria-hidden="true" />
            </RouterLink>
          </div>
        </section>
        <section class="dashboard-section" aria-labelledby="assignment-title">
          <div class="section-heading">
            <div>
              <p class="dashboard-kicker">教学作业</p>
              <h2 id="assignment-title">最近作业</h2>
            </div>
            <RouterLink to="/assignments" class="more-link"
              >全部作业 <ArrowRight aria-hidden="true"
            /></RouterLink>
          </div>
          <p v-if="states.assignments === 'loading'" class="dashboard-empty" role="status">
            正在读取作业…
          </p>
          <p v-else-if="states.assignments === 'unavailable'" class="dashboard-empty">
            暂时无法读取作业。
            <button class="text-button" @click="loadAssignments">重新读取</button>
          </p>
          <p v-else-if="!assignments.length" class="dashboard-empty">
            还没有可查看的作业，创建作业后可在这里跟进。
          </p>
          <div v-else class="dashboard-list">
            <RouterLink
              v-for="row in assignments"
              :key="row.id"
              :to="`/assignments/${row.id}/homework`"
              class="dashboard-row"
            >
              <span class="row-icon mint"><Collection aria-hidden="true" /></span
              ><span class="row-content"
                ><strong>{{ row.title }}</strong
                ><small
                  >{{ row.subject }} · {{ row.grade_level }} · {{ progressText(row) }}</small
                ></span
              ><span class="row-status">{{ status(row.status) }}</span
              ><ArrowRight class="row-arrow" aria-hidden="true" />
            </RouterLink>
          </div>
        </section>
      </div>
      <div class="dashboard-side">
        <section class="dashboard-section quick-section">
          <p class="dashboard-kicker">快捷入口</p>
          <h2>常用工作</h2>
          <div class="quick-links">
            <RouterLink to="/assignments"><Collection />我的作业</RouterLink
            ><RouterLink v-if="nav.papers" to="/papers"><Document />试卷管理</RouterLink
            ><RouterLink v-if="nav.exams" to="/exams"><Notebook />考试配置</RouterLink
            ><RouterLink v-if="nav.myMarking" to="/marking/my"><Reading />我的阅卷</RouterLink>
          </div>
        </section>
        <section class="dashboard-section">
          <div class="section-heading">
            <div>
              <p class="dashboard-kicker">考试阅卷</p>
              <h2>考试场次</h2>
            </div>
            <RouterLink v-if="nav.exams" to="/exams" class="more-link"
              >查看 <ArrowRight
            /></RouterLink>
          </div>
          <p v-if="states.sessions === 'loading'" class="dashboard-empty">正在读取场次…</p>
          <p v-else-if="states.sessions === 'unavailable'" class="dashboard-empty">
            当前无法读取场次。 <button class="text-button" @click="loadSessions">重试</button>
          </p>
          <p v-else-if="!sessions.length" class="dashboard-empty">暂无可查看的考试场次。</p>
          <div v-else class="dashboard-list compact">
            <RouterLink
              v-for="session in sessions"
              :key="session.id"
              :to="`/exams/sessions/${session.id}`"
              class="dashboard-row"
              ><span class="row-content"
                ><strong>{{ session.title }}</strong
                ><small>{{ session.event_title }} · {{ date(session.starts_at) }}</small></span
              ><span class="row-status">{{
                lifecycle[session.lifecycle] || session.lifecycle
              }}</span></RouterLink
            >
          </div>
        </section>
        <section class="dashboard-section">
          <div class="section-heading">
            <div>
              <p class="dashboard-kicker">需要关注</p>
              <h2>异常待办</h2>
            </div>
            <RouterLink v-if="nav.exceptions" to="/exceptions" class="more-link"
              >处理 <ArrowRight
            /></RouterLink>
          </div>
          <p v-if="states.exceptions === 'loading'" class="dashboard-empty">正在检查异常…</p>
          <p v-else-if="states.exceptions === 'unavailable'" class="dashboard-empty">
            当前无法读取异常。 <button class="text-button" @click="loadExceptions">重试</button>
          </p>
          <p v-else-if="!exceptions.length" class="dashboard-empty">
            {{ hasExceptionSessions ? '已查看的场次暂无待处理异常。' : '暂无可查看的考试场次。' }}
          </p>
          <div v-else class="dashboard-list compact">
            <RouterLink
              v-for="item in exceptions"
              :key="item.id"
              :to="`/exceptions?session=${item.id}`"
              class="dashboard-row"
              ><span class="row-icon amber"><Warning /></span
              ><span class="row-content"
                ><strong>{{ item.title }}</strong
                ><small>{{ item.event_title }}</small></span
              ><span class="row-status warning-text">{{ item.total }} 项</span></RouterLink
            >
          </div>
          <p class="dashboard-note">显示可查看场次中的前 3 场</p>
        </section>
      </div>
    </div>
  </div>
</template>
