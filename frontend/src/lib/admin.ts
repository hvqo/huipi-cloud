import { ref } from 'vue'
import { ApiError } from '../api/client'
/** Keep the exact operation in memory on uncertain acknowledgement. Never silently change UUID. */
export function useAdminMutation() {
  const busy = ref(false),
    error = ref(''),
    uncertain = ref(false),
    notice = ref('')
  let execute: (() => Promise<unknown>) | null = null
  let after: (() => Promise<void>) | null = null
  async function retry() {
    if (!execute || busy.value) return
    busy.value = true
    error.value = ''
    uncertain.value = false
    try {
      await execute()
      execute = null
      notice.value = '操作已保存，管理审计已记录。'
      try {
        await after?.()
      } catch {
        error.value = '操作已保存，但列表读取失败，请重新读取。'
      }
    } catch (e) {
      uncertain.value = e instanceof ApiError && e.uncertain
      error.value = e instanceof Error ? e.message : '操作未完成。'
      if (!uncertain.value) execute = null
    } finally {
      busy.value = false
    }
  }
  async function run(action: () => Promise<unknown>, refresh: () => Promise<void>) {
    if (execute || busy.value) return
    execute = action
    after = refresh
    notice.value = ''
    await retry()
  }
  return { busy, error, uncertain, notice, run, retry }
}
