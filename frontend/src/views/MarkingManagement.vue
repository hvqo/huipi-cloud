<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import ExceptionBadge from '../components/ExceptionBadge.vue'
import { recovery } from '../api/recovery'
import {
  marking,
  taskStatus,
  type PoolScript,
  type MarkTask,
  type PoolQuestion,
} from '../api/marking'
import { ApiError } from '../api/client'
const props = defineProps<{ progressOnly?: boolean }>()
const sessions = ref<Awaited<ReturnType<typeof marking.sessions>>>(),
  sid = ref(String(useRoute().query.session || '')),
  sessionOffset = ref(0),
  offset = ref(0),
  more = ref(false)
const scripts = ref<PoolScript[]>([]),
  rows = ref<MarkTask[]>([]),
  teachers = ref<{ user_id: string; label: string }[]>([]),
  counts = ref<{ assignee_user_id: string; status: string; count: number }[]>([])
const selected = ref<string[]>([]),
  teacher = ref(''),
  reason = ref('日常单评任务分配'),
  force = ref(false),
  busy = ref(false),
  error = ref(''),
  notice = ref('')
const pending = ref<{ path: string; body: string }>()
const locked = computed(() => busy.value || !!pending.value)
async function run(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await fn()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '操作未完成'
  } finally {
    busy.value = false
  }
}
async function loadSessions() {
  sessions.value = await marking.sessions(sessionOffset.value)
  sid.value ||= sessions.value.items[0]?.id || ''
  if (sid.value && !sessions.value.items.some((item) => item.id === sid.value)) {
    const current = await recovery.inspect(sid.value, 0, '', true)
    sessions.value.items.push({
      id: sid.value,
      title: current.session_title,
      event_title: current.event_title,
    })
  }
  await load()
}
async function load() {
  selected.value = []
  if (!sid.value) return
  if (props.progressOnly) {
    const p = await marking.progress(sid.value, offset.value)
    rows.value = p.items
    counts.value = p.counts
    more.value = p.has_next
  } else {
    const [p, t] = await Promise.all([
      marking.pool(sid.value, offset.value),
      marking.teachers(sid.value),
    ])
    scripts.value = p.items
    teachers.value = t.items
    more.value = p.has_next
  }
}
async function changeSession() {
  offset.value = 0
  scripts.value = []
  rows.value = []
  counts.value = []
  await load()
}
async function send() {
  if (!pending.value) return
  const p = pending.value
  try {
    await marking.write(p.path, p.body)
    pending.value = undefined
    await load()
    notice.value = '操作已保存。'
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
    throw e
  }
}
async function mutate(path: string, body: Record<string, unknown>) {
  pending.value = { path, body: JSON.stringify({ request_id: crypto.randomUUID(), ...body }) }
  await send()
}
function fullPaper(s: PoolScript) {
  for (const q of s.questions)
    if (q.workspace_id && !selected.value.includes(q.workspace_id))
      selected.value.push(q.workspace_id)
}
const available = computed(
  () =>
    new Map(
      scripts.value.flatMap((s) =>
        s.questions
          .filter((q) => q.workspace_id)
          .map((q) => [q.workspace_id!, q] as [string, PoolQuestion]),
      ),
    ),
)
async function assign() {
  if (!teacher.value || !selected.value.length) throw new Error('请选择教师和待批题目')
  const targets = selected.value.map((wid) => {
    const q = available.value.get(wid)!
    return {
      workspace_id: wid,
      expected_task_id: q.task?.id || null,
      expected_revision: q.task?.revision || 0,
    }
  })
  await mutate(`/exam-sessions/${sid.value}/marking-assignments`, {
    assignee_user_id: teacher.value,
    targets,
    reason: reason.value,
    force: force.value,
  })
}
function sum(uid: string, state?: string) {
  return counts.value
    .filter((c) => c.assignee_user_id === uid && (!state || c.status === state))
    .reduce((a, c) => a + c.count, 0)
}
const people = computed(() => [...new Set(counts.value.map((c) => c.assignee_user_id))])
onMounted(() => run(loadSessions))
</script>
<template>
  <header class="mark-heading">
    <div>
      <p class="eyebrow">协同阅卷 · 单评</p>
      <h1>{{ progressOnly ? '阅卷进度' : '阅卷管理' }}</h1>
      <p class="muted">按整卷、题目或题组安排教师。提交复核任务后，结果进入内部成绩预览。</p>
    </div>
    <RouterLink :to="progressOnly ? '/marking/manage' : '/marking/progress'">{{
      progressOnly ? '返回任务分配 →' : '查看阅卷进度 →'
    }}</RouterLink>
  </header>
  <ExceptionBadge :session-id="sid" />
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <aside v-if="pending" role="alert">
    保存结果尚未核实，请勿重复操作。<button :disabled="busy" @click="run(send)">重试原请求</button>
  </aside>
  <p v-if="busy" role="status">正在读取或保存…</p>
  <div class="mark-toolbar">
    <label
      >考试与科目场次<select v-model="sid" :disabled="locked" @change="run(changeSession)">
        <option value="">选择场次</option>
        <option v-for="s in sessions?.items" :key="s.id" :value="s.id">
          {{ s.event_title }} · {{ s.title }}
        </option>
      </select></label
    ><button :disabled="locked" @click="run(load)">刷新任务</button
    ><RouterLink to="/marking/my">我的阅卷任务 →</RouterLink>
  </div>
  <p v-if="sessions && !sessions.items.length">暂无负责人授权的考试场次，请联系考务人员。</p>
  <div v-if="sessions" class="mark-toolbar">
    <button
      :disabled="locked || sessionOffset === 0"
      @click="
        run(async () => {
          sessionOffset -= 20
          sid = ''
          await loadSessions()
        })
      "
    >
      上一页场次</button
    ><button
      :disabled="locked || !sessions.has_next"
      @click="
        run(async () => {
          sessionOffset += 20
          sid = ''
          await loadSessions()
        })
      "
    >
      下一页场次
    </button>
  </div>
  <template v-if="sid && !progressOnly">
    <section aria-label="任务分配" class="mark-allocation">
      <h2>分配单评任务</h2>
      <p class="muted">
        选择教师与题目，即可组成阅卷任务。账号未登记姓名时显示账号编号。
      </p>
      <div class="mark-toolbar">
        <label
          >阅卷教师<select v-model="teacher" :disabled="locked">
            <option value="">选择已授权账号</option>
            <option v-for="t in teachers" :key="t.user_id" :value="t.user_id">{{ t.label }}</option>
          </select></label
        ><label>分配说明<input v-model="reason" maxlength="256" :disabled="locked" /></label
        ><label
          ><input
            type="checkbox"
            v-model="force"
            :disabled="locked"
          />确认重派已开始或已完成任务</label
        ><el-button
          type="primary"
          :disabled="locked || !teacher || !selected.length"
          @click="run(assign)"
          >分配选中题目（{{ selected.length }}）</el-button
        >
      </div>
      <p v-if="!scripts.length">当前没有已解析的可分配答卷。请先完成分卷、身份确认和解析。</p>
      <article v-for="s in scripts" :key="s.job_id" class="mark-script">
        <header>
          <h3>
            {{ s.student_name || '来源待核对' }}
            <small
              >{{ s.student_code }} · {{ s.class_name }} ·
              {{ s.variant ? `${s.variant}卷` : '' }}</small
            >
          </h3>
          <span v-if="s.status !== 'ready'">来源已变更，请重新解析</span
          ><button v-else :disabled="locked" @click="fullPaper(s)">选择整卷</button
          ><button
            v-if="s.status === 'ready' && s.questions.some((q) => !q.workspace_id)"
            :disabled="locked"
            @click="run(() => mutate(`/exam-sessions/${sid}/marking-jobs/${s.job_id}/prepare`, {}))"
          >
            准备待批题目
          </button>
        </header>
        <div class="mark-question-list">
          <label v-for="q in s.questions" :key="q.question_id"
            ><input
              type="checkbox"
              v-model="selected"
              :value="q.workspace_id"
              :disabled="locked || !q.workspace_id"
            />第{{ q.number }}题 <span>{{ q.task ? taskStatus[q.task.status] : '尚未分配' }}</span
            ><small v-if="q.task"
              >账号 {{ q.task.assignee_user_id.slice(0, 8) }} · 第{{ q.task.revision }}版</small
            ></label
          >
        </div>
      </article>
    </section>
  </template>
  <template v-if="sid && progressOnly"
    ><section aria-label="教师进度">
      <h2>教师任务进度</h2>
      <p class="muted">已完成表示任务已提交；来源或授权变更后的有效分数仍以成绩预览核验为准。</p>
      <table>
        <thead>
          <tr>
            <th>教师账号</th>
            <th>已分配</th>
            <th>待领取</th>
            <th>批改中</th>
            <th>已完成</th>
            <th>已撤回</th>
            <th>来源或授权异常</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="uid in people" :key="uid">
            <td :title="uid">{{ uid.slice(0, 8) }}</td>
            <td>{{ sum(uid) }}</td>
            <td>{{ sum(uid, 'assigned') }}</td>
            <td>{{ sum(uid, 'in_progress') }}</td>
            <td>{{ sum(uid, 'completed') }}</td>
            <td>{{ sum(uid, 'cancelled') }}</td>
            <td>{{ sum(uid, 'invalidated') }}</td>
          </tr>
        </tbody>
      </table>
    </section>
    <section aria-label="任务明细">
      <h2>当前任务明细</h2>
      <table>
        <thead>
          <tr>
            <th>学生与班级</th>
            <th>题目</th>
            <th>教师账号</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in rows" :key="t.task_id">
            <td>
              {{ t.student_name }} · {{ t.student_code }}<small>{{ t.class_name }}</small>
            </td>
            <td>第{{ t.question_number }}题</td>
            <td>{{ t.assignee_user_id.slice(0, 8) }}</td>
            <td>{{ taskStatus[t.status] }}</td>
            <td>
              <button
                :disabled="
                  locked || t.stored_status === 'completed' || t.stored_status === 'cancelled'
                "
                @click="
                  run(() =>
                    mutate(`/marking-tasks/${t.task_id}/cancel`, {
                      expected_revision: t.revision,
                      reason: '负责人撤回异常或未开始任务',
                    }),
                  )
                "
              >
                撤回任务
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <RouterLink :to="`/scans/sessions/${sid}/results`"
        >整卷成绩预览（需独立班级授权） →</RouterLink
      >
    </section></template
  >
  <div v-if="sid" class="mark-toolbar">
    <button
      :disabled="locked || offset === 0"
      @click="
        run(async () => {
          offset -= progressOnly ? 25 : 5
          await load()
        })
      "
    >
      上一页{{ progressOnly ? '任务' : '答卷' }}</button
    ><button
      :disabled="locked || !more"
      @click="
        run(async () => {
          offset += progressOnly ? 25 : 5
          await load()
        })
      "
    >
      下一页{{ progressOnly ? '任务' : '答卷' }}
    </button>
  </div>
</template>
<style scoped>
.mark-heading,
.mark-toolbar,
.mark-script header {
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
}
.mark-toolbar {
  justify-content: flex-start;
  margin: 20px 0;
}
.mark-toolbar label {
  display: grid;
  gap: 8px;
}
select,
input:not([type='checkbox']) {
  padding: 10px;
  border: 1px solid #cedae2;
  border-radius: 6px;
  background: white;
  font: inherit;
}
button {
  padding: 8px 12px;
  border: 1px solid #cedae2;
  border-radius: 6px;
  background: white;
  color: #255264;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
section {
  margin-top: 24px;
}
.mark-script {
  padding: 20px 0;
  border-bottom: 1px solid #dbe4e9;
}
.mark-script h3 {
  margin: 0;
}
.mark-script small {
  font-weight: normal;
  color: #647989;
}
.mark-question-list {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 18px;
}
.mark-question-list label {
  padding: 12px;
  background: white;
  border-radius: 6px;
}
.mark-question-list small {
  display: block;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: white;
  margin: 16px 0;
}
th,
td {
  padding: 14px;
  text-align: left;
  border-bottom: 1px solid #e0e8ec;
}
td small {
  display: block;
  color: #647989;
}
</style>
