<script setup lang="ts">
import { computed } from 'vue'
import { contentTypeLabels } from '../lib/labels'
import type { Node, Schema } from '../api/client'
const props = defineProps<{
  node: Node
  pointer: string
  related: Set<string>
  disabled: boolean
  confirmed: Schema['ConfirmedRegionSummary'][]
}>()
defineEmits<{ asset: [pointer: string] }>()
const pieces = computed(() => {
  const chars = Array.from(props.node.value || '')
  const ranges = props.confirmed.filter(
    (r) => r.content_pointer === props.pointer && r.text_start != null && r.text_end != null,
  )
  const points = [
    ...new Set([0, chars.length, ...ranges.flatMap((r) => [r.text_start!, r.text_end!])]),
  ]
    .filter((p) => p >= 0 && p <= chars.length)
    .sort((a, b) => a - b)
  return points.slice(0, -1).map((a, i) => ({
    text: chars.slice(a, points[i + 1]).join(''),
    role: ranges.find((r) => r.text_start! <= a && r.text_end! >= points[i + 1]!)?.role,
  }))
})
</script>
<template>
  <div class="source-node" :class="{ related: related.has(pointer) }">
    <div v-if="node.value !== null && node.value !== undefined" class="source-content">
      <span class="source-type">{{ contentTypeLabels[node.normalized_type] ?? '内容' }}</span
      ><span :data-source-pointer="pointer" class="selectable-text"
        ><template v-for="(piece, index) in pieces" :key="index"
          ><mark v-if="piece.role" :class="piece.role">{{ piece.text }}</mark
          ><template v-else>{{ piece.text }}</template></template
        ></span
      >
    </div>
    <div v-if="node.asset_refs?.length" class="asset-note">
      <span>含素材引用 · 请对照原始文件核对，未自动加载外部图片</span
      ><el-button
        v-if="node.normalized_type === 'image'"
        size="small"
        :disabled="disabled"
        @click="$emit('asset', pointer)"
        >选择图片证据</el-button
      >
    </div>
    <SourceNode
      v-for="(child, index) in node.children"
      :key="index"
      :node="child"
      :pointer="`${pointer}/children/${index}`"
      :related="related"
      :disabled="disabled"
      :confirmed="confirmed"
      @asset="$emit('asset', $event)"
    />
  </div>
</template>
