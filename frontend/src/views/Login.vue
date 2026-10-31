<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { identity, login, manager } from '../auth'
const route = useRoute(),
  error = ref(''),
  busy = ref(false)
async function signIn() {
  busy.value = true
  try {
    await login(String(route.query.returnTo || '/dashboard'))
  } catch (e) {
    error.value = e instanceof Error ? e.message : '登录未完成。'
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
      <p class="muted">使用部署管理员配置的统一身份服务继续。</p>
      <div v-if="!manager" class="notice">
        身份服务尚未配置，业务页面已锁定。请联系部署管理员完成连接。
      </div>
      <p v-if="error || identity.error" role="alert" class="error">{{ error || identity.error }}</p>
      <el-button type="primary" size="large" :disabled="!manager" :loading="busy" @click="signIn"
        >通过统一身份登录</el-button
      >
      <p class="fine">此工作台不收集密码。登录身份与作业访问权限由服务端分别校验。</p>
    </section>
  </main>
</template>
