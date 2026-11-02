<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { confirmPasswordReset, confirmRecoveryEmail, identity, requestPasswordReset } from '../auth'

const route = useRoute(),
  router = useRouter()
const token = ref(new URLSearchParams(window.location.hash.slice(1)).get('token') || '')
// Fragments are never sent to the server; remove the token from browser history immediately.
if (window.location.hash)
  history.replaceState(history.state, '', location.pathname + location.search)
const account = ref(''),
  password = ref(''),
  repeated = ref(''),
  busy = ref(false)
const error = ref(''),
  message = ref('')

async function submit() {
  if (busy.value) return
  busy.value = true
  error.value = message.value = ''
  try {
    if (route.path === '/forgot-password') {
      await requestPasswordReset(account.value)
      message.value = '如果账号已绑定找回邮箱并启用邮件服务，你将收到重置邮件；否则请联系管理员。'
    } else if (route.path === '/verify-email') {
      if (!identity.authenticated) throw new Error('请先登录，再重新打开邮件中的验证链接。')
      await confirmRecoveryEmail(token.value)
      token.value = ''
      message.value = '找回邮箱已验证。'
    } else {
      if (password.value !== repeated.value) throw new Error('两次输入的新密码不一致。')
      if (Array.from(password.value).length < 6 || Array.from(password.value).length > 128)
        throw new Error('新密码须为 6 至 128 个字符。')
      await confirmPasswordReset(token.value, password.value)
      password.value = repeated.value = token.value = ''
      await router.replace({ path: '/login', query: { changed: '1' } })
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : '操作未完成，请重试。'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="page-card password-card">
    <h1>
      {{
        route.path === '/forgot-password'
          ? '找回密码'
          : route.path === '/verify-email'
            ? '验证找回邮箱'
            : '设置新密码'
      }}
    </h1>
    <form class="local-login-form" @submit.prevent="submit">
      <template v-if="route.path === '/forgot-password'">
        <p>请输入登录使用的邮箱或手机号。</p>
        <label for="reset-account">邮箱或手机号</label>
        <el-input
          id="reset-account"
          v-model="account"
          autocomplete="username"
          maxlength="254"
          required
        />
      </template>
      <template v-else-if="route.path === '/reset-password'">
        <label for="reset-new">新密码（6 至 128 个字符）</label>
        <el-input
          id="reset-new"
          v-model="password"
          type="password"
          show-password
          autocomplete="new-password"
          maxlength="128"
          required
        />
        <label for="reset-repeat">再次输入新密码</label>
        <el-input
          id="reset-repeat"
          v-model="repeated"
          type="password"
          show-password
          autocomplete="new-password"
          maxlength="128"
          required
        />
      </template>
      <p v-else>确认将此邮箱绑定为你的找回邮箱。</p>
      <p v-if="message" role="status">{{ message }}</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <el-button native-type="submit" type="primary" :loading="busy" :disabled="!!message">
        {{
          route.path === '/forgot-password'
            ? '发送重置邮件'
            : route.path === '/verify-email'
              ? '确认绑定'
              : '保存新密码'
        }}
      </el-button>
    </form>
    <p>
      <RouterLink :to="identity.authenticated ? '/change-password' : '/login'">{{
        identity.authenticated ? '返回账号设置' : '返回登录'
      }}</RouterLink>
    </p>
  </main>
</template>
