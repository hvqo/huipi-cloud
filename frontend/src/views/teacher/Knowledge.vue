<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import { BookMarked, FileSearch, FileUp, LibraryBig, Quote, Search, Trash2 } from 'lucide-vue-next'
import { services, type KnowledgeDocSummary, type RetrievalHit } from '../../services'
import { UnsupportedCapabilityError } from '../../services/types'
import { formatBytes, formatDate, label, knowledgeStatusLabels } from '../../lib/labels'
import EmptyState from '../../components/EmptyState.vue'
import PageState from '../../components/PageState.vue'
import UnsupportedPanel from '../../components/UnsupportedPanel.vue'

/**
 * 教学知识库：资料管理 + 检索测试。
 * 真实模式的 HTTP 管理端点尚未开放（现为受控服务操作），如实展示能力缺口；
 * 演示模式提供完整交互（上传→索引→检索→删除，状态真实变更）。
 */
const state = reactive({
  loading: false,
  error: '',
  docs: [] as KnowledgeDocSummary[],
  unsupported: false,
})
const assignments = ref<{ id: string; title: string }[]>([])
const category = ref('全部')
const uploadOpen = ref(false),
  uploadBusy = ref(false),
  uploadError = ref('')
const uploadForm = ref({ file: null as File | null, category: '教学讲义', assignment_id: '' })
const detail = ref<{
  doc: KnowledgeDocSummary
  chunks: { id: string; text: string; page_hint: string }[]
} | null>(null)
const detailOpen = ref(false),
  detailBusy = ref(false)
const removeTarget = ref<KnowledgeDocSummary | null>(null),
  removeBusy = ref(false),
  removeError = ref('')
const query = ref(''),
  searching = ref(false),
  hits = ref<RetrievalHit[]>([]),
  searchError = ref('')
const notice = ref('')

const categories = computed(() => {
  const set = new Set(state.docs.map((d) => d.category))
  return ['全部', ...[...set].sort()]
})
const filtered = computed(() =>
  category.value === '全部' ? state.docs : state.docs.filter((d) => d.category === category.value),
)

async function load() {
  state.loading = true
  state.error = ''
  state.unsupported = false
  try {
    const svc = await services()
    state.docs = await svc.knowledge.list()
    assignments.value = (await svc.assignments.list({ limit: 100 })).records
  } catch (e) {
    if (e instanceof UnsupportedCapabilityError) state.unsupported = true
    else state.error = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    state.loading = false
  }
}

function pickFile(event: Event) {
  const input = event.target as HTMLInputElement
  uploadForm.value.file = input.files?.[0] ?? null
}

async function upload() {
  if (!uploadForm.value.file || uploadBusy.value) return
  uploadBusy.value = true
  uploadError.value = ''
  try {
    await (
      await services()
    ).knowledge.upload({
      file: uploadForm.value.file,
      category: uploadForm.value.category || '未分类',
      assignment_id: uploadForm.value.assignment_id || null,
    })
    uploadOpen.value = false
    uploadForm.value = { file: null, category: '教学讲义', assignment_id: '' }
    notice.value = '资料已上传，正在建立索引；索引完成后即可被检索和 AI 助教引用。'
    await load()
  } catch (e) {
    uploadError.value = e instanceof Error ? e.message : '上传未完成。'
  } finally {
    uploadBusy.value = false
  }
}

async function openDetail(doc: KnowledgeDocSummary) {
  detailOpen.value = true
  detailBusy.value = true
  detail.value = { doc, chunks: [] }
  try {
    detail.value = { doc, chunks: (await (await services()).knowledge.detail(doc.id)).chunks }
  } catch (e) {
    detail.value = { doc, chunks: [] }
    notice.value = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    detailBusy.value = false
  }
}

async function remove() {
  if (!removeTarget.value || removeBusy.value) return
  removeBusy.value = true
  removeError.value = ''
  try {
    await (await services()).knowledge.remove(removeTarget.value.id)
    removeTarget.value = null
    notice.value = '资料已删除。已删除的资料不会再被检索到。'
    await load()
  } catch (e) {
    removeError.value = e instanceof Error ? e.message : '删除未完成。'
  } finally {
    removeBusy.value = false
  }
}

async function search() {
  if (!query.value.trim() || searching.value) return
  searching.value = true
  searchError.value = ''
  try {
    hits.value = await (await services()).knowledge.retrieve(query.value)
  } catch (e) {
    searchError.value = e instanceof Error ? e.message : '检索未完成。'
  } finally {
    searching.value = false
  }
}

function statusBadge(status: string) {
  if (status === 'indexed') return 'hp-badge-success'
  if (status === 'failed') return 'hp-badge-danger'
  if (status === 'indexing' || status === 'pending') return 'hp-badge-primary'
  return ''
}

let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  void load()
  timer = setInterval(async () => {
    if (state.docs.some((d) => d.status === 'indexing')) {
      try {
        state.docs = await (await services()).knowledge.list()
      } catch {
        /* Next explicit reload displays errors. */
      }
    }
  }, 1500)
})
onBeforeUnmount(() => clearInterval(timer))
</script>
<template>
  <div class="hp-section-gap">
    <div class="hp-pagehead">
      <div>
        <h1>知识库</h1>
        <p class="hp-pagehead-desc">
          上传讲义、命题资料与错题分析，供检索测试与 AI
          助教引用。所有回答都基于这里已索引的资料，无证据不回答。
        </p>
      </div>
      <div class="hp-pagehead-actions">
        <button class="hp-btn hp-btn-primary hp-btn-lg" type="button" @click="uploadOpen = true">
          <FileUp aria-hidden="true" />上传资料
        </button>
      </div>
    </div>

    <div v-if="notice" class="hp-note hp-note-success" role="status">{{ notice }}</div>
    <PageState :loading="state.loading" :error="state.error" @retry="load">
      <template v-if="state.unsupported">
        <UnsupportedPanel
          summary="教学知识库管理接口尚未开放"
          detail="知识库目前通过部署侧受控服务操作（文档解析、索引与检索），HTTP 管理端点尚未提供。演示模式（npm run dev:demo）可体验完整的上传、索引与检索交互；端点就绪后此处会自动接入真实资料。"
        />
      </template>
      <template v-else>
        <div class="hp-flex">
          <div class="hp-filter-chips" role="group" aria-label="资料分类">
            <button
              v-for="item in categories"
              :key="item"
              class="hp-filter-chip"
              :class="{ 'is-active': category === item }"
              type="button"
              @click="category = item"
            >
              {{ item }}
            </button>
          </div>
          <span class="hp-fine" style="margin-left: auto">共 {{ filtered.length }} 份资料</span>
        </div>

        <EmptyState
          v-if="!filtered.length && !state.loading"
          title="知识库还是空的"
          desc="上传教学讲义、命题资料或错题分析后，系统会自动建立索引；之后可以在这里做检索测试，AI 助教也会引用这些资料回答问题。"
        >
          <button class="hp-btn hp-btn-primary" type="button" @click="uploadOpen = true">
            <FileUp aria-hidden="true" />上传第一份资料
          </button>
        </EmptyState>

        <div v-else class="hp-assignment-grid">
          <article v-for="doc in filtered" :key="doc.id" class="hp-assignment-card">
            <div class="hp-assignment-top">
              <div class="hp-subject-chip">
                <LibraryBig aria-hidden="true" style="width: 18px; height: 18px" />
              </div>
              <div class="hp-grow" style="min-width: 0">
                <div class="hp-assignment-title">{{ doc.title }}</div>
                <div class="hp-assignment-meta">
                  {{ doc.category }} · {{ doc.file_type.toUpperCase() }} ·
                  {{ formatBytes(doc.size_bytes) }}
                </div>
              </div>
              <span class="hp-badge" :class="statusBadge(doc.status)">
                <span class="dot" :class="{ 'hp-dot-pulse': doc.status === 'indexing' }" />
                {{ label(knowledgeStatusLabels, doc.status) }}
              </span>
            </div>
            <p class="hp-assignment-desc">
              <template v-if="doc.status === 'indexed'">
                已建立 {{ doc.chunk_count }} 个内容段落索引 · 版本 v{{ doc.version
                }}<template v-if="doc.assignment_id"> · 已关联作业</template>
              </template>
              <template v-else-if="doc.status === 'indexing'"
                >正在解析并建立索引，完成后自动可检索…</template
              >
              <template v-else>等待处理…</template>
            </p>
            <div class="hp-assignment-foot">
              <span>{{ formatDate(doc.created_at) }}</span>
              <span class="hp-flex hp-gap-sm">
                <button class="hp-linkbtn" type="button" @click="openDetail(doc)">查看详情</button>
                <button
                  class="hp-linkbtn"
                  style="color: var(--hp-danger)"
                  type="button"
                  @click="((removeTarget = doc), (removeError = ''))"
                >
                  删除
                </button>
              </span>
            </div>
          </article>
        </div>

        <section class="hp-card">
          <div class="hp-card-head">
            <h2 class="hp-card-title"><FileSearch aria-hidden="true" />检索测试</h2>
            <span class="hp-card-note">输入问题片段，检查资料是否能被正确召回</span>
          </div>
          <div class="hp-card-body">
            <div class="hp-flex">
              <div class="hp-grow" style="position: relative; min-width: 260px">
                <input
                  v-model="query"
                  class="hp-input"
                  style="padding-left: 34px"
                  placeholder="例如：切线方程 常见错误"
                  @keyup.enter="search"
                />
                <Search
                  aria-hidden="true"
                  style="
                    position: absolute;
                    left: 11px;
                    top: 12px;
                    width: 15px;
                    height: 15px;
                    color: var(--hp-ink-400);
                  "
                />
              </div>
              <button
                class="hp-btn hp-btn-primary"
                type="button"
                :disabled="!query.trim() || searching"
                @click="search"
              >
                检索
              </button>
            </div>
            <p v-if="searchError" class="hp-field-error" style="margin-top: 8px">
              {{ searchError }}
            </p>
            <div v-if="searching" class="hp-state-card"><span class="hp-spin" />正在检索…</div>
            <template v-else-if="hits.length">
              <div
                v-for="(hit, index) in hits"
                :key="hit.chunk_id"
                style="
                  border: 1px solid var(--hp-border);
                  border-radius: 10px;
                  padding: 12px 14px;
                  margin-top: 10px;
                "
              >
                <div class="hp-flex" style="justify-content: space-between; margin-bottom: 6px">
                  <span class="hp-cell-title" style="font-size: 13px"
                    >[{{ index + 1 }}] {{ hit.doc_title }}</span
                  >
                  <span class="hp-badge hp-badge-teal" style="height: 20px; font-size: 11px"
                    >相关度 {{ hit.score }}</span
                  >
                </div>
                <p style="font-size: 13px; line-height: 1.8; color: var(--hp-ink-700)">
                  {{ hit.text }}
                </p>
              </div>
            </template>
            <EmptyState
              v-else-if="query && !searching"
              title="没有命中资料"
              desc="换个说法试试，或上传更多相关资料。AI 助教在无证据时不会编造回答。"
            />
          </div>
        </section>
      </template>
    </PageState>

    <el-dialog v-model="uploadOpen" title="上传教学资料" width="520" :close-on-click-modal="false">
      <div class="hp-form">
        <label class="hp-field">
          <span class="hp-field-label"
            >选择文件 <small>（PDF / DOCX / TXT / Markdown）</small></span
          >
          <input class="hp-input" type="file" accept=".pdf,.docx,.txt,.md" @change="pickFile" />
        </label>
        <label class="hp-field">
          <span class="hp-field-label">资料分类</span>
          <select v-model="uploadForm.category" class="hp-select">
            <option>教学讲义</option>
            <option>命题资料</option>
            <option>错题分析</option>
            <option>其他</option>
          </select>
        </label>
        <label class="hp-field"
          ><span class="hp-field-label">关联作业</span>
          <select v-model="uploadForm.assignment_id" class="hp-select">
            <option value="">演示公共资料</option>
            <option v-for="assignment in assignments" :key="assignment.id" :value="assignment.id">
              {{ assignment.title }}
            </option>
          </select>
        </label>
        <p class="hp-fine">
          演示模式模拟索引流程，PDF / DOCX不执行实际解析；TXT /
          Markdown按原文分段。未关联资料仅为本地演示公共资料，不代表真实权限策略。
        </p>
        <p v-if="uploadError" role="alert" class="hp-error-text">{{ uploadError }}</p>
      </div>
      <template #footer>
        <button class="hp-btn hp-btn-secondary" type="button" @click="uploadOpen = false">
          取消
        </button>
        <button
          class="hp-btn hp-btn-primary"
          type="button"
          :disabled="!uploadForm.file || uploadBusy"
          @click="upload"
        >
          {{ uploadBusy ? '上传中…' : '开始上传' }}
        </button>
      </template>
    </el-dialog>

    <div v-if="detailOpen" class="hp-drawer-veil" @click="detailOpen = false">
      <aside class="hp-drawer" aria-label="资料详情" @click.stop>
        <div class="hp-drawer-head">
          <div>
            <div class="hp-drawer-title">{{ detail?.doc.title }}</div>
            <div class="hp-drawer-sub">
              {{ detail?.doc.category }} · 版本 v{{ detail?.doc.version }} ·
              {{ label(knowledgeStatusLabels, detail?.doc.status ?? '') }}
            </div>
          </div>
          <button
            class="hp-iconbtn"
            style="margin-left: auto"
            type="button"
            aria-label="关闭"
            @click="detailOpen = false"
          >
            ✕
          </button>
        </div>
        <div class="hp-drawer-body">
          <div v-if="detailBusy" class="hp-state-card">
            <span class="hp-spin" />正在读取索引内容…
          </div>
          <template v-else>
            <p class="hp-fine">
              <BookMarked
                aria-hidden="true"
                style="width: 12px; height: 12px; vertical-align: -1px"
              />
              共 {{ detail?.chunks.length ?? 0 }} 个内容段落；以下为索引原文片段。
            </p>
            <div
              v-for="chunk in detail?.chunks"
              :key="chunk.id"
              style="border: 1px solid var(--hp-border); border-radius: 10px; padding: 12px 14px"
            >
              <div class="hp-fine" style="margin-bottom: 4px">
                <Quote aria-hidden="true" style="width: 11px; height: 11px; vertical-align: -1px" />
                {{ chunk.page_hint }}
              </div>
              <p style="font-size: 13px; line-height: 1.8; color: var(--hp-ink-700)">
                {{ chunk.text }}
              </p>
            </div>
          </template>
        </div>
      </aside>
    </div>

    <el-dialog
      :model-value="!!removeTarget"
      title="确认删除资料"
      width="440"
      :close-on-click-modal="false"
      @close="removeTarget = null"
    >
      <p style="font-size: 13.5px; line-height: 1.8">
        确认删除《{{
          removeTarget?.title
        }}》？删除后该资料不会再被检索到，历史引用将失效。此操作不可撤销。
      </p>
      <p v-if="removeError" role="alert" class="hp-error-text">{{ removeError }}</p>
      <template #footer>
        <button class="hp-btn hp-btn-secondary" type="button" @click="removeTarget = null">
          取消
        </button>
        <button class="hp-btn hp-btn-danger" type="button" :disabled="removeBusy" @click="remove">
          <Trash2 aria-hidden="true" />确认删除
        </button>
      </template>
    </el-dialog>
  </div>
</template>
