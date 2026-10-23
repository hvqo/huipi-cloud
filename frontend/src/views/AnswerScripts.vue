<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import ScanThumbnail from '../components/ScanThumbnail.vue'
import { scripts } from '../api/answer-scripts'
import { scans } from '../api/scans'
import { ApiError, type Schema } from '../api/client'

const route = useRoute(),
  sid = String(route.params.sid)
const pages = ref<Schema['SourcePage'][]>([]),
  selected = ref<Schema['SourcePage'][]>([])
const items = ref<Schema['ScriptRead'][]>([]),
  current = ref<Schema['ScriptRead']>()
const identities = ref<Schema['CandidateList']>(),
  history = ref<Schema['VersionRead'][]>([])
const batches = ref<Schema['BatchRead'][]>([]),
  batchId = ref(''),
  onlyFree = ref(true)
const offset = ref(0),
  total = ref(0),
  scriptOffset = ref(0),
  scriptTotal = ref(0)
const query = ref(''),
  candidateOffset = ref(0),
  entryId = ref(''),
  paperId = ref('')
const complete = ref(false),
  reason = ref(''),
  busy = ref(false),
  error = ref(''),
  notice = ref('')
const showIdentity = ref(false),
  preview = ref(''),
  previewPage = ref<Schema['SourcePage']>()
const historyOffset = ref(0),
  historyTotal = ref(0)
const pending = ref<{ aid?: string; body: Schema['ScriptCreate'] | Schema['ScriptChange'] }>()
const status: Record<string, string> = {
  unclaimed: '待身份确认',
  revoked: '已撤销',
  roster_changed: '名单已更正，须重新确认',
  paper_unavailable: '卷版不可用',
  session_unavailable: '场次已停止处理',
  incomplete: '已确认身份 · 答卷不完整',
  identity_confirmed: '身份已确认',
}
const title = computed(() =>
  current.value ? `答卷 ${current.value.script_id.slice(0, 8)}` : '新建答卷',
)
const chosenIdentity = computed(() =>
  identities.value?.items.find((i) => i.roster_entry_id === entryId.value),
)
const canEdit = computed(() => !busy.value && !pending.value)
function clearPreview() {
  if (preview.value) URL.revokeObjectURL(preview.value)
  preview.value = ''
}
async function run(work: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await work()
  } catch (e) {
    const code = e instanceof ApiError ? e.status : 0
    error.value =
      code === 409
        ? '页面已被占用、名单已更新或答卷版本发生冲突。请刷新后核对；已保存的历史不会改变。'
        : code === 404 || code === 403
          ? '没有此场次或名单的访问权限，请联系授权人员。'
          : code === 503
            ? '暂时无法确认保存结果。请先刷新答卷，再重试原请求。'
            : code === 413
              ? '答卷超过处理上限：最多100页、8份文件、256 MiB核验数据。'
              : '操作未完成，请检查输入或稍后重试。'
  } finally {
    busy.value = false
  }
}
async function loadPages() {
  const r = await scripts.pages(sid, offset.value, onlyFree.value, batchId.value)
  pages.value = r.items
  total.value = r.total
}
async function refresh() {
  await loadPages()
  const r = await scripts.list(sid, scriptOffset.value)
  items.value = r.items
  scriptTotal.value = r.total
}
async function pageBy(delta: number) {
  offset.value += delta
  await run(loadPages)
}
async function scriptBy(delta: number) {
  scriptOffset.value += delta
  await run(refresh)
}
async function filterPages() {
  offset.value = 0
  await run(loadPages)
}
function add(page: Schema['SourcePage']) {
  if (
    !canEdit.value ||
    selected.value.some((p) => p.scan_page_id === page.scan_page_id) ||
    (page.allocated_script_id && page.allocated_script_id !== current.value?.script_id)
  )
    return
  selected.value.push(page)
}
function move(index: number, delta: number) {
  if (!canEdit.value) return
  const to = index + delta
  if (to < 0 || to >= selected.value.length) return
  const [p] = selected.value.splice(index, 1)
  if (p) selected.value.splice(to, 0, p)
}
function reset() {
  if (!canEdit.value) return
  current.value = undefined
  selected.value = []
  reason.value = ''
  complete.value = false
  history.value = []
  historyOffset.value = 0
  historyTotal.value = 0
  notice.value = ''
  entryId.value = ''
  paperId.value = ''
}
async function choose(item: Schema['ScriptRead']) {
  await run(async () => {
    current.value = await scripts.inspect(item.script_id)
    selected.value = [...current.value.current.pages]
    complete.value = current.value.current.completeness === 'complete'
    reason.value = ''
    entryId.value = ''
    paperId.value = ''
    history.value = []
    historyOffset.value = 0
    historyTotal.value = 0
    notice.value = ''
  })
}
async function view(page: Schema['SourcePage']) {
  await run(async () => {
    clearPreview()
    previewPage.value = page
    preview.value = URL.createObjectURL(await scans.preview(page.scan_page_id))
  })
}
async function loadCandidates() {
  identities.value = await scripts.candidates(sid, query.value, candidateOffset.value)
  entryId.value = ''
  paperId.value = ''
}
async function openConfirm() {
  await run(async () => {
    candidateOffset.value = 0
    await loadCandidates()
    showIdentity.value = true
  })
}
async function findCandidates() {
  candidateOffset.value = 0
  await run(loadCandidates)
}
async function candidateBy(delta: number) {
  candidateOffset.value += delta
  await run(loadCandidates)
}
function chooseEntry() {
  paperId.value = chosenIdentity.value?.session_paper_id || ''
}
async function sendPending() {
  const q = pending.value
  if (!q) return
  const r = q.aid
    ? await scripts.change(q.aid, q.body as Schema['ScriptChange'])
    : await scripts.create(sid, q.body)
  pending.value = undefined
  current.value = await scripts.inspect(r.script_id)
  selected.value = [...current.value.current.pages]
  complete.value = current.value.current.completeness === 'complete'
  showIdentity.value = false
  notice.value =
    r.status === 'confirmed'
      ? '身份确认已保存。尚未解析或评分。'
      : r.status === 'revoked'
        ? '已撤销当前答卷，页面可重新分卷。'
        : '草稿已保存，页面尚未确认学生身份。'
  reason.value = ''
  await refresh()
}
async function save(action: 'save_draft' | 'confirm' | 'revoke') {
  if (!canEdit.value || !selected.value.length || !reason.value.trim()) return
  if (
    action === 'confirm' &&
    (!entryId.value || !paperId.value || !identities.value?.roster_revision_id)
  )
    return
  const common: Schema['ScriptCreate'] = {
    request_id: crypto.randomUUID(),
    page_ids: selected.value.map((p) => p.scan_page_id),
    completeness: complete.value ? 'complete' : 'incomplete',
    reason: reason.value.trim(),
  }
  if (current.value) {
    const body: Schema['ScriptChange'] = {
      ...common,
      expected_revision: current.value.revision,
      action,
      roster_revision_id: action === 'confirm' ? identities.value!.roster_revision_id : null,
      roster_entry_id: action === 'confirm' ? entryId.value : null,
      session_paper_id: action === 'confirm' ? paperId.value : null,
    }
    if (action === 'revoke') body.page_ids = current.value.current.pages.map((p) => p.scan_page_id)
    pending.value = { aid: current.value.script_id, body }
  } else pending.value = { body: common }
  await run(sendPending)
}
async function loadHistory(delta = 0) {
  historyOffset.value += delta
  await run(async () => {
    if (!current.value) return
    const r = await scripts.history(current.value.script_id, historyOffset.value)
    history.value = r.items
    historyTotal.value = r.total
  })
}
function abandonPending() {
  pending.value = undefined
  notice.value = '已解除请求锁定。此前请求仍可能成功，请先刷新再编辑。'
}
onMounted(() =>
  run(async () => {
    batches.value = (await scans.batches(sid)).items
    await refresh()
  }),
)
onUnmounted(clearPreview)
</script>

<template>
  <section class="script-workspace">
    <header class="script-heading">
      <div>
        <RouterLink to="/scans">← 考试扫描</RouterLink>
        <h1>人工分卷与身份确认</h1>
        <p>先整理答卷页面，再核对名单。原始扫描页始终保留。</p>
      </div>
      <button :disabled="!canEdit" class="primary" @click="reset">新建答卷</button>
    </header>
    <div v-if="error" role="alert" class="script-alert">{{ error }}</div>
    <div v-if="notice" role="status" class="script-notice">{{ notice }}</div>
    <div v-if="pending" class="script-alert">
      保存结果尚未核实，编辑已锁定。<button :disabled="busy" @click="run(refresh)">刷新答卷</button
      ><button :disabled="busy" @click="run(sendPending)">重试原请求</button
      ><button :disabled="busy" @click="abandonPending">解除编辑锁定</button>
    </div>
    <div class="script-columns">
      <section class="script-source">
        <h2>
          扫描页面 <span>{{ total }} 页</span>
        </h2>
        <div class="script-filters">
          <label
            >页面范围<select v-model="batchId" :disabled="busy" @change="filterPages">
              <option value="">全部批次</option>
              <option v-for="b in batches" :key="b.id" :value="b.id">{{ b.title }}</option>
            </select></label
          >
          <label
            ><input
              v-model="onlyFree"
              type="checkbox"
              :disabled="busy"
              @change="filterPages"
            />仅看未认领</label
          >
          <button :disabled="busy" @click="run(refresh)">刷新</button>
        </div>
        <p class="script-hint">
          可跨文件补页；点击预览核对内容，再按顺序加入。已占用的页面不能重复分配。
        </p>
        <div class="script-page-grid">
          <article
            v-for="p in pages"
            :key="p.scan_page_id"
            class="script-page"
            :class="{ taken: p.allocated_script_id }"
          >
            <button
              class="page-symbol"
              :aria-label="`预览扫描页 ${p.source_page_index + 1}`"
              :disabled="busy"
              @click="view(p)"
            >
              <ScanThumbnail :page-id="p.scan_page_id" /><span
                >第 {{ p.source_page_index + 1 }} 页</span
              >
            </button>
            <small>{{ p.original_filename }}</small
            ><small>{{ p.allocated_script_id ? '已分卷' : '未认领' }}</small>
            <button
              :aria-label="`加入扫描页 ${p.source_page_index + 1}`"
              :disabled="
                !canEdit ||
                !!p.allocated_script_id ||
                selected.some((s) => s.scan_page_id === p.scan_page_id) ||
                selected.length >= 100
              "
              @click="add(p)"
            >
              加入答卷
            </button>
          </article>
        </div>
        <p v-if="!pages.length && !busy">此范围没有待整理页面。可切换批次或关闭「仅看未认领」。</p>
        <nav class="script-pagination">
          <button :disabled="busy || offset === 0" @click="pageBy(-20)">上一页</button
          ><span>{{ offset + 1 }}–{{ Math.min(offset + 20, total) }} / {{ total }}</span
          ><button :disabled="busy || offset + 20 >= total" @click="pageBy(20)">下一页</button>
        </nav>
      </section>
      <section class="script-editor">
        <header>
          <h2>{{ title }}</h2>
          <span v-if="current" class="script-status"
            >v{{ current.revision }} · {{ status[current.readiness] }}</span
          >
        </header>
        <p v-if="current?.current.identity" class="script-identity">
          {{ current.current.identity.student_name }} · {{ current.current.identity.class_name }} ·
          考号 {{ current.current.identity.exam_number || '未登记' }}
        </p>
        <p v-if="current?.readiness === 'roster_changed'" class="script-alert">
          名单已更正。旧身份仅供历史核查，请重新选择当前名单后确认。
        </p>
        <ol class="script-order">
          <li v-for="(p, i) in selected" :key="p.scan_page_id">
            <button :disabled="busy" @click="view(p)">
              {{ i + 1 }}. {{ p.original_filename }} · 原第 {{ p.source_page_index + 1 }} 页
            </button>
            <div>
              <button
                :disabled="!canEdit || i === 0"
                :aria-label="`上移第${i + 1}项`"
                @click="move(i, -1)"
              >
                ↑</button
              ><button
                :disabled="!canEdit || i === selected.length - 1"
                :aria-label="`下移第${i + 1}项`"
                @click="move(i, 1)"
              >
                ↓</button
              ><button
                :disabled="!canEdit"
                :aria-label="`移除第${i + 1}项`"
                @click="selected.splice(i, 1)"
              >
                移除
              </button>
            </div>
          </li>
        </ol>
        <p v-if="!selected.length" class="script-placeholder">
          从左侧选择一页或多页，组成一份学生答卷。
        </p>
        <label class="script-field"
          ><input
            v-model="complete"
            type="checkbox"
            :disabled="!canEdit"
          />已核对页数，答卷完整</label
        >
        <p v-if="!complete" class="script-hint">缺页可以保存或确认身份，但不能进入后续解析桥接。</p>
        <label class="script-field"
          >操作说明<textarea
            v-model="reason"
            :disabled="!canEdit"
            maxlength="300"
            rows="2"
            placeholder="说明分卷依据、漏页或更正原因"
          />
        </label>
        <div class="script-actions">
          <button
            class="primary"
            :disabled="!canEdit || !selected.length || !reason.trim()"
            @click="save('save_draft')"
          >
            {{ current ? '保存为新版本草稿' : '保存答卷草稿' }}</button
          ><button
            :disabled="!canEdit || !current || !selected.length || !reason.trim()"
            @click="openConfirm"
          >
            核对学生并确认</button
          ><button
            :disabled="!canEdit || !current || current.readiness === 'revoked' || !reason.trim()"
            @click="save('revoke')"
          >
            撤销答卷
          </button>
        </div>
        <p class="script-hint">
          已确认答卷的任何修改都保留历史；保存草稿会解除当前身份确认，必须重新核对。
        </p>
      </section>
      <aside class="script-saved">
        <h2>
          场次答卷 <span>{{ scriptTotal }}</span>
        </h2>
        <button
          v-for="s in items"
          :key="s.script_id"
          class="script-saved-item"
          :class="{ active: current?.script_id === s.script_id }"
          :disabled="!canEdit"
          @click="choose(s)"
        >
          <strong>{{ s.current.identity?.student_name || '未确认学生' }}</strong
          ><span>{{ s.current.identity?.class_name || '答卷草稿' }}</span
          ><small
            >{{ s.current.pages.length }} 页 · v{{ s.revision }} · {{ status[s.readiness] }}</small
          >
        </button>
        <nav class="script-pagination">
          <button :disabled="busy || scriptOffset === 0" @click="scriptBy(-10)">上一页</button
          ><button :disabled="busy || scriptOffset + 10 >= scriptTotal" @click="scriptBy(10)">
            下一页
          </button>
        </nav>
        <button :disabled="busy || !current" @click="loadHistory()">查看历史版本</button>
        <ol v-if="history.length" class="script-history">
          <li v-for="v in history" :key="v.id">
            v{{ v.version_no }} ·
            {{ v.status === 'confirmed' ? '已确认' : v.status === 'revoked' ? '撤销' : '草稿'
            }}<br />{{ v.identity?.student_name || '未确认' }} · {{ v.pages.length }} 页
            <p>{{ v.reason }}</p>
          </li>
        </ol>
        <nav v-if="history.length" class="script-pagination">
          <button :disabled="busy || historyOffset === 0" @click="loadHistory(-10)">较新</button
          ><button :disabled="busy || historyOffset + 10 >= historyTotal" @click="loadHistory(10)">
            较早
          </button>
        </nav>
      </aside>
    </div>
    <el-dialog v-model="showIdentity" title="核对学生身份与实际卷别" width="min(720px, 94vw)">
      <p>仅从本场次当前冻结名单中选择。请对照原卷核验姓名、学号、考号和班级。</p>
      <form class="script-search" @submit.prevent="findCandidates">
        <label
          >名单搜索<input
            v-model="query"
            maxlength="100"
            placeholder="姓名、考号、学号或班级"
            :disabled="busy" /></label
        ><button :disabled="busy">搜索名单</button>
      </form>
      <label class="script-field"
        >参试学生<select
          v-model="entryId"
          aria-label="参试学生"
          :disabled="busy"
          @change="chooseEntry"
        >
          <option value="">请选择可信名单项</option>
          <option
            v-for="i in identities?.items"
            :key="i.roster_entry_id"
            :value="i.roster_entry_id"
          >
            {{ i.student_name }} · {{ i.class_name }} · 学号 {{ i.student_code }} · 考号
            {{ i.exam_number || '未登记' }}
          </option>
        </select></label
      >
      <nav class="script-pagination">
        <button :disabled="busy || candidateOffset === 0" @click="candidateBy(-20)">上一组</button
        ><span>{{ identities?.total || 0 }} 人 · 名单 v{{ identities?.roster_version_no }}</span
        ><button
          :disabled="busy || candidateOffset + 20 >= (identities?.total || 0)"
          @click="candidateBy(20)"
        >
          下一组
        </button>
      </nav>
      <label class="script-field"
        >实际卷别<select v-model="paperId" aria-label="实际卷别" :disabled="busy">
          <option value="">待核验，请明确选择</option>
          <option
            v-for="p in identities?.papers"
            :key="p.session_paper_id"
            :value="p.session_paper_id"
          >
            {{ p.variant_code }} 卷
          </option>
        </select></label
      >
      <p v-if="chosenIdentity && !chosenIdentity.session_paper_id" class="script-alert">
        名单卷别尚未确认。请按原卷证据明确选择，不会自动分配 A 卷。
      </p>
      <p class="script-hint">
        若实际卷别与已知名单卷别不符，须先更正名单。本操作不会创建学生提交或成绩。
      </p>
      <template #footer
        ><button :disabled="busy" @click="showIdentity = false">返回核对</button
        ><button
          class="primary"
          :disabled="!canEdit || !entryId || !paperId"
          @click="save('confirm')"
        >
          确认身份并保存新版本
        </button></template
      >
    </el-dialog>
    <el-dialog
      :model-value="!!preview"
      title="核对原始扫描页"
      width="min(800px, 96vw)"
      @close="clearPreview"
      ><p>
        {{ previewPage?.original_filename }} · 原第
        {{ (previewPage?.source_page_index || 0) + 1 }} 页
      </p>
      <img v-if="preview" :src="preview" alt="人工分卷页面预览" class="script-preview"
    /></el-dialog>
  </section>
</template>

<style scoped>
.script-workspace {
  max-width: 1680px;
  margin: auto;
  color: #20334b;
}
.script-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 22px;
}
.script-heading h1 {
  font-size: 25px;
  margin: 12px 0 8px;
}
.script-heading p,
.script-hint {
  font-size: 14px;
  line-height: 1.65;
  color: #60718a;
}
.script-columns {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(360px, 1.25fr) 240px;
  gap: 20px;
  align-items: start;
}
.script-source,
.script-editor,
.script-saved {
  background: #fff;
  border: 1px solid #dce3ec;
  border-radius: 10px;
  padding: 20px;
  min-width: 0;
}
.script-columns h2 {
  font-size: 18px;
  margin: 0 0 18px;
}
.script-columns h2 span {
  font-weight: 400;
  font-size: 14px;
  color: #61738c;
}
.script-filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.script-page-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.script-page {
  border: 1px solid #dae2ed;
  border-radius: 8px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: #f8fafc;
}
.script-page.taken {
  background: #f0f3f7;
}
.page-symbol {
  min-height: 88px;
  font-size: 32px;
  color: #4d6d94;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: white;
}
.page-symbol span {
  font-size: 14px;
  margin-top: 8px;
}
.script-page small {
  font-size: 12px;
  color: #5c6f88;
}
.script-workspace button {
  font: inherit;
  font-size: 14px;
  border: 1px solid #c9d4e3;
  background: white;
  border-radius: 6px;
  padding: 8px 12px;
  cursor: pointer;
  color: #254d7c;
}
.script-workspace button.primary {
  background: #2359ae;
  border-color: #2359ae;
  color: white;
}
.script-workspace button:disabled {
  opacity: 0.48;
  cursor: not-allowed;
}
.script-workspace select,
.script-workspace input:not([type='checkbox']),
.script-workspace textarea {
  font: inherit;
  font-size: 14px;
  padding: 9px;
  border: 1px solid #bdcbdd;
  border-radius: 6px;
  max-width: 100%;
  box-sizing: border-box;
}
.script-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 18px 0;
  font-size: 14px;
}
.script-field:has(input[type='checkbox']) {
  flex-direction: row;
  align-items: center;
}
.script-order {
  padding: 0;
  list-style: none;
  min-height: 200px;
}
.script-order li {
  border-bottom: 1px solid #e4eaf1;
  padding: 12px 0;
  display: flex;
  gap: 8px;
  justify-content: space-between;
  align-items: center;
}
.script-order li > button {
  text-align: left;
  border: 0;
  padding: 4px;
  font-size: 14px;
}
.script-order li > div {
  display: flex;
  gap: 4px;
}
.script-order li > div button {
  padding: 6px 8px;
}
.script-placeholder {
  padding: 46px 16px;
  border: 1px dashed #b7c8de;
  text-align: center;
  color: #60758e;
  line-height: 1.8;
}
.script-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.script-status {
  display: block;
  font-size: 14px;
  color: #496789;
  margin-bottom: 10px;
}
.script-identity {
  padding: 12px;
  background: #eef6f5;
  border-left: 3px solid #268578;
  line-height: 1.7;
}
.script-saved-item {
  display: flex;
  flex-direction: column;
  gap: 7px;
  width: 100%;
  margin-bottom: 10px;
  text-align: left;
}
.script-saved-item.active {
  border-color: #2359ae;
  background: #f1f6fd;
}
.script-saved-item small {
  font-size: 12px;
  line-height: 1.6;
}
.script-alert {
  background: #fff6e6;
  color: #794b13;
  padding: 14px;
  border-radius: 7px;
  margin-bottom: 14px;
  line-height: 1.7;
}
.script-notice {
  background: #edf7f2;
  color: #24654f;
  padding: 14px;
  margin-bottom: 14px;
  border-radius: 7px;
}
.script-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 16px;
  font-size: 13px;
}
.script-search {
  display: flex;
  gap: 12px;
  align-items: end;
}
.script-search label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1;
}
.script-history {
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.7;
}
.script-history p {
  color: #61738c;
}
.script-preview {
  display: block;
  width: 100%;
  height: auto;
  max-height: 70vh;
  object-fit: contain;
}
@media (min-width: 1800px) {
  .script-page-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 1150px) {
  .script-columns {
    grid-template-columns: minmax(250px, 1fr) minmax(340px, 1.2fr);
  }
  .script-saved {
    grid-column: 1/-1;
  }
  .script-saved-item {
    display: inline-flex;
    width: 220px;
    margin-right: 12px;
  }
}
@media (max-width: 720px) {
  .script-columns {
    grid-template-columns: 1fr;
  }
  .script-heading {
    align-items: start;
  }
  .script-source,
  .script-editor,
  .script-saved {
    padding: 16px;
  }
  .script-order li {
    flex-wrap: wrap;
  }
  .script-saved {
    grid-column: auto;
  }
}
</style>
