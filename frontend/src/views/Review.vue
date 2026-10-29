<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  api,
  ApiError,
  type Schema,
  type Review,
  type Region,
  type Page,
  type Node,
} from '../api/client'
import { identity } from '../auth'
import { addRegion, selectedRegion } from '../lib/selection'
import {
  allowedReasons,
  decisions,
  makeReview,
  PendingReview,
  reasons as reasonLabels,
  sourceMatches,
  type Decision,
} from '../lib/review'
import { date, status, useLoad } from '../lib/load'
import PageState from '../components/PageState.vue'
import SourceNode from '../components/SourceNode.vue'
import FilePreview from '../components/FilePreview.vue'
import HomeworkGrading from '../components/HomeworkGrading.vue'
import HomeworkVisualGrading from '../components/HomeworkVisualGrading.vue'

const route = useRoute(),
  aid = String(route.params.aid),
  sid = String(route.params.sid),
  qid = String(route.params.qid)
const state = useLoad(),
  view = ref<Review>(),
  page = ref<Page>(),
  submission = ref<Schema['SubmissionRead']>(),
  workbench = ref<Schema['AssignmentWorkbench']>(),
  history = ref<Schema['ReviewHistory']>()
const question = computed(() => workbench.value?.questions.find((q) => q.id === qid))
const pageNumber = ref(1),
  pageCount = ref(1),
  historyOffset = ref(0),
  decision = ref<Decision>('response_present'),
  reason = ref('student_work_visible'),
  regions = ref<Region[]>([]),
  notice = ref(''),
  conflict = ref(false),
  preview = ref(false),
  verified = ref(false),
  busy = ref(false),
  frozen = ref(false)
const permissionDenied = ref(false)
const visualOpen = ref(false)
const pending = new PendingReview(`huipi.review.${identity.actor}.${sid}.${qid}`)
frozen.value = !!pending.body
const nodes = new Map<string, string>(),
  currentNodes = new Map<string, Node>()
const related = computed(() => new Set(view.value?.evidence.map((e) => e.content_pointer) || []))
const editable = computed(
  () =>
    !!workbench.value?.can_write &&
    !frozen.value &&
    !conflict.value &&
    !permissionDenied.value &&
    !busy.value,
)
function index(node: Node, pointer: string) {
  currentNodes.set(pointer, node)
  if (typeof node.value === 'string') nodes.set(pointer, node.value)
  ;(node.children || []).forEach((child, i) => index(child, `${pointer}/children/${i}`))
}
async function loadPage(signal?: AbortSignal) {
  const result = await api.page(sid, pageNumber.value, signal)
  const current = await api.canonical(sid, signal)
  if (
    result.document_id !== view.value?.source.canonical_document_id ||
    current.canonical_document?.canonical_sha256 !== view.value?.source.canonical_sha256
  )
    throw new ApiError(409, 'source_changed')
  currentNodes.clear()
  result.page.blocks.forEach((b, i) =>
    index(b.content, `/pages/${result.page.page_index}/blocks/${i}/content`),
  )
  page.value = result
}
async function load() {
  await state.run(async (signal) => {
    const [s, c, v] = await Promise.all([
      api.submission(sid, signal),
      api.canonical(sid, signal),
      api.review(sid, qid, 0, signal),
    ])
    if (s.assignment_id !== aid) throw new Error('提交与作业不匹配。')
    submission.value = s
    view.value = v
    pageCount.value = c.canonical_document?.page_count || 1
    const w = await api.workbench(aid, 0, signal, qid)
    if (!w.questions.some((q) => q.id === qid)) throw new Error('此题目不可用或不属于当前作业。')
    workbench.value = w
    pageNumber.value = Math.min(pageNumber.value, pageCount.value)
    regions.value = []
    nodes.clear()
    conflict.value = false
    permissionDenied.value = false
    verified.value = false
    await loadPage(signal)
    history.value = await api.history(sid, qid, historyOffset.value, signal)
  })
}
function changeDecision(value: Decision) {
  decision.value = value
  reason.value = allowedReasons[value][0]!
  regions.value = []
  notice.value = ''
  verified.value = false
}
function addText() {
  if (!editable.value) return
  try {
    regions.value = addRegion(regions.value, selectedRegion(window.getSelection(), nodes))
    notice.value = ''
    verified.value = false
  } catch (e) {
    notice.value = (e as Error).message
  }
}
function addAsset(pointer: string) {
  if (!editable.value) return
  try {
    const node = currentNodes.get(pointer)
    if (node?.normalized_type !== 'image' || !node.asset_refs?.length)
      throw new Error('仅能选择有独立素材引用的图片节点。')
    regions.value = addRegion(regions.value, { content_pointer: pointer })
    verified.value = false
  } catch (e) {
    notice.value = (e as Error).message
  }
}
function openPreview() {
  if (!view.value || !editable.value) return
  try {
    makeReview(view.value, decision.value, reason.value, regions.value)
    verified.value = false
    preview.value = true
    notice.value = ''
  } catch (e) {
    notice.value = (e as Error).message
  }
}
async function send(retry = false) {
  if (busy.value || !view.value || (!retry && !verified.value)) return
  busy.value = true
  preview.value = false
  notice.value = ''
  try {
    if (!retry) {
      const fresh = await api.review(sid, qid)
      if (
        fresh.expected_revision !== view.value.expected_revision ||
        !sourceMatches(fresh.source, view.value.source)
      )
        throw new ApiError(409, 'review_changed')
      pending.freeze(makeReview(view.value, decision.value, reason.value, regions.value))
      frozen.value = true
    }
    if (!pending.body) throw new Error('没有待确认的原请求。')
    const saved = await api.save(sid, qid, pending.body)
    pending.clear()
    frozen.value = false
    historyOffset.value = 0
    await load()
    notice.value = `已确认保存 · revision ${saved.record.revision}。这不是成绩发布。`
  } catch (e) {
    if (e instanceof ApiError && [403, 404].includes(e.status)) permissionDenied.value = true
    if (e instanceof ApiError && e.status === 409) {
      pending.clear()
      frozen.value = false
      conflict.value = true
      notice.value = e.message
    } else if (e instanceof ApiError && !e.uncertain && e.status !== 401) {
      pending.clear()
      frozen.value = false
      notice.value = e.message
    } else
      notice.value = pending.body
        ? '保存结果待确认，不代表没有提交。请用下方按钮重试完全相同的请求。'
        : (e as Error).message
  } finally {
    busy.value = false
  }
}
async function moreConfirmed() {
  if (!view.value?.confirmed_truncated || view.value.next_confirmed_offset == null) return
  try {
    const next = await api.review(sid, qid, 0, undefined, view.value.next_confirmed_offset)
    if (
      !sourceMatches(next.source, view.value.source) ||
      next.expected_revision !== view.value.expected_revision
    )
      throw new ApiError(409, 'review_changed')
    view.value = {
      ...view.value,
      confirmed_regions: [
        ...(view.value.confirmed_regions || []),
        ...(next.confirmed_regions || []),
      ],
      confirmed_truncated: next.confirmed_truncated,
      next_confirmed_offset: next.next_confirmed_offset,
    }
  } catch (e) {
    notice.value = (e as Error).message
    conflict.value = true
  }
}
async function moreEvidence() {
  if (!view.value?.evidence_truncated || view.value.next_evidence_offset === null) return
  try {
    const next = await api.review(sid, qid, view.value.next_evidence_offset)
    if (
      !sourceMatches(next.source, view.value.source) ||
      next.expected_revision !== view.value.expected_revision
    )
      throw new ApiError(409, 'review_changed')
    view.value = { ...next, evidence: [...view.value.evidence, ...next.evidence] }
  } catch (e) {
    notice.value = (e as Error).message
    conflict.value = true
  }
}
async function pageTo(value: number) {
  pageNumber.value = value
  await state.run(loadPage)
}
async function historyTo(offset: number) {
  historyOffset.value = offset
  try {
    history.value = await api.history(sid, qid, offset)
  } catch (e) {
    notice.value = (e as Error).message
  }
}
function snippet(region: Region) {
  const text = nodes.get(region.content_pointer)
  return text && region.text_start != null
    ? Array.from(text).slice(region.text_start, region.text_end!).join('').slice(0, 180)
    : '独立图片节点；请对照原始资料'
}
function formatReasons(codes: string[]) {
  return codes.map((code) => reasonLabels[code] || code).join('、')
}
onMounted(load)
</script>
<template>
  <RouterLink class="back-link" :to="`/assignments/${aid}/submissions/${sid}`"
    >← 返回学生提交</RouterLink
  ><PageState :loading="state.loading.value" :error="state.error.value" @retry="load"
    ><template v-if="view && submission && question"
      ><div class="page-heading">
        <div>
          <p class="eyebrow">{{ workbench?.assignment.title }} / {{ submission.student_ref }}</p>
          <h1>第 {{ question.question_number }} 题 · 作答证据复核</h1>
          <p class="muted">{{ question.stem }}</p>
        </div>
        <FilePreview
          :sid="sid"
          :name="submission.file.original_filename"
          :type="submission.file.content_type"
          @denied="permissionDenied = true"
        />
      </div>
      <div class="review-strip">
        <span
          >来源关联 <strong>{{ status(view.alignment_status) }}</strong></span
        ><span
          >人工判断 <strong>{{ decisions[view.answer_presence] }}</strong></span
        ><span
          >当前 revision <strong>{{ view.expected_revision }}</strong></span
        ><span :class="{ error: view.latest && !view.latest_source_matches }">{{
          view.latest
            ? view.latest_source_matches
              ? '最新决定与本次来源匹配'
              : '历史决定来源已过期，需重新核对'
            : '尚无人工决定'
        }}</span>
      </div>
      <div v-if="permissionDenied" class="notice warning" role="alert">
        当前资源权限不可用，本页写入已锁定。请返回作业或刷新，服务端将重新校验权限。
      </div>
      <div v-if="conflict" class="notice warning" role="alert">
        来源或修订已变化，当前编辑已锁定。刷新后请重新选择证据，系统不会覆盖他人修订。<el-button
          @click="load"
          >刷新来源并重新核对</el-button
        >
      </div>
      <div v-if="frozen" class="notice warning" role="alert">
        上一笔保存结果待确认，编辑已锁定。重试会发送原 request_id 与完全相同的正文。<el-button
          type="primary"
          :loading="busy"
          @click="send(true)"
          >重试原请求</el-button
        >
      </div>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>
      <div class="review-grid">
        <section class="panel source-panel">
          <div class="section-heading">
            <div>
              <p class="eyebrow">CANONICAL · 可追溯原文</p>
              <h2>核对来源</h2>
            </div>
            <div class="page-controls">
              <el-button size="small" :disabled="pageNumber <= 1" @click="pageTo(pageNumber - 1)"
                >上一页</el-button
              ><span>第 {{ pageNumber }} / {{ pageCount }} 页</span
              ><el-button
                size="small"
                :disabled="pageNumber >= pageCount"
                @click="pageTo(pageNumber + 1)"
                >下一页</el-button
              >
            </div>
          </div>
          <p class="fine">
            原文按节点显示，不执行 HTML、Markdown
            或外部资源。浅绿色边框表示已关联来源，不代表已确认作答。
          </p>
          <div v-if="page" class="paper" data-testid="canonical-paper">
            <div
              v-for="(block, index) in page.page.blocks"
              :key="block.block_id"
              class="source-block"
            >
              <div class="block-label">来源 {{ index + 1 }} · {{ block.normalized_type }}</div>
              <SourceNode
                :node="block.content"
                :pointer="`/pages/${page.page.page_index}/blocks/${index}/content`"
                :related="related"
                :disabled="!editable"
                :confirmed="view.confirmed_regions || []"
                @asset="addAsset"
              />
            </div>
          </div>
          <el-button class="select-button" type="primary" :disabled="!editable" @click="addText"
            >将当前文字选区加入证据</el-button
          >
          <div v-if="view.confirmed_regions?.length" class="confirmed-note">
            <h3>本次来源的人工记录</h3>
            <p class="fine">绿色为确认作答，灰色为排除题干，黄色为不确定；均为历史只读范围。</p>
            <p v-for="(r, i) in view.confirmed_regions" :key="i" class="fine">
              第 {{ r.page_number }} 页 ·
              {{
                r.role === 'response_regions'
                  ? '确认作答'
                  : r.role === 'excluded_prompt_regions'
                    ? '排除题干'
                    : '不确定'
              }}
              · {{ r.content_pointer }} [{{ r.text_start ?? '整节点' }}, {{ r.text_end ?? '—' }})
            </p>
            <el-button v-if="view.confirmed_truncated" @click="moreConfirmed"
              >继续读取人工范围</el-button
            >
          </div>
          <details class="evidence-detail">
            <summary>已关联的来源范围（{{ view.evidence.length }}）</summary>
            <p v-for="(e, index) in view.evidence" :key="index" class="fine">
              第 {{ e.page_number }} 页 · {{ e.content_pointer }} · [{{ e.text_start ?? '整节点' }},
              {{ e.text_end ?? '—' }})
            </p>
            <el-button v-if="view.evidence_truncated" @click="moreEvidence"
              >继续读取关联来源</el-button
            >
          </details>
        </section>
        <aside class="panel review-editor">
          <p class="eyebrow">教师人工判断</p>
          <h2>本次复核</h2>
          <p class="fine">AI 来源关联与人工确认彼此独立。不要把印刷题干或 OCR 缺失误作学生作答。</p>
          <div v-if="!workbench?.can_write" class="notice">此作业为只读授权，不能保存复核。</div>
          <fieldset :disabled="!editable">
            <legend>审核决定</legend>
            <label
              v-for="(name, key) in {
                response_present: '确认存在作答',
                response_absent: '确认不存在作答',
                uncertain: '标记不确定',
              }"
              :key="key"
              class="decision-option"
              ><input
                type="radio"
                name="decision"
                :value="key"
                :checked="decision === key"
                @change="changeDecision(key)"
              />{{ name }}</label
            >
          </fieldset>
          <label
            >判断原因<select v-model="reason" :disabled="!editable" aria-label="判断原因">
              <option v-for="code in allowedReasons[decision]" :key="code" :value="code">
                {{ reasonLabels[code] }}
              </option>
            </select></label
          >
          <div class="section-heading">
            <h3>已选证据</h3>
            <span class="pill">{{ regions.length }} 个区域</span>
          </div>
          <p v-if="!regions.length" class="muted">
            请在左侧原文选择范围，然后加入证据。未选择不等于学生没有作答。
          </p>
          <ol class="selection-list">
            <li v-for="(r, i) in regions" :key="i">
              <p>{{ snippet(r) }}</p>
              <small
                >{{ r.content_pointer }}<br />码点范围 [{{ r.text_start ?? '整节点' }},
                {{ r.text_end ?? '—' }})</small
              ><el-button text :disabled="!editable" @click="regions.splice(i, 1)">移除</el-button>
            </li>
          </ol>
          <el-button
            type="primary"
            size="large"
            class="full"
            :disabled="!editable"
            @click="openPreview"
            >预览并保存复核</el-button
          >
          <p class="fine">
            保存后生成不可变 revision，不可直接编辑或删除历史。此操作不会产生分数。
          </p>
        </aside>
      </div>
      <HomeworkGrading :submission-id="sid" :question-id="qid" />
      <details v-if="workbench?.can_write" @toggle="visualOpen = ($event.target as HTMLDetailsElement).open">
        <summary>图片作答批改</summary>
        <HomeworkVisualGrading v-if="visualOpen" :submission-id="sid" :question-id="qid" />
      </details>
      <section class="panel history-panel">
        <div class="section-heading">
          <div>
            <p class="eyebrow">REVIEW HISTORY</p>
            <h2>本题审核历史</h2>
          </div>
          <span class="pill">只读</span>
        </div>
        <p v-if="!history?.records.length" class="muted">尚无复核记录。</p>
        <article v-for="record in history?.records" :key="record.decision_id" class="history-row">
          <div class="history-version">R{{ record.revision }}</div>
          <div>
            <strong>{{ decisions[record.decision] }}</strong>
            <p>{{ formatReasons(record.reason_codes) }}</p>
            <small class="muted"
              >{{ date(record.created_at) }} ·
              {{
                record.reviewer_kind === 'authenticated_teacher'
                  ? '认证教师'
                  : '历史自声明身份（未认证）'
              }}</small
            >
            <p class="fine">
              {{
                sourceMatches(record.source, view.source)
                  ? '与本次读取来源相符'
                  : '来源版本不同，仅供历史审计'
              }}
              · 前序 {{ record.supersedes_decision_id ? '已关联' : '无' }}
            </p>
          </div>
        </article>
        <div class="pager">
          <el-button
            :disabled="historyOffset === 0"
            @click="historyTo(Math.max(0, historyOffset - 10))"
            >上一页历史</el-button
          ><span>仅显示本题记录</span
          ><el-button
            :disabled="!history?.has_next"
            @click="historyTo(Number(history?.next_offset || 0))"
            >下一页历史</el-button
          >
        </div>
      </section>
      <el-dialog
        v-model="preview"
        title="确认本次人工复核"
        width="560"
        :close-on-click-modal="false"
        ><p>
          决定：<strong>{{ decisions[decision] }}</strong>
        </p>
        <p>原因：{{ reasonLabels[reason] }} · 已选 {{ regions.length }} 个证据范围</p>
        <p class="muted">
          基于 revision {{ view.expected_revision }} 与本次读取的 Canonical / Alignment 版本提交。
        </p>
        <ul>
          <li v-for="(r, index) in regions" :key="index">{{ snippet(r) }}</li>
        </ul>
        <label class="confirm-check"
          ><input
            v-model="verified"
            type="checkbox"
          />我已对照来源核对，本次仅确认作答证据，不发布成绩。</label
        ><template #footer
          ><el-button @click="preview = false">继续核对</el-button
          ><el-button type="primary" :disabled="!verified" :loading="busy" @click="send()"
            >确认保存</el-button
          ></template
        ></el-dialog
      >
    </template></PageState
  >
</template>
