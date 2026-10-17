import { reactive } from 'vue'
import { DEMO_MODE } from './mode'
import { identity, initializeAuth, logout } from '../auth'
import { currentDemoRole, setDemoRole, type DemoRole } from './demo-auth'
import { services } from '../services'

/**
 * 统一会话门面：真实模式包装 OIDC（auth.ts，行为不变），
 * 演示模式管理本地演示身份。路由守卫与界面只依赖本模块。
 */
export const session = reactive({
  ready: false,
  authenticated: false,
  userId: '',
  name: '',
  role: '' as 'teacher' | 'admin' | '',
  capabilities: [] as string[],
  /** 本地草稿隔离键（复核未确认请求等）。 */
  actor: '',
  /** 已通过身份服务登录，但本地账号未登记/停用/暂时无法读取档案。 */
  provisionError: '',
})

function reset() {
  session.authenticated = false
  session.userId = ''
  session.name = ''
  session.role = ''
  session.capabilities = []
  session.actor = ''
  session.provisionError = ''
}

async function loadProfile() {
  try {
    const me = await (await services()).me.current()
    session.userId = me.user_id
    session.role = me.role
    session.capabilities = me.capabilities
    session.name = DEMO_MODE
      ? me.display_name || '演示用户'
      : identity.name || me.display_name || '已登录教师'
    session.actor = DEMO_MODE ? `demo|${me.user_id}` : identity.actor
    session.provisionError = ''
  } catch (error) {
    // 已登录但 /me 不可用：不伪造任何角色，进入阻断提示。
    session.role = ''
    session.provisionError =
      error instanceof Error ? error.message : '无法读取账号档案，请稍后重试。'
  }
}

export async function initSession(): Promise<void> {
  if (session.ready) return
  if (DEMO_MODE) {
    const role = currentDemoRole()
    if (role) {
      session.authenticated = true
      await loadProfile()
    }
    session.ready = true
    return
  }
  await initializeAuth()
  if (identity.authenticated) {
    session.authenticated = true
    await loadProfile()
  }
  session.ready = true
}

export async function signInDemo(role: DemoRole): Promise<void> {
  setDemoRole(role)
  session.authenticated = true
  await loadProfile()
}

export async function signOut(): Promise<void> {
  if (DEMO_MODE) {
    setDemoRole(null)
    reset()
    return
  }
  reset()
  identity.error = ''
  try {
    await logout()
  } catch {
    identity.error = '已退出本地会话，统一身份服务的退出尚未确认。'
  }
}

export function hasCapability(capability: string): boolean {
  return session.capabilities.includes(capability)
}

export function refreshSession(): Promise<void> {
  return loadProfile()
}
