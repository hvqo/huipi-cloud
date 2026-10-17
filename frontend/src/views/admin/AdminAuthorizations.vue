<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { KeyRound, Plus, ShieldAlert } from 'lucide-vue-next'
import { services, type TeacherRecord, type S } from '../../services'
import { formatDateTime } from '../../lib/labels'
import { useAdminMutation } from '../../lib/admin'
import PageState from '../../components/PageState.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
const route = useRoute(),
  teachers = ref<TeacherRecord[]>([]),
  assignments = ref<S['AssignmentSummary'][]>([]),
  rows = ref<S['AssignmentMembershipSummary'][]>([])
const uid = ref(String(route.query.teacher || '')),
  loading = ref(false),
  error = ref(''),
  offset = ref(0),
  hasNext = ref(false),
  teacherOffset = ref(0),
  teacherNext = ref(false)
const assignmentOffset = ref(0),
  assignmentNext = ref(false)
async function moreAssignments(reset = false) {
  const next = reset ? 0 : assignmentOffset.value + 50
  const result = await (await services()).admin.assignableAssignments({ limit: 50, offset: next })
  assignments.value = reset ? result.records : [...assignments.value, ...result.records]
  assignmentOffset.value = next
  assignmentNext.value = result.has_next
}
const selectedTeacher = computed(() => teachers.value.find((t) => t.user_id === uid.value)),
  dialog = ref(false),
  aid = ref(''),
  write = ref(false),
  revokeTarget = ref<S['AssignmentMembershipSummary'] | null>(null)
const mutation = useAdminMutation(),
  { busy, error: writeError, uncertain, notice } = mutation
const assignmentTitle = (value: string) =>
  assignments.value.find((a) => a.id === value)?.title || `作业 ${value.slice(0, 12)}`
async function loadTeachers(value = 0) {
  const result = await (await services()).admin.teachers({ limit: 50, offset: value })
  teachers.value = result.records
  teacherOffset.value = value
  teacherNext.value = result.has_next
}
async function load(value = 0) {
  rows.value = []
  offset.value = value
  if (!uid.value) return
  loading.value = true
  error.value = ''
  try {
    const result = await (await services()).admin.memberships(uid.value, value)
    rows.value = result.records
    hasNext.value = result.has_next
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
  } finally {
    loading.value = false
  }
}
async function start() {
  loading.value = true
  error.value = ''
  try {
    await loadTeachers()
    await moreAssignments(true)
    if (!uid.value) uid.value = teachers.value[0]?.user_id || ''
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
  } finally {
    loading.value = false
  }
}
async function grant() {
  if (!aid.value || !uid.value) return
  const target = uid.value,
    assignment = aid.value,
    body = { request_id: crypto.randomUUID(), can_write: write.value }
  await mutation.run(
    async () => {
      await (await services()).admin.grant(assignment, target, body)
      dialog.value = false
    },
    () => load(offset.value),
  )
}
async function revoke() {
  if (!revokeTarget.value) return
  const target = revokeTarget.value,
    requestId = crypto.randomUUID()
  await mutation.run(
    async () => {
      await (await services()).admin.revoke(target.assignment_id, target.user_id, requestId)
      revokeTarget.value = null
    },
    () => load(offset.value),
  )
}
function edit(row: S['AssignmentMembershipSummary']) {
  aid.value = row.assignment_id
  write.value = row.can_write
  dialog.value = true
}
onMounted(start)
</script>
<template>
  <div class="hp-section-gap">
    <div class="hp-pagehead">
      <div>
        <h1>作业授权</h1>
        <p class="hp-pagehead-desc">为指定教师分配作业，控制只读与可写范围。</p>
      </div>
      <button
        class="hp-btn hp-btn-primary"
        :disabled="!uid || busy || uncertain || selectedTeacher?.is_active === false"
        @click="
          () => {
            aid = ''
            write = false
            dialog = true
          }
        "
      >
        <Plus />分配作业
      </button>
    </div>
    <section class="hp-note">
      <ShieldAlert />
      <div>授权对象为普通教师。管理员当前拥有全局教学资料访问权，不通过这里调整管理员角色。</div>
    </section>
    <p v-if="notice" class="hp-note hp-note-success" role="status">{{ notice }}</p>
    <p v-if="writeError" class="hp-note hp-note-warning" role="alert">
      {{ writeError
      }}<button v-if="uncertain" class="hp-btn hp-btn-secondary hp-btn-sm" @click="mutation.retry">
        重试原管理请求
      </button>
    </p>
    <section class="hp-card">
      <div class="hp-card-body hp-flex hp-wrap">
        <label class="hp-flex"
          ><KeyRound />选择教师<select
            v-model="uid"
            aria-label="选择教师"
            class="hp-select"
            @change="load(0)"
          >
            <option value="" disabled>请选择</option>
            <option v-for="teacher in teachers" :key="teacher.user_id" :value="teacher.user_id">
              {{ teacher.display_name || `教师 ${teacher.user_id.slice(0, 8)}`
              }}{{ teacher.is_active ? '' : '（已停用）' }}
            </option>
          </select></label
        ><button
          v-if="teacherOffset > 0"
          class="hp-btn hp-btn-secondary hp-btn-sm"
          @click="loadTeachers(teacherOffset - 50)"
        >
          上一页教师</button
        ><button
          v-if="teacherNext"
          class="hp-btn hp-btn-secondary hp-btn-sm"
          @click="loadTeachers(teacherOffset + 50)"
        >
          下一页教师</button
        ><RouterLink
          v-if="uid"
          class="hp-link"
          :to="`/admin/audit?teacher=${encodeURIComponent(uid)}`"
          >查看授权历史 →</RouterLink
        >
      </div>
      <PageState :loading="loading" :error="error" @retry="start"
        ><div class="hp-table-wrap">
          <table v-if="rows.length" class="hp-table">
            <thead>
              <tr>
                <th>作业</th>
                <th>权限</th>
                <th>状态</th>
                <th>授予时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in rows" :key="row.assignment_id">
                <td>
                  <b>{{ assignmentTitle(row.assignment_id) }}</b>
                </td>
                <td>{{ row.can_write ? '可写 · 可核对和审核' : '只读 · 仅查看' }}</td>
                <td>
                  <span
                    class="hp-badge"
                    :class="row.is_active ? 'hp-badge-success' : 'hp-badge-neutral'"
                    >{{ row.is_active ? '有效' : row.revoked_at ? '已撤销' : '账号停用' }}</span
                  >
                </td>
                <td>{{ formatDateTime(row.created_at) }}</td>
                <td>
                  <div class="hp-flex">
                    <button
                      class="hp-link"
                      :disabled="busy || uncertain || selectedTeacher?.is_active === false"
                      @click="edit(row)"
                    >
                      {{ row.revoked_at ? '重新授权' : '修改权限' }}</button
                    ><button
                      v-if="!row.revoked_at"
                      class="hp-link hp-danger-text"
                      :disabled="busy || uncertain"
                      @click="revokeTarget = row"
                    >
                      撤销
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <EmptyState
            v-else
            title="暂时没有作业授权"
            desc="选择教师后，可为其分配需要参与的作业。"
          />
        </div>
        <PaginationBar :offset="offset" :limit="50" :has-next="hasNext" @change="load"
      /></PageState>
    </section>
    <el-dialog v-model="dialog" title="分配作业权限" width="540px"
      ><form class="hp-form" @submit.prevent="grant">
        <p>
          操作对象：<b>{{ selectedTeacher?.display_name || uid.slice(0, 12) }}</b>
        </p>
        <label
          >作业<select v-model="aid" required class="hp-select" aria-label="待授权作业">
            <option value="" disabled>选择一份作业</option>
            <option v-for="item in assignments" :key="item.id" :value="item.id">
              {{ item.title }}
            </option>
          </select></label
        >
        <p class="hp-fine">已加载 {{ assignments.length }} 份可访问作业。</p>
        <button
          v-if="assignmentNext"
          type="button"
          class="hp-btn hp-btn-secondary hp-btn-sm"
          @click="moreAssignments()"
        >
          加载更多作业
        </button>
        <fieldset class="hp-radio-options">
          <legend>访问权限</legend>
          <label
            ><input
              v-model="write"
              type="radio"
              :value="false"
            />只读：查看资料，不能保存核对或审核</label
          ><label
            ><input
              v-model="write"
              type="radio"
              :value="true"
            />可写：可在作业内录入、核对与审核</label
          >
        </fieldset>
        <p v-if="writeError" class="hp-error-text" role="alert">{{ writeError }}</p>
        <div class="hp-dialog-actions">
          <button type="button" class="hp-btn hp-btn-secondary" @click="dialog = false">取消</button
          ><button class="hp-btn hp-btn-primary" :disabled="busy || uncertain">确认授权</button>
        </div>
      </form></el-dialog
    ><el-dialog
      :model-value="!!revokeTarget"
      title="撤销作业权限"
      width="480px"
      @update:model-value="revokeTarget = null"
      ><p>
        确认撤销该教师对「{{
          revokeTarget ? assignmentTitle(revokeTarget.assignment_id) : ''
        }}」的访问？后续请求将立即按新授权状态校验，历史审核记录保留。
      </p>
      <p v-if="writeError" class="hp-error-text" role="alert">{{ writeError }}</p>
      <template #footer
        ><button class="hp-btn hp-btn-secondary" @click="revokeTarget = null">取消</button
        ><button class="hp-btn hp-btn-primary" :disabled="busy || uncertain" @click="revoke">
          确认撤销
        </button></template
      ></el-dialog
    >
  </div>
</template>
