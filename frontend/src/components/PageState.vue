<script setup lang="ts">
defineProps<{ loading?: boolean; error?: string; empty?: boolean }>()
defineEmits<{ retry: [] }>()
</script>
<template>
  <div v-if="loading" class="state-card" role="status">
    <span class="spinner" /> 正在读取，请稍候…
  </div>
  <div v-else-if="error" class="state-card error" role="alert">
    <h3>暂时无法显示</h3>
    <p>{{ error }}</p>
    <el-button @click="$emit('retry')">重新读取</el-button>
  </div>
  <div v-else-if="empty" class="state-card">
    <span class="empty-icon">▤</span>
    <h3>这里还没有内容</h3>
    <p>新的资料准备好后，会显示在这里。</p>
  </div>
  <slot v-else />
</template>
