<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { BookOpen, Files, ClipboardCheck, ChartColumn } from 'lucide-vue-next'
import { services, UnsupportedCapabilityError, type InsightsSummary } from '../../services'
import PageState from '../../components/PageState.vue'
import UnsupportedPanel from '../../components/UnsupportedPanel.vue'
import MiniLineChart from '../../components/MiniLineChart.vue'
import EmptyState from '../../components/EmptyState.vue'
const data = ref<InsightsSummary | null>(null),
  range = ref<'7d' | '30d' | 'all'>('7d'),
  loading = ref(false),
  error = ref(''),
  unsupported = ref(false)
const chart = computed(
  () =>
    data.value?.series.map((p) => ({ date: p.date.slice(5), a: p.submissions, b: p.reviews })) ||
    [],
)
const success = computed(() => {
  if (!data.value) return '—'
  const finished = data.value.parse_succeeded + data.value.parse_failed
  return finished ? `${Math.round((data.value.parse_succeeded / finished) * 100)}%` : '—'
})
async function load() {
  loading.value = true
  error.value = ''
  unsupported.value = false
  try {
    data.value = await (await services()).insights.summary(range.value)
  } catch (e) {
    if (e instanceof UnsupportedCapabilityError) unsupported.value = true
    else error.value = e instanceof Error ? e.message : '读取失败'
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>
<template>
  <div class="hp-section-gap">
    <div class="hp-pagehead">
      <div>
        <h1>教学统计</h1>
        <p class="hp-pagehead-desc">观察提交、解析与人工核对工作量，为教学安排提供参考。</p>
      </div>
      <select v-model="range" class="hp-select" aria-label="统计时间范围" @change="load">
        <option value="7d">近 7 天</option>
        <option value="30d">近 30 天</option>
        <option value="all">全部</option>
      </select>
    </div>
    <PageState :loading="loading" :error="error" @retry="load"
      ><UnsupportedPanel
        v-if="unsupported"
        title="教学统计暂未接入"
        summary="统计汇总接口尚未开放" /><template v-else-if="data"
        ><p class="hp-fine">
          {{ data.scope_note }} 当前范围：{{ data.range_label }}；进度指标按提交批次统计。
        </p>
        <div class="hp-stats">
          <div class="hp-stat">
            <div class="hp-stat-icon"><BookOpen /></div>
            <div>
              <b class="hp-stat-num">{{ data.assignments_total }}</b>
              <p class="hp-stat-label">作业数（当前可访问）</p>
            </div>
          </div>
          <div class="hp-stat">
            <div class="hp-stat-icon is-teal"><Files /></div>
            <div>
              <b class="hp-stat-num">{{ data.submissions_total }}</b>
              <p class="hp-stat-label">范围内提交</p>
            </div>
          </div>
          <div class="hp-stat">
            <div class="hp-stat-icon is-amber"><ClipboardCheck /></div>
            <div>
              <b class="hp-stat-num">{{ data.review_done }}</b>
              <p class="hp-stat-label">已核对题次</p>
            </div>
          </div>
          <div class="hp-stat">
            <div class="hp-stat-icon"><ChartColumn /></div>
            <div>
              <b class="hp-stat-num">{{ success }}</b>
              <p class="hp-stat-label">已结束任务解析成功率</p>
            </div>
          </div>
        </div>
        <div class="hp-admin-columns">
          <section class="hp-card">
            <header class="hp-card-head"><h2 class="hp-card-title">提交与核对趋势</h2></header>
            <div class="hp-card-body">
              <MiniLineChart :series="chart" label-a="学生提交" label-b="人工核对" />
            </div>
          </section>
          <section class="hp-card">
            <header class="hp-card-head"><h2 class="hp-card-title">处理进度</h2></header>
            <div class="hp-card-body">
              <div
                v-for="item in [
                  { name: '解析完成', n: data.parse_succeeded },
                  { name: '解析失败', n: data.parse_failed },
                  { name: '等待或解析中', n: data.parse_pending },
                  { name: '待核对题次', n: data.review_pending },
                  { name: '评分候选已审核', n: data.grading_reviewed },
                  { name: '评分候选待审核', n: data.grading_pending },
                ]"
                :key="item.name"
                class="hp-module-row"
              >
                <span>{{ item.name }}</span
                ><b>{{ item.n }}</b>
              </div>
            </div>
          </section>
        </div>
        <section class="hp-card">
          <header class="hp-card-head"><h2 class="hp-card-title">作业工作量</h2></header>
          <div class="hp-table-wrap">
            <table v-if="data.workload.length" class="hp-table">
              <thead>
                <tr>
                  <th>作业</th>
                  <th>提交</th>
                  <th>已核对题次</th>
                  <th>待核对题次</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in data.workload" :key="row.assignment_title">
                  <td>{{ row.assignment_title }}</td>
                  <td>{{ row.submissions }}</td>
                  <td>{{ row.reviewed }}</td>
                  <td>{{ row.pending }}</td>
                </tr>
              </tbody>
            </table>
            <EmptyState v-else title="这个时间范围还没有提交" />
          </div></section></template
    ></PageState>
    <p class="hp-note">
      统计不包含正式成绩、班级均分或成绩排名；候选分与人工审核记录都不代表成绩已发布。
    </p>
  </div>
</template>
