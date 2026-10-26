<script setup lang="ts">
defineProps<{ offset: number; hasNext: boolean; label: string; disabled?: boolean }>()
defineEmits<{ change: [offset: number] }>()
</script>
<template>
  <nav class="school-pages" :aria-label="label + '分页'">
    <button :disabled="disabled || offset === 0" @click="$emit('change', Math.max(0, offset - 25))">
      上一页{{ label }}
    </button>
    <span>第{{ Math.floor(offset / 25) + 1 }}页</span>
    <button :disabled="disabled || !hasNext" @click="$emit('change', offset + 25)">
      下一页{{ label }}
    </button>
  </nav>
</template>
<style scoped>
.school-pages {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 16px 0;
  color: #627783;
  font-size: 14px;
}
button {
  padding: 7px 12px;
  border: 1px solid #cbd8df;
  border-radius: 6px;
  background: #fff;
  color: #254957;
  font: inherit;
  cursor: pointer;
}
button:hover:not(:disabled) {
  border-color: #237e83;
  color: #176c72;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
