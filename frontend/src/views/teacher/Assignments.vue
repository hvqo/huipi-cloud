<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Plus, Search, X } from 'lucide-vue-next'
import { services, type S } from '../../services'
import { DEMO_MODE } from '../../app/mode'
import { assignmentStatusLabels, formatRelative } from '../../lib/labels'
import { hasCapability } from '../../app/session'
import EmptyState from '../../components/EmptyState.vue'
import PageState from '../../components/PageState.vue'
import PaginationBar from '../../components/PaginationBar.vue'

/**
 * 我的作业：卡片化列表 + 关键词搜索 + 状态筛选 + 新建作业。
 * 搜索在真实模式作用于最近加载的作业（后端暂无查询参数），如实提示。
 */
const route = useRoute(),
  router = useRouter()
const state = reactive({
  loading: false,
  error: '',
  rows: [] as S['AssignmentSummary'][],
  has_next: false,
  offset: 0,
})
const keyword = ref(''),
  statusFilter = ref<'all' | 'draft' | 'published'>('all'),
  dialog = ref(false),
  saving = ref(false),
  saveError = ref('')
const form = ref({ title: '', subject: '', grade_level: '', description: '' })

async function load(offset = 0) {
  state.loading = true
  state.error = ''
  try {
    const result = await (
      await services()
    ).assignments.list({
      limit: 12,
      offset,
      keyword: keyword.value || undefined,
      status: statusFilter.value,
    })
    state.rows = result.records
    state.has_next = result.has_next
    state.offset = offset
  } catch (e) {
    state.error = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    state.loading = false
  }
}

function search() {
  void load(0)
}

async function create() {
  saving.value = true
  saveError.value = ''
  try {
    const created = await (
      await services()
    ).assignments.create({
      title: form.value.title,
      subject: form.value.subject,
      grade_level: form.value.grade_level,
      description: form.value.description || null,
    })
    dialog.value = false
    await router.push(`/assignments/${created.id}`)
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : '创建未完成。'
  } finally {
    saving.value = false
  }
}

function subjectClass(subject: string) {
  if (subject.includes('物') || subject.includes('化') || subject.includes('生'))
    return 'is-physics'
  return 'is-math'
}

onMounted(() => {
  if (route.query.create === '1') dialog.value = true
  void load(0)
})
</script>
<template>
  <div class="hp-section-gap">
    <div class="hp-pagehead">
      <div>
        <h1>我的作业</h1>
        <p class="hp-pagehead-desc">
          管理你创建和被授权的作业：题目、评分细则、学生提交与批阅进度都从这里进入。
        </p>
      </div>
      <div class="hp-pagehead-actions">
        <button
          v-if="hasCapability('assignment:create')"
          class="hp-btn hp-btn-primary hp-btn-lg"
          type="button"
          @click="dialog = true"
        >
          ＋ 新建作业
        </button>
      </div>
    </div>

    <div class="hp-filterbar">
      <div class="hp-field" style="flex: none">
        <div style="position: relative">
          <input
            v-model="keyword"
            class="hp-input"
            placeholder="搜索作业名称、学科、年级…"
            style="padding-left: 34px"
            @keyup.enter="search"
          />
          <Search
            aria-hidden="true"
            style="
              position: absolute;
              left: 11px;
              top: 11px;
              width: 15px;
              height: 15px;
              color: var(--hp-ink-400);
            "
          />
        </div>
      </div>
      <div class="hp-filter-chips" role="group" aria-label="状态筛选">
        <button
          v-for="option in [
            ['all', '全部'],
            ['published', '已发布'],
            ['draft', '草稿'],
          ] as const"
          :key="option[0]"
          class="hp-filter-chip"
          :class="{ 'is-active': statusFilter === option[0] }"
          type="button"
          @click="((statusFilter = option[0]), load(0))"
        >
          {{ option[1] }}
        </button>
      </div>
      <button
        v-if="keyword"
        class="hp-btn hp-btn-ghost hp-btn-sm"
        type="button"
        @click="((keyword = ''), load(0))"
      >
        <X aria-hidden="true" />清除搜索
      </button>
    </div>

    <p v-if="!DEMO_MODE" class="hp-fine">
      搜索与状态筛选仅作用于当前加载页，尚未提供全量搜索接口；可继续翻页查看。
    </p>
    <PageState
      :loading="state.loading && !state.rows.length"
      :error="state.error"
      @retry="load(state.offset)"
    >
      <EmptyState
        v-if="!state.rows.length && !state.loading"
        title="还没有作业"
        desc="创建第一份作业，上传学生答卷后，系统会自动解析并生成作答内容，即可开始批阅。"
      >
        <button class="hp-btn hp-btn-primary" type="button" @click="dialog = true">
          <Plus aria-hidden="true" />新建作业
        </button>
      </EmptyState>
      <template v-else>
        <div class="hp-assignment-grid">
          <RouterLink
            v-for="row in state.rows"
            :key="row.id"
            :to="`/assignments/${row.id}`"
            class="hp-assignment-card"
          >
            <div class="hp-assignment-top">
              <div class="hp-subject-chip" :class="subjectClass(row.subject)">
                {{ row.subject.slice(0, 1) }}
              </div>
              <div class="hp-grow" style="min-width: 0">
                <div class="hp-assignment-title">{{ row.title }}</div>
                <div class="hp-assignment-meta">{{ row.subject }} · {{ row.grade_level }}</div>
              </div>
              <span class="hp-badge" :class="row.status === 'published' ? 'hp-badge-success' : ''">
                <span v-if="row.status === 'published'" class="dot" />
                {{ assignmentStatusLabels[row.status] }}</span
              >
            </div>
            <p class="hp-assignment-desc">{{ row.description || '暂无作业说明' }}</p>
            <div class="hp-assignment-foot">
              <span>更新于 {{ formatRelative(row.updated_at) }}</span>
              <span class="hp-linkbtn">查看作业 <span aria-hidden="true">↗</span></span>
            </div>
          </RouterLink>
        </div>
        <div class="hp-card">
          <PaginationBar
            :offset="state.offset"
            :limit="12"
            :has-next="state.has_next"
            :loading="state.loading"
            @change="load"
          />
        </div>
      </template>
    </PageState>

    <el-dialog v-model="dialog" title="新建作业" width="560" :close-on-click-modal="false">
      <form id="create-assignment" class="hp-form" @submit.prevent="create">
        <label class="hp-field">
          <span class="hp-field-label">作业名称</span>
          <el-input
            v-model="form.title"
            maxlength="200"
            required
            placeholder="例如：函数与导数 · 课后练习"
          />
        </label>
        <div class="hp-form-row">
          <label class="hp-field">
            <span class="hp-field-label">学科</span>
            <el-input v-model="form.subject" maxlength="100" required placeholder="例如：数学" />
          </label>
          <label class="hp-field">
            <span class="hp-field-label">年级</span>
            <el-input
              v-model="form.grade_level"
              maxlength="50"
              required
              placeholder="例如：高二（3）班"
            />
          </label>
        </div>
        <label class="hp-field">
          <span class="hp-field-label">说明 <small>（可选）</small></span>
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            placeholder="作业范围、来源或批阅要求"
          />
        </label>
        <p v-if="saveError" role="alert" class="hp-error-text">{{ saveError }}</p>
        <p class="hp-fine">
          新建后为草稿状态。完成题目与评分细则后可发布；只有你和你授权的同事能看到这份作业。
        </p>
      </form>
      <template #footer>
        <button class="hp-btn hp-btn-secondary" type="button" @click="dialog = false">取消</button>
        <button
          class="hp-btn hp-btn-primary"
          type="submit"
          form="create-assignment"
          :disabled="!form.title.trim() || !form.subject.trim() || !form.grade_level.trim()"
          :style="saving ? 'opacity: .7' : ''"
        >
          创建草稿
        </button>
      </template>
    </el-dialog>
  </div>
</template>
