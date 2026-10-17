<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ArrowRight,
  BookOpen,
  CheckCircle2,
  ClipboardList,
  FileWarning,
  Hourglass,
  Inbox,
  Plus,
  Upload,
} from 'lucide-vue-next'
import { services, type WorkbenchDigest } from '../../services'
import { UnsupportedCapabilityError } from '../../services/types'
import { session } from '../../app/session'
import { formatRelative, label, parsingLabels } from '../../lib/labels'
import EmptyState from '../../components/EmptyState.vue'
import PageState from '../../components/PageState.vue'

/**
 * 教师工作台首页：突出“下一步该做什么”。
 * 待办任务、最新提交与统计全部来自业务服务；真实模式无聚合接口的能力
 * 不会伪造数据，口径说明如实展示（digest.scope_note）。
 */
const digest = ref<WorkbenchDigest | null>(null)
const loading = ref(false)
const error = ref('')
const unsupported = ref(false)

const hour = new Date().getHours()
const greeting = computed(
  () =>
    `${hour < 6 ? '凌晨好' : hour < 12 ? '上午好' : hour < 18 ? '下午好' : '晚上好'}，${session.name || '老师'}`,
)
const dateLine = new Date().toLocaleDateString('zh-CN', {
  month: 'long',
  day: 'numeric',
  weekday: 'long',
})

async function load() {
  loading.value = true
  error.value = ''
  unsupported.value = false
  try {
    digest.value = await (await services()).reviews.digest()
  } catch (e) {
    if (e instanceof UnsupportedCapabilityError) unsupported.value = true
    else error.value = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    loading.value = false
  }
}

const taskMeta: Record<string, { label: string; badge: 'primary' | 'violet' | 'warning' }> = {
  review: { label: '作答核对', badge: 'primary' },
  grading: { label: '候选评分审核', badge: 'violet' },
  parse_alert: { label: '解析异常', badge: 'warning' },
}

function parseBadgeClass(status: string) {
  if (status === 'succeeded') return 'hp-badge-success'
  if (status === 'failed') return 'hp-badge-danger'
  if (status === 'running' || status === 'pending') return 'hp-badge-primary'
  return 'hp-badge-warning'
}

onMounted(load)
</script>
<template>
  <div class="hp-section-gap">
    <section class="hp-hero">
      <div>
        <h1>{{ greeting }}</h1>
        <p class="hp-hero-date">
          {{ dateLine
          }}<template v-if="digest">
            · 近 7 天有 {{ digest.submissions_recent }} 份新提交<template
              v-if="digest.parsing_active"
            >
              ，{{ digest.parsing_active }} 份解析中</template
            ></template
          >
        </p>
      </div>
      <div class="hp-hero-actions">
        <RouterLink class="hp-btn hp-btn-primary hp-btn-lg" to="/assignments?create=1"
          ><Plus aria-hidden="true" />新建作业</RouterLink
        >
        <RouterLink class="hp-btn hp-btn-secondary hp-btn-lg" to="/assignments"
          ><Upload aria-hidden="true" />上传答卷</RouterLink
        >
        <RouterLink
          v-if="digest?.tasks.length"
          class="hp-btn hp-btn-secondary hp-btn-lg"
          :to="digest.tasks[0]!.href"
          >继续批阅<ArrowRight aria-hidden="true"
        /></RouterLink>
      </div>
    </section>

    <PageState :loading="loading" :error="error" @retry="load">
      <div class="hp-section-gap">
        <template v-if="digest">
          <div class="hp-stats">
            <div class="hp-stat">
              <div class="hp-stat-icon"><BookOpen aria-hidden="true" /></div>
              <div>
                <div class="hp-stat-num">{{ digest.assignments_total }}<small>个</small></div>
                <div class="hp-stat-label">我的作业</div>
              </div>
            </div>
            <div class="hp-stat">
              <div class="hp-stat-icon is-teal"><CheckCircle2 aria-hidden="true" /></div>
              <div>
                <div class="hp-stat-num">{{ digest.assignments_published }}<small>个</small></div>
                <div class="hp-stat-label">已发布作业</div>
              </div>
            </div>
            <div class="hp-stat">
              <div class="hp-stat-icon is-amber"><Inbox aria-hidden="true" /></div>
              <div>
                <div class="hp-stat-num">{{ digest.submissions_recent }}<small>份</small></div>
                <div class="hp-stat-label">近 7 天新提交</div>
              </div>
            </div>
            <div v-if="digest.parse_alerts > 0" class="hp-stat">
              <div class="hp-stat-icon is-rose"><FileWarning aria-hidden="true" /></div>
              <div>
                <div class="hp-stat-num">{{ digest.parse_alerts }}<small>份</small></div>
                <div class="hp-stat-label">解析异常待处理</div>
              </div>
            </div>
          </div>
          <p class="hp-fine">{{ digest.scope_note }}</p>

          <section class="hp-card">
            <div class="hp-card-head">
              <h2 class="hp-card-title"><ClipboardList aria-hidden="true" />待办任务</h2>
              <span class="hp-card-note">按提交时间排序 · 点击卡片直接进入处理</span>
            </div>
            <div class="hp-card-body">
              <div v-if="digest.tasks.length" class="hp-task-grid">
                <RouterLink
                  v-for="task in digest.tasks"
                  :key="task.href + task.title"
                  :to="task.href"
                  class="hp-task-card"
                >
                  <div class="hp-task-top">
                    <span
                      class="hp-badge"
                      :class="{
                        'hp-badge-primary': task.kind === 'review',
                        'hp-badge-violet': task.kind === 'grading',
                        'hp-badge-warning': task.kind === 'parse_alert',
                      }"
                      >{{ taskMeta[task.kind]?.label ?? '任务' }}</span
                    >
                    <span class="hp-fine">{{ formatRelative(task.created_at) }}</span>
                  </div>
                  <div class="hp-task-title">{{ task.title }}</div>
                  <p class="hp-task-desc">{{ task.description }}</p>
                  <div class="hp-task-foot">
                    <span class="hp-linkbtn"
                      >进入处理<ArrowRight aria-hidden="true" style="width: 13px; height: 13px"
                    /></span>
                  </div>
                </RouterLink>
              </div>
              <EmptyState
                v-else
                title="暂无待办任务"
                desc="当前没有需要人工处理的核对或审核任务。有新的学生提交或候选评分时会出现在这里。"
              />
            </div>
          </section>

          <section class="hp-card">
            <div class="hp-card-head">
              <h2 class="hp-card-title"><Inbox aria-hidden="true" />最新提交</h2>
              <span class="hp-card-note">来自我有权限的作业</span>
              <RouterLink class="hp-card-side hp-linkbtn" to="/assignments"
                >前往我的作业<ArrowRight aria-hidden="true" style="width: 13px; height: 13px"
              /></RouterLink>
            </div>
            <div class="hp-table-wrap">
              <table v-if="digest.recent_submissions.length" class="hp-table">
                <thead>
                  <tr>
                    <th>学生</th>
                    <th style="width: 34%">所属作业</th>
                    <th>提交时间</th>
                    <th>解析状态</th>
                    <th style="text-align: right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in digest.recent_submissions" :key="row.submission.id">
                    <td>
                      <span class="hp-cell-title">{{ row.submission.student_ref }}</span>
                    </td>
                    <td>{{ row.assignment_title }}</td>
                    <td class="num">{{ formatRelative(row.submission.submitted_at) }}</td>
                    <td>
                      <span
                        class="hp-badge"
                        :class="parseBadgeClass(row.submission.parsing_task.status)"
                      >
                        <span
                          class="dot"
                          :class="{
                            'hp-dot-pulse': row.submission.parsing_task.status === 'running',
                          }"
                        />
                        {{ label(parsingLabels, row.submission.parsing_task.status) }}
                      </span>
                    </td>
                    <td class="hp-cell-actions">
                      <RouterLink
                        class="hp-linkbtn"
                        :to="`/assignments/${row.submission.assignment_id}/submissions/${row.submission.id}`"
                        >{{
                          row.submission.parsing_task.status === 'succeeded'
                            ? '核对作答'
                            : '查看详情'
                        }}<ArrowRight aria-hidden="true" style="width: 13px; height: 13px"
                      /></RouterLink>
                    </td>
                  </tr>
                </tbody>
              </table>
              <EmptyState
                v-else
                title="还没有学生提交"
                desc="进入作业页上传学生答卷（PDF / PNG / JPG），系统会自动解析生成作答内容。"
              />
            </div>
          </section>

          <section v-if="digest.recent_ops.length" class="hp-card">
            <div class="hp-card-head">
              <h2 class="hp-card-title"><Hourglass aria-hidden="true" />最近教学操作</h2>
            </div>
            <div class="hp-card-body-tight">
              <div
                v-for="op in digest.recent_ops"
                :key="op.id"
                class="hp-flex"
                style="padding: 9px 8px; border-bottom: 1px dashed var(--hp-border)"
              >
                <span class="hp-cell-title" style="font-size: 13px">{{ op.summary }}</span>
                <span class="hp-fine hp-grow">{{ op.detail }}</span>
                <span class="hp-fine" style="flex: none">{{ formatRelative(op.at) }}</span>
              </div>
            </div>
          </section>
        </template>
        <EmptyState
          v-else-if="unsupported"
          title="工作台摘要暂不可用"
          desc="当前数据源未提供工作台聚合信息。可在「我的作业」中查看作业与提交。"
        />
      </div>
    </PageState>
  </div>
</template>
