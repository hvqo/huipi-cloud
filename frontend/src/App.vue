<script setup lang="ts">
import { watch } from 'vue'
import { ElConfigProvider } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { useRoute, useRouter } from 'vue-router'
import { ShieldAlert } from 'lucide-vue-next'
import AppShell from './components/AppShell.vue'
import { DEMO_MODE } from './app/mode'
import { session, refreshSession, signOut } from './app/session'
import { identity } from './auth'

const route = useRoute(),
  router = useRouter()

// 真实模式：令牌过期或 401 后端清除会话时同步门面状态，触发回登录页。
if (!DEMO_MODE) {
  watch(
    () => identity.authenticated,
    (signed) => {
      if (session.ready && session.authenticated && !signed) {
        session.signOutReason = 'expired'
        session.authenticated = false
        session.role = ''
        session.provisionError = ''
      }
    },
  )
}

watch(
  () => session.authenticated,
  (signed) => {
    if (!signed && session.ready && route.path !== '/login' && route.path !== '/auth/callback') {
      const query = session.signOutReason === 'manual' ? {} : { returnTo: route.fullPath }
      void router.replace({ path: '/login', query })
    }
  },
)
</script>
<template>
  <ElConfigProvider :locale="zhCn">
    <RouterView v-if="!session.authenticated" />
    <AppShell v-else-if="session.provisionError">
      <div class="hp-card">
        <div class="hp-card-body hp-state-card">
          <ShieldAlert
            aria-hidden="true"
            style="width: 34px; height: 34px; color: var(--hp-warning)"
          />
          <h3>账号暂时无法使用</h3>
          <p>{{ session.provisionError }}</p>
          <div class="hp-flex">
            <button class="hp-btn hp-btn-secondary" type="button" @click="refreshSession()">
              重新读取
            </button>
            <button class="hp-btn hp-btn-ghost" type="button" @click="signOut()">退出登录</button>
          </div>
        </div>
      </div>
    </AppShell>
    <AppShell v-else>
      <RouterView />
      <p class="hp-pagefoot">
        慧批云端 · 人工核对与来源可追溯 · 作答核对与评分审核均不等于发布正式成绩
      </p>
    </AppShell>
  </ElConfigProvider>
</template>
