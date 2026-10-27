import { reactive } from 'vue'
import { InMemoryWebStorage, UserManager, WebStorageStateStore, type User } from 'oidc-client-ts'

const authority = import.meta.env.VITE_OIDC_AUTHORITY?.trim()
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID?.trim()
export const identity = reactive({
  ready: false,
  authenticated: false,
  name: '',
  error: '',
  actor: '',
})
let user: User | null = null
const validAuthority =
  authority &&
  (authority.startsWith('https://') ||
    (import.meta.env.DEV && /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(authority)))
export const manager =
  validAuthority && clientId
    ? new UserManager({
        authority,
        client_id: clientId,
        response_type: 'code',
        disablePKCE: false,
        redirect_uri: `${location.origin}/auth/callback`,
        post_logout_redirect_uri: `${location.origin}/login`,
        scope: import.meta.env.VITE_OIDC_SCOPE || 'openid profile',
        userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
        // Only the short-lived PKCE verifier/state goes in sessionStorage, never tokens.
        stateStore: new WebStorageStateStore({ store: sessionStorage }),
        automaticSilentRenew: false,
        loadUserInfo: false,
        filterProtocolClaims: true,
        extraQueryParams: import.meta.env.VITE_OIDC_AUDIENCE
          ? { audience: import.meta.env.VITE_OIDC_AUDIENCE }
          : {},
        resource: import.meta.env.VITE_OIDC_RESOURCE || undefined,
      })
    : null

function apply(next: User | null) {
  user = next
  if (next) identity.error = ''
  identity.authenticated = !!next && !next.expired
  identity.name = identity.authenticated ? String(next!.profile.name || '已登录教师') : ''
  identity.actor = identity.authenticated ? `${next!.profile.iss}|${next!.profile.sub}` : ''
  identity.ready = true
}
export async function initializeAuth() {
  apply(manager ? await manager.getUser() : null)
}
export async function expireAuth() {
  apply(null)
  await manager?.removeUser()
}
export function accessToken(): string | null {
  if (!user || user.expired) {
    void expireAuth()
    return null
  }
  return user.access_token
}
export function safeReturn(value: unknown) {
  return typeof value === 'string' &&
    /^\/(assignments|submissions|scans|exams|exam-results|papers|omr|exceptions|school|marking)(\/|\?|$)/.test(
      value,
    ) &&
    !value.includes('\\')
    ? value
    : '/assignments'
}
export async function login(path: string) {
  if (!manager) throw new Error('尚未配置身份提供者，请联系部署管理员。')
  await manager.signinRedirect({
    nonce: crypto.randomUUID(),
    state: { returnTo: safeReturn(path) },
  })
}
export async function callback() {
  if (!manager) throw new Error('尚未配置身份提供者。')
  const result = await manager.signinRedirectCallback()
  apply(result)
  return safeReturn((result.state as { returnTo?: string } | undefined)?.returnTo)
}
export async function logout() {
  const hint = user?.id_token
  await expireAuth()
  // Clear local evidence drafts on explicit sign-out, never retain a teacher's workspace.
  for (const key of Object.keys(sessionStorage))
    if (key.startsWith('huipi.review.')) sessionStorage.removeItem(key)
  if (manager && hint) await manager.signoutRedirect({ id_token_hint: hint })
}
manager?.events.addAccessTokenExpired(() => {
  void expireAuth()
})
