import { expect, test, type Page } from '@playwright/test'
const aid = process.env.WORKBENCH_AID!,
  sid = process.env.WORKBENCH_SID!,
  qid = process.env.WORKBENCH_QID!
const reviewPath = `/assignments/${aid}/submissions/${sid}/questions/${qid}/review`
async function signIn(page: Page, target = '/assignments') {
  await page.goto('/login?returnTo=' + encodeURIComponent(target))
  await page.getByRole('button', { name: '通过统一身份登录' }).click()
  await expect(page).toHaveURL(new RegExp(target + '$'))
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
}
async function select(page: Page, prompt = false) {
  await page
    .locator('[data-source-pointer]')
    .first()
    .evaluate((element, pickPrompt) => {
      const text = element.textContent!,
        range = document.createRange()
      const start = pickPrompt ? 0 : text.indexOf('学生作答')
      const end = pickPrompt ? text.indexOf('\n') : text.length
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
      let node = walker.nextNode(),
        consumed = 0
      while (node) {
        const length = node.textContent!.length
        if (start >= consumed && start < consumed + length) range.setStart(node, start - consumed)
        if (end > consumed && end <= consumed + length) {
          range.setEnd(node, end - consumed)
          break
        }
        consumed += length
        node = walker.nextNode()
      }
      const selection = window.getSelection()!
      selection.removeAllRanges()
      selection.addRange(range)
    }, prompt)
  await page.getByRole('button', { name: '将当前文字选区加入证据' }).click()
  await expect(page.locator('.selection-list li')).toHaveCount(1)
}
async function save(page: Page) {
  await page.getByRole('button', { name: '预览并保存复核' }).click()
  await page
    .getByRole('checkbox', { name: '我已对照来源核对，本次仅确认作答证据，不发布成绩。' })
    .check()
  await page.getByRole('button', { name: '确认保存', exact: true }).click()
}
test('真实签名身份、PG/S3、Unicode证据、未知提交重放、冲突与只读工作流', async ({
  page,
  browser,
  request,
}) => {
  await page.setViewportSize({ width: 1366, height: 900 })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto(reviewPath)
  await expect(page.getByRole('heading', { name: '登录慧批云端' })).toBeVisible()
  await signIn(page)
  await page
    .getByRole('link', { name: /查看作业/ })
    .first()
    .click()
  await expect(page.getByRole('heading', { name: '评分细则概要' })).toBeVisible()
  await page.getByRole('tab', { name: '学生提交' }).click()
  await page.locator(`a[href="/assignments/${aid}/submissions/${sid}"]`).click()
  const fileRequest = page.waitForRequest((req) => req.url().endsWith(`/submissions/${sid}/file`))
  await page.getByRole('button', { name: '查看原始文件' }).click()
  const fetched = await fileRequest
  expect(fetched.headers().authorization).toMatch(/^Bearer /)
  expect(fetched.url()).not.toContain('token')
  await expect(page.getByTitle('受保护的原始 PDF')).toHaveAttribute('src', /^blob:/)
  await page.getByRole('button', { name: /Close this dialog|关闭此对话框/ }).click()
  await page.getByRole('link', { name: '核对作答 →' }).click()
  await expect(page.getByTestId('canonical-paper')).toContainText('😀𠀀é')
  await page.getByRole('button', { name: '预览并保存复核' }).click()
  await expect(page.getByRole('status')).toContainText('请选择真实证据')
  await page
    .locator('[data-source-pointer]')
    .first()
    .evaluate((element) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
      let node = walker.nextNode() as Text | null
      while (node && !node.data.includes('学生作答')) node = walker.nextNode() as Text | null
      if (!node) throw new Error('Expected original source text')
      node.splitText(node.data.indexOf('学生作答') + 4)
    })
  await select(page)
  let original = '',
    retried = '',
    postCount = 0
  await page.route(
    `**/api/v1/submissions/${sid}/answer-reviews/questions/${qid}`,
    async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      postCount++
      if (postCount === 1) {
        original = route.request().postData()!
        const committed = await route.fetch()
        expect(committed.status()).toBe(201)
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: '{"detail":"synthetic_lost_ack"}',
        })
      } else {
        retried = route.request().postData()!
        await route.continue()
      }
    },
  )
  await save(page)
  await expect(page.getByRole('button', { name: '重试原请求' })).toBeVisible()
  await page.getByRole('button', { name: '重试原请求' }).click()
  await expect(page.getByRole('status')).toContainText('revision 1')
  expect(retried).toBe(original)
  expect(Object.keys(JSON.parse(original))).not.toContain('reviewer_ref')
  await expect(page.locator('.history-row')).toHaveCount(1)
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('access_token')
  expect(await page.evaluate(() => JSON.stringify(sessionStorage))).not.toContain('access_token')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/workbench-review.png', fullPage: true })
  // A second independently signed teacher produces the competing revision first.
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/b`, { ignoreHTTPSErrors: true })
  const secondContext = await browser.newContext({ ignoreHTTPSErrors: true })
  const second = await secondContext.newPage()
  await signIn(second, reviewPath)
  await expect(second.getByTestId('canonical-paper')).toBeVisible()
  await second.getByRole('radio', { name: '标记不确定' }).check()
  await second.getByLabel('判断原因').selectOption('source_quality_insufficient')
  await save(second)
  await expect(second.getByRole('status')).toContainText('revision 2')
  await select(page)
  await save(page)
  await expect(page.getByRole('button', { name: '刷新来源并重新核对' })).toBeVisible()
  await page.getByRole('button', { name: '刷新来源并重新核对' }).click()
  await page.getByRole('radio', { name: '确认不存在作答' }).check()
  await select(page, true)
  await save(page)
  await expect(page.getByRole('status')).toContainText('revision 3')
  await expect(page.locator('.history-row')).toHaveCount(3)
  await expect(page.locator('.history-panel')).toContainText('仅有印刷题干')
  await expect(
    page.locator('.history-panel').getByRole('button', { name: /编辑|删除/ }),
  ).toHaveCount(0)
  // Reader sees real business data but no writable controls.
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/reader`, {
    ignoreHTTPSErrors: true,
  })
  const readerContext = await browser.newContext({ ignoreHTTPSErrors: true })
  const reader = await readerContext.newPage()
  await signIn(reader, reviewPath)
  await expect(reader.getByRole('button', { name: '预览并保存复核' })).toBeDisabled()
  // Resource mismatch is rejected by the actual server, even though the caller is authenticated.
  await reader.evaluate((path) => {
    history.pushState({}, '', path)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, `/assignments/00000000-0000-4000-8000-000000000001/submissions/${sid}`)
  await expect(reader.getByRole('alert')).toContainText('资源不可用')
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/revoke/b`, { ignoreHTTPSErrors: true })
  await second.getByRole('button', { name: '查看原始文件' }).click()
  await expect(second.getByRole('alert').filter({ hasText: '资源不可用' })).toBeVisible()
  await expect(second.getByRole('button', { name: '预览并保存复核' })).toBeDisabled()
  // A 401 after successful login invalidates memory state; no anonymous retry.
  await page.route('**/api/v1/assignments?*', (route) => route.fulfill({ status: 401, body: '{}' }))
  await page.getByRole('link', { name: /我的作业/ }).click()
  await expect(page.getByRole('heading', { name: '登录慧批云端' })).toBeVisible()
  expect(errors).toEqual([])
  await secondContext.close()
  await readerContext.close()
})

test('真实PNG预览与新建作业表单', async ({ page, request }) => {
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  await signIn(page)
  await page.locator(`a[href="/assignments/${aid}"]`).click()
  await page.getByRole('tab', { name: '学生提交' }).click()
  await page
    .locator(`a[href="/assignments/${aid}/submissions/${process.env.WORKBENCH_IMAGE_SID}"]`)
    .click()
  await page.getByRole('button', { name: '查看原始文件' }).click()
  await expect(page.getByRole('img', { name: 'synthetic-image.png' })).toHaveAttribute(
    'src',
    /^blob:/,
  )
  await page.getByRole('button', { name: /Close this dialog|关闭此对话框/ }).click()
  await page.getByRole('link', { name: /我的作业/ }).click()
  await page.getByRole('button', { name: '＋ 新建作业' }).click()
  await page.getByLabel('作业名称').fill('合成新建作业验收')
  await page.getByLabel('学科', { exact: true }).fill('数学')
  await page.getByLabel('年级', { exact: true }).fill('高一')
  await page.getByRole('button', { name: '创建草稿', exact: true }).click()
  await expect(page.getByRole('heading', { name: '合成新建作业验收' })).toBeVisible()
  await expect(page.locator('.page-heading')).toContainText('草稿')
})

test('真实考试扫描：冻结混班名单、PDF接收、逐页预览和原件校验', async ({ page, request }) => {
  const { readFile } = await import('node:fs/promises')
  const { createHash } = await import('node:crypto')
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.setViewportSize({ width: 1440, height: 900 })
  await signIn(page, '/scans')
  await expect(page.getByLabel('考试场次')).toHaveValue(process.env.WORKBENCH_SCAN_SESSION!)
  await expect(page.getByText('已具备冻结名单；页面身份仍须后续人工确认。')).toBeVisible()
  await page.getByLabel('批次名称').fill('浏览器合成混班扫描')
  await page.getByRole('button', { name: '新建扫描批次' }).click()
  await expect(page.getByRole('heading', { name: '浏览器合成混班扫描' })).toBeVisible()
  await page.getByLabel('扫描 PDF', { exact: true }).setInputFiles(process.env.WORKBENCH_SCAN_PDF!)
  const received = page.waitForResponse(
    (r) =>
      /\/scan-batches\/[^/]+\/files$/.test(new URL(r.url()).pathname) &&
      r.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '上传并生成预览' }).click()
  const result = await received
  expect(result.status()).toBe(201)
  const metadata = await result.json()
  expect(metadata.page_count).toBe(4)
  expect(metadata.status).toBe('stored')
  expect(metadata).not.toHaveProperty('object_key')
  expect(metadata).not.toHaveProperty('bucket')
  await expect(page.getByRole('button', { name: '第 4 页', exact: true })).toBeVisible()
  const preview = page.waitForRequest((r) => r.url().endsWith('/preview'))
  await page.getByRole('button', { name: '第 2 页', exact: true }).click()
  expect((await preview).headers().authorization).toMatch(/^Bearer /)
  await expect(page.getByAltText('第2页扫描预览')).toHaveAttribute('src', /^blob:/)
  await expect(page.getByAltText('第2页扫描预览')).toBeVisible()
  expect(
    await page
      .getByAltText('第2页扫描预览')
      .evaluate((image) => (image as HTMLImageElement).naturalWidth),
  ).toBeGreaterThan(0)
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载原始 PDF' }).click()
  const file = await downloaded
  const actual = await readFile((await file.path())!)
  const expected = await readFile(process.env.WORKBENCH_SCAN_PDF!)
  expect(createHash('sha256').update(actual).digest('hex')).toBe(metadata.sha256)
  expect(actual.equals(expected)).toBe(true)
  await expect(page.getByText(/第 2 页 · 待身份核验/)).toBeVisible()
  await expect(page.getByRole('link', { name: '放大查看' })).toHaveAttribute('href', /^blob:/)
  expect(errors).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/scan-intake.png', fullPage: true })
})
