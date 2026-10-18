<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Plus, Search, Users, KeyRound } from 'lucide-vue-next'
import { services, type TeacherRecord, type S } from '../../services'
import { DEMO_MODE } from '../../app/mode'
import { formatDateTime } from '../../lib/labels'
import { useAdminMutation } from '../../lib/admin'
import PageState from '../../components/PageState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import EmptyState from '../../components/EmptyState.vue'
const route = useRoute(),
  rows = ref<TeacherRecord[]>([]),
  loading = ref(false),
  error = ref(''),
  offset = ref(0),
  hasNext = ref(false),
  total = ref<number | null>(null)
const keyword = ref(''),
  active = ref<'all' | 'active' | 'inactive'>(route.query.active === 'active' ? 'active' : 'all')
const registration = ref(false),
  confirmation = ref<TeacherRecord | null>(null),
  details = ref<TeacherRecord | null>(null),
  memberships = ref<S['AssignmentMembershipSummary'][]>([]),
  detailError = ref('')
const form = reactive({ issuer: DEMO_MODE ? 'https://idp.huipi-demo.cn' : '', subject: '' })
const mutation = useAdminMutation(),
  { busy, uncertain, notice, error: writeError } = mutation
async function load(value = offset.value) {
  loading.value = true
  error.value = ''
  offset.value = value
  try {
    const result = await (
      await services()
    ).admin.teachers({ limit: 12, offset: value, keyword: keyword.value, active: active.value })
    rows.value = result.records
    hasNext.value = result.has_next
    total.value = result.total
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
  } finally {
    loading.value = false
  }
}
function name(row: TeacherRecord) {
  return row.display_name || `教师 ${row.user_id.slice(0, 8)}`
}
async function register() {
  if (!form.subject.trim() || !form.issuer.trim()) return
  const body = { request_id: crypto.randomUUID(), issuer: form.issuer, subject: form.subject }
  await mutation.run(
    async () => {
      await (await services()).admin.register(body)
      registration.value = false
      form.subject = ''
    },
    () => load(0),
  )
}
async function setActive() {
  const target = confirmation.value
  if (!target) return
  const body = { request_id: crypto.randomUUID(), is_active: !target.is_active }
  await mutation.run(
    async () => {
      await (await services()).admin.setActive(target.user_id, body)
      confirmation.value = null
    },
    () => load(),
  )
}
async function inspect(row: TeacherRecord) {
  details.value = row
  memberships.value = []
  detailError.value = ''
  try {
    const result = await (await services()).admin.memberships(row.user_id)
    memberships.value = result.records
    if (result.has_next) detailError.value = '这里只展示前 50 条授权，完整分页请进入作业授权页。'
  } catch (e) {
    detailError.value = e instanceof Error ? e.message : '读取失败'
  }
}
onMounted(() => load(0))
</script>
<template>
  <div class="hp-section-gap">
    <div class="hp-pagehead">
      <div>
        <h1>教师账号</h1>
        <p class="hp-pagehead-desc">维护教师的使用资格。作业访问需另行分配授权。</p>
      </div>
      <button
        class="hp-btn hp-btn-primary"
        :disabled="busy || uncertain"
        @click="registration = true"
      >
        <Plus />登记教师
      </button>
    </div>
    <p v-if="!DEMO_MODE" class="hp-note">
      账号仅展示服务端提供的内部标识，不推测教师姓名或学校。筛选作用于当前页。
    </p>
    <p v-if="notice" class="hp-note hp-note-success" role="status">{{ notice }}</p>
    <div v-if="writeError" class="hp-note hp-note-warning" role="alert">
      {{ writeError
      }}<button v-if="uncertain" class="hp-btn hp-btn-secondary hp-btn-sm" @click="mutation.retry">
        重试原管理请求
      </button>
    </div>
    <section class="hp-card">
      <div class="hp-card-body hp-toolbar">
        <form class="hp-flex hp-wrap" @submit.prevent="load(0)">
          <label class="hp-search"
            ><Search /><input
              v-model="keyword"
              aria-label="搜索教师"
              placeholder="搜索教师姓名或账号标识" /></label
          ><select v-model="active" aria-label="账号状态" class="hp-select">
            <option value="all">全部状态</option>
            <option value="active">已启用</option>
            <option value="inactive">已停用</option></select
          ><button class="hp-btn hp-btn-secondary">查询</button>
        </form>
      </div>
      <PageState :loading="loading" :error="error" @retry="load()"
        ><div class="hp-table-wrap">
          <table v-if="rows.length" class="hp-table">
            <thead>
              <tr>
                <th>教师账号</th>
                <th>状态</th>
                <th>登记时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in rows" :key="row.user_id">
                <td>
                  <div class="hp-flex">
                    <div class="hp-avatar"><Users /></div>
                    <div>
                      <b>{{ name(row) }}</b>
                      <p class="hp-fine">{{ row.user_id.slice(0, 12) }}</p>
                    </div>
                  </div>
                </td>
                <td>
                  <span
                    class="hp-badge"
                    :class="row.is_active ? 'hp-badge-success' : 'hp-badge-neutral'"
                    >{{ row.is_active ? '已启用' : '已停用' }}</span
                  >
                </td>
                <td>{{ formatDateTime(row.created_at) }}</td>
                <td>
                  <div class="hp-flex hp-wrap">
                    <button class="hp-link" @click="inspect(row)">账号详情</button
                    ><RouterLink
                      class="hp-link"
                      :to="`/admin/authorizations?teacher=${encodeURIComponent(row.user_id)}`"
                      >作业授权</RouterLink
                    ><button
                      class="hp-link"
                      :class="{ 'hp-danger-text': row.is_active }"
                      :disabled="busy || uncertain"
                      @click="confirmation = row"
                    >
                      {{ row.is_active ? '停用' : '启用' }}
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <EmptyState v-else title="没有符合条件的教师" desc="可调整筛选条件，或先登记教师身份。" />
        </div>
        <PaginationBar
          :offset="offset"
          :limit="12"
          :total="total ?? undefined"
          :has-next="hasNext"
          @change="load"
      /></PageState>
    </section>
    <el-dialog
      v-model="registration"
      title="登记教师账号"
      width="520px"
      :close-on-click-modal="false"
      ><form class="hp-form" @submit.prevent="register">
        <p class="hp-muted">登记受信任身份服务中的教师，不会创建密码或授予管理员权限。</p>
        <label
          >身份服务地址（Issuer）<input
            v-model="form.issuer"
            required
            maxlength="512"
            class="hp-input"
            placeholder="由部署管理员提供" /></label
        ><label
          >外部身份标识（Subject）<input
            v-model="form.subject"
            required
            maxlength="255"
            class="hp-input"
            autocomplete="off"
            placeholder="从受信任身份服务取得稳定标识"
        /></label>
        <p v-if="writeError" role="alert" class="hp-error-text">{{ writeError }}</p>
        <div class="hp-dialog-actions">
          <button type="button" class="hp-btn hp-btn-secondary" @click="registration = false">
            取消</button
          ><button class="hp-btn hp-btn-primary" :disabled="busy || uncertain">
            {{ busy ? '正在登记…' : '确认登记' }}
          </button>
        </div>
      </form></el-dialog
    >
    <el-dialog
      :model-value="!!confirmation"
      :title="confirmation?.is_active ? '停用教师账号' : '启用教师账号'"
      width="460px"
      @update:model-value="confirmation = null"
      ><p v-if="confirmation">
        {{
          confirmation.is_active
            ? '停用后，该账号的新请求将被拒绝；历史记录和授权关系会保留。'
            : '重新启用后，未撤销的作业权限将恢复。'
        }}
      </p>
      <p class="hp-confirm-target">{{ confirmation ? name(confirmation) : '' }}</p>
      <p v-if="writeError" class="hp-error-text" role="alert">{{ writeError }}</p>
      <template #footer
        ><button class="hp-btn hp-btn-secondary" @click="confirmation = null">取消</button
        ><button class="hp-btn hp-btn-primary" :disabled="busy || uncertain" @click="setActive">
          确认{{ confirmation?.is_active ? '停用' : '启用' }}
        </button></template
      ></el-dialog
    >
    <el-dialog
      :model-value="!!details"
      title="教师账号详情"
      width="580px"
      @update:model-value="details = null"
      ><template v-if="details"
        ><h3>{{ name(details) }}</h3>
        <p class="hp-muted">{{ details.is_active ? '账号正常启用' : '账号已停用' }}</p>
        <details>
          <summary>账号标识</summary>
          <code>{{ details.user_id }}</code>
        </details>
        <h3><KeyRound class="hp-inline-icon" />作业授权</h3>
        <p v-if="detailError" class="hp-note">{{ detailError }}</p>
        <div v-for="item in memberships" :key="item.assignment_id" class="hp-module-row">
          <span>作业 {{ item.assignment_id.slice(0, 12) }}</span
          ><span class="hp-badge">{{
            !item.is_active ? '无效' : item.can_write ? '可写' : '只读'
          }}</span>
        </div>
        <EmptyState v-if="!memberships.length" title="尚无作业授权" /><RouterLink
          class="hp-btn hp-btn-primary"
          :to="`/admin/authorizations?teacher=${encodeURIComponent(details.user_id)}`"
          >管理授权</RouterLink
        ></template
      ></el-dialog
    >
  </div>
</template>
