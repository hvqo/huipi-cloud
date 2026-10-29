<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, ApiError, id, request, type Schema } from '../api/client'
import { useLoad, date, status } from '../lib/load'
import PageState from '../components/PageState.vue'
import FilePreview from '../components/FilePreview.vue'
const route = useRoute(),
  aid = String(route.params.aid),
  sid = String(route.params.sid),
  state = useLoad()
const alignment = ref<Schema['AlignmentSummaryRead'] | null>(null)
const homework = ref<{student_name: string; student_code: string; version: number; is_current: boolean} | null>(null)
const preparing = ref(false), preparationError = ref('')
const submission = ref<Schema['SubmissionRead']>(),
  workbench = ref<Schema['AssignmentWorkbench']>(),
  canonical = ref<Schema['CanonicalDocumentStatusRead']>(),
  parsing = ref<Schema['ParsingTaskStatusRead']>(),
  offset = ref(0)
function load() {
  return state.run(async (signal) => {
    const [s, w, c, p, a, h] = await Promise.all([
      api.submission(sid, signal),
      api.workbench(aid, offset.value, signal),
      api.canonical(sid, signal),
      api.parsing(sid, signal),
      api.alignment(sid, signal).catch((error) => {
        if (error instanceof ApiError && error.status === 409) return null
        throw error
      }),
      request<{student_name: string; student_code: string; version: number; is_current: boolean}>(`/api/v1/homework/submissions/${id(sid)}`, { signal }).catch((error) => {
        if (error instanceof ApiError && error.status === 404) return null
        throw error
      }),
    ])
    if (s.assignment_id !== aid) throw new Error('提交与当前作业不匹配。')
    submission.value = s
    workbench.value = w
    canonical.value = c
    parsing.value = p
    alignment.value = a
    homework.value = h
  })
}
function paginate(_kind: string, delta: number) {
  offset.value += delta
  void load()
}
onMounted(load)
async function prepareHomework() {
  if (preparing.value) return
  preparing.value = true
  preparationError.value = ''
  try {
    await request(`/api/v1/homework/submissions/${id(sid)}/prepare`, { body: '{}', timeoutMs: 120000, errorCode: true })
    await load()
  } catch (error) {
    preparationError.value = error instanceof Error ? error.message : '作答准备未完成，请稍后重试。'
  } finally { preparing.value = false }
}
</script>
<template>
  <RouterLink class="back-link" :to="`/assignments/${aid}`">← 返回作业</RouterLink
  ><PageState :loading="state.loading.value" :error="state.error.value" @retry="load"
    ><template v-if="submission && workbench"
      ><div class="page-heading">
        <div>
          <p class="eyebrow">{{ workbench.assignment.title }}</p>
          <h1>提交 · {{ homework ? `${homework.student_name}（${homework.student_code}）` : submission.student_ref }}</h1>
          <p class="muted">提交于 {{ date(submission.submitted_at) }} · {{ homework ? `第 ${homework.version} 次 · ${homework.is_current ? '当前有效' : '历史版本'}` : '模拟学生标识' }}</p>
        </div>
        <FilePreview
          :sid="sid"
          :name="submission.file.original_filename"
          :type="submission.file.content_type"
        />
      </div>
      <div class="summary-grid">
        <section class="panel">
          <h3>原始资料</h3>
          <strong>{{ submission.file.original_filename }}</strong>
          <p class="muted">
            {{ (submission.file.size_bytes / 1024).toFixed(1) }} KiB ·
            {{ submission.file.content_type }}
          </p>
        </section>
        <section class="panel">
          <h3>解析进度</h3>
          <strong>{{ status(parsing?.status || 'pending') }}</strong>
          <p class="muted">已尝试 {{ parsing?.attempt_count || 0 }} 次</p>
        </section>
        <section class="panel">
          <h3>标准化文档</h3>
          <strong>{{ status(canonical?.status || 'not_generated') }}</strong>
          <p class="muted">{{ canonical?.canonical_document?.page_count || 0 }} 页来源</p>
        </section>
        <section class="panel">
          <h3>题目来源对齐</h3>
          <strong>{{ alignment ? status(alignment.status) : '尚未就绪' }}</strong>
          <p class="muted">{{ alignment?.aligned_count || 0 }} 题已关联 · 不表示已作答</p>
        </section>
      </div>
      <section class="panel">
        <div class="section-heading">
          <h2>逐题核对作答证据</h2>
          <span class="muted">人工判断，不是自动评分</span>
        </div>
        <p class="notice">题号关联只代表来源映射，不代表学生已作答。请进入题目核对原文。</p>
        <button v-if="homework?.is_current && parsing?.status === 'succeeded' && (!alignment || canonical?.status !== 'available')" :disabled="preparing" @click="prepareHomework">{{ preparing ? '正在准备作答…' : '准备作答核对' }}</button>
        <p v-if="preparationError" role="alert" class="error">{{ preparationError }}</p>
        <p v-if="homework && !homework.is_current" class="notice">这是历史提交，请从班级作业进入当前版本进行批改。</p>
        <div v-for="q in workbench.questions" :key="q.id" class="question-row">
          <div>
            <strong>第 {{ q.question_number }} 题</strong>
            <p class="muted">{{ q.stem }}</p>
          </div>
          <RouterLink
            v-if="
              canonical?.status === 'available' && alignment && alignment.status !== 'not_generated' && (!homework || homework.is_current)
            "
            class="action-link"
            :to="`/assignments/${aid}/submissions/${sid}/questions/${q.id}/review`"
            >核对作答 →</RouterLink
          ><span v-else class="muted">等待来源就绪</span>
        </div>
        <div class="pager">
          <el-button :disabled="!offset" @click="paginate('offset', -20)">上一页题目</el-button
          ><el-button :disabled="!workbench.has_next" @click="paginate('offset', 20)"
            >下一页题目</el-button
          >
        </div>
      </section></template
    ></PageState
  >
</template>
