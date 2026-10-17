<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { GraduationCap, ShieldCheck } from 'lucide-vue-next'
import { DEMO_MODE } from '../app/mode'
import { signInDemo } from '../app/session'
import { identity, login, manager } from '../auth'

/**
 * 登录页：真实模式保留 OIDC Code + PKCE（统一身份登录）；
 * 演示模式提供教师/管理员本地演示身份（仅存在于 dev:demo）。
 */
const route = useRoute(),
  router = useRouter(),
  error = ref(''),
  busy = ref(false)

async function signIn() {
  busy.value = true
  try {
    await login(String(route.query.returnTo || '/'))
  } catch (e) {
    error.value = e instanceof Error ? e.message : '登录未完成。'
    busy.value = false
  }
}

async function demoSignIn(role: 'teacher' | 'admin') {
  busy.value = true
  try {
    await signInDemo(role)
    await router.replace(
      String(route.query.returnTo || (role === 'admin' ? '/admin' : '/workbench')),
    )
  } catch (e) {
    error.value = e instanceof Error ? e.message : '演示登录未完成。'
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <main class="login-wrap">
    <section class="login-story">
      <span class="brand-mark">慧</span>
      <p class="eyebrow">HUIPI CLOUD · 慧批云端</p>
      <h1>把每一份作答，<br />交回教师的判断。</h1>
      <p>从学生提交到来源核对，在清晰、可追溯的工作流中完成人工复核与评分审核。</p>
      <div class="login-line">查看提交 <span>→</span> 核对证据 <span>→</span> 审核评分</div>
    </section>
    <section class="login-panel">
      <template v-if="DEMO_MODE">
        <p class="eyebrow">演示模式 · 合成数据</p>
        <h2>登录慧批云端</h2>
        <p class="hp-muted" style="font-size: 13px">
          选择一个演示身份进入。演示数据保存在本地浏览器，可随时在页面上方重置。
        </p>
        <div class="hp-flex" style="align-items: stretch">
          <button
            class="hp-demo-role"
            type="button"
            :disabled="busy"
            @click="demoSignIn('teacher')"
          >
            <h3><GraduationCap aria-hidden="true" />教师演示</h3>
            <p>李文静 · 数学教师：作业管理、作答核对、候选评分审核、知识库与 AI 助教</p>
          </button>
          <button class="hp-demo-role" type="button" :disabled="busy" @click="demoSignIn('admin')">
            <h3><ShieldCheck aria-hidden="true" />管理员演示</h3>
            <p>周明远 · 教务管理员：教师账号、作业授权、操作审计与系统状态</p>
          </button>
        </div>
        <p v-if="error || identity.error" role="alert" class="hp-error-text">
          {{ error || identity.error }}
        </p>
        <p class="login-demo-note">
          演示模式仅用于本地体验，不连接真实统一身份服务、数据库或评分模型；正式部署使用 OIDC 登录。
        </p>
      </template>
      <template v-else>
        <p class="eyebrow">教师工作空间</p>
        <h2>登录慧批云端</h2>
        <p class="hp-muted" style="font-size: 13px">使用部署管理员配置的统一身份服务继续。</p>
        <div v-if="!manager" class="hp-note hp-note-warning" role="note">
          身份服务尚未配置，业务页面已锁定。请联系部署管理员完成连接。
        </div>
        <p v-if="error || identity.error" role="alert" class="hp-error-text">
          {{ error || identity.error }}
        </p>
        <el-button type="primary" size="large" :disabled="!manager" :loading="busy" @click="signIn"
          >通过统一身份登录</el-button
        >
        <p class="hp-fine">此工作台不收集密码。登录身份与作业访问权限由服务端分别校验。</p>
      </template>
    </section>
  </main>
</template>
