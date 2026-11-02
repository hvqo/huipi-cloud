import { reactive } from 'vue'

type Session = {
  user_id: string
  display_name: string | null
  must_change_password: boolean
  recovery_email?: string | null
}

export const identity = reactive({
  ready: false,
  authenticated: false,
  mustChangePassword: false,
  recoveryEmail: '',
  name: '',
  error: '',
  actor: '',
})

function apply(session: Session | null) {
  identity.authenticated = !!session
  identity.mustChangePassword = !!session?.must_change_password
  identity.recoveryEmail = session?.recovery_email || ''
  identity.name = session?.display_name || (session ? '教师' : '')
  identity.actor = session?.user_id || ''
  identity.ready = true
}

export function csrfToken(): string {
  const entry = document.cookie
    .split('; ')
    .find((part) => part.startsWith('huipi_csrf=') || part.startsWith('__Host-huipi_csrf='))
  return entry ? decodeURIComponent(entry.slice(entry.indexOf('=') + 1)) : ''
}

async function send(path: string, body?: object): Promise<Response> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 30000)
  try {
    return await fetch('/api/v1/auth/' + path, {
      method: body ? 'POST' : 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      signal: controller.signal,
      headers: body
        ? { 'Content-Type': 'application/json', 'X-Huipi-Client': '1', 'X-Huipi-CSRF': csrfToken() }
        : {},
      ...(body ? { body: JSON.stringify(body) } : {}),
    })
  } catch {
    throw new Error('无法连接登录服务，请检查网络后重试。')
  } finally {
    clearTimeout(timer)
  }
}

export async function initializeAuth() {
  try {
    const response = await send('session')
    if (response.status === 401) return apply(null)
    if (!response.ok) throw new Error('服务暂时不可用，请稍后重试。')
    apply((await response.json()) as Session)
  } catch {
    apply(null)
    identity.error = '无法连接登录服务，请检查后端是否启动。'
  }
}

export async function expireAuth() {
  apply(null)
}

export function safeReturn(value: unknown) {
  return typeof value === 'string' &&
    /^\/(dashboard|assignments|submissions|scans|exams|exam-results|final-results|papers|omr|exceptions|school|marking|admin)(\/|\?|$)/.test(
      value,
    ) &&
    !value.includes('\\')
    ? value
    : '/dashboard'
}

export async function login(account: string, password: string) {
  const response = await send('login', { account, password })
  if (response.status === 401) throw new Error('账号或密码不正确。')
  if (response.status === 429) throw new Error('尝试次数过多，请稍后再试。')
  if (!response.ok) throw new Error('登录暂时不可用，请稍后重试。')
  identity.error = ''
  apply((await response.json()) as Session)
}

export async function logout() {
  const response = await send('logout', {})
  if (!response.ok && response.status !== 401) throw new Error('退出未完成，请重试。')
  apply(null)
  for (const key of Object.keys(sessionStorage))
    if (key.startsWith('huipi.review.')) sessionStorage.removeItem(key)
}

export async function changePassword(current_password: string, new_password: string) {
  const response = await send('change-password', { current_password, new_password })
  if (response.status === 401) throw new Error('当前密码不正确，或登录已失效。')
  if (response.status === 422) throw new Error('新密码须为 6 至 128 个字符。')
  if (!response.ok) throw new Error('密码修改未完成，请稍后重试。')
  apply(null)
}

export async function requestRecoveryEmail(email: string) {
  const response = await send('recovery-email/request', { email })
  if (response.status === 429) throw new Error('操作过于频繁，请稍后再试。')
  if (response.status === 503) throw new Error('邮件服务尚不可用，请联系管理员。')
  if (!response.ok) throw new Error('发送验证邮件失败，请检查邮箱后重试。')
}

export async function confirmRecoveryEmail(token: string) {
  const response = await send('recovery-email/confirm', { token })
  if (!response.ok) throw new Error('验证链接无效、已过期，或邮箱已被使用。')
  await initializeAuth()
}

export async function requestPasswordReset(account: string) {
  const response = await send('reset/request', { account })
  if (!response.ok) throw new Error('请求暂未完成，请稍后重试。')
}

export async function confirmPasswordReset(token: string, password: string) {
  const response = await send('reset/confirm', { token, password })
  if (!response.ok) throw new Error('重置链接无效或已过期，请重新申请。')
  apply(null)
}
