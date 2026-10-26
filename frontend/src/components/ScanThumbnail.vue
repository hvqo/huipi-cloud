<script lang="ts">
let active = 0
const waiting: Array<() => void> = []
async function thumbnailSlot(work: () => Promise<void>) {
  if (active >= 2) {
    if (waiting.length >= 40) throw new Error('thumbnail_queue_full')
    // A released slot is transferred directly to the next waiter.
    await new Promise<void>((resolve) => waiting.push(resolve))
  } else active++
  try {
    await work()
  } finally {
    const next = waiting.shift()
    if (next) next()
    else active--
  }
}
</script>
<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { scans } from '../api/scans'
const props = defineProps<{ pageId: string; loadPreview?: (id: string) => Promise<Blob> }>()
const root = ref<HTMLElement>(),
  url = ref(''),
  failed = ref(false)
let observer: IntersectionObserver | undefined,
  alive = true
onMounted(() => {
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        observer?.disconnect()
        void load()
      }
    },
    { rootMargin: '0px' },
  )
  if (root.value) observer.observe(root.value)
})
async function load() {
  try {
    await thumbnailSlot(async () => {
      if (!alive) return
      const blob = await (props.loadPreview || scans.preview)(props.pageId)
      if (alive) url.value = URL.createObjectURL(blob)
    })
  } catch {
    failed.value = true
  }
}
onUnmounted(() => {
  alive = false
  observer?.disconnect()
  if (url.value) URL.revokeObjectURL(url.value)
})
</script>
<template>
  <span ref="root" class="scan-thumb"
    ><img v-if="url" :src="url" alt="扫描页缩略图" loading="lazy" /><span v-else>{{
      failed ? '点击重试预览' : '扫描页'
    }}</span></span
  >
</template>
<style scoped>
.scan-thumb {
  display: flex;
  height: 126px;
  width: 100%;
  align-items: center;
  justify-content: center;
  background: #f5f7fa;
}
.scan-thumb img {
  max-width: 100%;
  max-height: 126px;
  object-fit: contain;
}
.scan-thumb span {
  font-size: 13px;
  color: #60718a;
}
</style>
