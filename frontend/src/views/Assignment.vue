<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, type Schema } from '../api/client'
import { useLoad, date, status } from '../lib/load'
import PageState from '../components/PageState.vue'
const aid = String(useRoute().params.aid),
  state = useLoad(),
  view = ref<Schema['AssignmentWorkbench']>(),
  submissions = ref<Schema['SubmissionRead'][]>([]),
  tab = ref('questions'),
  qOffset = ref(0),
  sOffset = ref(0)
function load() {
  return state.run(async (signal) => {
    const result = await Promise.all([
      api.workbench(aid, qOffset.value, signal),
      api.submissions(aid, sOffset.value, signal),
    ])
    view.value = result[0]
    submissions.value = result[1]
  })
}
function paginate(kind: string, delta: number) {
  if (kind === 'qOffset') qOffset.value += delta
  else sOffset.value += delta
  void load()
}
onMounted(load)
</script>
<template>
  <RouterLink class="back-link" to="/assignments">← 返回我的作业</RouterLink
  ><PageState :loading="state.loading.value" :error="state.error.value" @retry="load"
    ><template v-if="view"
      ><div class="page-heading">
        <div>
          <p class="eyebrow">{{ view.assignment.subject }} · {{ view.assignment.grade_level }}</p>
          <h1>{{ view.assignment.title }}</h1>
          <p class="muted">{{ view.assignment.description || '暂无作业说明' }}</p>
        </div>
        <span class="pill" :class="view.assignment.status">{{
          status(view.assignment.status)
        }}</span>
      </div>
      <div v-if="!view.can_write" class="notice">
        当前作业为只读授权，可以查看内容，不能保存复核。
      </div>
      <div class="tabs" role="tablist" aria-label="作业内容">
        <button role="tab" :aria-selected="tab === 'questions'" @click="tab = 'questions'">
          题目与评分细则</button
        ><button role="tab" :aria-selected="tab === 'submissions'" @click="tab = 'submissions'">
          学生提交
        </button>
      </div>
      <section v-if="tab === 'questions'">
        <PageState :empty="!view.questions.length"
          ><article v-for="q in view.questions" :key="q.id" class="panel question-card">
            <div class="section-heading">
              <h2>第 {{ q.question_number }} 题</h2>
              <span class="muted">满分 {{ q.max_score }}</span>
            </div>
            <p class="stem">{{ q.stem }}</p>
            <div class="rubric">
              <h3>评分细则概要</h3>
              <ul>
                <li v-for="r in q.rubric_criteria" :key="r.id">
                  <span>{{ r.description }}</span
                  ><strong>{{ r.points }} 分</strong>
                </li>
              </ul>
              <p v-if="q.rubric_truncated" class="notice">
                共 {{ q.rubric_count }} 项，此处仅显示前 5 项概要。
              </p>
              <p v-if="!q.rubric_count" class="muted">尚未配置评分细则。</p>
            </div>
          </article></PageState
        >
        <div class="pager">
          <el-button :disabled="!qOffset" @click="paginate('qOffset', -20)">上一页题目</el-button
          ><el-button :disabled="!view.has_next" @click="paginate('qOffset', 20)"
            >下一页题目</el-button
          >
        </div>
      </section>
      <section v-else class="panel">
        <div class="section-heading">
          <h2>学生提交</h2>
          <span class="muted">按提交时间分页显示</span>
        </div>
        <PageState :empty="!submissions.length"
          ><table>
            <thead>
              <tr>
                <th>提交标识</th>
                <th>原始文件</th>
                <th>解析状态</th>
                <th>提交时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in submissions" :key="s.id">
                <td>
                  <strong>{{ s.student_ref }}</strong
                  ><small class="muted block">模拟标识，不是认证身份</small>
                </td>
                <td>{{ s.file.original_filename }}</td>
                <td>
                  <span class="pill">{{ status(s.parsing_task.status) }}</span>
                </td>
                <td>{{ date(s.submitted_at) }}</td>
                <td>
                  <RouterLink class="action-link" :to="`/assignments/${aid}/submissions/${s.id}`"
                    >查看提交 →</RouterLink
                  >
                </td>
              </tr>
            </tbody>
          </table></PageState
        >
        <div class="pager">
          <el-button :disabled="!sOffset" @click="paginate('sOffset', -12)">上一页提交</el-button
          ><el-button :disabled="submissions.length < 12" @click="paginate('sOffset', 12)"
            >下一页提交</el-button
          >
        </div>
      </section></template
    ></PageState
  >
</template>
