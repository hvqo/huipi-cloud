<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { services } from '../services'
import { ApiError } from '../api/client'

/**
 * 受保护原始文件预览：认证读取 → Blob URL → iframe/img，
 * 关闭与销毁均 revoke，不把令牌放入 URL。
 * 真实模式读取学生原文件；演示模式读取合成试卷图片（同一管线）。
 */
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
    const blob = await (await services()).submissions.file(props.sid, controller.signal)
    if (controller.signal.aborted) return
    if (!['application/pdf', 'image/png', 'image/jpeg'].includes(blob.type))
      throw new Error('此文件类型不能安全预览。')
    url.value = URL.createObjectURL(blob)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '预览未完成。'
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
    <p class="hp-fine" style="padding: 8px 4px">
      原始资料通过认证请求读取，仅在当前页面临时预览，不会外链原始文件。
    </p></el-dialog
  >
</template>
