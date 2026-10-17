<script setup lang="ts">
import { computed } from 'vue'

/**
 * 迷你双序列折线图（纯 SVG，无图表库依赖）。
 * series: { date: 'MM-DD', a: 提交数, b: 核对数 }
 */
const props = defineProps<{
  series: { date: string; a: number; b: number }[]
  labelA: string
  labelB: string
}>()

const W = 640
const H = 220
const PAD = { top: 16, right: 12, bottom: 26, left: 34 }

const maxY = computed(() => {
  const max = Math.max(1, ...props.series.map((p) => Math.max(p.a, p.b)))
  return Math.ceil(max * 1.2)
})

const points = computed(() => {
  const innerW = W - PAD.left - PAD.right
  const innerH = H - PAD.top - PAD.bottom
  const step = props.series.length > 1 ? innerW / (props.series.length - 1) : 0
  return props.series.map((point, index) => ({
    x: PAD.left + step * index,
    ay: H - PAD.bottom - (point.a / maxY.value) * innerH,
    by: H - PAD.bottom - (point.b / maxY.value) * innerH,
    label: point.date,
  }))
})

function path(key: 'ay' | 'by') {
  return points.value
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p[key].toFixed(1)}`)
    .join(' ')
}

const yTicks = computed(() => {
  const innerH = H - PAD.top - PAD.bottom
  return [0, 0.5, 1].map((ratio) => ({
    y: H - PAD.bottom - ratio * innerH,
    value: Math.round(ratio * maxY.value),
  }))
})

const xTicks = computed(() => {
  if (points.value.length <= 8) return points.value
  const every = Math.ceil(points.value.length / 6)
  return points.value.filter((_, index) => index % every === 0 || index === points.value.length - 1)
})
</script>

<template>
  <div>
    <div class="hp-flex" style="gap: 16px; margin-bottom: 6px">
      <span class="hp-flex hp-gap-sm" style="font-size: 12px; color: var(--hp-ink-500)">
        <i style="width: 12px; height: 3px; border-radius: 2px; background: var(--hp-primary)" />{{
          labelA
        }}
      </span>
      <span class="hp-flex hp-gap-sm" style="font-size: 12px; color: var(--hp-ink-500)">
        <i style="width: 12px; height: 3px; border-radius: 2px; background: var(--hp-teal)" />{{
          labelB
        }}
      </span>
    </div>
    <svg
      class="hp-chart"
      :viewBox="`0 0 ${W} ${H}`"
      role="img"
      :aria-label="`${labelA}与${labelB}趋势`"
    >
      <g v-for="tick in yTicks" :key="'y' + tick.y" stroke="var(--hp-border)" stroke-width="1">
        <line :x1="PAD.left" :x2="W - PAD.right" :y1="tick.y" :y2="tick.y" />
        <text
          :x="PAD.left - 6"
          :y="tick.y + 4"
          text-anchor="end"
          font-size="11"
          fill="var(--hp-ink-400)"
        >
          {{ tick.value }}
        </text>
      </g>
      <g v-for="tick in xTicks" :key="'x' + tick.x">
        <text :x="tick.x" :y="H - 8" text-anchor="middle" font-size="11" fill="var(--hp-ink-400)">
          {{ tick.label }}
        </text>
      </g>
      <path
        :d="path('ay')"
        fill="none"
        stroke="var(--hp-primary)"
        stroke-width="2.5"
        stroke-linejoin="round"
        stroke-linecap="round"
      />
      <path
        :d="path('by')"
        fill="none"
        stroke="var(--hp-teal)"
        stroke-width="2.5"
        stroke-linejoin="round"
        stroke-linecap="round"
      />
      <g v-for="(p, index) in points" :key="index">
        <circle v-if="series[index]!.a > 0" :cx="p.x" :cy="p.ay" r="3" fill="var(--hp-primary)" />
        <circle v-if="series[index]!.b > 0" :cx="p.x" :cy="p.by" r="3" fill="var(--hp-teal)" />
      </g>
    </svg>
  </div>
</template>
