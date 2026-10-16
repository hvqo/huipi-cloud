import { onBeforeUnmount, ref } from 'vue'
export function useLoad() {
  const loading = ref(false),
    error = ref('')
  let controller: AbortController | null = null
  async function run(action: (signal: AbortSignal) => Promise<void>) {
    controller?.abort()
    const active = new AbortController()
    controller = active
    loading.value = true
    error.value = ''
    try {
      await action(active.signal)
    } catch (e) {
      if (!active.signal.aborted) error.value = e instanceof Error ? e.message : '读取未完成。'
    } finally {
      if (controller === active) loading.value = false
    }
  }
  onBeforeUnmount(() => controller?.abort())
  return { loading, error, run }
}
export function date(value: string) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}
export function status(value: string) {
  return (
    (
      {
        draft: '草稿',
        published: '已发布',
        pending: '待解析',
        running: '解析中',
        succeeded: '解析完成',
        failed: '解析失败',
        retry_wait: '等待重试',
        available: '已生成',
        not_generated: '尚未生成',
        aligned: '已关联',
        complete: '关联完成',
        review_required: '需核对',
        not_observed: '未观察到题号',
      } as Record<string, string>
    )[value] || value
  )
}
