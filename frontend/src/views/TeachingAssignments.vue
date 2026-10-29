<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { id, request } from '../api/client'

type Option = { id: string; name?: string }
type Teaching = { id: string; teacher_id: string; class_id: string; term_id: string; subject_id: string }
type Options = { teachers: Option[]; classes: Option[]; terms: Option[]; subjects: Option[]; assignments: Teaching[]; truncated: boolean }
const data = ref<Options>(), busy = ref(false), error = ref(''), notice = ref('')
const form = ref({ teacher_id: '', class_id: '', term_id: '', subject_id: '' })
async function load() {
  try { data.value = await request<Options>('/api/v1/homework/admin/teaching-assignments') }
  catch (e) { error.value = e instanceof Error ? e.message : '任教关系读取失败。' }
}
async function grant() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    await request('/api/v1/homework/admin/teaching-assignments', { body: JSON.stringify(form.value), errorCode: true })
    notice.value = '任教关系已保存。'; await load()
  } catch (e) { error.value = e instanceof Error ? e.message : '保存未完成。' }
  finally { busy.value = false }
}
async function revoke(value: Teaching) {
  if (busy.value || !window.confirm('撤销后，该教师将不能再通过此任教关系访问该班作业。确认撤销？')) return
  busy.value = true; error.value = ''
  try {
    await request(`/api/v1/homework/admin/teaching-assignments/${id(value.id)}`, { method: 'DELETE', errorCode: true })
    notice.value = '任教关系已撤销。'; await load()
  } catch (e) { error.value = e instanceof Error ? e.message : '撤销未完成，请刷新核对。' }
  finally { busy.value = false }
}
function name(items: Option[] | undefined, key: string) { return items?.find(x => x.id === key)?.name || key }
onMounted(load)
</script>
<template>
  <div class="page-heading"><div><p class="eyebrow">学校管理</p><h1>教师任教关系</h1><p class="muted">登记教师、本学期、学科与班级的真实关系，供日常作业布置使用。</p></div></div>
  <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
  <template v-if="data">
    <p v-if="data.truncated" role="alert">可选记录超过 100 条；当前只显示前 100 条，请联系管理员通过受控接口处理其余记录。</p>
    <section class="panel"><h2>登记任教关系</h2><p>教师账号显示内部编号；请先与账号管理记录核对。</p>
      <form @submit.prevent="grant">
        <label>教师账号 <select v-model="form.teacher_id" required><option value="">请选择</option><option v-for="x in data.teachers" :key="x.id" :value="x.id">{{ x.id }}</option></select></label>
        <label>学期 <select v-model="form.term_id" required><option value="">请选择</option><option v-for="x in data.terms" :key="x.id" :value="x.id">{{ x.name }}</option></select></label>
        <label>学科 <select v-model="form.subject_id" required><option value="">请选择</option><option v-for="x in data.subjects" :key="x.id" :value="x.id">{{ x.name }}</option></select></label>
        <label>行政班 <select v-model="form.class_id" required><option value="">请选择</option><option v-for="x in data.classes" :key="x.id" :value="x.id">{{ x.name }}</option></select></label>
        <button :disabled="busy">保存任教关系</button>
      </form>
    </section>
    <section class="panel"><h2>当前任教关系</h2><table><thead><tr><th>教师账号</th><th>学期</th><th>学科</th><th>班级</th><th>操作</th></tr></thead><tbody>
      <tr v-for="x in data.assignments" :key="x.id"><td>{{ x.teacher_id }}</td><td>{{ name(data.terms, x.term_id) }}</td><td>{{ name(data.subjects, x.subject_id) }}</td><td>{{ name(data.classes, x.class_id) }}</td><td><button :disabled="busy" @click="revoke(x)">撤销</button></td></tr>
    </tbody></table><p v-if="!data.assignments.length">尚无任教关系。</p></section>
  </template>
</template>
<style scoped>
label { display: block; margin: 12px 0; } select, button { font: inherit; padding: 7px; } table { width: 100%; border-collapse: collapse; } th, td { padding: 10px; text-align: left; border-bottom: 1px solid #e1e7ef; }
</style>
