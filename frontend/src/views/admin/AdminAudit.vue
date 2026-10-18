<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { History, Search } from 'lucide-vue-next'
import { services, type S } from '../../services'
import { adminActionLabels, label, formatDateTime } from '../../lib/labels'
import PageState from '../../components/PageState.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
const route = useRoute(),
  rows = ref<S['AdminAuditSummary'][]>([]),
  loading = ref(false),
  error = ref(''),
  offset = ref(0),
  hasNext = ref(false)
const teacher = ref(String(route.query.teacher || '')),
  assignment = ref(''),
  action = ref(''),
  since = ref(''),
  until = ref(''),
  selected = ref<S['AdminAuditSummary'] | null>(null)
const visible = computed(() =>
  rows.value.filter(
    (r) =>
      (!action.value || r.action === action.value) &&
      (!since.value || r.created_at >= since.value) &&
      (!until.value || r.created_at.slice(0, 10) <= until.value),
  ),
)
async function load(value = 0) {
  loading.value = true
  error.value = ''
  offset.value = value
  try {
    const result = await (
      await services()
    ).admin.audit({
      offset: value,
      limit: 20,
      target_user_id: teacher.value || undefined,
      assignment_id: assignment.value || undefined,
    })
    rows.value = result.records
    hasNext.value = result.has_next
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
  } finally {
    loading.value = false
  }
}
function stateText(value: Record<string, boolean>) {
  return Object.entries(value)
    .map(
      ([k, v]) =>
        `${({ is_active: '是否有效', can_write: '是否可写', is_admin: '是否管理员' } as Record<string, string>)[k] || k}：${v ? '是' : '否'}`,
    )
    .join('；')
}
onMounted(() => load())
</script>
<template>
  <div class="hp-section-gap">
    <div class="hp-pagehead">
      <div>
        <h1>操作审计</h1>
        <p class="hp-pagehead-desc">查看管理变更的操作者、对象与结果。历史事件只读，不可修改。</p>
      </div>
      <span class="hp-badge hp-badge-primary"><History />只读记录</span>
    </div>
    <section class="hp-card">
      <form class="hp-card-body hp-audit-filter" @submit.prevent="load(0)">
        <label
          >目标教师<input
            v-model="teacher"
            class="hp-input"
            placeholder="内部账号标识（可选）" /></label
        ><label
          >关联作业<input
            v-model="assignment"
            class="hp-input"
            placeholder="作业标识（可选）" /></label
        ><label
          >操作类型<select v-model="action" class="hp-select">
            <option value="">全部操作</option>
            <option v-for="(text, key) in adminActionLabels" :key="key" :value="key">
              {{ text }}
            </option>
          </select></label
        ><label>开始日期<input v-model="since" type="date" class="hp-input" /></label
        ><label>结束日期<input v-model="until" type="date" class="hp-input" /></label
        ><button class="hp-btn hp-btn-secondary"><Search />查询</button>
      </form>
      <p class="hp-fine hp-inset">
        教师与作业由服务端筛选；类型和日期仅筛选当前已加载页，不代表全量检索结果。
      </p>
      <PageState :loading="loading" :error="error" @retry="load(offset)"
        ><div class="hp-table-wrap">
          <table v-if="visible.length" class="hp-table">
            <thead>
              <tr>
                <th>操作</th>
                <th>目标账号</th>
                <th>权限结果</th>
                <th>发生时间</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in visible" :key="row.operation_id">
                <td>
                  <b>{{ label(adminActionLabels, row.action) }}</b>
                </td>
                <td>{{ row.target_user_id.slice(0, 12) }}</td>
                <td>{{ stateText(row.state) }}</td>
                <td>{{ formatDateTime(row.created_at) }}</td>
                <td><button class="hp-link" @click="selected = row">查看详情</button></td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-else title="当前页没有符合条件的记录" desc="可调整条件或继续查看下一页。" />
        </div>
        <PaginationBar :offset="offset" :limit="20" :has-next="hasNext" @change="load"
      /></PageState>
    </section>
    <el-dialog
      :model-value="!!selected"
      title="管理事件详情"
      width="600px"
      @update:model-value="selected = null"
      ><template v-if="selected"
        ><span class="hp-badge hp-badge-primary">{{
          label(adminActionLabels, selected.action)
        }}</span>
        <dl class="hp-detail-list">
          <dt>时间</dt>
          <dd>{{ formatDateTime(selected.created_at) }}</dd>
          <dt>操作者</dt>
          <dd>
            <code>{{ selected.actor_user_id }}</code>
          </dd>
          <dt>目标教师</dt>
          <dd>
            <code>{{ selected.target_user_id }}</code>
          </dd>
          <dt>关联作业</dt>
          <dd>{{ selected.assignment_id || '不涉及作业' }}</dd>
          <dt>变更后状态</dt>
          <dd>{{ stateText(selected.state) }}</dd>
          <dt>请求关联标识</dt>
          <dd>
            <code>{{ selected.request_id }}</code>
          </dd>
          <dt>事件标识</dt>
          <dd>
            <code>{{ selected.operation_id }}</code>
          </dd>
        </dl>
        <p class="hp-note">
          这是一条不可变历史事件，不代表账号或授权此刻仍处于相同状态。
        </p></template
      ></el-dialog
    >
  </div>
</template>
