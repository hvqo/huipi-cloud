<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { scans } from '../api/scans'
import { ApiError, type Schema } from '../api/client'
import PageState from '../components/PageState.vue'
import ExceptionBadge from '../components/ExceptionBadge.vue'

const route = useRoute()
const sessions = ref<Schema['ScanSession'][]>([]),
  sessionId = ref(''),
  sessionOffset = ref(0),
  moreSessions = ref(false),
  batchOffset = ref(0),
  moreBatches = ref(false),
  limits = ref({ max_file_bytes: 32 * 1024 * 1024, max_pages: 50 }),
  batches = ref<Schema['BatchRead'][]>([]),
  batch = ref<Schema['BatchRead']>(),
  files = ref<Schema['FileRead'][]>([]),
  pages = ref<Schema['PageRead'][]>([]),
  activeFile = ref<Schema['FileRead']>(),
  pageOffset = ref(0),
  morePages = ref(false),
  activePage = ref<Schema['PageRead']>(),
  image = ref(''),
  title = ref(''),
  file = ref<File>(),
  pending = ref<{ id: string; file: File; batchId: string }>(),
  pendingBatch = ref<{ id: string; sid: string; title: string }>(),
  loading = ref(false),
  busy = ref(false),
  error = ref(''),
  notice = ref('')
const previewZoom = ref(100)
const showFilePanel = ref(true)
let generation = 0
function clearImage() {
  if (image.value) URL.revokeObjectURL(image.value)
  image.value = ''
}
async function run(action: () => Promise<void>) {
  error.value = ''
  try {
    await action()
  } catch (e) {
    const messages: Record<number, string> = {
      400: 'PDF无法读取，请检查原文件是否完整或加密。',
      409: '文件重复、正在接收或请求发生冲突，请先刷新文件状态。',
      413: '文件、页数或预览超过接收上限，请拆分PDF后再上传。',
      415: '只接收PDF文件，请检查文件类型。',
      429: '接收任务较多，请稍后重试。',
    }
    error.value = e instanceof ApiError ? messages[e.status] || e.message : (e as Error).message
  }
}
async function paginateSessions(delta: number) {
  sessionOffset.value += delta
  await loadSessions()
}
async function paginatePages(delta: number) {
  pageOffset.value += delta
  await loadPages()
}
async function loadSessions() {
  loading.value = true
  await run(async () => {
    const value = await scans.sessions(sessionOffset.value, String(route.query.session || ''))
    sessions.value = value.items
    moreSessions.value = value.truncated
    limits.value = value.limits
    sessionId.value = value.items[0]?.id || ''
    await loadBatches()
  })
  loading.value = false
}
async function loadBatches() {
  const current = ++generation,
    sid = sessionId.value
  batchOffset.value = 0
  clearImage()
  batches.value = []
  batch.value = undefined
  files.value = []
  pages.value = []
  activeFile.value = undefined
  activePage.value = undefined
  pending.value = undefined
  if (!sessionId.value) return
  await run(async () => {
    const result = await scans.batches(sid)
    if (current === generation) {
      batches.value = result.items
      moreBatches.value = result.truncated
    }
  })
}
async function chooseBatch(value: Schema['BatchRead']) {
  const current = ++generation
  showFilePanel.value = true
  clearImage()
  batch.value = value
  pages.value = []
  activePage.value = undefined
  activeFile.value = undefined
  pending.value = undefined
  notice.value = ''
  await run(async () => {
    const result = await scans.files(value.id)
    if (current === generation) files.value = result.items
  })
}
async function createBatch() {
  const selected = sessions.value.find((s) => s.id === sessionId.value)
  if (!selected || selected.lifecycle !== 'ready' || !title.value.trim()) return
  busy.value = true
  await run(async () => {
    if (
      !pendingBatch.value ||
      pendingBatch.value.sid !== sessionId.value ||
      pendingBatch.value.title !== title.value.trim()
    )
      pendingBatch.value = {
        id: crypto.randomUUID(),
        sid: sessionId.value,
        title: title.value.trim(),
      }
    const q = pendingBatch.value
    const created = await scans.create(q.sid, q.id, q.title)
    const result = await scans.batches(q.sid)
    batches.value = result.items
    moreBatches.value = result.truncated
    await chooseBatch(created)
    pendingBatch.value = undefined
    notice.value = '扫描批次已建立，请选择 PDF。'
  })
  busy.value = false
}
function selectFile(event: Event) {
  file.value = (event.target as HTMLInputElement).files?.[0]
  pending.value = undefined
  notice.value = ''
}
async function upload() {
  if (!batch.value || !file.value || busy.value) return
  if (!/\.pdf$/i.test(file.value.name) || file.value.size > limits.value.max_file_bytes) {
    error.value = `请选择不超过 ${limits.value.max_file_bytes / 1024 / 1024} MiB 的 PDF。`
    return
  }
  busy.value = true
  let confirmed = false
  await run(async () => {
    pending.value ||= { id: crypto.randomUUID(), file: file.value!, batchId: batch.value!.id }
    const q = pending.value
    const result = await scans.upload(q.batchId, q.id, q.file)
    confirmed = true
    files.value = (await scans.files(q.batchId)).items
    notice.value = `已安全接收 ${result.page_count} 页，所有页面均待身份核验。`
    pending.value = undefined
    await showFile(result)
  })
  if (error.value && !confirmed)
    notice.value = '尚未确认上传成功。保留原文件，可重试原请求；请先刷新文件状态。'
  busy.value = false
}
async function showFile(value: Schema['FileRead']) {
  generation++
  clearImage()
  activeFile.value = value
  activePage.value = undefined
  pages.value = []
  pageOffset.value = 0
  if (value.status === 'stored') await loadPages()
}
async function loadPages() {
  if (!activeFile.value) return
  await run(async () => {
    const fid = activeFile.value!.id,
      offset = pageOffset.value
    const value = await scans.pages(fid, offset)
    if (activeFile.value?.id !== fid || pageOffset.value !== offset) return
    pages.value = value.items
    morePages.value = value.truncated
    if (value.items[0]) await showPage(value.items[0])
  })
}
async function showPage(value: Schema['PageRead']) {
  const current = ++generation
  clearImage()
  activePage.value = value
  await run(async () => {
    const blob = await scans.preview(value.id)
    if (current === generation) image.value = URL.createObjectURL(blob)
  })
}
async function original() {
  if (!activeFile.value) return
  busy.value = true
  await run(async () => {
    const blob = await scans.original(activeFile.value!.id),
      url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = activeFile.value!.original_filename
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  })
  busy.value = false
}
async function refreshFiles() {
  if (batch.value)
    await run(async () => {
      const bid = batch.value!.id,
        result = await scans.files(bid)
      if (batch.value?.id === bid) files.value = result.items
    })
}
async function paginateBatches(delta: number) {
  const sid = sessionId.value,
    current = ++generation
  batchOffset.value += delta
  await run(async () => {
    const result = await scans.batches(sid, batchOffset.value)
    if (current === generation) {
      batches.value = result.items
      moreBatches.value = result.truncated
    }
  })
}
const stateLabel = (s: string) =>
  ({ stored: '已接收', receiving: '接收处理中', failed: '接收失败' })[s] || s
onMounted(async () => {
  await loadSessions()
  if (route.query.batch && sessionId.value)
    await run(async () => {
      const selectedBatch = await scans.batch(String(route.query.batch))
      if (selectedBatch.exam_session_id !== sessionId.value)
        throw new Error('来源文件不属于当前场次。')
      await chooseBatch(selectedBatch)
      if (route.query.file) {
        const selectedFile = await scans.file(String(route.query.file))
        if (selectedFile.batch_id !== selectedBatch.id) throw new Error('来源文件不属于当前批次。')
        await showFile(selectedFile)
      }
    })
})
onUnmounted(() => {
  generation++
  clearImage()
})
</script>
<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">考务工作空间</p>
      <h1>考试扫描</h1>
      <RouterLink v-if="sessionId" :to="`/scans/sessions/${sessionId}/scripts`"
        >人工分卷与身份确认 →</RouterLink
      >
      <RouterLink v-if="sessionId" :to="`/scans/sessions/${sessionId}/results`"
        >整卷成绩预览 →</RouterLink
      >
      <p class="muted">接收混班扫描 PDF，逐页核对原件。此步骤不确认学生身份，也不生成成绩。</p>
    </div>
  </div>
  <ExceptionBadge :session-id="sessionId" />
  <p v-if="error" class="scan-alert" role="alert">{{ error }}</p>
  <p v-if="notice" class="scan-notice" role="status">{{ notice }}</p>
  <p v-if="!loading && !error && !sessions.length" class="muted">
    没有可访问的考试场次，请联系考务负责人确认场次授权。
  </p>
  <PageState :loading="loading" :empty="!sessions.length" @retry="loadSessions">
    <section class="scan-controls">
      <label
        >考试场次<select v-model="sessionId" :disabled="busy" @change="loadBatches">
          <option v-for="s in sessions" :key="s.id" :value="s.id">
            {{ s.event_title }} · {{ s.subject }} · {{ s.title }}（{{
              s.lifecycle === 'ready' ? '可接收' : '仅查看'
            }}）
          </option>
        </select></label
      >
      <p class="muted">
        {{
          sessions.find((s) => s.id === sessionId)?.roster_frozen
            ? '已具备冻结名单；页面身份仍须后续人工确认。'
            : '该场次尚无冻结名单；不得据此确定学生身份。'
        }}
      </p>
      <div class="scan-actions">
        <el-button :disabled="sessionOffset === 0 || busy" @click="paginateSessions(-20)"
          >上一组场次</el-button
        ><el-button :disabled="!moreSessions || busy" @click="paginateSessions(20)"
          >下一组场次</el-button
        >
      </div>
      <form class="scan-actions" @submit.prevent="createBatch">
        <label
          >批次名称<input
            v-model="title"
            maxlength="120"
            :disabled="busy"
            placeholder="例如：数学期中考试 · 第一批" /></label
        ><el-button
          native-type="submit"
          type="primary"
          :disabled="
            busy || !title.trim() || sessions.find((s) => s.id === sessionId)?.lifecycle !== 'ready'
          "
          >新建扫描批次</el-button
        >
      </form>
    </section>
    <div class="scan-workspace">
      <aside class="scan-batches">
        <h2>扫描批次</h2>
        <p v-if="!batches.length" class="muted">还没有批次，请先新建。</p>
        <button
          v-for="b in batches"
          :key="b.id"
          :disabled="busy"
          :class="{ chosen: batch?.id === b.id }"
          @click="chooseBatch(b)"
        >
          {{ b.title }}<small>{{ new Date(b.created_at).toLocaleString('zh-CN') }}</small>
        </button>
        <div class="scan-actions">
          <el-button :disabled="batchOffset === 0 || busy" @click="paginateBatches(-20)"
            >上一组批次</el-button
          ><el-button :disabled="!moreBatches || busy" @click="paginateBatches(20)"
            >下一组批次</el-button
          >
        </div>
      </aside>
      <section v-if="batch" class="scan-content" :class="{ 'files-collapsed': !showFilePanel }">
        <div id="scan-file-panel" v-show="showFilePanel" class="scan-file-panel">
          <h2>{{ batch.title }}</h2>
          <p class="muted">
            PDF 上限 {{ limits.max_file_bytes / 1024 / 1024 }} MiB、{{ limits.max_pages }}
            页。每批最多 20 个文件，不支持断点续传。
          </p>
          <div class="scan-actions">
            <label
              >扫描 PDF<input
                type="file"
                accept="application/pdf,.pdf"
                :disabled="busy"
                @change="selectFile" /></label
            ><el-button type="primary" :loading="busy" :disabled="!file || busy" @click="upload">{{
              pending ? '重试原上传' : '上传并生成预览'
            }}</el-button
            ><el-button :disabled="busy" @click="refreshFiles">刷新文件状态</el-button>
          </div>
          <ul class="scan-files">
            <li v-for="f in files" :key="f.id">
              <button :disabled="busy" @click="showFile(f)">{{ f.original_filename }}</button
              ><span>{{ stateLabel(f.status) }} · {{ f.page_count ?? '—' }} 页 · 待身份核验</span>
              <div v-if="f.failure_code" class="scan-alert">
                接收未完成，请保留原文件重试。
                <details>
                  <summary>查看接收诊断</summary>
                  {{ f.failure_code }}
                </details>
              </div>
            </li>
          </ul>
        </div>
        <div class="scan-preview-column">
          <div class="scan-preview-heading">
            <h3>{{ activeFile?.original_filename || '原卷预览' }}</h3>
            <button
              type="button"
              aria-controls="scan-file-panel"
              :aria-expanded="showFilePanel"
              @click="showFilePanel = !showFilePanel"
            >
              {{ showFilePanel ? '收起上传与文件' : '展开上传与文件' }}
            </button>
          </div>
          <div v-if="activeFile" class="scan-preview">
            <div class="scan-actions">
              <el-button :disabled="busy || activeFile.status !== 'stored'" @click="original"
                >下载原始 PDF</el-button
              >
            </div>
            <details class="scan-fingerprint">
              <summary>查看文件校验信息</summary>
              SHA-256：{{ activeFile.sha256 }}
            </details>
            <p v-if="activeFile.status !== 'stored'" class="muted">
              仅成功接收的文件可以预览。处理中断后，原请求在租约到期后可重试。
            </p>
            <nav class="scan-page-buttons" aria-label="扫描页选择">
              <button
                v-for="p in pages"
                :key="p.id"
                :class="{ chosen: activePage?.id === p.id }"
                @click="showPage(p)"
              >
                第 {{ p.page_index + 1 }} 页
              </button>
            </nav>
            <div v-if="pages.length" class="scan-actions">
              <el-button :disabled="pageOffset === 0" @click="paginatePages(-20)"
                >上一组页面</el-button
              ><el-button :disabled="!morePages" @click="paginatePages(20)">下一组页面</el-button>
            </div>
            <p v-if="activePage" class="scan-notice">
              第 {{ activePage.page_index + 1 }} 页 · 待身份核验
              <a v-if="image" :href="image" target="_blank" rel="noopener noreferrer">放大查看</a>
            </p>
            <div v-if="image && activePage" class="scan-zoom">
              <button
                aria-label="缩小扫描页"
                :disabled="previewZoom <= 60"
                @click="previewZoom -= 20"
              >
                −
              </button>
              <span>{{ previewZoom }}%</span>
              <button
                aria-label="放大扫描页"
                :disabled="previewZoom >= 200"
                @click="previewZoom += 20"
              >
                ＋
              </button>
            </div>
            <div v-if="image && activePage" class="scan-image-scroll">
              <img
                :src="image"
                :alt="`第${activePage.page_index + 1}页扫描预览`"
                :style="{ width: previewZoom + '%' }"
              />
            </div>
          </div>
          <div v-else class="scan-preview scan-preview-empty">
            选择已接收的文件，在这里查看扫描页面。
          </div>
        </div>
      </section>
      <section v-else class="scan-content muted">选择已有批次，或新建批次开始接收。</section>
    </div>
  </PageState>
</template>
<style scoped>
.scan-controls,
.scan-content {
  background: white;
  padding: 24px;
  border: 1px solid #dfe5ef;
  border-radius: 12px;
}
.scan-controls {
  margin-bottom: 24px;
}
.scan-controls label,
.scan-content label {
  display: grid;
  gap: 8px;
  font-size: 15px;
}
.scan-controls input,
.scan-controls select {
  min-width: 260px;
  padding: 10px;
  border: 1px solid #bcc8da;
  border-radius: 7px;
  background: white;
  font: inherit;
}
.scan-actions {
  display: flex;
  align-items: end;
  gap: 12px;
  flex-wrap: wrap;
  margin: 16px 0;
}
.scan-workspace {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 24px;
}
.scan-content {
  min-width: 0;
}
.scan-content:has(.scan-file-panel) {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(235px, 0.8fr);
  grid-template-areas: 'preview files';
  gap: 18px;
  align-items: start;
}
.scan-file-panel {
  grid-area: files;
  min-width: 0;
}
.scan-file-panel input[type='file'] {
  width: 100%;
  max-width: 100%;
}
.scan-content.files-collapsed {
  grid-template-columns: minmax(0, 1fr);
  grid-template-areas: 'preview';
}
.scan-preview-column {
  grid-area: preview;
  min-width: 0;
}
.scan-preview-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}
.scan-preview-heading h3 {
  margin: 0;
  overflow-wrap: anywhere;
}
.scan-batches h2,
.scan-content h2 {
  font-size: 20px;
  margin: 0 0 16px;
}
.scan-batches button {
  width: 100%;
  text-align: left;
  padding: 16px;
  margin-bottom: 10px;
}
button:not(.el-button) {
  font: inherit;
  cursor: pointer;
  border: 1px solid #c5cfdf;
  border-radius: 7px;
  background: white;
  padding: 8px 12px;
}
button:not(.el-button):disabled {
  opacity: 0.5;
  cursor: default;
}
button.chosen {
  border-color: #2563eb;
  background: #eff6ff;
  color: #1849a6;
}
small {
  display: block;
  color: #667085;
  margin-top: 8px;
}
.scan-files {
  list-style: none;
  padding: 0;
}
.scan-files li {
  border-bottom: 1px solid #e5e9ef;
  padding: 16px 0;
}
.scan-files span {
  display: block;
  margin-top: 8px;
  color: #536176;
}
.scan-alert {
  color: #ad2632;
  background: #fff0f1;
  padding: 12px;
  border-radius: 7px;
}
.scan-notice {
  color: #166953;
  background: #eefaf5;
  padding: 12px;
  border-radius: 7px;
}
.scan-preview {
  margin-top: 0;
  border: 1px solid #e3e9f0;
  border-radius: 8px;
  padding: 16px;
  background: #f8fafc;
}
.scan-preview-empty {
  min-height: 300px;
  display: grid;
  place-items: center;
  text-align: center;
  color: #667a92;
}
.scan-zoom {
  display: flex;
  gap: 8px;
  align-items: center;
}
.scan-image-scroll {
  max-height: 65vh;
  overflow: auto;
  background: #e9eef4;
}
.scan-preview img {
  display: block;
  max-width: none;
  height: auto;
  margin: auto;
  border: 1px solid #dfe5ef;
}
.scan-fingerprint {
  font-family: monospace;
  overflow-wrap: anywhere;
  font-size: 13px;
  color: #5a687c;
}
.scan-page-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
@media (max-width: 1000px) {
  .scan-workspace {
    grid-template-columns: 1fr;
  }
  .scan-controls select {
    min-width: 0;
    width: 100%;
  }
  .scan-content:has(.scan-file-panel) {
    grid-template-columns: 1fr;
    grid-template-areas: 'files' 'preview';
  }
  .scan-content.files-collapsed {
    grid-template-areas: 'preview';
  }
}
</style>
