<script setup lang="ts">
import { computed, watch } from 'vue'
import { ElConfigProvider } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { useRoute, useRouter } from 'vue-router'
import { identity, logout } from './auth'
const route = useRoute(),
  router = useRouter()
const title = computed(() =>
  route.path.endsWith('/results') || route.path === '/exam-results'
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
        <RouterLink to="/scans" class="nav-link">▤ <span>考试扫描</span></RouterLink>
        <RouterLink to="/exam-results" class="nav-link" aria-label="成绩预览"
          >▦ <span>成绩预览</span></RouterLink
        >
        <div class="nav-note">候选评分审核<small>尚未开放工作台页面</small></div>
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
