<script setup lang="ts">
import { computed } from 'vue'
import { Info } from 'lucide-vue-next'
import { UnsupportedCapabilityError } from '../services/types'

/**
 * 后端能力缺口面板：数据源为真实后端且接口尚未提供时如实展示，
 * 不伪造数据、不放假按钮。演示模式下不会出现本组件。
 */
const props = defineProps<{ error?: unknown; summary?: string; detail?: string }>()

const info = computed(() => {
  if (props.error instanceof UnsupportedCapabilityError)
    return { summary: props.error.summary, capability: props.error.capability }
  return { summary: props.summary ?? '该功能暂不可用', capability: '' }
})
</script>

<template>
  <div class="hp-note hp-note-info" role="note">
    <Info aria-hidden="true" />
    <div>
      <b>{{ info.summary }}</b>
      <p class="hp-fine" style="margin-top: 4px">
        {{
          detail ??
          '当前为真实数据模式，此功能依赖的后端接口尚未开发。切换到演示模式（npm run dev:demo）可体验完整交互；接口就绪后此处会自动展示真实数据。'
        }}
      </p>
    </div>
  </div>
</template>
