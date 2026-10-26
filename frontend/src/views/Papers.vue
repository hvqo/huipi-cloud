<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { papers as api } from '../api/papers'
import { ApiError, type Schema } from '../api/client'
import PageState from '../components/PageState.vue'

type Detail = Schema['PaperWebDetail']
type Paper = Schema['PaperWebSummary']
const route = useRoute(),
  router = useRouter()
const items = ref<Paper[]>([]),
  history = ref<Paper[]>([]),
  detail = ref<Detail>()
const sourceItems = ref<Schema['PaperWebSource'][]>([])
const offset = ref(0),
  sourceOffset = ref(0),
  historyOffset = ref(0)
const more = ref(false),
  moreSources = ref(false),
  moreHistory = ref(false),
  canCreate = ref(false)
const busy = ref(false),
  error = ref(''),
  notice = ref(''),
  needsAuthorization = ref(false),
  dialog = ref(false)
const importForm = ref({ source: '', title: '' })
const pending = ref<{ path: string; body: unknown }>()
const index = ref(0),
  question = ref<Detail['questions'][number]>()
const order = ref<{ question_key: string; printed_label: string }[]>([])
const savedQuestion = ref(''),
  savedOrder = ref('')
const paper = computed(() => detail.value?.paper)
const locked = computed(() => busy.value || !!pending.value)
const dirtyQuestion = computed(
  () => !!question.value && JSON.stringify(question.value) !== savedQuestion.value,
)
const dirtyOrder = computed(() => JSON.stringify(order.value) !== savedOrder.value)
const dirty = computed(() => dirtyQuestion.value || dirtyOrder.value)
const canEdit = computed(
  () => paper.value?.status === 'draft' && paper.value.actions.includes('paper.edit'),
)
const checks = {
  questions_complete: '题目完整',
  answers_complete: '标准答案完整',
  rubric_complete: '评分项与满分一致',
  mapping_complete: '题目映射完整',
  content_current: '内容版本正常',
} as const
const names: Record<string, string> = { draft: '草稿', locked: '已锁定', retired: '已退役' }
const typeNames = {
  single_choice: '单选题',
  multiple_choice: '多选题',
  fill_blank: '填空题',
  short_answer: '简答题',
  essay: '论述题',
}
function cents(s: string | number) {
  const value = String(s)
  if (!/^\d+(\.\d{1,2})?$/.test(value)) return null
  const [a, b = ''] = value.split('.')
  return BigInt(a!) * 100n + BigInt(b.padEnd(2, '0'))
}
const pointsValid = computed(() => {
  if (!question.value) return false
  const expected = cents(question.value.question.max_score)
  const values = question.value.rubric.criteria.map((c) => cents(c.points))
  return (
    expected !== null &&
    expected > 0n &&
    values.every((v) => v !== null && v > 0n) &&
    values.reduce<bigint>((a, v) => a + (v || 0n), 0n) === expected
  )
})
function discard() {
  return !dirty.value || window.confirm('有未保存修改，确定放弃并读取其他内容吗？')
}
onBeforeRouteLeave(() => !pending.value && (busy.value || discard()))
onBeforeRouteUpdate(() => !pending.value && (busy.value || discard()))
function leaving(event: BeforeUnloadEvent) {
  if (dirty.value || pending.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
window.addEventListener('beforeunload', leaving)
onUnmounted(() => window.removeEventListener('beforeunload', leaving))
function questionChanged(event: Event) {
  const target = event.target as HTMLSelectElement
  const next = Number(target.value)
  target.value = String(index.value)
  choose(next)
}
async function run(work: () => Promise<unknown>) {
  if (locked.value) return
  busy.value = true
  error.value = ''
  try {
    await work()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
async function loadList() {
  const result = await api.list(offset.value)
  items.value = result.items
  more.value = result.has_more
  canCreate.value = result.can_create
}
async function loadSources() {
  const result = await api.sources(sourceOffset.value)
  sourceItems.value = result.items
  moreSources.value = result.has_more
}
function removeCriterion(i: number) {
  question.value!.rubric.criteria.splice(i, 1)
  question.value!.rubric.criteria.forEach((c, n) => (c.sort_order = n + 1))
}
function choose(i: number) {
  if (!discard()) return
  index.value = i
  question.value = JSON.parse(JSON.stringify(detail.value!.questions[i]))
  savedQuestion.value = JSON.stringify(question.value)
}
async function read(pid: string) {
  detail.value = undefined
  history.value = []
  question.value = undefined
  order.value = []
  savedOrder.value = ''
  savedQuestion.value = ''
  needsAuthorization.value = false
  const result = await api.detail(pid)
  detail.value = result
  index.value = 0
  question.value = JSON.parse(JSON.stringify(result.questions[0]))
  savedQuestion.value = JSON.stringify(question.value)
  order.value = result.questions.map((q) => ({
    question_key: q.question_key,
    printed_label: q.printed_label,
  }))
  savedOrder.value = JSON.stringify(order.value)
  const versions = await api.history(pid, historyOffset.value)
  history.value = versions.items
  moreHistory.value = versions.has_more
}
async function select(pid: string) {
  if (!discard()) return
  historyOffset.value = 0
  await router.push('/papers/' + pid)
  await read(pid)
}
async function pageHistory(step: number) {
  historyOffset.value += step
  const result = await api.history(paper.value!.id, historyOffset.value)
  history.value = result.items
  moreHistory.value = result.has_more
}
async function refresh() {
  if (!discard()) return
  await loadList()
  if (route.params.pid) await read(String(route.params.pid))
}
function version() {
  return {
    request_id: crypto.randomUUID(),
    expected_revision: paper.value!.revision,
    expected_content_sha256: paper.value!.expected_content_sha256,
    expected_mapping_digest: paper.value!.expected_mapping_digest,
  }
}
async function send() {
  const intent = pending.value
  if (!intent) return
  const result = await api.write(intent.path, intent.body)
  pending.value = undefined
  dialog.value = false
  notice.value = '操作已保存。'
  needsAuthorization.value = false
  await loadList()
  await router.push('/papers/' + result.paper_version_id)
  try {
    await read(result.paper_version_id)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      needsAuthorization.value = true
      detail.value = undefined
      question.value = undefined
    } else throw e
  }
}
async function mutate(path: string, body: unknown) {
  pending.value = { path, body: JSON.parse(JSON.stringify(body)) }
  try {
    await send()
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
    if (!(e instanceof ApiError && e.uncertain)) pending.value = undefined
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
async function create() {
  if (!importForm.value.source || !importForm.value.title.trim())
    throw new Error('请选择已发布来源并填写试卷名称。')
  await mutate('', {
    request_id: crypto.randomUUID(),
    source_assignment_id: importForm.value.source,
    title: importForm.value.title,
  })
}
async function saveQuestion() {
  if (
    !pointsValid.value ||
    !question.value?.question.stem.trim() ||
    !question.value.answer_content.trim()
  )
    throw new Error('请补全题目、标准答案，并核对每项分值与题目满分。')
  const q = question.value
  await mutate('/' + paper.value!.id + '/questions', {
    ...version(),
    question_key: q.question_key,
    question: q.question,
    answer_content: q.answer_content,
    rubric: q.rubric,
  })
}
function move(i: number, step: number) {
  const target = i + step
  if (target < 0 || target >= order.value.length) return
  const [item] = order.value.splice(i, 1)
  order.value.splice(target, 0, item!)
}
async function state(action: 'lock' | 'fork' | 'retire') {
  if (dirty.value) throw new Error('请先保存或放弃修改。')
  if (
    action === 'lock' &&
    !window.confirm('锁定后不可原地修改；后续修改需要创建新版本。确认锁定吗？')
  )
    return
  if (
    action === 'retire' &&
    !window.confirm('退役会阻止新的考试使用，已有场次可能需要重新核对。历史记录保留。确认退役吗？')
  )
    return
  await mutate('/' + paper.value!.id + '/' + action, version())
}
onMounted(() =>
  run(async () => {
    await loadList()
    if (route.params.pid) await read(String(route.params.pid))
  }),
)
watch(
  () => route.params.pid,
  (value, old) => {
    if (value !== old && !busy.value && value) void run(() => read(String(value)))
  },
)
</script>

<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">教学内容 · 共享版本</p>
      <h1>试卷管理</h1>
      <p class="muted">整理题目、标准答案与评分标准，锁定后供考试共用。</p>
    </div>
    <div class="paper-actions">
      <RouterLink to="/exams">考试配置 →</RouterLink
      ><el-button
        type="primary"
        :disabled="locked || !canCreate"
        @click="
          run(async () => {
            await loadSources()
            dialog = true
          })
        "
        >从已发布作业创建</el-button
      >
    </div>
  </div>
  <p v-if="error && !dialog" role="alert" class="error">
    {{ error }} <button :disabled="locked" @click="run(refresh)">刷新当前版本</button>
  </p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="needsAuthorization" role="status" class="paper-warning">
    新版本已创建，需要授权后才能查看和编辑。请联系考务负责人，不要重复创建。
  </p>
  <p v-if="pending && !dialog" class="paper-warning" role="alert">
    保存结果尚未核实，编辑已锁定。<button :disabled="busy" @click="retry">重试原请求</button>
  </p>
  <div class="paper-workspace">
    <aside class="paper-list" aria-label="已授权试卷">
      <h2>我的试卷</h2>
      <p class="muted">仅列出当前有查看权限的版本。</p>
      <PageState v-if="!items.length && !busy" empty />
      <button
        v-for="item in items"
        :key="item.id"
        class="paper-item"
        :class="{ 'paper-selected': paper?.id === item.id }"
        :disabled="locked"
        @click="run(() => select(item.id))"
      >
        <strong>{{ item.title }}</strong
        ><span>第{{ item.version_no }}版 · {{ names[item.status] }}</span
        ><small>{{ item.question_count }}题 · {{ item.max_points }}分</small>
      </button>
      <div class="paper-actions">
        <button
          v-if="offset"
          :disabled="locked"
          @click="
            run(async () => {
              offset -= 20
              await loadList()
            })
          "
        >
          上一页</button
        ><button
          v-if="more"
          :disabled="locked"
          @click="
            run(async () => {
              offset += 20
              await loadList()
            })
          "
        >
          下一页
        </button>
      </div>
    </aside>
    <main v-if="paper && detail" class="paper-editor" aria-label="试卷编辑工作区">
      <header class="paper-header">
        <div>
          <p class="eyebrow">共享试卷 · 第{{ paper.version_no }}版</p>
          <h2>{{ paper.title }}</h2>
          <p>
            {{ names[paper.status] }} · {{ paper.question_count }}题 · 总分{{ paper.max_points }}
          </p>
        </div>
        <button :disabled="locked" @click="run(refresh)">刷新版本</button>
      </header>
      <p v-if="paper.status !== 'draft'" class="paper-warning">
        本版本只读保留。需要修改时请创建后继草稿；不能解锁或覆盖原版。
      </p>
      <p v-if="dirty" role="status" class="paper-warning">
        有未保存修改，锁定与版本操作暂不可用。保存题目和保存题序是两项独立操作。
      </p>
      <div class="paper-checks">
        <span v-for="(label, key) in checks" :key="key" :class="{ error: !paper.checks[key] }"
          >{{ paper.checks[key] ? '✓' : '!' }} {{ label }}</span
        >
      </div>
      <div class="paper-actions">
        <el-button
          v-if="paper.status === 'draft' && paper.actions.includes('paper.lock')"
          type="primary"
          :disabled="locked || dirty || !Object.values(paper.checks).every(Boolean)"
          @click="run(() => state('lock'))"
          >检查并锁定试卷</el-button
        ><el-button
          v-if="paper.status !== 'draft' && paper.actions.includes('paper.edit')"
          :disabled="locked || dirty"
          @click="run(() => state('fork'))"
          >创建后继草稿</el-button
        ><button
          v-if="paper.status !== 'retired' && paper.actions.includes('paper.edit')"
          :disabled="locked || dirty"
          @click="run(() => state('retire'))"
        >
          退役此版本</button
        ><span class="muted">{{
          paper.usable_for_exam
            ? '已锁定，可由另有用卷权限的考务人员选择。'
            : '当前版本不可建立新的考试用卷关联。'
        }}</span>
      </div>
      <section v-if="question" class="paper-section" aria-label="题目与评分标准">
        <h2>题目与评分标准</h2>
        <label
          >当前题目<select
            :value="index"
            :disabled="locked || dirtyOrder"
            @change="questionChanged"
          >
            <option v-for="(q, i) in detail.questions" :key="q.question_key" :value="i">
              {{ q.printed_label }} · {{ q.question.stem.slice(0, 30) }}
            </option>
          </select></label
        >
        <fieldset :disabled="locked || !canEdit || dirtyOrder" class="paper-form">
          <div class="paper-actions">
            <label
              >题型<select v-model="question.question.question_type">
                <option v-for="(label, key) in typeNames" :key="key" :value="key">
                  {{ label }}
                </option>
              </select></label
            ><label
              >题目满分<input
                v-model="question.question.max_score"
                inputmode="decimal"
                maxlength="12"
            /></label>
          </div>
          <label
            >题目正文<textarea v-model="question.question.stem" rows="5" maxlength="20000" /></label
          ><label
            >教师标准答案<textarea v-model="question.answer_content" rows="4" maxlength="50000" />
          </label>
          <h3>评分项</h3>
          <div v-for="(criterion, i) in question.rubric.criteria" :key="i" class="paper-criterion">
            <label
              >评分要求<textarea v-model="criterion.description" rows="2" maxlength="2000" /></label
            ><label
              >分值<input v-model="criterion.points" inputmode="decimal" maxlength="12" /></label
            ><button :disabled="question.rubric.criteria.length === 1" @click="removeCriterion(i)">
              移除
            </button>
          </div>
          <button
            :disabled="question.rubric.criteria.length >= 100"
            @click="
              question.rubric.criteria.push({
                description: '',
                points: '1.00',
                sort_order: question.rubric.criteria.length + 1,
              })
            "
          >
            添加评分项
          </button>
          <p :class="{ error: !pointsValid }" role="status">
            {{
              pointsValid
                ? '分值合计与题目满分一致。'
                : '请检查分值：各项须为正数，合计须与题目满分严格一致。'
            }}
          </p>
          <el-button
            type="primary"
            :disabled="locked || !canEdit || !dirtyQuestion || !pointsValid"
            @click="run(saveQuestion)"
            >保存题目与评分标准</el-button
          >
        </fieldset>
      </section>
      <section class="paper-section" aria-label="题序与显示题号">
        <h2>题序与显示题号</h2>
        <p class="muted">题目键保持稳定。调整展示顺序不会新增、删除或复制试题。</p>
        <fieldset :disabled="locked || !canEdit || dirtyQuestion">
          <div v-for="(q, i) in order" :key="q.question_key" class="paper-order">
            <label>显示题号<input v-model="q.printed_label" maxlength="80" /></label
            ><span>{{
              detail.questions
                .find((x) => x.question_key === q.question_key)
                ?.question.stem.slice(0, 45)
            }}</span
            ><button :disabled="i === 0" @click="move(i, -1)">上移</button
            ><button :disabled="i === order.length - 1" @click="move(i, 1)">下移</button>
          </div>
          <el-button
            :disabled="locked || !canEdit || !dirtyOrder"
            @click="
              run(() => mutate('/' + paper!.id + '/order', { ...version(), mappings: order }))
            "
            >保存题序与题号</el-button
          >
        </fieldset>
      </section>
      <section class="paper-section" aria-label="可访问版本历史">
        <h2>同一试卷家族 · 版本历史</h2>
        <p class="muted">只展示本家族中你有查看权限的版本。未授权后继版本不会显示。</p>
        <button
          v-for="v in history"
          :key="v.id"
          :disabled="locked"
          class="paper-history"
          @click="run(() => select(v.id))"
        >
          第{{ v.version_no }}版 · {{ names[v.status] }} · {{ v.title }}
        </button>
        <div class="paper-actions">
          <button v-if="historyOffset" :disabled="locked" @click="run(() => pageHistory(-20))">
            上一页版本</button
          ><button v-if="moreHistory" :disabled="locked" @click="run(() => pageHistory(20))">
            下一页版本
          </button>
        </div>
      </section>
    </main>
    <p v-else-if="!busy && !needsAuthorization" class="muted">
      选择一份已授权试卷，或从已发布作业创建草稿。
    </p>
  </div>
  <el-dialog
    v-model="dialog"
    title="创建试卷快照"
    width="580px"
    :close-on-click-modal="!pending"
    :close-on-press-escape="!pending"
    :show-close="!pending"
  >
    <p class="muted">
      只可选择明确授权给你的已发布作业。导入后独立保存为受保护试卷，不修改原作业。
    </p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="pending" role="alert">
      保存结果尚未核实。<button :disabled="busy" @click="retry">重试原请求</button>
    </p>
    <fieldset :disabled="locked" class="paper-form">
      <label
        >已发布来源<select v-model="importForm.source">
          <option value="">请选择来源</option>
          <option v-for="s in sourceItems" :key="s.id" :value="s.id">
            {{ s.title }} · {{ s.subject }} · {{ s.grade_level }}
          </option>
        </select></label
      >
      <p v-if="!sourceItems.length" class="muted">
        没有已授权且发布的来源，请先完善作业或联系负责人授权。
      </p>
      <div class="paper-actions">
        <button
          v-if="sourceOffset"
          @click="
            run(async () => {
              sourceOffset -= 20
              await loadSources()
            })
          "
        >
          上一页来源</button
        ><button
          v-if="moreSources"
          @click="
            run(async () => {
              sourceOffset += 20
              await loadSources()
            })
          "
        >
          下一页来源
        </button>
      </div>
      <label>试卷名称<input v-model="importForm.title" maxlength="200" /></label
      ><el-button type="primary" @click="run(create)">创建草稿快照</el-button>
    </fieldset>
  </el-dialog>
</template>

<style scoped>
.paper-workspace {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: 28px;
  align-items: start;
}
.paper-list {
  position: sticky;
  top: 20px;
}
.paper-item {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 18px 16px;
  margin-bottom: 12px;
  border: 1px solid #dce4ed;
  background: white;
  border-radius: 8px;
  color: #283d52;
}
.paper-selected {
  border-color: #2860aa;
  background: #f1f6fd;
}
.paper-item strong {
  font-size: 16px;
}
.paper-item span {
  font-size: 14px;
  color: #53667b;
}
.paper-header,
.paper-actions {
  display: flex;
  align-items: center;
  gap: 14px;
  justify-content: space-between;
  flex-wrap: wrap;
}
.paper-actions {
  justify-content: flex-start;
}
.paper-editor {
  min-width: 0;
  background: white;
  border: 1px solid #e0e6ee;
  border-radius: 10px;
  padding: 28px;
}
.paper-checks {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 20px;
  padding: 18px 0;
  color: #397168;
  font-size: 14px;
}
.paper-section {
  border-top: 1px solid #e0e6ee;
  margin-top: 28px;
  padding-top: 24px;
}
.paper-warning {
  background: #fff8e9;
  color: #755320;
  border-left: 3px solid #cf9d3a;
  padding: 12px 16px;
  font-size: 14px;
}
.paper-form {
  display: grid;
  gap: 18px;
  margin-top: 20px;
}
.paper-form label,
.paper-order label {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.paper-form input,
.paper-form textarea,
.paper-form select,
.paper-order input,
select {
  font: inherit;
  font-size: 15px;
  padding: 10px;
  border: 1px solid #ccd7e4;
  border-radius: 6px;
  max-width: 100%;
  background: #fff;
  color: #223a40;
}
.paper-form textarea {
  width: 100%;
  resize: vertical;
  line-height: 1.65;
}
.paper-criterion {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 100px 60px;
  gap: 12px;
  align-items: center;
}
.paper-order {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.paper-order input {
  width: 90px;
}
.paper-order span {
  flex: 1;
}
.paper-history {
  display: block;
  margin: 10px 0;
}
.paper-actions :deep(.el-button--primary:not(.is-disabled)),
.paper-editor :deep(.el-button--primary:not(.is-disabled)) {
  background: #285fa5;
  border-color: #285fa5;
}
fieldset {
  border: 0;
  margin: 0;
  padding: 0;
  min-width: 0;
}
@media (max-width: 1100px) {
  .paper-workspace {
    grid-template-columns: 1fr;
  }
  .paper-list {
    position: static;
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
  }
  .paper-list h2,
  .paper-list > p {
    width: 100%;
  }
  .paper-item {
    max-width: 300px;
  }
  .paper-editor {
    padding: 20px;
  }
}
@media (max-width: 700px) {
  .paper-criterion {
    grid-template-columns: 1fr;
  }
  .paper-order {
    flex-wrap: wrap;
  }
}
</style>
