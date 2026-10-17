<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Server, RefreshCw, ShieldCheck, Info } from 'lucide-vue-next'
import { services, type SystemModuleStatus } from '../../services'
import { DEMO_MODE } from '../../app/mode'
import { label, systemStatusLabels } from '../../lib/labels'
import PageState from '../../components/PageState.vue'
const rows = ref<SystemModuleStatus[]>([]),
  loading = ref(false),
  error = ref(''),
  checked = ref('')
async function load() {
  loading.value = true
  error.value = ''
  try {
    rows.value = await (await services()).system.modules()
    checked.value = new Date().toLocaleTimeString('zh-CN')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
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
        <h1>系统状态</h1>
        <p class="hp-pagehead-desc">查看服务连接情况与部署边界，配置变更由部署管理员执行。</p>
      </div>
      <button class="hp-btn hp-btn-secondary" :disabled="loading" @click="load">
        <RefreshCw />刷新状态
      </button>
    </div>
    <PageState :loading="loading" :error="error" @retry="load"
      ><p class="hp-fine">
        {{
          DEMO_MODE
            ? '以下为合成运行状态，不代表真实服务监测。'
            : '应用健康检查只证明进程存活；未接入的依赖状态不推断为正常。'
        }}
        最后检查 {{ checked }}
      </p>
      <div class="hp-module-grid">
        <section v-for="item in rows" :key="item.key" class="hp-card hp-card-body">
          <div class="hp-flex hp-between">
            <Server class="hp-module-icon" /><span
              class="hp-badge"
              :class="{
                'hp-badge-success': item.status === 'ok',
                'hp-badge-warning': item.status === 'degraded',
              }"
              >{{ label(systemStatusLabels, item.status) }}</span
            >
          </div>
          <h2>{{ item.name }}</h2>
          <p class="hp-muted">{{ item.detail }}</p>
        </section>
      </div></PageState
    >
    <section class="hp-note">
      <ShieldCheck />
      <div>
        <b>安全配置由部署环境管理</b>
        <p>此页面不读取或保存 API Key、数据库密码、身份服务密钥，不提供管理员晋升入口。</p>
      </div>
    </section>
    <section class="hp-card hp-card-body">
      <h2><Info class="hp-inline-icon" />基础信息</h2>
      <dl class="hp-detail-list">
        <dt>运行方式</dt>
        <dd>{{ DEMO_MODE ? '本地合成演示' : '真实 API 模式' }}</dd>
        <dt>生成模型</dt>
        <dd>{{ DEMO_MODE ? '未调用模型，使用合成回答' : '模型配置未通过管理接口提供' }}</dd>
        <dt>检索配置</dt>
        <dd>{{ DEMO_MODE ? '关键词检索演示' : '当前配置未通过管理接口提供' }}</dd>
        <dt>管理员权限</dt>
        <dd>目前包含全局教学资料访问，尚未拆分为仅系统管理权限。</dd>
        <dt>正式成绩</dt>
        <dd>未提供发布能力，人工审核也不等于发布成绩。</dd>
      </dl>
    </section>
  </div>
</template>
