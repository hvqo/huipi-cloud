<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type Schema } from '../api/client'
import PageState from '../components/PageState.vue'
import { useLoad, date, status } from '../lib/load'
const router = useRouter(),
  state = useLoad(),
  rows = ref<Schema['AssignmentSummary'][]>([]),
  offset = ref(0),
  dialog = ref(false),
  saving = ref(false),
  saveError = ref('')
const form = ref({ title: '', subject: '', grade_level: '', description: '' })
function load() {
  return state.run(async (signal) => {
    rows.value = await api.assignments(offset.value, signal)
  })
}
async function create() {
  saving.value = true
  saveError.value = ''
  try {
    const value = await api.create(form.value)
    dialog.value = false
    await router.push(`/assignments/${value.id}`)
  } catch (e) {
    saveError.value = (e as Error).message
  } finally {
    saving.value = false
  }
}
function paginate(_kind: string, delta: number) {
  offset.value += delta
  void load()
}
onMounted(load)
</script>
<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">教学管理</p>
      <h1>我的作业</h1>
      <p class="muted">查看已授权的作业，跟进提交与人工复核。</p>
    </div>
    <el-button type="primary" size="large" @click="dialog = true">＋ 新建作业</el-button>
  </div>
  <PageState
    :loading="state.loading.value"
    :error="state.error.value"
    :empty="!rows.length"
    @retry="load"
    ><div class="assignment-grid">
      <RouterLink
        v-for="row in rows"
        :key="row.id"
        :to="`/assignments/${row.id}`"
        class="assignment-card"
        ><div class="card-top">
          <span class="subject-icon">{{ row.subject.slice(0, 1) }}</span
          ><span class="pill" :class="row.status">{{ status(row.status) }}</span>
        </div>
        <h2>{{ row.title }}</h2>
        <p>{{ row.subject }} <span class="separator">/</span> {{ row.grade_level }}</p>
        <p class="description">{{ row.description || '暂无作业说明' }}</p>
        <div class="card-bottom">
          <span>{{ date(row.created_at) }}</span
          ><strong>查看作业 ↗</strong>
        </div></RouterLink
      >
    </div></PageState
  >
  <div class="pager">
    <el-button :disabled="offset === 0 || state.loading.value" @click="paginate('offset', -12)"
      >上一页</el-button
    ><span>第 {{ offset / 12 + 1 }} 页</span
    ><el-button :disabled="rows.length < 12 || state.loading.value" @click="paginate('offset', 12)"
      >下一页</el-button
    >
  </div>
  <el-dialog v-model="dialog" title="新建作业" width="520" :close-on-click-modal="false"
    ><form id="create-assignment" @submit.prevent="create">
      <label
        >作业名称<el-input
          v-model="form.title"
          maxlength="200"
          required
          placeholder="例如：函数与导数 · 课后练习"
      /></label>
      <div class="form-row">
        <label>学科<el-input v-model="form.subject" maxlength="100" required /></label
        ><label>年级<el-input v-model="form.grade_level" maxlength="50" required /></label>
      </div>
      <label>说明（可选）<el-input v-model="form.description" type="textarea" :rows="3" /></label>
      <p v-if="saveError" role="alert" class="error">{{ saveError }}</p>
    </form>
    <template #footer
      ><el-button @click="dialog = false">取消</el-button
      ><el-button
        type="primary"
        native-type="submit"
        form="create-assignment"
        :loading="saving"
        :disabled="!form.title.trim() || !form.subject.trim() || !form.grade_level.trim()"
        >创建草稿</el-button
      ></template
    ></el-dialog
  >
</template>
