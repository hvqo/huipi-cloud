<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Bot,
  CircleStop,
  MessagesSquare,
  Plus,
  Quote,
  RefreshCw,
  Search,
  Send,
  Trash2,
  User,
} from 'lucide-vue-next'
import { services, type AssistantConversation, type AssistantMessage } from '../../services'
import { UnsupportedCapabilityError } from '../../services/types'
import { formatRelative } from '../../lib/labels'
import EmptyState from '../../components/EmptyState.vue'
import PageState from '../../components/PageState.vue'
import UnsupportedPanel from '../../components/UnsupportedPanel.vue'

/**
 * AI 教学助教：基于知识库资料的有据回答。
 * 无证据不回答（检索不到资料时明确说明，不编造内容）；
 * 真实模式聊天接口尚未开发，如实展示能力缺口。
 */
const loading = ref(false),
  error = ref(''),
  unsupported = ref(false)
const conversations = ref<AssistantConversation[]>([])
const activeId = ref('')
const messages = ref<AssistantMessage[]>([])
const draft = ref('')
const sending = ref(false)
const sendError = ref('')
const citation = ref<AssistantMessage['citations'] | null>(null)
const streamEnd = ref<HTMLElement | null>(null)
let controller: AbortController | null = null

const active = computed(() => conversations.value.find((c) => c.id === activeId.value) ?? null)
const recommended = [
  '学生在求切线方程时常犯哪些错误？',
  '如何讲清“在某点处的切线”与“过某点的切线”的区别？',
  '单调区间的表述有哪些规范要求？',
]

async function load() {
  loading.value = true
  error.value = ''
  unsupported.value = false
  try {
    const svc = await services()
    conversations.value = await svc.assistant.conversations()
    if (conversations.value.length) await open(conversations.value[0]!.id)
  } catch (e) {
    if (e instanceof UnsupportedCapabilityError) unsupported.value = true
    else error.value = e instanceof Error ? e.message : '读取未完成。'
  } finally {
    loading.value = false
  }
}

async function open(id: string) {
  if (sending.value) return
  activeId.value = id
  sendError.value = ''
  try {
    messages.value = await (await services()).assistant.messages(id)
  } catch (e) {
    messages.value = []
    sendError.value = e instanceof Error ? e.message : '读取未完成。'
  }
  await nextTick(() => streamEnd.value?.scrollIntoView({ block: 'end' }))
}

async function newConversation() {
  if (sending.value) return
  try {
    const conversation = await (await services()).assistant.newConversation()
    conversations.value = await (await services()).assistant.conversations()
    await open(conversation.id)
  } catch (e) {
    sendError.value = e instanceof Error ? e.message : '创建未完成。'
  }
}

async function removeConversation(id: string) {
  if (sending.value) return
  try {
    await (await services()).assistant.removeConversation(id)
    conversations.value = await (await services()).assistant.conversations()
    if (activeId.value === id) {
      if (conversations.value.length) await open(conversations.value[0]!.id)
      else {
        activeId.value = ''
        messages.value = []
      }
    }
  } catch (e) {
    sendError.value = e instanceof Error ? e.message : '删除未完成。'
  }
}

function stop() {
  controller?.abort()
}

async function send(content?: string) {
  const text = (content ?? draft.value).trim()
  if (!text || sending.value) return
  sending.value = true
  sendError.value = ''
  draft.value = ''
  controller = new AbortController()
  const optimistic: AssistantMessage = {
    id: `local-${Date.now()}`,
    role: 'user',
    content: text,
    created_at: new Date().toISOString(),
  }
  messages.value = [...messages.value, optimistic]
  await nextTick(() => streamEnd.value?.scrollIntoView({ block: 'end' }))
  try {
    const answer = await (await services()).assistant.send(activeId.value, text, controller.signal)
    messages.value = [...messages.value, answer]
    conversations.value = await (await services()).assistant.conversations()
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      messages.value = messages.value.filter((m) => m.id !== optimistic.id)
      draft.value = text
      sendError.value = '已中断本次回答，问题已恢复到输入框。'
    } else {
      sendError.value = e instanceof Error ? e.message : '回答未完成。'
      messages.value = messages.value.filter((m) => m.id !== optimistic.id)
      draft.value = text
    }
  } finally {
    sending.value = false
    controller = null
    await nextTick(() => streamEnd.value?.scrollIntoView({ block: 'end' }))
  }
}

watch(sending, async (busy) => {
  if (busy)
    await nextTick(() => streamEnd.value?.scrollIntoView({ behavior: 'smooth', block: 'end' }))
})

onMounted(load)
onBeforeUnmount(() => controller?.abort())
</script>
<template>
  <div class="hp-section-gap">
    <div class="hp-pagehead">
      <div>
        <h1>AI 助教</h1>
        <p class="hp-pagehead-desc">
          基于知识库资料回答教学问题、整理教学建议；所有回答附原文引用，无证据不回答。
        </p>
      </div>
    </div>

    <PageState :loading="loading" :error="error" @retry="load">
      <template v-if="unsupported">
        <UnsupportedPanel
          summary="AI 助教聊天接口尚未接入"
          detail="真实模式的对话、检索与回答由部署侧 AI 服务提供，对应 HTTP 接口尚未开发。演示模式（npm run dev:demo）使用知识库合成资料模拟检索与回答（明确标注为演示数据）；接口就绪后此处会接入真实服务。"
        />
      </template>
      <template v-else>
        <div
          style="
            display: grid;
            grid-template-columns: minmax(220px, 300px) 1fr;
            gap: 14px;
            align-items: start;
          "
        >
          <aside class="hp-card" style="min-width: 0">
            <div class="hp-card-head" style="padding: 12px 14px">
              <h2 class="hp-card-title" style="font-size: 14px">
                <MessagesSquare aria-hidden="true" />历史会话
              </h2>
              <button
                class="hp-card-side hp-btn hp-btn-secondary hp-btn-sm"
                type="button"
                @click="newConversation"
              >
                <Plus aria-hidden="true" />新对话
              </button>
            </div>
            <div style="padding: 8px">
              <p v-if="!conversations.length" class="hp-fine" style="padding: 8px">
                还没有会话，点击“新对话”开始。
              </p>
              <div
                v-for="conversation in conversations"
                :key="conversation.id"
                class="hp-flex"
                style="
                  justify-content: space-between;
                  border-radius: 10px;
                  padding: 8px 10px;
                  cursor: pointer;
                "
                :style="{
                  background:
                    conversation.id === activeId ? 'var(--hp-primary-soft)' : 'transparent',
                }"
                @click="open(conversation.id)"
              >
                <div style="min-width: 0">
                  <div
                    style="
                      font-size: 13px;
                      font-weight: 600;
                      overflow: hidden;
                      text-overflow: ellipsis;
                      white-space: nowrap;
                    "
                  >
                    {{ conversation.title }}
                  </div>
                  <div class="hp-fine">
                    {{ formatRelative(conversation.updated_at) }} ·
                    {{ conversation.message_count }} 条
                  </div>
                </div>
                <button
                  class="hp-iconbtn"
                  style="width: 26px; height: 26px; color: var(--hp-ink-400)"
                  type="button"
                  aria-label="删除会话"
                  @click.stop="removeConversation(conversation.id)"
                >
                  <Trash2 aria-hidden="true" style="width: 13px; height: 13px" />
                </button>
              </div>
            </div>
          </aside>

          <section
            class="hp-card"
            style="min-width: 0; display: flex; flex-direction: column; min-height: 560px"
          >
            <div class="hp-card-head" style="padding: 12px 18px">
              <h2 class="hp-card-title" style="font-size: 14px">
                <Bot aria-hidden="true" />{{ active?.title ?? 'AI 助教' }}
              </h2>
              <span class="hp-card-note">回答基于知识库已索引资料</span>
            </div>

            <div style="flex: 1; overflow-y: auto; padding: 18px; min-height: 320px">
              <EmptyState
                v-if="!messages.length"
                title="向 AI 助教提问"
                desc="试着问一个教学问题，例如学生在解题中的常见错误、某知识点的讲解思路。助教只根据知识库资料回答，并给出原文出处。"
              />
              <div v-else class="hp-chat">
                <div
                  v-for="message in messages"
                  :key="message.id"
                  class="hp-msg"
                  :class="message.role === 'user' ? 'is-user' : 'is-assistant'"
                >
                  <div class="hp-msg-avatar">
                    <User
                      v-if="message.role === 'user'"
                      aria-hidden="true"
                      style="width: 16px; height: 16px"
                    />
                    <Bot v-else aria-hidden="true" style="width: 16px; height: 16px" />
                  </div>
                  <div class="hp-msg-body">
                    <div class="hp-msg-bubble">{{ message.content }}</div>
                    <div v-if="message.toolCalls?.length" class="hp-msg-meta">
                      <span
                        v-for="tool in message.toolCalls"
                        :key="tool.name"
                        class="hp-flex hp-gap-sm"
                        style="display: inline-flex"
                      >
                        <Search aria-hidden="true" style="width: 11px; height: 11px" />
                        {{ tool.name }}：{{ tool.summary }}
                      </span>
                    </div>
                    <div v-if="message.citations?.length" style="margin-top: 5px">
                      <button
                        v-for="cite in message.citations"
                        :key="cite.index"
                        class="hp-cite-chip"
                        type="button"
                        @click="citation = message.citations ?? null"
                      >
                        <Quote aria-hidden="true" style="width: 10px; height: 10px" />[{{
                          cite.index
                        }}]
                        {{ cite.doc_title }}
                      </button>
                    </div>
                    <div class="hp-msg-meta">{{ formatRelative(message.created_at) }}</div>
                  </div>
                </div>
                <div v-if="sending" class="hp-msg is-assistant">
                  <div class="hp-msg-avatar">
                    <Bot aria-hidden="true" style="width: 16px; height: 16px" />
                  </div>
                  <div class="hp-msg-body">
                    <div class="hp-msg-bubble">
                      <span
                        class="hp-flex hp-gap-sm"
                        style="display: inline-flex; font-size: 13px; color: var(--hp-ink-500)"
                      >
                        <span class="hp-spin" style="width: 14px; height: 14px"></span>
                        正在检索知识库并整理回答…
                      </span>
                    </div>
                  </div>
                </div>
                <div ref="streamEnd"></div>
              </div>
            </div>

            <div style="border-top: 1px solid var(--hp-border); padding: 14px 18px">
              <div v-if="!messages.length" class="hp-flex" style="margin-bottom: 10px">
                <button
                  v-for="item in recommended"
                  :key="item"
                  class="hp-btn hp-btn-ghost hp-btn-sm"
                  type="button"
                  @click="send(item)"
                >
                  {{ item }}
                </button>
              </div>
              <div
                v-if="sendError"
                class="hp-note hp-note-danger"
                role="alert"
                style="margin-bottom: 10px; padding: 8px 12px"
              >
                {{ sendError }}
                <button
                  v-if="draft"
                  class="hp-btn hp-btn-secondary hp-btn-sm"
                  type="button"
                  @click="send()"
                >
                  <RefreshCw aria-hidden="true" />重试
                </button>
              </div>
              <div class="hp-flex" style="align-items: flex-end">
                <textarea
                  v-model="draft"
                  class="hp-textarea"
                  rows="2"
                  style="flex: 1; min-width: 0"
                  placeholder="输入教学问题…（Enter 发送，Shift+Enter 换行）"
                  :disabled="sending"
                  @keydown.enter.exact.prevent="send()"
                />
                <button v-if="sending" class="hp-btn hp-btn-secondary" type="button" @click="stop">
                  <CircleStop aria-hidden="true" />停止生成
                </button>
                <button
                  v-else
                  class="hp-btn hp-btn-primary"
                  type="button"
                  :disabled="!draft.trim()"
                  @click="send()"
                >
                  <Send aria-hidden="true" />发送
                </button>
              </div>
              <p class="hp-fine" style="margin-top: 6px">
                回答由 AI 基于知识库资料生成，仅供教学参考；不用于成绩评定，不代替教师判断。
              </p>
            </div>
          </section>
        </div>
      </template>
    </PageState>

    <el-dialog :model-value="!!citation" title="引用出处" width="520" @close="citation = null">
      <div v-for="item in citation ?? []" :key="item.index" style="margin-bottom: 12px">
        <div class="hp-cell-title" style="font-size: 13.5px">
          [{{ item.index }}] {{ item.doc_title }}
        </div>
        <p style="font-size: 13px; color: var(--hp-ink-700); line-height: 1.8; margin-top: 4px">
          “{{ item.quote }}…”
        </p>
      </div>
      <p class="hp-fine">完整资料可在「知识库」中查看。</p>
      <template #footer>
        <RouterLink class="hp-btn hp-btn-secondary" to="/knowledge" @click="citation = null"
          >前往知识库</RouterLink
        >
      </template>
    </el-dialog>
  </div>
</template>
