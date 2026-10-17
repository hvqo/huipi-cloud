<script setup lang="ts">
import { Compass } from 'lucide-vue-next'
</script>
<template>
  <div class="hp-card">
    <div class="hp-card-body hp-state-card">
      <Compass aria-hidden="true" style="width: 34px; height: 34px; color: var(--hp-ink-400)" />
      <h3>页面不存在</h3>
      <p>你访问的地址没有对应的页面，可能已被移动或从未存在。</p>
      <RouterLink class="hp-btn hp-btn-primary" to="/workbench" style="margin-top: 6px"
        >返回工作台</RouterLink
      >
    </div>
  </div>
</template>
