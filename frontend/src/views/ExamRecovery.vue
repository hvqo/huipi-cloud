<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { recovery, exceptionNames, type RecoveryItem, type RecoveryView } from '../api/recovery'
import { scans } from '../api/scans'
import { ApiError } from '../api/client'
const resultNames: Record<string, string> = {
  stored_files: '已接收文件',
  confirmed_complete_scripts: '已确认完整答卷',
  parsed_scripts: '当前解析成功答卷',
}
const route = useRoute()
const sessions = ref<Awaited<ReturnType<typeof recovery.sessions>>>(),
  sid = ref(''),
  sessionOffset = ref(0)
const data = ref<RecoveryView>(),
  kind = ref(''),
  offset = ref(0),
  busy = ref(false),
  error = ref(''),
  notice = ref('')
const preview = ref(''),
  pending = ref<{ item: RecoveryItem; file: File }>()
let disposed = false,
  timer: ReturnType<typeof setInterval> | undefined
function clearPreview() {
  if (preview.value) URL.revokeObjectURL(preview.value)
  preview.value = ''
}
async function refresh() {
  if (!sid.value || disposed) return
  const selected = sid.value
  const value = await recovery.inspect(selected, offset.value, kind.value)
  if (!disposed && selected === sid.value) data.value = value
}
async function run(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await fn()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '操作未完成'
  } finally {
    busy.value = false
  }
}
async function change() {
  offset.value = 0
  data.value = undefined
  clearPreview()
  await refresh()
}
async function loadSessions() {
  sessions.value = await recovery.sessions(sessionOffset.value)
  sid.value ||= String(route.query.session || sessions.value.items[0]?.id || '')
  await refresh()
}
function scriptLink(item: RecoveryItem) {
  return `/scans/sessions/${sid.value}/scripts?script=${item.script_id || ''}`
}
function scanLink(item?: RecoveryItem) {
  return `/scans?session=${sid.value}${item?.batch_id ? `&batch=${item.batch_id}` : ''}${item?.file_id ? `&file=${item.file_id}` : ''}`
}
async function show(item: RecoveryItem) {
  if (!item.page_id) return
  clearPreview()
  preview.value = URL.createObjectURL(await scans.preview(item.page_id))
}
async function original(item: RecoveryItem) {
  if (!item.file_id) return
  const blob = await scans.original(item.file_id),
    url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = item.original_filename || '扫描原件.pdf'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function uploadAgain() {
  const q = pending.value
  if (!q?.item.retry_request_id || !q.item.batch_id) return
  try {
    await scans.upload(q.item.batch_id, q.item.retry_request_id, q.file)
    pending.value = undefined
    notice.value = '重传已完成，页面仍待人工分卷及身份确认。'
    await refresh()
  } catch (e) {
    if (e instanceof ApiError && !e.uncertain) pending.value = undefined
    throw e
  }
}
async function chooseFile(event: Event, item: RecoveryItem) {
  const input = event.target as HTMLInputElement,
    file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.name !== item.original_filename || file.size > 32 * 1024 ** 2 || !file.size) {
    error.value = '重传须选择原文件名及相同内容的PDF，且不超过32 MiB；内容不同请新建补扫批次。'
    return
  }
  pending.value = { item, file }
  await run(uploadAgain)
}
async function autoRefresh() {
  if (!busy.value && !pending.value && !document.hidden) await run(refresh)
}
onMounted(() => {
  void run(loadSessions)
  timer = setInterval(autoRefresh, 20000)
  window.addEventListener('focus', autoRefresh)
})
onUnmounted(() => {
  disposed = true
  clearInterval(timer)
  pending.value = undefined
  clearPreview()
  window.removeEventListener('focus', autoRefresh)
})
</script>
<template>
  <header class="page-heading">
    <div>
      <p class="eyebrow">考务工作空间 · 扫描补救</p>
      <h1>异常处理</h1>
      <p class="muted">
        定位待处理答卷，核对原页并恢复流程。缺页、身份不明或解析失败，不代表缺考和零分。
      </p>
    </div>
  </header>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="busy" role="status">正在读取或处理…</p>
  <aside v-if="pending" role="alert">
    上传结果尚未确认，请保留当前页面。<el-button :disabled="busy" @click="run(uploadAgain)"
      >使用原请求重试上传</el-button
    >
  </aside>
  <section class="recovery-controls">
    <label
      >考试与科目场次<select v-model="sid" :disabled="busy || !!pending" @change="run(change)">
        <option value="">选择有权场次</option>
        <option v-for="s in sessions?.items" :key="s.id" :value="s.id">
          {{ s.event_title }} · {{ s.title }}
        </option>
        <option v-if="sid && !sessions?.items.some((s) => s.id === sid) && data" :value="sid">
          {{ data.event_title }} · {{ data.session_title }}
        </option>
      </select></label
    >
    <el-button :disabled="busy || !!pending" @click="run(refresh)">刷新异常</el-button>
    <RouterLink v-if="sid" :to="scanLink()">新建补扫批次 →</RouterLink>
    <RouterLink v-if="sid" :to="`/scans/sessions/${sid}/scripts`">人工分卷 →</RouterLink>
    <RouterLink v-if="sid" :to="`/marking/manage?session=${sid}`">送回阅卷流程 →</RouterLink>
  </section>
  <div class="recovery-controls" v-if="sessions">
    <el-button
      :disabled="busy || sessionOffset === 0"
      @click="
        run(async () => {
          sessionOffset -= 20
          sid = ''
          await loadSessions()
        })
      "
      >上一组场次</el-button
    >
    <el-button
      :disabled="busy || !sessions.has_next"
      @click="
        run(async () => {
          sessionOffset += 20
          sid = ''
          await loadSessions()
        })
      "
      >下一组场次</el-button
    >
  </div>
  <p v-if="sessions && !sessions.items.length && !data && !busy">
    暂无可访问的扫描或阅卷场次，请联系考务负责人。
  </p>
  <template v-if="data">
    <section class="recovery-summary" aria-label="异常统计">
      <div>
        <strong>{{ Object.values(data.counts).reduce((a, b) => a + b, 0) }}</strong
        ><span>当前有权范围内待处理事项</span>
      </div>
      <div v-for="(v, k) in data.results" :key="k">
        <strong>{{ v }}</strong
        ><span>{{ resultNames[k] || k }}</span>
      </div>
    </section>
    <p class="muted">
      一份答卷可能有多个待办；这里只汇总当前权限可见状态，不是已到考人数或正式成绩。疑似重复页须人工核验。
    </p>
    <div class="recovery-controls">
      <label
        >异常类型<select
          v-model="kind"
          :disabled="busy || !!pending"
          @change="
            run(async () => {
              offset = 0
              await refresh()
            })
          "
        >
          <option value="">全部类型</option>
          <option v-for="(n, k) in exceptionNames" :key="k" :value="k">
            {{ n }}（{{ data.counts[k] || 0 }}）
          </option>
        </select></label
      ><span>{{ data.total }}项 · 每页25项</span>
    </div>
    <p v-if="!data.items.length && !busy" role="status">
      当前筛选下没有待处理事项。不代表已经完成全部阅卷。
    </p>
    <div class="recovery-table" v-else>
      <table aria-label="异常列表">
        <thead>
          <tr>
            <th>异常与对象</th>
            <th>状态及原因</th>
            <th>处理操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in data.items"
            :key="item.kind + item.resource_id"
            :data-kind="item.kind"
            :data-resource="item.resource_id"
          >
            <td>
              <strong>{{ item.title }}</strong
              ><small>{{ data.event_title }} · {{ data.session_title }}</small>
            </td>
            <td>
              <span class="state">{{
                item.state === 'manual_review' ? '待人工核验' : '待处理'
              }}</span>
              <p>{{ item.reason }}</p>
            </td>
            <td>
              <div class="row-actions">
                <el-button
                  v-if="item.page_id"
                  :disabled="busy || !!pending"
                  @click="run(() => show(item))"
                  >查看原页</el-button
                >
                <el-button
                  v-if="item.file_id && item.page_id"
                  :disabled="busy || !!pending"
                  @click="run(() => original(item))"
                  >下载来源PDF</el-button
                >
                <RouterLink v-if="item.file_id" :to="scanLink(item)">定位来源文件</RouterLink>
                <RouterLink
                  v-if="item.script_id && data.capabilities.scripts"
                  :to="scriptLink(item)"
                  >{{
                    item.kind.startsWith('parsing_') ? '核对答卷与重新解析' : '补页、调序或重新确认'
                  }}</RouterLink
                >
                <RouterLink
                  v-if="item.kind === 'task_invalidated' && data.capabilities.manage_tasks"
                  :to="`/marking/progress?session=${sid}`"
                  >调整失效任务</RouterLink
                >
                <RouterLink
                  v-if="item.task_id && !data.capabilities.grading && data.capabilities.own_tasks"
                  :to="`/marking/my/${item.task_id}`"
                  >打开我的任务</RouterLink
                >
                <label class="retry-file" v-if="item.retry_request_id && !pending"
                  >重传原PDF<input
                    type="file"
                    accept="application/pdf,.pdf"
                    :disabled="busy"
                    @change="chooseFile($event, item)"
                /></label>
                <small v-if="item.kind.startsWith('upload_') && !item.retry_request_id"
                  >原上传人可重试同一文件；其他操作员请新建补扫批次。</small
                >
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="recovery-controls">
      <el-button
        :disabled="busy || !!pending || offset === 0"
        @click="
          run(async () => {
            offset -= 25
            await refresh()
          })
        "
        >上一页异常</el-button
      ><el-button
        :disabled="busy || !!pending || !data.has_next"
        @click="
          run(async () => {
            offset += 25
            await refresh()
          })
        "
        >下一页异常</el-button
      ><small
        >更新于
        {{
          new Date(data.generated_at).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai' })
        }}</small
      >
    </div>
  </template>
  <section v-if="preview" aria-label="异常原页预览">
    <el-button @click="clearPreview">关闭预览</el-button
    ><img class="recovery-preview" :src="preview" alt="异常扫描原页" />
  </section>
</template>
<style scoped>
.recovery-controls {
  display: flex;
  gap: 18px;
  align-items: center;
  flex-wrap: wrap;
  margin: 20px 0;
}
select {
  padding: 10px;
  margin-left: 10px;
  min-width: 200px;
  border: 1px solid #ccd9e1;
  border-radius: 5px;
  font: inherit;
  background: white;
}
.recovery-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 40px;
  padding: 24px 0;
  border-block: 1px solid #dce5eb;
}
.recovery-summary strong {
  display: block;
  font-size: 28px;
  color: #234957;
}
.recovery-summary span,
small {
  display: block;
  color: #65798a;
  font-size: 13px;
  margin-top: 7px;
}
.recovery-table {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: white;
}
th,
td {
  padding: 18px;
  border-bottom: 1px solid #dce5eb;
  text-align: left;
  vertical-align: top;
}
th {
  background: #edf3f7;
}
td:first-child {
  width: 30%;
}
td p {
  margin: 10px 0;
  line-height: 1.7;
}
.state {
  color: #98641d;
  font-weight: 600;
}
.row-actions {
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: flex-start;
}
.row-actions .el-button {
  margin: 0;
}
.recovery-preview {
  display: block;
  width: min(100%, 800px);
  margin: 20px auto;
}
.retry-file {
  color: #22617c;
  font-weight: 600;
}
.retry-file input {
  display: block;
  max-width: 230px;
  margin-top: 8px;
}
</style>
