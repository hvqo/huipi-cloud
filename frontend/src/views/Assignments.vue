<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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
const search = ref(''),
  statusFilter = ref('all'),
  viewMode = ref<'grid' | 'list'>('grid')
const visibleRows = computed(() =>
  rows.value.filter((row) => {
    const term = search.value.trim().toLocaleLowerCase()
    return (
      (!term ||
        [row.title, row.subject, row.grade_level].some((value) =>
          value.toLocaleLowerCase().includes(term),
        )) &&
      (statusFilter.value === 'all' || row.status === statusFilter.value)
    )
  }),
)
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
  <div class="assignment-toolbar" aria-label="作业筛选">
    <div class="assignment-filters">
      <label class="filter-field"
        ><span>本页筛选</span
        ><el-input
          v-model="search"
          clearable
          placeholder="搜索作业、学科或年级"
          aria-label="本页搜索作业"
      /></label>
      <label class="filter-field"
        ><span>作业状态</span
        ><select v-model="statusFilter" aria-label="作业状态">
          <option value="all">全部状态</option>
          <option value="draft">草稿</option>
          <option value="published">已发布</option>
        </select></label
      >
    </div>
    <div class="view-switch" aria-label="作业显示方式">
      <button type="button" :aria-pressed="viewMode === 'grid'" @click="viewMode = 'grid'">
        卡片</button
      ><button type="button" :aria-pressed="viewMode === 'list'" @click="viewMode = 'list'">
        列表
      </button>
    </div>
  </div>
  <PageState
    :loading="state.loading.value"
    :error="state.error.value"
    :empty="!rows.length"
    @retry="load"
    ><div
      v-if="visibleRows.length"
      :class="viewMode === 'grid' ? 'assignment-grid' : 'assignment-list'"
    >
      <RouterLink
        v-for="row in visibleRows"
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
    </div>
    <div v-else class="state-card">
      <h3>本页没有匹配的作业</h3>
      <p>试试其他筛选条件，或翻页查看。</p>
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
