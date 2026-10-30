<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { marking, taskStatus, type MarkTask } from '../api/marking'
import { ApiError } from '../api/client'
import ExamGrading from '../components/ExamGrading.vue'
const route = useRoute(),
  router = useRouter(),
  tasks = ref<MarkTask[]>([]),
  current = ref<MarkTask>(),
  offset = ref(0),
  more = ref(false),
  busy = ref(false),
  error = ref(''),
  notice = ref('')
const personalCounts = ref<{ status: string; count: number }[]>([])
const pending = ref<{ path: string; body: string }>(),
  locked = computed(() => busy.value || !!pending.value)
async function load() {
  const page = await marking.mine(offset.value)
  tasks.value = page.items
  personalCounts.value = page.counts
  more.value = page.has_next
  if (route.params.tid) current.value = await marking.task(String(route.params.tid))
}
async function run(work: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await work()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '操作未完成'
  } finally {
    busy.value = false
  }
}
async function send() {
  if (!pending.value) return
  try {
    await marking.write(pending.value.path, pending.value.body)
    pending.value = undefined
    await load()
    notice.value =
      current.value?.status === 'completed'
        ? '任务已提交，可进入下一题。'
        : '任务已领取，请核对学生作答。'
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
    throw e
  }
}
async function act(t: MarkTask, kind: 'claim' | 'complete') {
  pending.value = {
    path: `/marking-tasks/${t.task_id}/${kind}`,
    body: JSON.stringify({
      request_id: crypto.randomUUID(),
      expected_revision: t.revision,
      reason: '教师' + (kind === 'claim' ? '领取单评任务' : '提交当前人工复核'),
    }),
  }
  await send()
}
async function next() {
  const t = tasks.value.find(
    (t) => ['assigned', 'in_progress'].includes(t.status) && t.task_id !== current.value?.task_id,
  )
  if (t) await router.push(`/marking/my/${t.task_id}`)
  else notice.value = '本页暂无其他待批任务，请查看下一页。'
}
onMounted(() => run(load))
</script>
<template>
  <header>
    <p class="eyebrow">协同阅卷 · 我的工作</p>
    <h1>我的阅卷任务</h1>
    <p class="muted">领取后核对作答、查看AI建议并保存人工复核，最后提交任务。缺评不会记为零分。</p>
  </header>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="busy" role="status">正在处理…</p>
  <aside v-if="pending" role="alert">
    保存结果尚未核实。<button :disabled="busy" @click="run(send)">重试原请求</button>
  </aside>
  <div class="task-toolbar">
    <RouterLink to="/marking/my">全部个人任务 →</RouterLink
    ><button :disabled="locked" @click="run(load)">刷新个人任务</button
    ><span
      >共 {{ personalCounts.reduce((n, c) => n + c.count, 0) }} 项 · 已完成
      {{
        personalCounts.filter((c) => c.status === 'completed').reduce((n, c) => n + c.count, 0)
      }}
      项</span
    >
  </div>
  <template v-if="current"
    ><section aria-label="当前阅卷任务" class="current-task">
      <h2>
        {{ current.student_name }} · {{ current.student_code }}
        <small>{{ current.class_name }} · 第{{ current.question_number }}题</small>
      </h2>
      <p>{{ taskStatus[current.status] }} · 第{{ current.generation }}次分配</p>
      <button
        v-if="current.status === 'assigned'"
        class="primary"
        :disabled="locked"
        @click="run(() => act(current!, 'claim'))"
      >
        领取并开始</button
      ><button
        v-if="current.status === 'in_progress'"
        class="primary"
        :disabled="locked"
        @click="run(() => act(current!, 'complete'))"
      >
        提交当前任务</button
      ><button :disabled="locked" @click="run(next)">下一道待批题目</button>
      <p v-if="current.status === 'invalidated'" role="alert">
        答卷来源或授权已变化，请联系负责人核对，不会自动记零分。
      </p>
    </section>
    <ExamGrading
      v-if="['in_progress', 'completed'].includes(current.status)"
      :key="current.task_id + current.status"
      :job-id="current.job_id"
      :workspace-id="current.workspace_id"
      :task-id="current.task_id"
      :read-only="current.status === 'completed'"
      :student-label="
        current.student_name + ' · ' + current.student_code + ' · ' + current.class_name
      "
    />
  </template>
  <section v-else aria-label="个人任务列表" class="marking-task-list">
    <p v-if="!tasks.length && !busy">当前没有已授权的阅卷任务。</p>
    <div v-else class="table-scroll"><table>
      <thead>
        <tr>
          <th>学生</th>
          <th>班级</th>
          <th>题目</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="t in tasks" :key="t.task_id">
          <td>{{ t.student_name }} · {{ t.student_code }}</td>
          <td>{{ t.class_name }}</td>
          <td>第{{ t.question_number }}题</td>
          <td>{{ taskStatus[t.status] }}</td>
          <td><RouterLink :to="`/marking/my/${t.task_id}`">进入任务 →</RouterLink></td>
        </tr>
      </tbody>
    </table></div>
  </section>
  <div class="task-toolbar">
    <button
      :disabled="locked || offset === 0"
      @click="
        run(async () => {
          offset -= 25
          await load()
        })
      "
    >
      上一页任务</button
    ><button
      :disabled="locked || !more"
      @click="
        run(async () => {
          offset += 25
          await load()
        })
      "
    >
      下一页任务
    </button>
  </div>
</template>
<style scoped>
.task-toolbar {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  align-items: center;
  margin: 24px 0;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: white;
}
td,
th {
  padding: 16px;
  text-align: left;
  border-bottom: 1px solid #dbe4e9;
}
button {
  padding: 10px 14px;
  border: 1px solid #ccdae2;
  background: white;
  color: #255264;
  border-radius: 6px;
  margin: 4px;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.current-task {
  padding: 20px;
  background: #eaf3f5;
  border-left: 3px solid #287c86;
  margin-bottom: 24px;
}
.current-task h2 {
  margin: 0;
}
small {
  font-weight: normal;
  color: #647989;
}
</style>
