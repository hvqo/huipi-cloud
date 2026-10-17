<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  Activity,
  BookOpen,
  Bot,
  ChartColumn,
  ChevronRight,
  Gauge,
  History,
  KeyRound,
  LayoutDashboard,
  LibraryBig,
  LogOut,
  RotateCcw,
  Server,
  Settings,
  Users,
} from 'lucide-vue-next'
import { DEMO_MODE } from '../app/mode'
import { DEMO_STATE_KEY } from '../app/demo-auth'
import { session, signOut } from '../app/session'

/**
 * 应用外壳：侧边导航 + 顶栏面包屑。
 * 管理员额外看到“管理”分组；未实现的功能不出现在导航中。
 */

const route = useRoute()

const groups = computed(() => {
  const base = [
    {
      caption: '教学',
      items: [
        { to: '/workbench', label: '工作台', icon: LayoutDashboard },
        { to: '/assignments', label: '我的作业', icon: BookOpen },
        { to: '/knowledge', label: '知识库', icon: LibraryBig },
      ],
    },
    {
      caption: '智能助教',
      items: [{ to: '/assistant', label: 'AI 助教', icon: Bot }],
    },
    {
      caption: '统计',
      items: [{ to: '/insights', label: '教学统计', icon: ChartColumn }],
    },
    {
      caption: '账户',
      items: [{ to: '/settings', label: '设置', icon: Settings }],
    },
  ]
  if (session.role === 'admin') {
    base.push({
      caption: '管理',
      items: [
        { to: '/admin', label: '管理概览', icon: Gauge },
        { to: '/admin/teachers', label: '教师账号', icon: Users },
        { to: '/admin/authorizations', label: '作业授权', icon: KeyRound },
        { to: '/admin/audit', label: '操作审计', icon: History },
        { to: '/admin/system', label: '系统状态', icon: Server },
      ],
    })
  }
  return base
})

const crumbs = computed(() => {
  const list = (route.meta.breadcrumb as string[] | undefined) ?? []
  return list.length ? list : ['慧批云端']
})

const roleLabel = computed(() => (session.role === 'admin' ? '管理员' : '教师'))

function resetDemo() {
  if (DEMO_MODE) {
    // 仅清除本地演示状态并重载；不引用演示数据模块，生产构建无演示代码路径。
    try {
      localStorage.removeItem(DEMO_STATE_KEY)
    } catch {
      /* 忽略 */
    }
    window.location.reload()
  }
}
</script>

<template>
  <div class="hp-shell">
    <aside class="hp-sidebar">
      <div class="hp-brand">
        <div class="hp-brand-mark">慧</div>
        <div>
          <div class="hp-brand-name">慧批云端</div>
          <div class="hp-brand-sub">智能批改 · 教学平台</div>
        </div>
      </div>
      <div v-for="group in groups" :key="group.caption" class="hp-nav-group">
        <div class="hp-nav-caption">{{ group.caption }}</div>
        <RouterLink
          v-for="item in group.items"
          :key="item.to"
          :to="item.to"
          class="hp-nav-item"
          :class="{ 'is-active': route.path === item.to || route.path.startsWith(item.to + '/') }"
        >
          <component :is="item.icon" aria-hidden="true" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </div>
      <div class="hp-sidebar-foot">
        <div class="hp-userchip">
          <div class="hp-avatar">{{ session.name.slice(0, 1) }}</div>
          <div class="hp-grow" style="min-width: 0">
            <div class="hp-userchip-name">{{ session.name || '未命名' }}</div>
            <div class="hp-userchip-role">{{ roleLabel }} · 身份会话</div>
          </div>
          <button class="hp-iconbtn" type="button" aria-label="退出登录" @click="signOut()">
            <LogOut aria-hidden="true" style="width: 16px; height: 16px" />
          </button>
        </div>
      </div>
    </aside>
    <div class="hp-main">
      <header class="hp-topbar">
        <nav class="hp-crumb" aria-label="页面路径">
          <template v-for="(crumb, index) in crumbs" :key="crumb + index">
            <ChevronRight
              v-if="index > 0"
              class="sep"
              aria-hidden="true"
              style="width: 13px; height: 13px"
            />
            <strong v-if="index === crumbs.length - 1">{{ crumb }}</strong>
            <span v-else>{{ crumb }}</span>
          </template>
        </nav>
        <div class="hp-topbar-side">
          <span v-if="DEMO_MODE" class="hp-demo-chip">
            <Activity aria-hidden="true" style="width: 13px; height: 13px" />
            演示数据
            <button type="button" @click="resetDemo">
              <RotateCcw aria-hidden="true" style="width: 11px; height: 11px" /> 重置
            </button>
          </span>
        </div>
      </header>
      <main class="hp-content" :key="route.fullPath">
        <slot />
      </main>
    </div>
  </div>
</template>
