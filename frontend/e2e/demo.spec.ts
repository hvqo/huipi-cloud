import { expect, test, type Page } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
const png = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZwwAAAAASUVORK5CYII=',
  'base64',
)
async function login(page: Page, role = '教师') {
  await page.goto('/login')
  await page.getByRole('button', { name: new RegExp(`${role}演示`) }).click()
  await expect(page).toHaveURL(role === '教师' ? /\/workbench$/ : /\/admin$/)
  await expect(page.getByRole('banner')).toContainText('演示数据')
}
async function selectText(page: Page, selector = '[data-source-pointer]', text?: string) {
  await page
    .locator(selector)
    .filter({ hasText: text })
    .first()
    .evaluate((el) => {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
      let node = walker.nextNode()!
      while (node && !node.textContent?.length) node = walker.nextNode()!
      const range = document.createRange()
      range.setStart(node, 0)
      range.setEnd(node, Math.min(node.textContent!.length, 9))
      window.getSelection()!.removeAllRanges()
      window.getSelection()!.addRange(range)
    })
}
async function screenshot(page: Page, name: string) {
  const dir =
    process.env.UPDATE_SCREENSHOTS === '1' ? '../docs/frontend/screenshots' : 'test-results/visual'
  await mkdir(dir, { recursive: true })
  await page.screenshot({ path: `${dir}/${name}.png`, fullPage: true })
}

test('教师创建题目、发布校验、多文件提交与原始文件回读', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await login(page)
  await page.getByRole('link', { name: '我的作业', exact: true }).click()
  await page.getByRole('button', { name: '＋ 新建作业' }).click()
  await page.getByLabel('作业名称').fill('浏览器合成作业')
  await page.getByLabel('学科', { exact: true }).fill('数学')
  await page.getByLabel('年级', { exact: true }).fill('高一')
  await page.getByRole('button', { name: '创建草稿', exact: true }).click()
  await expect(page.getByRole('heading', { name: '浏览器合成作业' })).toBeVisible()
  await page.getByRole('button', { name: /发布作业/ }).click()
  await expect(page.getByRole('button', { name: '确认发布', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('link', { name: /新增题目/ }).click()
  await page.getByLabel('题干', { exact: true }).fill('计算1+1。')
  await page.getByPlaceholder('输入标准答案或评分参考').fill('2')
  await page.getByRole('button', { name: '添加评分项' }).click()
  await page.getByLabel('第 1 项 · 给分点说明').fill('结果正确')
  await page.getByLabel('分值上限').fill('10')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('heading', { name: '第 1 题 · 编辑' })).toBeVisible()
  await expect(page.getByPlaceholder('输入标准答案或评分参考')).toHaveValue('2')
  await page.getByRole('link', { name: '返回作业' }).click()
  await page.getByRole('button', { name: /发布作业/ }).click()
  await page.getByRole('button', { name: '确认发布', exact: true }).click()
  await expect(page.getByText('已发布', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: '上传答卷', exact: true }).click()
  await page.locator('input[type=file]').setInputFiles([
    { name: 'synthetic-a.png', mimeType: 'image/png', buffer: png },
    { name: 'synthetic-b.png', mimeType: 'image/png', buffer: png },
  ])
  await page.getByPlaceholder('学生标识').nth(0).fill('SYN-A')
  await page.getByPlaceholder('学生标识').nth(1).fill('SYN-B')
  await page.getByRole('button', { name: '开始上传' }).click()
  await expect(page.getByText('已提交', { exact: true })).toHaveCount(2)
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
  await page.getByRole('tab', { name: '学生提交' }).click()
  await expect(page.getByText('SYN-A', { exact: true })).toBeVisible()
  await expect(page.locator('tbody').getByText('解析完成', { exact: true })).toHaveCount(2, {
    timeout: 15000,
  })
  await page.locator('tbody tr').filter({ hasText: 'SYN-A' }).getByRole('link').first().click()
  await page.getByRole('button', { name: '查看原始文件' }).click()
  await expect(page.getByRole('img', { name: 'synthetic-a.png' })).toHaveAttribute('src', /^blob:/)
  const original = await page.getByRole('img', { name: 'synthetic-a.png' }).getAttribute('src')
  expect(
    await page.evaluate(
      async (url) => [...new Uint8Array(await (await fetch(url!)).arrayBuffer())],
      original,
    ),
  ).toEqual([...png])
  await page
    .getByRole('dialog')
    .getByRole('button', { name: /Close this dialog|关闭此对话框/ })
    .click()
  await page.reload()
  await page.getByRole('button', { name: '查看原始文件' }).click()
  await expect(page.getByRole('img', { name: 'synthetic-a.png' })).toHaveAttribute('src', /^blob:/)
  await page
    .getByRole('dialog')
    .getByRole('button', { name: /Close this dialog|关闭此对话框/ })
    .click()
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: '重置', exact: true }).click(),
  ])
  await page.goto('/assignments')
  await expect(page.getByText('浏览器合成作业', { exact: true })).toHaveCount(0)
  expect(errors).toEqual([])
})

test('三栏作答核对和完整评分修订历史', async ({ page }) => {
  await login(page)
  await page.goto('/assignments/asg-1/submissions/sub-1/questions/q-1-1/review')
  await expect(page.getByTestId('canonical-paper')).toBeVisible()
  await selectText(page, '[data-source-pointer]', '学生作答：选 B')
  await page.getByRole('button', { name: '将当前文字选区加入证据' }).click()
  await page.getByRole('button', { name: '预览并保存复核' }).click()
  await page
    .getByRole('checkbox', { name: '我已对照来源核对，本次仅确认作答证据，不发布成绩。' })
    .check()
  await page.getByRole('button', { name: '确认保存', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('已确认保存')
  await page.reload()
  await expect(page.locator('.history-row')).not.toHaveCount(0)
  await selectText(page, '[data-source-pointer]', '学生作答：选 B')
  await page.getByRole('button', { name: '将当前文字选区加入证据' }).click()
  await page.getByRole('button', { name: '保存并核对下一题' }).click()
  await page
    .getByRole('checkbox', { name: '我已对照来源核对，本次仅确认作答证据，不发布成绩。' })
    .check()
  await page.getByRole('button', { name: '确认保存', exact: true }).click()
  await expect(page).toHaveURL(/questions\/q-1-2\/review$/)
  await page.goto('/assignments/asg-1/submissions/sub-1/questions/q-1-3/grading')
  await page.getByRole('button', { name: '逐项修订', exact: true }).click()
  await page.getByLabel('修订分值').nth(1).fill('0')
  await page.getByLabel('修改理由').nth(1).fill('此步缺少充分依据，合成审核演示。')
  await page.locator('input[type=checkbox]').first().check()
  await page.getByRole('button', { name: '提交修订' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认提交' }).click()
  await expect(page.getByRole('status')).toContainText('已保存')
  await expect(page.getByText('审核历史')).toBeVisible()
  await expect(page.getByText(/6 分|6.00 分/).first()).toBeVisible()
  await page.reload()
  await expect(page.getByText('人工修订', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('候选 ≠ 正式成绩')).toBeVisible()
})

test('知识资料上传、索引、检索、引用、无证据与停止', async ({ page }) => {
  await login(page)
  await page.goto('/knowledge')
  await page.getByRole('button', { name: '上传资料', exact: true }).click()
  await page.locator('input[type=file]').setInputFiles({
    name: '合成恒星讲义.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('恒星演化验收：恒星通过核聚变释放能量。此为合成教学材料。'),
  })
  await page.getByRole('button', { name: '开始上传' }).click()
  const card = page.locator('article').filter({ hasText: '合成恒星讲义' })
  await expect(card.getByText('已索引', { exact: true })).toBeVisible({ timeout: 15000 })
  await page.getByPlaceholder('例如：切线方程 常见错误').fill('恒星演化验收')
  await page.getByRole('button', { name: '检索', exact: true }).click()
  await expect(page.getByText(/恒星通过核聚变/).first()).toBeVisible()
  await page.goto('/assistant')
  await page.getByRole('button', { name: '新对话' }).click()
  const input = page.getByPlaceholder('输入教学问题…（Enter 发送，Shift+Enter 换行）')
  await input.fill('恒星演化验收')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.locator('.hp-cite-chip')).not.toHaveCount(0)
  await page.locator('.hp-cite-chip').last().click()
  await expect(page.getByText('引用出处', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '新对话' }).click()
  await input.fill('不存在的XYZ探测器')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.locator('.hp-msg.is-assistant').last()).toContainText(/没有|未找到|缺少/)
  await input.fill('切线方程')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.getByRole('button', { name: '停止生成', exact: true }).click()
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeEnabled()
})

test('管理员登记、停用启用、只读授权、撤销与只读审计', async ({ page }) => {
  await login(page, '管理员')
  await page.getByRole('link', { name: '教师账号', exact: true }).click()
  await page.getByRole('button', { name: '登记教师', exact: true }).click()
  await page.getByLabel(/外部身份标识/).fill('synthetic-browser-teacher')
  await page.getByRole('button', { name: '确认登记' }).click()
  const row = page.locator('tbody tr').filter({ hasText: '演示教师5' })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: '停用', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认停用' }).click()
  await expect(row).toContainText('已停用')
  await row.getByRole('button', { name: '启用', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认启用' }).click()
  await row.getByRole('link', { name: '作业授权' }).click()
  await page.getByRole('button', { name: '分配作业', exact: true }).click()
  await page.getByLabel('待授权作业').selectOption('asg-1')
  await page.getByRole('button', { name: '确认授权' }).click()
  await expect(page.locator('tbody')).toContainText('只读')
  await page.getByRole('button', { name: '撤销', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认撤销' }).click()
  await expect(page.locator('tbody')).toContainText('已撤销')
  await page.getByRole('link', { name: '操作审计', exact: true }).click()
  await expect(page.locator('main')).toContainText('撤销作业权限')
  await expect(page.locator('main').getByRole('button', { name: /删除|编辑/ })).toHaveCount(0)
})

test('教师不能进入管理页；主要页面1366/1440/1920无横向溢出及视觉留档', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await login(page)
  await page.goto('/admin/teachers')
  await expect(page).toHaveURL(/\/403$/)
  const routes = [
    ['/workbench', 'teacher-home'],
    ['/assignments/asg-1', 'assignment'],
    ['/assignments/asg-1/submissions/sub-1/questions/q-1-3/review', 'answer-review'],
    ['/assignments/asg-1/submissions/sub-1/questions/q-1-3/grading', 'grade-review'],
    ['/knowledge', 'knowledge'],
    ['/assistant', 'assistant'],
    ['/insights', 'insights'],
    ['/settings', 'settings'],
  ]
  for (const width of [1366, 1440, 1920]) {
    await page.setViewportSize({ width, height: 1000 })
    for (const [path, name] of routes) {
      await page.goto(path!)
      await expect(page.locator('h1')).toBeVisible()
      await expect(page.locator('.state-card[role="status"]')).toHaveCount(0)
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        `${name} @${width}`,
      ).toBe(true)
      if (width === 1440) await screenshot(page, name!)
    }
  }
  await page.goto('/settings')
  await page.getByRole('button', { name: '退出登录', exact: true }).last().click()
  await login(page, '管理员')
  for (const width of [1366, 1440, 1920]) {
    await page.setViewportSize({ width, height: 1000 })
    for (const [path, name] of [
      ['/admin', 'admin-home'],
      ['/admin/teachers', 'admin-teachers'],
      ['/admin/authorizations', 'admin-authorizations'],
      ['/admin/audit', 'admin-audit'],
      ['/admin/system', 'admin-system'],
    ]) {
      await page.goto(path!)
      await expect(page.locator('h1')).toBeVisible()
      if (width === 1440) await screenshot(page, name!)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
        true,
      )
    }
  }
  expect(errors).toEqual([])
})

test('评分引文映射、503确认丢失与刷新后绑定原候选重放', async ({ page }) => {
  await login(page)
  await page.goto('/assignments/asg-1/submissions/sub-1/questions/q-1-3/grading')
  await page.getByRole('button', { name: '逐项修订', exact: true }).click()
  await page.getByLabel('修订分值').first().fill('3')
  await page.getByLabel('修改理由').first().fill('合成审核：只满足部分条件。')
  await page.getByRole('button', { name: '选择原文引文', exact: true }).first().click()
  await page
    .getByRole('dialog')
    .locator('[data-source-pointer]')
    .first()
    .evaluate((el) => {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
      let n = walker.nextNode()!
      while (n && !n.textContent?.length) n = walker.nextNode()!
      // Seeded quote mapping covers [2,14); stay inside that verified range.
      const r = document.createRange()
      r.setStart(n, 2)
      r.setEnd(n, 10)
      window.getSelection()!.removeAllRanges()
      window.getSelection()!.addRange(r)
    })
  await page.getByRole('button', { name: '使用当前选区' }).click()
  await page.locator('input[type=checkbox]').first().check()
  await page.evaluate(async () => {
    const path = '/src/services/index.ts'
    const client = '/src/api/client.ts'
    const svc = await (await import(path)).services()
    const { ApiError } = await import(client)
    const save = svc.grading.save
    svc.grading.save = async (...args: unknown[]) => {
      const result = await save(...args)
      svc.grading.save = save
      void result
      throw new ApiError(503, 'synthetic_lost_ack', true)
    }
  })
  await page.getByRole('button', { name: '提交修订' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认提交' }).click()
  await expect(page.getByRole('button', { name: '重试原请求' })).toBeVisible()
  const pending = await page.evaluate(() =>
    Object.entries(sessionStorage).filter(([key]) => key.startsWith('huipi.grading.')),
  )
  expect(pending.some(([key, value]) => key.endsWith('.target') && value === 'prop-3')).toBe(true)
  await expect(page.getByRole('button', { name: /候选 1 ·/ })).toBeDisabled()
  await page.reload()
  await page.getByRole('button', { name: '重试原请求' }).click()
  await expect(page.getByRole('status')).toContainText('已确认保存')
  await expect(page.locator('.history-row')).toHaveCount(1)
  await expect(page.getByText('候选 ≠ 正式成绩')).toBeVisible()
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((k) => k.startsWith('huipi.grading.')),
    ),
  ).toEqual([])
})
