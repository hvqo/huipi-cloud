<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { changePassword, identity, requestRecoveryEmail } from '../auth'

const router = useRouter()
const current = ref(''),
  next = ref(''),
  confirm = ref(''),
  busy = ref(false),
  error = ref('')
const recoveryEmail = ref(''),
  recoveryNotice = ref('')
async function bindEmail() {
  recoveryNotice.value = error.value = ''
  busy.value = true
  try {
    await requestRecoveryEmail(recoveryEmail.value)
    recoveryNotice.value = '验证邮件已发送，请在 30 分钟内打开邮件中的链接。'
  } catch (e) {
    error.value = e instanceof Error ? e.message : '无法发送验证邮件。'
  } finally {
    busy.value = false
  }
}
async function save() {
  if (busy.value) return
  error.value = ''
  if (next.value !== confirm.value) {
    error.value = '两次输入的新密码不一致。'
    return
  }
  if (Array.from(next.value).length < 6 || Array.from(next.value).length > 128) {
    error.value = '新密码须为 6 至 128 个字符。'
    return
  }
  busy.value = true
  try {
    await changePassword(current.value, next.value)
    current.value = next.value = confirm.value = ''
    await router.replace({ path: '/login', query: { changed: '1' } })
  } catch (e) {
    error.value = e instanceof Error ? e.message : '密码修改未完成，请重试。'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="page-card password-card">
    <h1>修改密码</h1>
    <p v-if="identity.mustChangePassword">首次登录请先设置自己的密码。</p>
    <p v-else>修改成功后需要重新登录。</p>
    <form class="local-login-form" @submit.prevent="save">
      <label for="current-password">当前密码</label>
      <el-input
        id="current-password"
        v-model="current"
        type="password"
        show-password
        autocomplete="current-password"
        required
      />
      <label for="new-password">新密码</label>
      <el-input
        id="new-password"
        v-model="next"
        type="password"
        show-password
        autocomplete="new-password"
        maxlength="128"
        required
      />
      <label for="confirm-password">再次输入新密码</label>
      <el-input
        id="confirm-password"
        v-model="confirm"
        type="password"
        show-password
        autocomplete="new-password"
        maxlength="128"
        required
      />
      <small>请使用 6 至 128 个字符，可以是数字、字母或符号。</small>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <el-button native-type="submit" type="primary" :loading="busy">保存新密码</el-button>
    </form>
  </section>
  <section v-if="!identity.mustChangePassword" class="page-card password-card">
    <h2>找回邮箱</h2>
    <p v-if="identity.recoveryEmail">已验证：{{ identity.recoveryEmail }}</p>
    <p v-else>绑定并验证邮箱后，忘记密码时可以自助找回。</p>
    <form class="local-login-form" @submit.prevent="bindEmail">
      <label for="recovery-email">{{ identity.recoveryEmail ? '更换邮箱' : '邮箱地址' }}</label>
      <el-input id="recovery-email" v-model="recoveryEmail" type="email" maxlength="254" required />
      <p v-if="recoveryNotice" role="status">{{ recoveryNotice }}</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <el-button native-type="submit" :loading="busy">发送验证邮件</el-button>
    </form>
  </section>
</template>
