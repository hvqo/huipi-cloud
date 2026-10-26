<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { recovery } from '../api/recovery'
const props = defineProps<{ sessionId: string }>()
const count = ref<number>(),
  unavailable = ref(false)
let disposed = false,
  timer: ReturnType<typeof setInterval> | undefined
async function refresh() {
  const sid = props.sessionId
  if (!sid || disposed || document.hidden) return
  try {
    const value = await recovery.inspect(sid, 0, '', true)
    if (!disposed && sid === props.sessionId) {
      count.value = Object.values(value.counts).reduce((a, b) => a + b, 0)
      unavailable.value = false
    }
  } catch {
    if (sid === props.sessionId && !disposed) unavailable.value = true
  }
}
watch(
  () => props.sessionId,
  () => {
    count.value = undefined
    void refresh()
  },
)
onMounted(() => {
  void refresh()
  timer = setInterval(refresh, 30000)
  window.addEventListener('focus', refresh)
})
onUnmounted(() => {
  disposed = true
  clearInterval(timer)
  window.removeEventListener('focus', refresh)
})
</script>
<template>
  <RouterLink v-if="sessionId" class="exception-badge" :to="`/exceptions?session=${sessionId}`">
    异常处理<span v-if="!unavailable && count !== undefined"> · {{ count }}项待处理</span
    ><span v-else> · 查看当前状态</span> →
  </RouterLink>
</template>
<style scoped>
.exception-badge {
  display: inline-block;
  margin: 12px 16px 12px 0;
  color: #9a631a;
  font-weight: 600;
}
</style>
