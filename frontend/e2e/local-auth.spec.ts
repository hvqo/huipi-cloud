import { expect, test } from '@playwright/test'

test('本地账号：管理员建号、教师首次改密及密码重置', async ({ page }) => {
  test.skip(process.env.RUN_LOCAL_AUTH_BROWSER !== '1', 'local auth browser opt-in')
  const admin = process.env.LOCAL_AUTH_ADMIN!,
    teacher = process.env.LOCAL_AUTH_TEACHER!
  const initial = '123456'
  const changed = 'abcdef'
  const mailChanged = '!@#$%^'
  const reset = '654321'
  const directory = process.env.LOCAL_AUTH_SCREENSHOT_DIR
  async function screenshot(name: string) {
    if (!directory) return
    const { mkdir } = await import('node:fs/promises')
    await mkdir(directory, { recursive: true })
    await page.screenshot({ path: `${directory}/${name}.png`, animations: 'disabled' })
  }
  async function login(account: string, password: string) {
    await page.goto('/login')
    await page.getByLabel('邮箱或手机号').fill(account)
    await page.getByLabel('密码', { exact: true }).fill(password)
    await page.getByRole('button', { name: '登 录' }).click()
  }
  await page.goto('/login')
  await expect(page.getByLabel('邮箱或手机号')).toBeVisible()
  await screenshot('login')
  await login(admin, process.env.LOCAL_AUTH_ADMIN_PASSWORD!)
  await expect(page).toHaveURL(/dashboard$/)
  await page.reload()
  await expect(page.getByRole('link', { name: '教师账号' })).toBeVisible()
  await page.getByRole('link', { name: '教师账号' }).click()
  await page.getByLabel('姓名').fill('合成教师')
  await page.getByLabel('邮箱或手机号').fill(teacher)
  await page.getByLabel('初始密码').fill(initial)
  await page.getByRole('button', { name: '创建账号' }).click()
  await expect(page.getByRole('status')).toContainText('账号已创建')
  await screenshot('teacher-accounts')
  await page.getByRole('button', { name: '退出' }).click()
  await login(teacher, initial)
  await expect(page).toHaveURL(/change-password$/)
  await screenshot('first-login-password')
  await page.getByLabel('当前密码').fill(initial)
  await page.getByLabel('新密码', { exact: true }).fill(changed)
  await page.getByLabel('再次输入新密码').fill(changed)
  await page.getByRole('button', { name: '保存新密码' }).click()
  await expect(page).toHaveURL(/login/)
  await login(teacher, changed)
  await expect(page).toHaveURL(/dashboard$/)
  await expect(page.getByRole('link', { name: '教师账号' })).toHaveCount(0)
  await page.getByRole('link', { name: '修改密码' }).click()
  await page.getByLabel('邮箱地址').fill('recovery@example.invalid')
  await page.getByRole('button', { name: '发送验证邮件' }).click()
  await expect(page.getByRole('status')).toContainText('验证邮件已发送')
  await screenshot('recovery-email')
  const control = process.env.LOCAL_AUTH_CONTROL_URL!
  const verification = await (await page.request.get(`${control}/mail/verify_email`)).json()
  await page.goto(`/verify-email#token=${verification.token}`)
  await page.getByRole('button', { name: '确认绑定' }).click()
  await expect(page.getByRole('status')).toContainText('找回邮箱已验证')
  await page.getByRole('button', { name: '退出' }).click()
  await page.getByRole('link', { name: '忘记密码？' }).click()
  await screenshot('forgot-password')
  await page.getByLabel('邮箱或手机号').fill(teacher)
  await page.getByRole('button', { name: '发送重置邮件' }).click()
  await expect(page.getByRole('status')).toContainText('如果账号已绑定')
  await expect
    .poll(async () => (await page.request.get(`${control}/mail/reset_password`)).status())
    .toBe(200)
  const recovery = await (await page.request.get(`${control}/mail/reset_password`)).json()
  await page.goto(`/reset-password#token=${recovery.token}`)
  await page.getByLabel('新密码（6 至 128 个字符）').fill(mailChanged)
  await page.getByLabel('再次输入新密码').fill(mailChanged)
  await page.getByRole('button', { name: '保存新密码' }).click()
  await expect(page).toHaveURL(/login/)
  await login(teacher, mailChanged)
  await expect(page).toHaveURL(/dashboard$/)
  await page.getByRole('button', { name: '退出' }).click()
  await login(admin, process.env.LOCAL_AUTH_ADMIN_PASSWORD!)
  await page.getByRole('link', { name: '教师账号' }).click()
  await page
    .locator('tr')
    .filter({ hasText: teacher })
    .getByRole('button', { name: '重置密码' })
    .click()
  await page.getByLabel('临时密码（至少 6 个字符）').fill(reset)
  await page.getByRole('button', { name: '确认重置' }).click()
  await expect(page.getByRole('status')).toContainText('密码已重置')
  await page.getByRole('button', { name: '退出' }).click()
  await login(teacher, mailChanged)
  await expect(page.getByRole('alert')).toContainText('账号或密码不正确')
  await login(teacher, reset)
  await expect(page).toHaveURL(/change-password$/)
})
