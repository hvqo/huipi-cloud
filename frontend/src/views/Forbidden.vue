<script setup lang="ts">
import { Lock } from 'lucide-vue-next'
</script>
<template>
  <div class="hp-card">
    <div class="hp-card-body hp-state-card">
      <Lock aria-hidden="true" style="width: 34px; height: 34px; color: var(--hp-ink-400)" />
      <h3>没有访问权限</h3>
      <p>
        当前账号无法访问管理员页面。权限由服务端实时校验，如需管理员能力，请联系部署管理员登记。
      </p>
      <RouterLink class="hp-btn hp-btn-secondary" to="/workbench" style="margin-top: 6px"
        >返回工作台</RouterLink
      >
    </div>
  </div>
</template>
