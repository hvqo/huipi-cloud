<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { identity, login, safeReturn } from '../auth'
const route = useRoute(),
  router = useRouter(),
  error = ref(''),
  busy = ref(false),
  account = ref(''),
  password = ref('')
async function signIn() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await login(account.value, password.value)
    password.value = ''
    await router.replace(
      identity.mustChangePassword ? '/change-password' : safeReturn(route.query.returnTo),
    )
  } catch (e) {
    error.value = e instanceof Error ? e.message : '登录未完成。'
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <main class="login">
    <section class="login-story">
      <span class="brand-mark">慧</span>
      <p class="eyebrow">HUIPI CLOUD</p>
      <h1>把每一份作答，<br />交回教师的判断。</h1>
      <p>从学生提交到来源核对，在清晰、可追溯的工作流中完成人工复核。</p>
      <div class="login-line">查看提交 <span>→</span> 核对证据 <span>→</span> 留存判断</div>
    </section>
    <section class="login-panel">
      <p class="eyebrow">教师工作空间</p>
      <h2>登录慧批云端</h2>
      <p class="muted">请输入学校管理员为你开通的账号。</p>
      <form class="local-login-form" @submit.prevent="signIn">
        <label for="login-account">邮箱或手机号</label>
        <el-input
          id="login-account"
          v-model="account"
          autocomplete="username"
          maxlength="254"
          required
        />
        <label for="login-password">密码</label>
        <el-input
          id="login-password"
          v-model="password"
          type="password"
          show-password
          autocomplete="current-password"
          maxlength="128"
          required
        />
        <p v-if="error || identity.error" role="alert" class="error">
          {{ error || identity.error }}
        </p>
        <el-button native-type="submit" type="primary" size="large" :loading="busy"
          >登 录</el-button
        >
      </form>
      <p class="fine"><RouterLink to="/forgot-password">忘记密码？</RouterLink></p>
    </section>
  </main>
</template>
