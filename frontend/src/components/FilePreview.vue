<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { api, ApiError } from '../api/client'
const emit = defineEmits<{ denied: [] }>()
const props = defineProps<{ sid: string; name: string; type: string }>()
const url = ref(''),
  error = ref(''),
  busy = ref(false),
  controller = new AbortController()
function close() {
  if (url.value) URL.revokeObjectURL(url.value)
  url.value = ''
}
async function open() {
  if (busy.value) return
  close()
  busy.value = true
  error.value = ''
  try {
    const blob = await api.file(props.sid, controller.signal)
    if (controller.signal.aborted) return
    if (!['application/pdf', 'image/png', 'image/jpeg'].includes(blob.type))
      throw new Error('此文件类型不能安全预览。')
    url.value = URL.createObjectURL(blob)
  } catch (e) {
    error.value = (e as Error).message
    if (e instanceof ApiError && [403, 404].includes(e.status)) emit('denied')
  } finally {
    busy.value = false
  }
}
onBeforeUnmount(() => {
  controller.abort()
  close()
})
</script>
<template>
  <el-button :loading="busy" @click="open">查看原始文件</el-button>
  <p v-if="error" role="alert" class="error">{{ error }}</p>
  <el-dialog :model-value="!!url" :title="name" width="85%" @close="close"
    ><iframe
      v-if="url && type === 'application/pdf'"
      :src="url"
      title="受保护的原始 PDF"
      class="pdf-preview"
    /><img v-else-if="url" :src="url" :alt="name" class="image-preview" />
    <p class="fine">原始资料通过认证请求读取，仅在当前页面临时预览。</p></el-dialog
  >
</template>
