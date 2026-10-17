<script setup lang="ts">
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'

/**
 * 分页条：基于 offset 分页合同（与后端 limit/offset/has_next 一致）。
 */
const props = withDefaults(
  defineProps<{
    offset: number
    limit: number
    total?: number
    hasNext: boolean
    loading?: boolean
  }>(),
  { total: undefined, loading: false },
)
const emit = defineEmits<{ change: [offset: number] }>()

function page(delta: number) {
  const next = props.offset + delta * props.limit
  if (next < 0) return
  emit('change', next)
}
</script>

<template>
  <div class="hp-table-foot">
    <span v-if="total != null">共 {{ total }} 条</span>
    <span v-else>{{ offset + 1 }}–{{ offset + limit }} 条</span>
    <span class="hp-flex" style="gap: 8px">
      <button
        class="hp-btn hp-btn-secondary hp-btn-sm"
        type="button"
        :disabled="offset === 0 || loading"
        @click="page(-1)"
      >
        <ChevronLeft aria-hidden="true" />上一页
      </button>
      <span v-if="total != null && limit > 0">第 {{ Math.floor(offset / limit) + 1 }} 页</span>
      <button
        class="hp-btn hp-btn-secondary hp-btn-sm"
        type="button"
        :disabled="!hasNext || loading"
        @click="page(1)"
      >
        下一页<ChevronRight aria-hidden="true" />
      </button>
    </span>
  </div>
</template>
