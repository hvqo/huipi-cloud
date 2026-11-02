<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { request } from '../api/client'

type Account = {
  user_id: string
  account: string
  display_name: string | null
  is_active: boolean
  must_change_password: boolean
}
type Page = { records: Account[]; offset: number; limit: number; has_next: boolean }
const page = ref<Page>(),
  loading = ref(false),
  error = ref(''),
  notice = ref('')
const form = reactive({
  account: '',
  display_name: '',
  password: '',
  request_id: crypto.randomUUID(),
})
const bindExisting = ref(false), existingUserId = ref('')
const resetting = ref<Account | null>(null),
  resetPassword = ref(''),
  resetId = ref(crypto.randomUUID())
function openReset(item: Account) {
  resetting.value = item
  resetPassword.value = ''
  resetId.value = crypto.randomUUID()
}
function closeReset() {
  resetting.value = null
  resetPassword.value = ''
}

async function load(offset = 0) {
  loading.value = true
  try {
    page.value = await request<Page>(`/api/v1/admin/local-accounts?limit=25&offset=${offset}`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '账号列表读取失败。'
  } finally {
    loading.value = false
  }
}
async function create() {
  error.value = notice.value = ''
  loading.value = true
  try {
    const path = bindExisting.value
      ? `/api/v1/admin/local-accounts/${encodeURIComponent(existingUserId.value.trim())}/bind`
      : '/api/v1/admin/local-accounts'
    await request(path, { body: JSON.stringify(form) })
    form.account = form.display_name = form.password = ''
    form.request_id = crypto.randomUUID()
    existingUserId.value = ''
    notice.value = '账号已创建；请通过安全方式将初始密码交给教师。首次登录须修改密码。'
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '创建未完成。'
  } finally {
    loading.value = false
  }
}
async function reset() {
  if (!resetting.value) return
  error.value = notice.value = ''
  loading.value = true
  try {
    await request(
      `/api/v1/admin/local-accounts/${encodeURIComponent(resetting.value.user_id)}/reset-password`,
      { body: JSON.stringify({ request_id: resetId.value, password: resetPassword.value }) },
    )
    resetting.value = null
    resetPassword.value = ''
    resetId.value = crypto.randomUUID()
    notice.value = '密码已重置，该教师需要重新登录并修改密码。'
    await load(page.value?.offset || 0)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '密码重置未完成。'
  } finally {
    loading.value = false
  }
}
async function setActive(item: Account) {
  error.value = notice.value = ''
  loading.value = true
  try {
    await request(`/api/v1/admin/teachers/${encodeURIComponent(item.user_id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ request_id: crypto.randomUUID(), is_active: !item.is_active }),
    })
    notice.value = item.is_active ? '账号已停用，会话已失效。' : '账号已启用。'
    await load(page.value?.offset || 0)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '操作未完成。'
  } finally {
    loading.value = false
  }
}
onMounted(() => void load())
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div>
        <h1>教师账号</h1>
        <p>开通账号或协助教师重置密码。</p>
      </div>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <section class="page-card">
      <h2>开通教师账号</h2>
      <label><input v-model="bindExisting" type="checkbox" /> 关联已有教师身份（保留原有授权）</label>
      <p v-if="bindExisting" class="muted">请先在线下核对教师身份和用户编号；关联后沿用该用户原有权限。</p>
      <form class="local-account-form" @submit.prevent="create">
        <label v-if="bindExisting">已有教师用户编号<el-input v-model="existingUserId" required /></label>
        <label>姓名<el-input v-model="form.display_name" maxlength="100" required /></label>
        <label>邮箱或手机号<el-input v-model="form.account" maxlength="254" required /></label>
        <label
          >初始密码<el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="new-password"
            minlength="6"
            maxlength="128"
            required
        /></label>
        <el-button native-type="submit" type="primary" :loading="loading">创建账号</el-button>
      </form>
      <small>初始密码至少 6 个字符，教师首次登录后须自行修改。</small>
    </section>
    <section class="page-card">
      <h2>已开通账号</h2>
      <table class="local-account-table">
        <thead>
          <tr>
            <th>姓名</th>
            <th>账号</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in page?.records" :key="item.user_id">
            <td>{{ item.display_name || '—' }}</td>
            <td>{{ item.account }}</td>
            <td>{{ item.is_active ? '已启用' : '已停用' }}</td>
            <td>
              <el-button text @click="openReset(item)">重置密码</el-button>
              <el-button text @click="setActive(item)">{{
                item.is_active ? '停用' : '启用'
              }}</el-button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="page && !page.records.length">暂无教师账号。</p>
      <div class="local-account-pager" v-if="page">
        <el-button
          :disabled="page.offset === 0"
          @click="load(Math.max(0, page.offset - page.limit))"
          >上一页</el-button
        >
        <el-button :disabled="!page.has_next" @click="load(page.offset + page.limit)"
          >下一页</el-button
        >
      </div>
    </section>
    <el-dialog
      :model-value="!!resetting"
      title="重置教师密码"
      @close="closeReset"
    >
      <p>为 {{ resetting?.display_name || '该教师' }} 设置临时密码，原有会话将失效。</p>
      <form class="local-login-form" @submit.prevent="reset">
        <label for="admin-reset-password">临时密码（至少 6 个字符）</label>
        <el-input
          id="admin-reset-password"
          v-model="resetPassword"
          type="password"
          show-password
          autocomplete="new-password"
          minlength="6"
          maxlength="128"
          required
        />
        <el-button native-type="submit" type="primary" :loading="loading">确认重置</el-button>
      </form>
    </el-dialog>
  </div>
</template>
