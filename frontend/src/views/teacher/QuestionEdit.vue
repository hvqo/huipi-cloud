<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowDown, ArrowLeft, ArrowUp, Plus, Save, Trash2 } from 'lucide-vue-next'
import { DEMO_MODE } from '../../app/mode'
import { services, type S } from '../../services'
import { UnsupportedCapabilityError } from '../../services/types'
import { formatScore, label, questionTypeLabels } from '../../lib/labels'
import { sumDecimals, validateScoreInput } from '../../lib/decimal'
import PageState from '../../components/PageState.vue'

/**
 * 题目与评分细则编辑器。
 * 真实模式：新增题目、标准答案、评分细则是既有 API；题目正文修改为后端缺口，
 * 相应字段只读并说明。演示模式：全部可编辑。
 */
const route = useRoute(),
  router = useRouter(),
  aid = String(route.params.aid),
  qidParam = String(route.params.qid),
  isNew = qidParam === 'new'

const state = reactive({
  loading: false,
  error: '',
  assignmentTitle: '',
  question: null as S['QuestionRead'] | null,
  canWrite: false,
})
const notice = ref('')
const saveError = ref('')
const saving = ref(false)

const form = reactive({
  question_number: 1,
  question_type: 'short_answer' as S['QuestionType'],
  stem: '',
  max_score: '10.00',
  answer: '',
})
const criteria = ref<{ description: string; points: string }[]>([])

const bodyEditable = computed(() => state.canWrite && (isNew || DEMO_MODE))
const rubricTotal = computed(() => {
  try {
    return criteria.value.length
      ? formatScore(sumDecimals(criteria.value.map((c) => c.points)))
      : '0'
  } catch {
    return '—'
  }
})
const maxScoreError = computed(() =>
  form.max_score && validateScoreInput(form.max_score, '9999') ? '满分格式不正确' : '',
)

async function load() {
  state.loading = true
  state.error = ''
  try {
    const svc = await services()
    const [detail, workbench] = await Promise.all([
      svc.assignments.detail(aid),
      svc.assignments.workbench(aid, 0),
    ])
    state.assignmentTitle = detail.title
    state.canWrite = workbench.can_write
    if (isNew) {
      form.question_number = detail.questions.length + 1
      return
    }
    const question = detail.questions.find((q) => q.id === qidParam)
    if (!question) throw new Error('题目不存在或不属于当前作业。')
    state.question = question
    form.question_number = question.question_number
    form.question_type = question.question_type
    form.stem = question.stem
    form.max_score = question.max_score
    form.answer = question.answer_key?.answer_content ?? ''
    criteria.value = question.rubric_criteria.map((c) => ({
      description: c.description,
      points: c.points,
    }))
  } catch (e) {
    state.error = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    state.loading = false
  }
}

function addCriterion() {
  criteria.value.push({ description: '', points: '1.00' })
}

function move(index: number, delta: number) {
  const target = index + delta
  if (target < 0 || target >= criteria.value.length) return
  const list = criteria.value
  ;[list[index], list[target]] = [list[target]!, list[index]!]
}

function criterionError(item: { description: string; points: string }) {
  if (!item.description.trim()) return '请填写评分项说明'
  return validateScoreInput(item.points, '9999')
}

async function save() {
  if (saving.value) return
  saveError.value = ''
  notice.value = ''
  for (const item of criteria.value) {
    const issue = criterionError(item)
    if (issue) {
      saveError.value = `评分细则：${issue}`
      return
    }
  }
  saving.value = true
  try {
    const svc = await services()
    if (isNew) {
      if (!form.stem.trim() || maxScoreError.value) throw new Error('请完整填写题干与满分。')
      const created = await svc.assignments.addQuestion(aid, {
        question_number: form.question_number,
        question_type: form.question_type,
        stem: form.stem,
        max_score: form.max_score,
      })
      await router.replace(`/assignments/${aid}/questions/${created.id}`)
      notice.value = '题目已创建。请继续完善标准答案与评分细则。'
      await load()
      return
    }
    const question = state.question!
    if (bodyEditable.value) {
      await svc.assignments.updateQuestion(aid, question.id, {
        question_number: form.question_number,
        question_type: form.question_type,
        stem: form.stem,
        max_score: form.max_score,
      })
    }
    await svc.assignments.replaceAnswerKey(question.id, form.answer)
    await svc.assignments.replaceRubric(question.id, {
      criteria: criteria.value.map((item, index) => ({
        description: item.description.trim(),
        points: item.points,
        sort_order: index,
      })),
    })
    notice.value = '已保存。标准答案与评分细则立即生效。'
    await load()
  } catch (e) {
    saveError.value =
      e instanceof UnsupportedCapabilityError
        ? `${e.message}（真实模式下仅支持标准答案与评分细则修改）`
        : e instanceof Error
          ? e.message
          : '保存未完成。'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
<template>
  <div class="hp-section-gap">
    <RouterLink class="hp-linkbtn" :to="`/assignments/${aid}`"
      ><ArrowLeft aria-hidden="true" style="width: 14px; height: 14px" />返回作业</RouterLink
    >
    <PageState :loading="state.loading" :error="state.error" @retry="load">
      <div v-if="isNew || state.question" class="hp-section-gap">
        <div class="hp-pagehead">
          <div>
            <p class="hp-eyebrow">{{ state.assignmentTitle }}</p>
            <h1>{{ isNew ? '新增题目' : `第 ${state.question!.question_number} 题 · 编辑` }}</h1>
          </div>
          <div class="hp-pagehead-actions">
            <button
              class="hp-btn hp-btn-primary hp-btn-lg"
              type="button"
              :disabled="saving"
              @click="save"
            >
              <Save aria-hidden="true" />{{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>

        <div v-if="notice" class="hp-note hp-note-success" role="status">{{ notice }}</div>
        <div v-if="saveError" class="hp-note hp-note-danger" role="alert">{{ saveError }}</div>
        <div v-if="!state.canWrite" class="hp-note hp-note-info" role="note">
          当前作业为只读授权，不能编辑题目。
        </div>
        <div v-if="!bodyEditable && !isNew" class="hp-note hp-note-info" role="note">
          题号、题型、题干与满分修改需要后端“题目更新”接口（开发中）；当前可修改标准答案与评分细则。
        </div>

        <section class="hp-card">
          <div class="hp-card-head"><h2 class="hp-card-title">题目信息</h2></div>
          <div class="hp-card-body hp-form">
            <div class="hp-form-row">
              <label class="hp-field">
                <span class="hp-field-label">题号</span>
                <input
                  v-model.number="form.question_number"
                  class="hp-input"
                  type="number"
                  min="1"
                  :disabled="!bodyEditable"
                />
              </label>
              <label class="hp-field">
                <span class="hp-field-label">题型</span>
                <select v-model="form.question_type" class="hp-select" :disabled="!bodyEditable">
                  <option v-for="(text, value) in questionTypeLabels" :key="value" :value="value">
                    {{ text }}
                  </option>
                </select>
              </label>
              <label class="hp-field">
                <span class="hp-field-label">满分（分）</span>
                <input
                  v-model="form.max_score"
                  class="hp-input"
                  :disabled="!bodyEditable"
                  placeholder="例如 12.00"
                />
                <span v-if="maxScoreError" class="hp-field-error">{{ maxScoreError }}</span>
              </label>
            </div>
            <label class="hp-field">
              <span class="hp-field-label">题干</span>
              <textarea
                v-model="form.stem"
                class="hp-textarea"
                rows="4"
                :disabled="!bodyEditable"
                placeholder="输入完整题干，包含小题与配分说明"
              />
            </label>
            <label class="hp-field">
              <span class="hp-field-label"
                >标准答案 <small>（保存后用于核对与候选评分参考）</small></span
              >
              <textarea
                v-model="form.answer"
                class="hp-textarea"
                rows="3"
                :disabled="!state.canWrite"
                placeholder="输入标准答案或评分参考"
              />
            </label>
          </div>
        </section>

        <section class="hp-card">
          <div class="hp-card-head">
            <h2 class="hp-card-title">评分细则</h2>
            <span class="hp-card-note"
              >共 {{ criteria.length }} 项 · 合计 {{ rubricTotal }} 分 · 题目满分
              {{ formatScore(form.max_score || '0') }} 分</span
            >
            <button
              class="hp-card-side hp-btn hp-btn-secondary hp-btn-sm"
              type="button"
              :disabled="!state.canWrite"
              @click="addCriterion"
            >
              <Plus aria-hidden="true" />添加评分项
            </button>
          </div>
          <div class="hp-card-body">
            <p v-if="!criteria.length" class="hp-fine">
              尚未配置评分细则。按步骤拆分给分点，逐项填写说明与分值上限。
            </p>
            <div v-else style="display: flex; flex-direction: column; gap: 10px">
              <div
                v-for="(item, index) in criteria"
                :key="index"
                class="hp-flex"
                style="
                  align-items: flex-start;
                  border: 1px solid var(--hp-border);
                  border-radius: 10px;
                  padding: 10px 12px;
                "
              >
                <div class="hp-flex hp-gap-sm" style="flex-direction: column">
                  <button
                    class="hp-iconbtn"
                    style="width: 24px; height: 20px"
                    type="button"
                    :disabled="index === 0"
                    aria-label="上移"
                    @click="move(index, -1)"
                  >
                    <ArrowUp aria-hidden="true" style="width: 13px; height: 13px" />
                  </button>
                  <button
                    class="hp-iconbtn"
                    style="width: 24px; height: 20px"
                    type="button"
                    :disabled="index === criteria.length - 1"
                    aria-label="下移"
                    @click="move(index, 1)"
                  >
                    <ArrowDown aria-hidden="true" style="width: 13px; height: 13px" />
                  </button>
                </div>
                <label class="hp-field hp-grow" style="min-width: 200px">
                  <span class="hp-field-label">第 {{ index + 1 }} 项 · 给分点说明</span>
                  <input
                    v-model="item.description"
                    class="hp-input"
                    :disabled="!state.canWrite"
                    placeholder="例如：正确求出切点坐标"
                  />
                </label>
                <label class="hp-field" style="width: 130px; flex: none">
                  <span class="hp-field-label">分值上限</span>
                  <input
                    v-model="item.points"
                    class="hp-input"
                    :disabled="!state.canWrite"
                    placeholder="4.00"
                  />
                  <span v-if="criterionError(item)" class="hp-field-error">{{
                    criterionError(item)
                  }}</span>
                </label>
                <button
                  class="hp-iconbtn"
                  style="color: var(--hp-danger)"
                  type="button"
                  :disabled="!state.canWrite"
                  aria-label="删除评分项"
                  @click="criteria.splice(index, 1)"
                >
                  <Trash2 aria-hidden="true" style="width: 15px; height: 15px" />
                </button>
              </div>
            </div>
            <p class="hp-fine" style="margin-top: 10px">
              评分项合计不应超过题目满分；保存时服务端同样校验。候选评分与人工修订均以此为基准。
            </p>
          </div>
        </section>

        <p class="hp-fine">
          当前题型：{{ label(questionTypeLabels, form.question_type) }} ·
          题目保存后立即出现在作业详情中。
        </p>
      </div>
    </PageState>
  </div>
</template>
