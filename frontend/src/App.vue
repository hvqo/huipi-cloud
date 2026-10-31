<script setup lang="ts">
import { computed, ref, watch, type Component } from 'vue'
import { ElConfigProvider } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { useRoute, useRouter } from 'vue-router'
import {
  House,
  Collection,
  Document,
  Notebook,
  Tickets,
  UploadFilled,
  DataAnalysis,
  List,
  User,
  Setting,
  Reading,
  Warning,
  Histogram,
  Fold,
  Expand,
} from '@element-plus/icons-vue'
import { request } from './api/client'
import { schoolAccess, resetSchoolAccess, loadSchoolAccess } from './api/school'
import { identity, logout } from './auth'
import { navigationAccess as nav, clearNavigation, loadNavigation } from './navigation'

const route = useRoute(),
  router = useRouter()
const serverRole = ref<'admin' | 'teacher' | null>(null)
const isAdmin = computed(() => serverRole.value === 'admin')
const collapsed = ref(false)
const hasSchoolAccess = computed(() => Object.values(schoolAccess).some(Boolean))
interface NavItem {
  to: string
  label: string
  icon: Component
  visible?: boolean
}
interface NavGroup {
  label: string
  items: NavItem[]
}
const groups = computed<NavGroup[]>(() => {
  const all: NavGroup[] = [
    { label: '工作台', items: [{ to: '/dashboard', label: '工作台首页', icon: House }] },
    {
      label: '教学作业',
      items: [
        { to: '/assignments', label: '我的作业', icon: Collection },
        { to: '/papers', label: '试卷管理', icon: Document, visible: nav.papers },
      ],
    },
    {
      label: '考试阅卷',
      items: [
        { to: '/exams', label: '考试配置', icon: Notebook, visible: nav.exams },
        { to: '/scans', label: '考试扫描', icon: UploadFilled, visible: nav.scans },
        { to: '/omr', label: '答题卡批改', icon: Tickets, visible: nav.omr },
        { to: '/marking/manage', label: '阅卷管理', icon: Setting, visible: nav.markingManagement },
        { to: '/marking/my', label: '我的阅卷任务', icon: Reading, visible: nav.myMarking },
        {
          to: '/marking/progress',
          label: '阅卷进度',
          icon: Histogram,
          visible: nav.markingManagement,
        },
        { to: '/exceptions', label: '异常处理', icon: Warning, visible: nav.exceptions },
      ],
    },
    {
      label: '成绩管理',
      items: [
        { to: '/exam-results', label: '成绩预览', icon: DataAnalysis, visible: nav.results },
        { to: '/final-results', label: '成绩管理', icon: List, visible: nav.results },
      ],
    },
    {
      label: '学校管理',
      items: [
        { to: '/school', label: '学校管理', icon: House, visible: hasSchoolAccess.value },
        { to: '/school/teaching', label: '教师任教关系', icon: User, visible: isAdmin.value },
      ],
    },
  ]
  return all
    .map((group) => ({ ...group, items: group.items.filter((item) => item.visible !== false) }))
    .filter((group) => group.items.length)
})
const currentNav = computed(() => {
  const items = groups.value.flatMap((group) =>
    group.items.map((item) => ({ ...item, group: group.label })),
  )
  return items
    .filter((item) => route.path === item.to || route.path.startsWith(item.to + '/'))
    .sort((a, b) => b.to.length - a.to.length)[0]
})
const title = computed(() => {
  if (route.path.endsWith('/scripts')) return '人工分卷与身份确认'
  if (route.path.startsWith('/scans/') && route.path.endsWith('/results')) return '整卷成绩预览'
  if (route.path.endsWith('/review')) return '作答证据复核'
  if (route.path.includes('/submissions/')) return '学生提交'
  if (route.path.endsWith('/homework')) return '作业管理'
  if (route.path.startsWith('/assignments/')) return '作业详情'
  return currentNav.value?.label || '工作台'
})
function active(path: string) {
  return route.path === path || route.path.startsWith(path + '/')
}
watch(
  () => identity.actor,
  async (actor) => {
    resetSchoolAccess()
    clearNavigation()
    serverRole.value = null
    if (!actor) return
    const [user, school] = await Promise.allSettled([
      request<{ role: string }>('/api/v1/me'),
      loadSchoolAccess(),
    ])
    if (identity.actor !== actor) return
    if (user.status === 'fulfilled' && ['admin', 'teacher'].includes(user.value.role))
      serverRole.value = user.value.role as 'admin' | 'teacher'
    if (school.status === 'fulfilled') Object.assign(schoolAccess, school.value)
    void loadNavigation(actor)
  },
  { immediate: true },
)
watch(
  () => route.path,
  () => {
    if (identity.actor) void loadNavigation(identity.actor)
  },
)
watch(
  () => identity.authenticated,
  (signed) => {
    if (!signed && route.path !== '/auth/callback')
      void router.replace({ path: '/login', query: { returnTo: route.fullPath } })
  },
)
async function signOut() {
  try {
    await logout()
  } catch {
    identity.error = '已退出本地会话，统一身份服务的退出尚未确认。'
  } finally {
    await router.replace('/login')
  }
}
</script>
<template>
  <ElConfigProvider :locale="zhCn">
    <RouterView v-if="!identity.authenticated" />
    <div v-else class="shell" :class="{ 'shell-collapsed': collapsed }">
      <aside class="sidebar" aria-label="主导航">
        <RouterLink class="brand" to="/dashboard" aria-label="慧批云端首页">
          <span class="brand-mark">慧</span
          ><span class="brand-name">慧批云端<small>HUIPI CLOUD</small></span>
        </RouterLink>
        <nav class="nav-scroll" aria-label="功能导航">
          <div v-for="group in groups" :key="group.label" class="nav-group">
            <p class="nav-caption">{{ group.label }}</p>
            <RouterLink
              v-for="item in group.items"
              :key="item.to"
              :to="item.to"
              class="nav-link"
              :class="{ 'is-current': active(item.to) }"
              :aria-label="item.label"
              :title="collapsed ? item.label : undefined"
            >
              <component :is="item.icon" class="nav-icon" aria-hidden="true" />
              <span class="nav-text">{{ item.label }}</span>
            </RouterLink>
          </div>
        </nav>
        <div class="sidebar-bottom">
          <button
            class="collapse-control"
            type="button"
            :aria-label="collapsed ? '展开导航' : '收起导航'"
            :aria-expanded="!collapsed"
            @click="collapsed = !collapsed"
          >
            <component :is="collapsed ? Expand : Fold" class="nav-icon" aria-hidden="true" />
            <span class="nav-text">收起导航</span>
          </button>
        </div>
      </aside>
      <div class="workspace">
        <header class="topbar">
          <div class="breadcrumbs">
            <span>工作空间</span><span aria-hidden="true">/</span
            ><span>{{ currentNav?.group || '工作台' }}</span
            ><span aria-hidden="true">/</span><strong>{{ title }}</strong>
          </div>
          <div class="identity">
            <span class="avatar">{{ identity.name.slice(0, 1) || '师' }}</span>
            <div class="identity-detail">
              <strong>{{ identity.name }}</strong
              ><small>{{
                serverRole === 'admin' ? '管理员' : serverRole === 'teacher' ? '教师' : '角色待确认'
              }}</small>
            </div>
            <el-button text @click="signOut">退出</el-button>
          </div>
        </header>
        <main id="main-content" :key="route.fullPath"><RouterView /></main>
      </div>
    </div>
  </ElConfigProvider>
</template>
