<script setup lang="ts">
import { computed, watch } from 'vue'
import { schoolAccess, resetSchoolAccess, loadSchoolAccess } from './api/school'
import { ElConfigProvider } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { useRoute, useRouter } from 'vue-router'
import { identity, logout } from './auth'
const route = useRoute(),
  router = useRouter()
const title = computed(() =>
  route.path.startsWith('/marking')
    ? '协同阅卷工作台'
    : route.path.startsWith('/school')
      ? '学校基础数据'
      : route.path.startsWith('/papers')
        ? '试卷管理'
        : route.path.startsWith('/exams')
          ? '考试配置工作台'
          : route.path.endsWith('/results') || route.path === '/exam-results'
            ? '整卷成绩预览'
            : route.path.endsWith('/scripts') && route.path.startsWith('/scans/')
              ? '人工分卷与身份确认'
              : route.path === '/scans'
                ? '考试扫描'
                : route.path.endsWith('/review')
                  ? '作答证据复核'
                  : route.params.sid
                    ? '学生提交'
                    : route.params.aid
                      ? '作业详情'
                      : '我的作业',
)
watch(
  () => identity.actor,
  async (actor) => {
    resetSchoolAccess()
    if (!actor) return
    try {
      const value = await loadSchoolAccess()
      if (identity.actor === actor) Object.assign(schoolAccess, value)
    } catch {
      /* No capability projection means no school navigation. */
    }
  },
  { immediate: true },
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
    <div v-else class="shell">
      <aside class="sidebar">
        <RouterLink class="brand" to="/assignments"
          ><span class="brand-mark">慧</span
          ><span>慧批云端<small>HUIPI · TEACHER</small></span></RouterLink
        >
        <p class="nav-caption">教学工作空间</p>
        <RouterLink to="/assignments" class="nav-link">▦ <span>我的作业</span></RouterLink>
        <RouterLink to="/papers" class="nav-link">▤ <span>试卷管理</span></RouterLink>
        <RouterLink to="/exams" class="nav-link" aria-label="考试配置"
          >▦ <span>考试配置</span></RouterLink
        >
        <RouterLink to="/scans" class="nav-link">▤ <span>考试扫描</span></RouterLink>
        <RouterLink to="/exam-results" class="nav-link" aria-label="成绩预览"
          >▦ <span>成绩预览</span></RouterLink
        >
        <RouterLink
          v-if="
            schoolAccess.can_read ||
            schoolAccess.can_manage ||
            schoolAccess.can_preview ||
            schoolAccess.can_confirm
          "
          to="/school"
          aria-label="学校管理"
          class="nav-link"
          >▦ <span>学校基础数据</span></RouterLink
        >
        <RouterLink to="/marking/manage" class="nav-link" aria-label="阅卷管理"
          >▤ <span>阅卷管理</span></RouterLink
        >
        <RouterLink to="/marking/my" class="nav-link" aria-label="我的阅卷任务"
          >▤ <span>我的阅卷任务</span></RouterLink
        >
        <RouterLink to="/marking/progress" class="nav-link" aria-label="阅卷进度"
          >▤ <span>阅卷进度</span></RouterLink
        >
        <div class="nav-note">考试批改与复核<small>从考试答卷工作区进入</small></div>
        <div class="sidebar-foot">
          <span class="live-dot" /> 受保护的教师工作台<small>人工确认 · 来源可追溯</small>
        </div>
      </aside>
      <div class="workspace">
        <header class="topbar">
          <div><span class="eyebrow">教师工作台 / </span>{{ title }}</div>
          <div class="identity">
            <span class="avatar">师</span>
            <div>{{ identity.name }}<small>身份会话已登录 · 权限由服务端校验</small></div>
            <el-button text @click="signOut">退出</el-button>
          </div>
        </header>
        <main id="main-content" :key="route.fullPath"><RouterView /></main>
        <footer>慧批云端 · 所有人工复核均保留修订历史，审核不等于发布成绩。</footer>
      </div>
    </div>
  </ElConfigProvider>
</template>
