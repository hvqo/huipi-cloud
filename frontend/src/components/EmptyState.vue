<script setup lang="ts">
/**
 * 空状态：轻量线条插画 + 一句话引导 + 可选操作按钮。
 */
defineProps<{ title: string; desc?: string }>()
</script>

<template>
  <div class="hp-empty">
    <svg class="hp-empty-art" viewBox="0 0 96 72" fill="none" aria-hidden="true">
      <rect x="14" y="18" width="44" height="34" rx="4" fill="currentColor" opacity="0.08" />
      <rect x="14" y="18" width="44" height="34" rx="4" stroke="currentColor" stroke-width="1.6" />
      <path
        d="M22 28h20M22 35h28M22 42h14"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        opacity="0.6"
      />
      <rect x="44" y="30" width="38" height="28" rx="4" fill="#0e9f8e" opacity="0.12" />
      <rect x="44" y="30" width="38" height="28" rx="4" stroke="#0e9f8e" stroke-width="1.6" />
      <path
        d="M52 39h16M52 46h22M52 53h10"
        stroke="#0e9f8e"
        stroke-width="1.8"
        stroke-linecap="round"
        opacity="0.7"
      />
      <circle cx="24" cy="12" r="2.5" fill="currentColor" opacity="0.4" />
      <circle cx="78" cy="20" r="2" fill="#0e9f8e" opacity="0.5" />
      <circle cx="88" cy="46" r="1.6" fill="currentColor" opacity="0.3" />
    </svg>
    <div class="hp-empty-title">{{ title }}</div>
    <p v-if="desc" class="hp-empty-desc">{{ desc }}</p>
    <slot />
  </div>
</template>
