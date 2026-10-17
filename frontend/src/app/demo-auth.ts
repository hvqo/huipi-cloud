/**
 * 演示模式的本地身份。仅存在于 `npm run dev:demo`（VITE_DEMO_MODE=true），
 * 与真实 OIDC 会话完全隔离；生产构建不会包含本模块的使用方。
 */
export type DemoRole = 'teacher' | 'admin'

const KEY = 'huipi.demo.user'

export function currentDemoRole(): DemoRole | null {
  try {
    const value = sessionStorage.getItem(KEY)
    return value === 'teacher' || value === 'admin' ? value : null
  } catch {
    return null
  }
}

export function setDemoRole(role: DemoRole | null): void {
  try {
    if (role) sessionStorage.setItem(KEY, role)
    else sessionStorage.removeItem(KEY)
  } catch {
    /* 演示身份无法持久化时仅影响刷新后的登录页展示 */
  }
}
