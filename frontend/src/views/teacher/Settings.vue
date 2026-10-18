<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { UserRound, ShieldCheck, LogOut, RefreshCw } from 'lucide-vue-next'
import { session, refreshSession, signOut } from '../../app/session'
import { DEMO_MODE } from '../../app/mode'
const busy = ref(false),
  error = ref('')
async function refresh() {
  busy.value = true
  error.value = ''
  try {
    await refreshSession()
    if (session.provisionError) error.value = session.provisionError
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
  } finally {
    busy.value = false
  }
}
onMounted(refresh)
</script>
<template>
  <div class="hp-section-gap">
    <div class="hp-pagehead">
      <div>
        <h1>账号与设置</h1>
        <p class="hp-pagehead-desc">查看当前身份与可用能力，安全管理本地会话。</p>
      </div>
      <button class="hp-btn hp-btn-secondary" :disabled="busy" @click="refresh">
        <RefreshCw />重新核验身份
      </button>
    </div>
    <p v-if="error" class="hp-note hp-note-warning" role="alert">{{ error }}</p>
    <section class="hp-card hp-card-body hp-account-card">
      <div class="hp-avatar hp-avatar-lg"><UserRound /></div>
      <div>
        <h2>{{ session.name }}</h2>
        <span class="hp-badge hp-badge-primary">{{
          session.role === 'admin' ? '管理员' : '教师'
        }}</span
        ><span v-if="DEMO_MODE" class="hp-badge">合成演示身份</span>
        <p class="hp-muted">
          {{
            DEMO_MODE
              ? '这是演示账号，修改不会影响任何真实账号。'
              : '角色与作业权限以服务端当前授权为准。'
          }}
        </p>
      </div>
    </section>
    <div class="hp-admin-columns">
      <section class="hp-card hp-card-body">
        <h2><ShieldCheck class="hp-inline-icon" />会话安全</h2>
        <p>登录凭据不写入本地长期存储。身份失效后需要重新登录。</p>
        <p>退出不会删除已保存的核对、评分审核或管理审计。</p>
        <button class="hp-btn hp-btn-secondary" @click="signOut"><LogOut />退出登录</button>
      </section>
      <section class="hp-card hp-card-body">
        <h2>数据与偏好</h2>
        <p>
          教学和审核记录由业务服务保存。演示操作保存在当前浏览器，刷新后保留，可通过顶部“重置”恢复。
        </p>
        <p class="hp-fine">
          真实账号姓名、密码和身份服务配置由部署方管理，当前页面不提供无后端支持的资料保存按钮。
        </p>
        <details>
          <summary>账号与能力详情</summary>
          <p class="hp-fine">仅用于对接与排查；这些字段本身不构成授权。</p>
          <dl class="hp-detail-list">
            <dt>内部账号</dt>
            <dd>
              <code>{{ session.userId }}</code>
            </dd>
            <dt>角色</dt>
            <dd>{{ session.role }}</dd>
            <dt>操作能力</dt>
            <dd>
              <ul>
                <li v-for="cap in session.capabilities" :key="cap">
                  <code>{{ cap }}</code>
                </li>
              </ul>
            </dd>
          </dl>
        </details>
      </section>
    </div>
  </div>
</template>
