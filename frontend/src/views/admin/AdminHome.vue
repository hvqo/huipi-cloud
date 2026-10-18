<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Users, UserCheck, KeyRound, ArrowRight, ShieldCheck, History } from 'lucide-vue-next'
import { services, type AdminOverview } from '../../services'
import { DEMO_MODE } from '../../app/mode'
import { adminActionLabels, formatDateTime, label, systemStatusLabels } from '../../lib/labels'
import PageState from '../../components/PageState.vue'
import EmptyState from '../../components/EmptyState.vue'
const data = ref<AdminOverview | null>(null),
  loading = ref(false),
  error = ref('')
async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await (await services()).admin.overview()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>
<template>
  <div class="hp-section-gap">
    <section class="hp-admin-hero">
      <div>
        <p class="hp-eyebrow">教务管理 · 协作与保障</p>
        <h1>让教学协作，井然有序</h1>
        <p>管理教师账号与作业权限，追溯每一次管理变更。</p>
      </div>
      <ShieldCheck aria-hidden="true" />
    </section>
    <PageState :loading="loading" :error="error" @retry="load">
      <template v-if="data">
        <div class="hp-stats">
          <RouterLink to="/admin/teachers" class="hp-stat"
            ><div class="hp-stat-icon"><Users /></div>
            <div>
              <b class="hp-stat-num">{{ data.teachers_total }}</b>
              <p class="hp-stat-label">{{ DEMO_MODE ? '教师账号' : '本页教师账号' }}</p>
            </div></RouterLink
          >
          <RouterLink to="/admin/teachers?active=active" class="hp-stat"
            ><div class="hp-stat-icon is-teal"><UserCheck /></div>
            <div>
              <b class="hp-stat-num">{{ data.teachers_active }}</b>
              <p class="hp-stat-label">{{ DEMO_MODE ? '启用中的账号' : '本页启用账号' }}</p>
            </div></RouterLink
          >
          <RouterLink to="/admin/authorizations" class="hp-stat"
            ><div class="hp-stat-icon is-amber"><KeyRound /></div>
            <div>
              <b class="hp-stat-num">{{ data.authorizations_active ?? '—' }}</b>
              <p class="hp-stat-label">有效作业授权</p>
            </div></RouterLink
          >
        </div>
        <p class="hp-fine">{{ data.scope_note }}</p>
        <div class="hp-admin-columns">
          <section class="hp-card">
            <header class="hp-card-head"><h2 class="hp-card-title">管理快捷入口</h2></header>
            <div class="hp-card-body hp-section-gap">
              <RouterLink class="hp-management-link" to="/admin/teachers"
                ><Users />
                <div>
                  <b>管理教师账号</b>
                  <p>登记外部身份，核查账号启停状态</p>
                </div>
                <ArrowRight
              /></RouterLink>
              <RouterLink class="hp-management-link" to="/admin/authorizations"
                ><KeyRound />
                <div>
                  <b>分配作业权限</b>
                  <p>按教师查看、授予或撤销作业访问</p>
                </div>
                <ArrowRight
              /></RouterLink>
              <RouterLink class="hp-management-link" to="/admin/audit"
                ><History />
                <div>
                  <b>查看管理记录</b>
                  <p>只读追溯操作对象、时间与权限状态</p>
                </div>
                <ArrowRight
              /></RouterLink>
            </div>
          </section>
          <section class="hp-card">
            <header class="hp-card-head">
              <h2 class="hp-card-title">服务运行情况</h2>
              <RouterLink class="hp-link" to="/admin/system">查看详情 →</RouterLink>
            </header>
            <div class="hp-card-body">
              <div v-for="module in data.modules" :key="module.key" class="hp-module-row">
                <span>{{ module.name }}</span
                ><span
                  class="hp-badge"
                  :class="{
                    'hp-badge-success': module.status === 'ok',
                    'hp-badge-warning': module.status === 'degraded',
                  }"
                  >{{ label(systemStatusLabels, module.status) }}</span
                >
              </div>
            </div>
          </section>
        </div>
        <section class="hp-card">
          <header class="hp-card-head">
            <h2 class="hp-card-title">最近管理操作</h2>
            <RouterLink class="hp-link" to="/admin/audit">全部记录 →</RouterLink>
          </header>
          <div class="hp-card-body">
            <div v-for="event in data.recent_audit" :key="event.operation_id" class="hp-audit-row">
              <span class="hp-audit-dot" />
              <div>
                <b>{{ label(adminActionLabels, event.action) }}</b>
                <p>目标账号 · {{ event.target_user_id.slice(0, 12) }}</p>
              </div>
              <time>{{ formatDateTime(event.created_at) }}</time>
            </div>
            <EmptyState
              v-if="!data.recent_audit.length"
              title="还没有管理记录"
              desc="账号和作业权限变更后，将在这里保留记录。"
            />
          </div>
        </section>
      </template>
    </PageState>
  </div>
</template>
