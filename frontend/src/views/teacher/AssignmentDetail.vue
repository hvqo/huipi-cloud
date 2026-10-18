<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  ArrowLeft,
  ArrowRight,
  BookOpenText,
  ClipboardCheck,
  FileUp,
  ListChecks,
  Plus,
  Rocket,
  Users,
} from 'lucide-vue-next'
import { services, type S } from '../../services'
import {
  assignmentStatusLabels,
  formatRelative,
  label,
  parsingLabels,
  questionTypeLabels,
} from '../../lib/labels'
import EmptyState from '../../components/EmptyState.vue'
import PageState from '../../components/PageState.vue'

/**
 * 作业详情：题目与评分细则 / 学生提交 两个页签 + 上传答卷 + 发布前检查。
 */
const route = useRoute(),
  aid = String(route.params.aid)
const state = reactive({
  loading: false,
  error: '',
  detail: null as S['AssignmentDetail'] | null,
  can_write: false,
  submissions: [] as S['SubmissionRead'][],
  submissionsHasNext: false,
  submissionOffset: 0,
  questionEditable: false,
})
const tab = ref<'questions' | 'submissions'>('questions')
const studentFilter = ref(''),
  parsingFilter = ref('all')
const filteredSubmissions = computed(() =>
  state.submissions.filter(
    (s) =>
      (!studentFilter.value ||
        `${s.student_ref} ${s.file.original_filename}`
          .toLowerCase()
          .includes(studentFilter.value.trim().toLowerCase())) &&
      (parsingFilter.value === 'all' || s.parsing_task.status === parsingFilter.value),
  ),
)

const uploadOpen = ref(false)
const uploadFiles = ref<
  {
    file: File
    ref: string
    status: 'pending' | 'uploading' | 'done' | 'failed'
    message: string
  }[]
>([])
const publishOpen = ref(false)
const publishChecking = ref(false)
const publishCheck = ref<{
  ready: boolean
  items: { question_number: number; ok: boolean; issues: string[] }[]
} | null>(null)
const publishError = ref('')
const publishing = ref(false)
const notice = ref('')

const questions = computed(() =>
  (state.detail?.questions ?? []).slice().sort((a, b) => a.question_number - b.question_number),
)

async function load() {
  state.loading = true
  state.error = ''
  try {
    const svc = await services()
    const [detail, workbench, submissions] = await Promise.all([
      svc.assignments.detail(aid),
      svc.assignments.workbench(aid, 0),
      svc.submissions.list(aid, { limit: 12, offset: 0 }),
    ])
    state.detail = detail
    state.can_write = workbench.can_write
    state.questionEditable = workbench.can_write && detail.status === 'draft'
    state.submissions = submissions.records
    state.submissionsHasNext = submissions.has_next
  } catch (e) {
    state.error = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    state.loading = false
  }
}

async function loadSubmissions(offset: number) {
  const result = await (await services()).submissions.list(aid, { limit: 12, offset })
  state.submissions = result.records
  state.submissionsHasNext = result.has_next
  state.submissionOffset = offset
}

function pickFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const next = [...(input.files ?? [])].map((file, index) => ({
    file,
    ref: `STU-${String(Date.now()).slice(-3)}${String(index + 1).padStart(2, '0')}`,
    status: 'pending' as const,
    message: '',
  }))
  uploadFiles.value = [...uploadFiles.value, ...next]
  input.value = ''
}

async function uploadAll() {
  const pending = uploadFiles.value.filter((row) => row.status === 'pending')
  if (!pending.length) return
  const svc = await services()
  for (const row of pending) {
    if (!row.ref.trim()) {
      row.status = 'failed'
      row.message = '请填写学生标识'
      continue
    }
    row.status = 'uploading'
    try {
      await svc.submissions.create(aid, { student_ref: row.ref.trim(), file: row.file })
      row.status = 'done'
      row.message = '已提交，解析进行中'
    } catch (e) {
      row.status = 'failed'
      row.message = e instanceof Error ? e.message : '上传未完成。'
    }
  }
  await loadSubmissions(0)
  notice.value = `已上传 ${uploadFiles.value.filter((r) => r.status === 'done').length} 份答卷，解析完成后即可批阅。`
}

async function openPublishCheck() {
  publishOpen.value = true
  publishChecking.value = true
  publishError.value = ''
  publishCheck.value = null
  try {
    publishCheck.value = await (await services()).assignments.publishCheck(aid)
  } catch (e) {
    publishError.value = e instanceof Error ? e.message : '检查未完成。'
  } finally {
    publishChecking.value = false
  }
}

async function publish() {
  publishing.value = true
  publishError.value = ''
  try {
    await (await services()).assignments.publish(aid)
    publishOpen.value = false
    await load()
    notice.value = '作业已发布。学生提交与批阅流程照常进行；发布不代表成绩发布。'
  } catch (e) {
    publishError.value = e instanceof Error ? e.message : '发布未完成。'
  } finally {
    publishing.value = false
  }
}

function parseBadgeClass(status: string) {
  if (status === 'succeeded') return 'hp-badge-success'
  if (status === 'failed') return 'hp-badge-danger'
  if (status === 'running' || status === 'pending') return 'hp-badge-primary'
  return 'hp-badge-warning'
}

let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  void load()
  timer = setInterval(async () => {
    if (
      state.submissions.some((s) =>
        ['pending', 'running', 'retry_wait'].includes(s.parsing_task.status),
      )
    ) {
      try {
        await loadSubmissions(state.submissionOffset)
      } catch {
        notice.value = '提交状态刷新未完成，请重新加载。'
      }
    }
  }, 3000)
})
onBeforeUnmount(() => clearInterval(timer))
</script>
<template>
  <div class="hp-section-gap">
    <RouterLink class="hp-linkbtn" to="/assignments"
      ><ArrowLeft aria-hidden="true" style="width: 14px; height: 14px" />返回我的作业</RouterLink
    >
    <PageState :loading="state.loading" :error="state.error" @retry="load">
      <div v-if="state.detail" class="hp-section-gap">
        <div class="page-heading hp-card" style="padding: 18px 20px">
          <div class="hp-grow" style="min-width: 260px">
            <p class="hp-eyebrow">{{ state.detail.subject }} · {{ state.detail.grade_level }}</p>
            <h1 style="font-size: 21px; font-weight: 700; margin-top: 4px">
              {{ state.detail.title }}
            </h1>
            <p class="hp-pagehead-desc">{{ state.detail.description || '暂无作业说明' }}</p>
          </div>
          <div class="hp-pagehead-actions">
            <span
              class="hp-badge"
              :class="state.detail.status === 'published' ? 'hp-badge-success' : ''"
              style="height: 28px; font-size: 13px"
            >
              <span v-if="state.detail.status === 'published'" class="dot" />
              {{ assignmentStatusLabels[state.detail.status] }}
            </span>
            <button
              v-if="state.detail.status === 'draft' && state.can_write"
              class="hp-btn hp-btn-primary"
              type="button"
              @click="openPublishCheck"
            >
              <Rocket aria-hidden="true" />发布作业
            </button>
            <button
              class="hp-btn hp-btn-secondary"
              type="button"
              :disabled="!state.can_write || state.detail.status !== 'published'"
              @click="uploadOpen = true"
            >
              <FileUp aria-hidden="true" />上传答卷
            </button>
          </div>
        </div>

        <div v-if="!state.can_write" class="hp-note hp-note-info" role="note">
          <ListChecks aria-hidden="true" />
          <span><b>只读授权：</b>当前账号对该作业只有查看权限，不能保存作答核对或修改题目。</span>
        </div>
        <div v-if="notice" class="hp-note hp-note-success" role="status">
          <ClipboardCheck aria-hidden="true" /><span>{{ notice }}</span>
        </div>

        <div class="hp-stats">
          <div class="hp-stat">
            <div class="hp-stat-icon"><ListChecks aria-hidden="true" /></div>
            <div>
              <div class="hp-stat-num">{{ questions.length }}<small>题</small></div>
              <div class="hp-stat-label">题目总数</div>
            </div>
          </div>
          <div class="hp-stat">
            <div class="hp-stat-icon is-teal"><Users aria-hidden="true" /></div>
            <div>
              <div class="hp-stat-num">{{ state.submissions.length }}<small>份</small></div>
              <div class="hp-stat-label">学生提交（当前页）</div>
            </div>
          </div>
        </div>

        <div class="hp-tabs" role="tablist" aria-label="作业内容">
          <button
            class="hp-tab"
            :class="{ 'is-active': tab === 'questions' }"
            role="tab"
            :aria-selected="tab === 'questions'"
            type="button"
            @click="tab = 'questions'"
          >
            <BookOpenText aria-hidden="true" />题目与评分细则
          </button>
          <button
            class="hp-tab"
            :class="{ 'is-active': tab === 'submissions' }"
            role="tab"
            :aria-selected="tab === 'submissions'"
            type="button"
            @click="tab = 'submissions'"
          >
            <Users aria-hidden="true" />学生提交
          </button>
        </div>

        <section v-if="tab === 'questions'" class="hp-section-gap">
          <EmptyState
            v-if="!questions.length"
            title="还没有题目"
            desc="先添加题目和评分细则，再上传学生答卷。发布前系统会逐题检查评分细则与标准答案是否齐备。"
          >
            <RouterLink
              v-if="state.can_write"
              class="hp-btn hp-btn-primary"
              :to="`/assignments/${aid}/questions/new`"
              ><Plus aria-hidden="true" />新增题目</RouterLink
            >
          </EmptyState>
          <article v-for="question in questions" :key="question.id" class="hp-card">
            <div class="hp-card-head">
              <h2 class="hp-card-title">第 {{ question.question_number }} 题</h2>
              <span class="hp-badge">{{ label(questionTypeLabels, question.question_type) }}</span>
              <span class="hp-card-note">满分 {{ question.max_score }} 分</span>
              <RouterLink
                class="hp-card-side hp-linkbtn"
                :to="`/assignments/${aid}/questions/${question.id}`"
                >编辑题目与细则<ArrowRight aria-hidden="true" style="width: 13px; height: 13px"
              /></RouterLink>
            </div>
            <div class="hp-card-body">
              <p style="line-height: 1.9; white-space: pre-wrap">{{ question.stem }}</p>
              <div style="margin-top: 12px">
                <h3 style="font-size: 14px; font-weight: 700; margin-bottom: 8px">评分细则概要</h3>
                <ul
                  v-if="question.rubric_criteria.length"
                  style="display: flex; flex-direction: column; gap: 6px"
                >
                  <li
                    v-for="criterion in question.rubric_criteria"
                    :key="criterion.id"
                    class="hp-flex"
                    style="
                      justify-content: space-between;
                      border-bottom: 1px dashed var(--hp-border);
                      padding-bottom: 6px;
                    "
                  >
                    <span style="font-size: 13.5px">{{ criterion.description }}</span>
                    <span class="hp-mono num">{{ criterion.points }} 分</span>
                  </li>
                </ul>
                <p v-else class="hp-fine">尚未配置评分细则。发布前需要补齐。</p>
              </div>
              <details
                v-if="question.answer_key?.answer_content"
                class="hp-details"
                style="margin-top: 10px; border-top: none"
              >
                <summary>查看标准答案</summary>
                <p style="padding: 6px 4px; white-space: pre-wrap; font-size: 13.5px">
                  {{ question.answer_key.answer_content }}
                </p>
              </details>
            </div>
          </article>
          <RouterLink
            v-if="state.questionEditable && questions.length"
            class="hp-btn hp-btn-secondary"
            :to="`/assignments/${aid}/questions/new`"
            ><Plus aria-hidden="true" />新增题目</RouterLink
          >
        </section>

        <section v-else class="hp-card">
          <div class="hp-card-head">
            <h2 class="hp-card-title"><Users aria-hidden="true" />学生提交</h2>
            <span class="hp-card-note">按提交时间排序 · 点击进入作答核对</span>
            <button
              class="hp-card-side hp-btn hp-btn-secondary hp-btn-sm"
              type="button"
              :disabled="!state.can_write || state.detail.status !== 'published'"
              @click="uploadOpen = true"
            >
              <FileUp aria-hidden="true" />上传答卷
            </button>
          </div>
          <div class="hp-filterbar" style="padding: 14px 20px">
            <input
              v-model="studentFilter"
              class="hp-input"
              style="max-width: 280px"
              aria-label="搜索当前页学生提交"
              placeholder="搜索学生标识或文件名"
            /><select v-model="parsingFilter" class="hp-select" aria-label="解析状态筛选">
              <option value="all">全部解析状态</option>
              <option v-for="(text, status) in parsingLabels" :key="status" :value="status">
                {{ text }}
              </option></select
            ><span class="hp-fine">仅筛选当前加载页</span>
          </div>
          <div class="hp-table-wrap">
            <table v-if="filteredSubmissions.length" class="hp-table">
              <thead>
                <tr>
                  <th>学生</th>
                  <th>原始文件</th>
                  <th>解析状态</th>
                  <th>提交时间</th>
                  <th style="text-align: right">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="submission in filteredSubmissions" :key="submission.id">
                  <td>
                    <RouterLink
                      class="hp-cell-title hp-linkbtn"
                      :to="`/assignments/${aid}/submissions/${submission.id}`"
                      >{{ submission.student_ref }}</RouterLink
                    >
                    <div class="hp-cell-sub">学生标识</div>
                  </td>
                  <td>{{ submission.file.original_filename }}</td>
                  <td>
                    <span class="hp-badge" :class="parseBadgeClass(submission.parsing_task.status)">
                      <span
                        class="dot"
                        :class="{ 'hp-dot-pulse': submission.parsing_task.status === 'running' }"
                      />
                      {{ label(parsingLabels, submission.parsing_task.status) }}
                    </span>
                  </td>
                  <td class="num">{{ formatRelative(submission.submitted_at) }}</td>
                  <td class="hp-cell-actions">
                    <RouterLink
                      class="hp-linkbtn"
                      :to="`/assignments/${aid}/submissions/${submission.id}`"
                      >核对作答<ArrowRight aria-hidden="true" style="width: 13px; height: 13px"
                    /></RouterLink>
                  </td>
                </tr>
              </tbody>
            </table>
            <EmptyState
              v-else
              title="暂无学生提交"
              desc="上传学生答卷（PDF / PNG / JPG）后，系统会自动解析并生成作答内容。"
            />
          </div>
          <PaginationBar
            :offset="state.submissionOffset"
            :limit="12"
            :has-next="state.submissionsHasNext"
            @change="loadSubmissions"
          />
        </section>
      </div>
    </PageState>

    <el-dialog v-model="uploadOpen" title="上传学生答卷" width="620" :close-on-click-modal="false">
      <div class="hp-form">
        <label class="hp-field">
          <span class="hp-field-label"
            >选择文件 <small>（支持 PDF / PNG / JPG，可多选）</small></span
          >
          <input
            class="hp-input"
            type="file"
            multiple
            accept=".pdf,.png,.jpg,.jpeg"
            @change="pickFiles"
          />
        </label>
        <p class="hp-fine">
          每份文件对应一名学生；学生标识用于区分提交（当前为本地标识，不代表学生认证身份）。上传成功后登记待解析任务，解析服务运行时会自动处理；未运行时保持等待状态。
        </p>
        <div v-if="uploadFiles.length" style="display: flex; flex-direction: column; gap: 8px">
          <div
            v-for="(row, index) in uploadFiles"
            :key="index"
            class="hp-flex"
            style="border: 1px solid var(--hp-border); border-radius: 10px; padding: 8px 12px"
          >
            <span class="hp-grow hp-cell-sub" style="min-width: 160px">{{ row.file.name }}</span>
            <input
              v-model="row.ref"
              class="hp-input"
              style="width: 150px; height: 32px"
              placeholder="学生标识"
              :disabled="row.status !== 'pending'"
            />
            <span
              class="hp-badge"
              :class="{
                'hp-badge-success': row.status === 'done',
                'hp-badge-danger': row.status === 'failed',
                'hp-badge-primary': row.status === 'uploading',
              }"
              style="height: 22px; font-size: 11px"
            >
              {{
                { pending: '待上传', uploading: '上传中', done: '已提交', failed: '失败' }[
                  row.status
                ]
              }}
            </span>
            <span v-if="row.message" class="hp-fine">{{ row.message }}</span>
          </div>
        </div>
      </div>
      <template #footer>
        <button class="hp-btn hp-btn-secondary" type="button" @click="uploadOpen = false">
          关闭
        </button>
        <button
          class="hp-btn hp-btn-primary"
          type="button"
          :disabled="!uploadFiles.some((row) => row.status === 'pending')"
          @click="uploadAll"
        >
          开始上传
        </button>
      </template>
    </el-dialog>

    <el-dialog v-model="publishOpen" title="发布前检查" width="520" :close-on-click-modal="false">
      <div v-if="publishChecking" class="hp-state-card"><span class="hp-spin" />正在检查…</div>
      <template v-else-if="publishCheck">
        <p class="hp-fine" style="margin-bottom: 10px">
          逐题检查评分细则与标准答案是否齐备。发布后作业对学生提交开放；发布不代表发布成绩。
        </p>
        <div style="display: flex; flex-direction: column; gap: 8px">
          <div
            v-for="item in publishCheck.items"
            :key="item.question_number"
            class="hp-flex"
            style="
              justify-content: space-between;
              border-bottom: 1px dashed var(--hp-border);
              padding-bottom: 6px;
            "
          >
            <span style="font-size: 13.5px">第 {{ item.question_number }} 题</span>
            <span
              v-if="item.ok"
              class="hp-badge hp-badge-success"
              style="height: 22px; font-size: 11px"
              >就绪</span
            >
            <span v-else class="hp-badge hp-badge-warning" style="height: 22px; font-size: 11px">{{
              item.issues.join('、')
            }}</span>
          </div>
        </div>
        <p
          v-if="!publishCheck.ready"
          class="hp-note hp-note-warning"
          style="margin-top: 12px"
          role="note"
        >
          尚有题目未就绪，发布前请补齐评分细则与标准答案。
        </p>
      </template>
      <p v-if="publishError" role="alert" class="hp-error-text">{{ publishError }}</p>
      <template #footer>
        <button class="hp-btn hp-btn-secondary" type="button" @click="publishOpen = false">
          取消
        </button>
        <button
          class="hp-btn hp-btn-primary"
          type="button"
          :disabled="!publishCheck?.ready || publishing"
          @click="publish"
        >
          确认发布
        </button>
      </template>
    </el-dialog>
  </div>
</template>
