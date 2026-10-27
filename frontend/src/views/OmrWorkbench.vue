<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ApiError, type Schema } from '../api/client'
import { omr } from '../api/omr'
import { papers } from '../api/papers'
import { recovery } from '../api/recovery'
import { scripts } from '../api/answer-scripts'

type Sheet = Schema['RecognitionRead']
type Script = Schema['ScriptRead']
type Card = { script: Script; result: Sheet; image: string; checked: boolean }
const route = useRoute()
const paperList = ref<Schema['PaperWebSummary'][]>([]),
  paperOffset = ref(0),
  paperMore = ref(false)
const paperId = ref(''),
  template = ref<Schema['TemplateRead']>(),
  selectedQuestions = ref<string[]>([])
const sessions = ref<{ id: string; title: string; event_title: string }[]>([]),
  sessionId = ref(''),
  sessionOffset = ref(0),
  sessionMore = ref(false)
const list = ref<Script[]>([]),
  scriptOffset = ref(0),
  scriptTotal = ref(0),
  selectedScript = ref<Script>()
const pageId = ref(''),
  current = ref<Schema['CurrentRecognition']>(),
  cards = ref<Card[]>([])
const edits = ref<Record<string, string>>({}),
  busy = ref(false),
  error = ref(''),
  notice = ref('')
const statusNames: Record<string, string> = { blank: '空白', multiple: '多涂', uncertain: '模糊' }
const pendingTemplate = ref<Schema['TemplateCreate']>()
const pendingRecognition = ref<Schema['RecognitionCreate']>()

const describeError = (e: unknown) => {
  if (e instanceof ApiError) {
    const details: Record<string, string> = {
      omr_alignment_markers_missing: '无法定位答题卡，请检查四角标记或重新扫描。',
      omr_alignment_markers_invalid: '页面定位不可靠，请重新扫描或人工处理。',
      omr_alignment_markers_ambiguous: '定位标记无法区分，请重新扫描。',
      omr_template_code_mismatch: '答题卡与该学生的卷版不一致，请核对 A/B 卷。',
      omr_template_code_unclear: '答题卡标识不清晰，请重新扫描。',
      omr_page_blurry: '图片过于模糊，请重新扫描或人工处理。',
      omr_busy: '识别资源繁忙，请稍后重试。',
      omr_recognition_already_exists: '当前答卷已有识别记录，请刷新查看。',
    }
    return details[e.code] || e.message
  }
  return '操作未完成，请检查网络及当前授权。'
}
async function run(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await fn()
  } catch (e) {
    error.value = describeError(e)
  } finally {
    busy.value = false
  }
}
function clearCards() {
  cards.value.forEach((c) => URL.revokeObjectURL(c.image))
  cards.value = []
  edits.value = {}
}
async function loadPapers() {
  const page = await papers.list(paperOffset.value)
  paperList.value = page.items.filter((p) => p.status === 'locked')
  paperMore.value = page.has_more
}
async function loadSessions() {
  const page = await recovery.sessions(sessionOffset.value)
  sessions.value = page.items
  sessionMore.value = page.has_next
}
async function changeSessionPage(delta: number) {
  sessionOffset.value += delta
  await loadSessions()
  sessionId.value = ''
  list.value = []
  selectedScript.value = undefined
  current.value = undefined
  clearCards()
}
async function choosePaper() {
  pendingTemplate.value = undefined
  template.value = paperId.value ? await omr.paper(paperId.value) : undefined
  selectedQuestions.value = template.value?.questions.map((q) => q.mapping_id) || []
}
async function createTemplate() {
  if (!template.value || !selectedQuestions.value.length) return
  pendingTemplate.value ||= {
    request_id: crypto.randomUUID(),
    mapping_ids: [...selectedQuestions.value],
  }
  await run(async () => {
    try {
      await omr.create(paperId.value, pendingTemplate.value!)
    } catch (e) {
      if (!(e instanceof ApiError && e.uncertain)) pendingTemplate.value = undefined
      throw e
    }
    pendingTemplate.value = undefined
    await choosePaper()
    notice.value = '答题卡已创建并绑定当前锁定试卷。'
  })
}
async function downloadTemplate() {
  if (!template.value?.template_id) return
  await run(async () => {
    const blob = await omr.print(template.value!.template_id!)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = '慧批答题卡.pdf'
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  })
}
async function loadScripts() {
  if (!sessionId.value) return
  const page = await scripts.list(sessionId.value, scriptOffset.value)
  list.value = page.items
  scriptTotal.value = page.total
  selectedScript.value = undefined
  pageId.value = ''
  current.value = undefined
  clearCards()
}
async function chooseScript(item: Script) {
  selectedScript.value = item
  pageId.value = ''
  pendingRecognition.value = undefined
  current.value = await omr.current(item.script_id)
}
async function showRecognition(item: Script, rid: string) {
  const result = await omr.inspect(rid)
  const image = URL.createObjectURL(await omr.image(rid))
  const old = cards.value.find((c) => c.result.recognition_id === rid)
  if (old) {
    URL.revokeObjectURL(old.image)
    old.result = result
    old.image = image
  } else {
    cards.value.push({ script: item, result, image, checked: false })
  }
}
async function recognize() {
  if (!selectedScript.value || !current.value?.template_id || !pageId.value) return
  const item = selectedScript.value
  pendingRecognition.value ||= {
    request_id: crypto.randomUUID(),
    template_id: current.value.template_id,
    scan_page_id: pageId.value,
    expected_revision: current.value.revision,
  }
  await run(async () => {
    let receipt: Schema['WriteReceipt']
    try {
      receipt = await omr.recognize(item.script_id, pendingRecognition.value!)
    } catch (e) {
      if (!(e instanceof ApiError && e.uncertain)) pendingRecognition.value = undefined
      throw e
    }
    pendingRecognition.value = undefined
    current.value = await omr.current(item.script_id)
    await showRecognition(item, receipt.id)
    notice.value = '识别完成，请对照原图确认；识别建议还不是有效分数。'
  })
}
async function showCurrent() {
  if (!selectedScript.value || !current.value?.id) return
  await run(async () => showRecognition(selectedScript.value!, current.value!.id!))
}
async function confirmClear() {
  await run(async () => {
    let total = 0
    for (const card of cards.value.filter((c) => c.checked)) {
      const fresh = await omr.inspect(card.result.recognition_id)
      const decisions = fresh.questions
        .filter((q) => q.status === 'single' && q.choice && !q.confirmed_choice)
        .map((q) => ({
          mapping_id: q.mapping_id,
          expected_revision: q.revision,
          choice: q.choice as 'A' | 'B' | 'C' | 'D',
          reason: '教师对照原图确认清晰填涂',
        }))
      if (decisions.length) {
        await omr.confirm(fresh.recognition_id, { request_id: crypto.randomUUID(), decisions })
        total += decisions.length
      }
      card.checked = false
      await showRecognition(card.script, fresh.recognition_id)
    }
    notice.value = `已确认 ${total} 道清晰题；空白、多涂及模糊题仍须单独核验。`
  })
}
async function correct(card: Card, question: Sheet['questions'][number]) {
  const choice = edits.value[`${card.result.recognition_id}:${question.mapping_id}`]
  if (!choice || !['A', 'B', 'C', 'D', 'blank'].includes(choice)) return
  await run(async () => {
    await omr.confirm(card.result.recognition_id, {
      request_id: crypto.randomUUID(),
      decisions: [
        {
          mapping_id: question.mapping_id,
          expected_revision: question.revision,
          choice: choice as 'A' | 'B' | 'C' | 'D' | 'blank',
          reason: '教师对照原图逐题核验',
        },
      ],
    })
    await showRecognition(card.script, card.result.recognition_id)
    notice.value = '已保存教师确认；历史识别和确认记录保留。'
  })
}
onMounted(async () => {
  try {
    await loadPapers()
    const pid = typeof route.query.paper === 'string' ? route.query.paper : ''
    if (pid) {
      if (!paperList.value.some((p) => p.id === pid)) {
        const detail = await papers.detail(pid)
        if (detail.paper.status === 'locked') paperList.value.push(detail.paper)
      }
      if (paperList.value.some((p) => p.id === pid)) {
        paperId.value = pid
        await choosePaper()
      }
    }
  } catch {
    /* May be an examiner without paper-read access. */
  }
  try {
    await loadSessions()
    const sid = typeof route.query.session === 'string' ? route.query.session : ''
    if (sid && !sessions.value.some((s) => s.id === sid)) {
      const value = await recovery.inspect(sid, 0, '', true)
      sessions.value.push({ id: sid, title: value.session_title, event_title: value.event_title })
    }
    if (sid && sessions.value.some((s) => s.id === sid)) {
      sessionId.value = sid
      await loadScripts()
    }
  } catch {
    /* The original services still enforce current authorization. */
  }
})
onUnmounted(clearCards)
</script>

<template>
  <section class="omr-workbench">
    <header class="omr-title">
      <div>
        <h1>答题卡与客观题批改</h1>
        <p>使用锁定试卷生成答题卡，核对扫描原图后确认分数。</p>
      </div>
      <RouterLink to="/scans" class="omr-link">进入考试扫描 →</RouterLink>
    </header>
    <p v-if="error" role="alert" class="omr-error">{{ error }}</p>
    <p v-if="notice" role="status" class="omr-notice">{{ notice }}</p>
    <section class="omr-panel">
      <h2>1. 准备答题卡</h2>
      <p>仅支持本平台生成的 A4 单选题答题卡。每份试卷最多选择 30 题。</p>
      <p>创建后模板不能原位更改；选入模板的题目须以答题卡教师确认为成绩依据。</p>
      <div class="omr-controls">
        <label
          >锁定试卷
          <select
            v-model="paperId"
            :disabled="busy || !!pendingTemplate"
            @change="run(choosePaper)"
          >
            <option value="">请选择</option>
            <option v-for="p in paperList" :key="p.id" :value="p.id">
              {{ p.title }} · v{{ p.version_no }}
            </option>
          </select>
        </label>
        <button
          :disabled="busy || !!pendingTemplate || paperOffset === 0"
          @click="
            run(async () => {
              paperOffset -= 20
              await loadPapers()
            })
          "
        >
          上一页
        </button>
        <button
          :disabled="busy || !!pendingTemplate || !paperMore"
          @click="
            run(async () => {
              paperOffset += 20
              await loadPapers()
            })
          "
        >
          下一页
        </button>
      </div>
      <template v-if="template">
        <p v-if="!template.questions.length" class="omr-error">
          该卷没有满足要求的单选题；标准答案须为单个 A、B、C 或 D。
        </p>
        <div v-else class="omr-questions">
          <label v-for="q in template.questions" :key="q.mapping_id">
            <input
              v-model="selectedQuestions"
              type="checkbox"
              :value="q.mapping_id"
              :disabled="!!template.template_id || busy || !!pendingTemplate"
            />
            第 {{ q.printed_label }} 题 · {{ q.max_points }} 分
          </label>
        </div>
        <button
          v-if="!template.template_id"
          class="primary"
          :disabled="busy || !selectedQuestions.length || selectedQuestions.length > 30"
          @click="createTemplate"
        >
          {{ pendingTemplate ? '重新核对创建结果' : '创建答题卡' }}
        </button>
        <button v-else class="primary" :disabled="busy" @click="downloadTemplate">
          下载 A4 打印版
        </button>
      </template>
    </section>
    <section class="omr-panel">
      <h2>2. 选择已确认答卷并识别</h2>
      <p>
        先在扫描工作区完成人工分卷和身份确认，再选择其中的答题卡页面。系统不会根据文件名或顺序猜学生。
      </p>
      <div class="omr-controls">
        <label
          >考试场次
          <select
            v-model="sessionId"
            :disabled="busy || !!pendingRecognition"
            @change="
              run(async () => {
                scriptOffset = 0
                await loadScripts()
              })
            "
          >
            <option value="">请选择</option>
            <option v-for="s in sessions" :key="s.id" :value="s.id">
              {{ s.event_title }} · {{ s.title }}
            </option>
          </select></label
        >
        <button
          :disabled="busy || !!pendingRecognition || sessionOffset === 0"
          @click="run(() => changeSessionPage(-20))"
        >
          上一组场次
        </button>
        <button
          :disabled="busy || !!pendingRecognition || !sessionMore"
          @click="run(() => changeSessionPage(20))"
        >
          下一组场次
        </button>
        <RouterLink v-if="sessionId" :to="`/scans/sessions/${sessionId}/scripts`"
          >打开人工分卷 →</RouterLink
        >
        <RouterLink v-if="sessionId" :to="`/scans/sessions/${sessionId}/results`"
          >查看成绩预览 →</RouterLink
        >
      </div>
      <div v-if="sessionId" class="omr-scripts">
        <button
          v-for="s in list"
          :key="s.script_id"
          :disabled="busy || !!pendingRecognition || s.readiness !== 'identity_confirmed'"
          :class="{ active: selectedScript?.script_id === s.script_id }"
          @click="run(() => chooseScript(s))"
        >
          <strong>{{ s.current.identity?.student_name || '未确认身份' }}</strong>
          <span
            >{{ s.current.identity?.class_name || '待核对' }} · 考号
            {{ s.current.identity?.exam_number || '未登记' }}</span
          >
        </button>
      </div>
      <div v-if="sessionId" class="omr-controls">
        <button
          :disabled="busy || !!pendingRecognition || scriptOffset === 0"
          @click="
            run(async () => {
              scriptOffset -= 10
              await loadScripts()
            })
          "
        >
          上一页
        </button>
        <button
          :disabled="busy || !!pendingRecognition || scriptOffset + 10 >= scriptTotal"
          @click="
            run(async () => {
              scriptOffset += 10
              await loadScripts()
            })
          "
        >
          下一页
        </button>
        <span>共 {{ scriptTotal }} 份答卷</span>
      </div>
      <div v-if="selectedScript && current" class="omr-controls">
        <p v-if="!current.template_id" class="omr-error">
          当前卷版尚未创建答题卡，请先联系试卷负责人。
        </p>
        <template v-else-if="!current.id">
          <label
            >答题卡页面<select v-model="pageId" :disabled="busy || !!pendingRecognition">
              <option value="">请选择</option>
              <option
                v-for="page in selectedScript.current.pages"
                :key="page.scan_page_id"
                :value="page.scan_page_id"
              >
                {{ page.original_filename }} · 第 {{ page.source_page_index + 1 }} 页
              </option>
            </select></label
          >
          <button class="primary" :disabled="busy || !pageId" @click="recognize">
            {{ pendingRecognition ? '重新核对识别结果' : '识别该页' }}
          </button>
        </template>
        <button v-else class="primary" :disabled="busy" @click="showCurrent">查看识别结果</button>
      </div>
    </section>
    <section v-if="cards.length" class="omr-panel">
      <h2>3. 对照原图确认</h2>
      <p>
        橙框是填涂检测区域。勾选已逐份核对的答卷后，可批量确认清晰单选；空白、多涂和模糊项须逐题处理。
      </p>
      <button
        class="primary"
        :disabled="busy || !cards.some((c) => c.checked)"
        @click="confirmClear"
      >
        批量确认已核对的清晰题
      </button>
      <article v-for="card in cards" :key="card.result.recognition_id" class="omr-card">
        <header>
          <label><input v-model="card.checked" type="checkbox" /> 已核对该答卷</label>
          <strong
            >{{ card.script.current.identity?.student_name }} ·
            {{ card.script.current.identity?.class_name }} · 考号
            {{ card.script.current.identity?.exam_number || '未登记' }}</strong
          >
        </header>
        <div class="omr-review">
          <a :href="card.image" target="_blank" rel="noopener"
            ><img :src="card.image" alt="扫描原图与填涂区域"
          /></a>
          <div class="omr-table">
            <table>
              <thead>
                <tr>
                  <th>题号</th>
                  <th>识别</th>
                  <th>标准答案</th>
                  <th>建议 / 已确认</th>
                  <th>人工核验</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="q in card.result.questions" :key="q.mapping_id">
                  <td>{{ q.label }}</td>
                  <td>{{ q.status === 'single' ? q.choice : statusNames[q.status] }}</td>
                  <td>{{ q.correct_choice }}</td>
                  <td>
                    {{ q.suggested_points ?? '待核验' }} / {{ q.confirmed_points ?? '未确认' }}
                  </td>
                  <td>
                    <select
                      v-model="edits[`${card.result.recognition_id}:${q.mapping_id}`]"
                      :disabled="busy"
                    >
                      <option value="">选择结果</option>
                      <option v-for="v in ['A', 'B', 'C', 'D']" :key="v" :value="v">{{ v }}</option>
                      <option value="blank">确认空白</option>
                    </select>
                    <button
                      :disabled="busy || !edits[`${card.result.recognition_id}:${q.mapping_id}`]"
                      @click="correct(card, q)"
                    >
                      确认
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </article>
    </section>
  </section>
</template>

<style scoped>
.omr-workbench {
  max-width: 1500px;
  margin: 0 auto;
  color: #243447;
}
.omr-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
h1 {
  font-size: 24px;
  margin: 0 0 7px;
}
h2 {
  font-size: 18px;
  margin: 0 0 8px;
}
p {
  color: #627386;
  line-height: 1.6;
  margin: 8px 0 16px;
}
.omr-panel {
  background: #fff;
  border: 1px solid #dde5ed;
  border-radius: 10px;
  padding: 22px;
  margin-bottom: 16px;
}
.omr-controls,
.omr-questions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin: 14px 0;
}
.omr-controls label {
  display: flex;
  align-items: center;
  gap: 8px;
}
select,
button {
  font: inherit;
  font-size: 14px;
  padding: 8px 11px;
  border: 1px solid #cbd6e2;
  border-radius: 6px;
  background: white;
  color: #31465c;
}
button {
  cursor: pointer;
}
button:disabled {
  opacity: 0.48;
  cursor: not-allowed;
}
.primary {
  background: #246bd0;
  color: white;
  border-color: #246bd0;
}
.omr-scripts {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.omr-scripts button {
  text-align: left;
  display: grid;
  gap: 4px;
  min-width: 180px;
}
.omr-scripts button.active {
  border-color: #246bd0;
  background: #f0f6ff;
}
.omr-scripts span {
  font-size: 13px;
  color: #61758b;
}
.omr-card {
  border-top: 1px solid #e2e8ef;
  padding-top: 18px;
  margin-top: 18px;
}
.omr-card header {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin-bottom: 12px;
}
.omr-review {
  display: grid;
  grid-template-columns: minmax(250px, 36%) 1fr;
  gap: 18px;
}
.omr-review img {
  display: block;
  width: 100%;
  max-height: 700px;
  object-fit: contain;
  object-position: top;
  border: 1px solid #d8e1eb;
}
.omr-table {
  overflow-x: auto;
}
table {
  border-collapse: collapse;
  width: 100%;
  font-size: 14px;
}
th,
td {
  border-bottom: 1px solid #e5ebf0;
  text-align: left;
  padding: 10px 7px;
  white-space: nowrap;
}
.omr-error {
  color: #aa3030;
}
.omr-notice {
  color: #11735f;
}
.omr-link {
  white-space: nowrap;
}
@media (max-width: 900px) {
  .omr-review {
    grid-template-columns: 1fr;
  }
  .omr-title {
    align-items: flex-start;
    gap: 12px;
  }
}
</style>
