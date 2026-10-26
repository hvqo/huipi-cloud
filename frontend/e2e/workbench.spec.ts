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
  test.setTimeout(process.env.RUN_EXAM_GRADING_BROWSER === '1' ? 180000 : 120000)
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

  await page.getByRole('link', { name: '人工分卷与身份确认 →' }).click()
  await expect(page.getByRole('heading', { name: '人工分卷与身份确认' })).toBeVisible()
  await page.getByRole('button', { name: '加入扫描页 1', exact: true }).click()
  await page.getByRole('button', { name: '加入扫描页 3', exact: true }).click()
  await page.getByRole('button', { name: '上移第2项' }).click()
  await page.getByLabel('已核对页数，答卷完整').check()
  await page.getByLabel('操作说明').fill('合成原卷，页序3、1，核对第一位同名学生')
  await page.getByRole('button', { name: '保存答卷草稿', exact: true }).click()
  await expect(page.getByText('草稿已保存，页面尚未确认学生身份。')).toBeVisible()
  await page.getByLabel('操作说明').fill('合成确认第一位同名学生')
  await page.getByRole('button', { name: '核对学生并确认', exact: true }).click()
  const identity = page.getByRole('dialog', { name: '核对学生身份与实际卷别' })
  await expect(identity).toBeVisible()
  await expect(identity.getByLabel('参试学生').locator('option')).toHaveCount(5)
  const options = await identity
    .getByLabel('参试学生')
    .locator('option')
    .evaluateAll((opts) =>
      opts.map((o) => ({ value: (o as HTMLOptionElement).value, label: o.textContent || '' })),
    )
  const twins = options.filter((o) => o.label.includes('合成同名'))
  expect(twins.length).toBe(2)
  expect(twins[0]!.value).not.toBe(twins[1]!.value)
  const firstTwin = twins.find((o) => o.label.includes('考号 N001'))!
  const secondTwin = twins.find((o) => o.label.includes('考号 N002'))!
  expect(firstTwin.label).toContain('合成1班')
  expect(secondTwin.label).toContain('合成2班')
  await identity.getByLabel('参试学生').selectOption(firstTwin.value)
  await expect(identity.getByLabel('实际卷别')).not.toHaveValue('')
  await page.screenshot({
    path: 'test-results/answer-script-identity-check.png',
    fullPage: true,
    animations: 'disabled',
  })
  await identity.getByRole('button', { name: '确认身份并保存新版本' }).click()
  await expect(page.getByText('身份确认已保存。尚未解析或评分。')).toBeVisible()
  await expect(identity).not.toBeVisible()
  await page.screenshot({
    path: 'test-results/answer-script-confirmed.png',
    fullPage: true,
    animations: 'disabled',
  })
  // Real API intent -> dedicated worker -> private PG/S3 Canonical result. No timed fake success.
  await page.getByRole('button', { name: '开始解析', exact: true }).click()
  await expect(page.locator('.parse-state')).toHaveText('解析完成', { timeout: 90000 })
  await page.getByRole('button', { name: '查看解析内容', exact: true }).click()
  await expect(page.getByRole('region', { name: '解析页面内容' })).toBeVisible()
  await expect(page.getByText('解析第 1 页 · 原扫描第 3 页', { exact: true })).toBeVisible()
  await expect(page.locator('.parsed-content')).not.toBeEmpty()
  await page.screenshot({ path: 'test-results/exam-protected-canonical.png', fullPage: true })
  if (process.env.RUN_EXAM_GRADING_BROWSER === '1') await markExamAnswer(page, true)
  if (process.env.RUN_EXAM_RESULTS_BROWSER === '1') await markExamAnswer(page, false, 2)

  await page.getByRole('button', { name: '新建答卷', exact: true }).click()
  await page.getByRole('button', { name: '加入扫描页 2', exact: true }).click()
  // One page stays unclaimed until the later correction adds it.
  await page.getByLabel('操作说明').fill('合成第二位同名学生，单页草稿')
  await page.getByRole('button', { name: '保存答卷草稿', exact: true }).click()
  await expect(page.getByText('草稿已保存，页面尚未确认学生身份。')).toBeVisible()
  await page.getByLabel('操作说明').fill('核对另一班同名学生')
  await page.getByRole('button', { name: '核对学生并确认', exact: true }).click()
  await identity.getByLabel('参试学生').selectOption(secondTwin.value)
  await identity.getByRole('button', { name: '确认身份并保存新版本' }).click()
  await expect(page.getByText('身份确认已保存。尚未解析或评分。')).toBeVisible()
  await expect(page.getByRole('button', { name: '加入扫描页 4', exact: true })).toBeVisible()
  await expect(identity).not.toBeVisible()
  await expect(page.locator('.script-identity')).toContainText('考号 N002')
  await expect(page.locator('.script-status')).toContainText('答卷不完整')
  await page.screenshot({
    path: 'test-results/answer-scripts-two-students.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('button', { name: '加入扫描页 4', exact: true }).click()
  await page.getByLabel('已核对页数，答卷完整').check()
  await page.getByLabel('操作说明').fill('漏扫页补充，重新确认前保留新草稿')
  await page.getByRole('button', { name: '保存为新版本草稿', exact: true }).click()
  await expect(page.getByText('草稿已保存，页面尚未确认学生身份。')).toBeVisible()
  await page.getByRole('button', { name: '查看历史版本', exact: true }).click()
  await expect(page.locator('.script-history')).toContainText('v3')
  await expect(page.locator('.script-history')).toContainText('v2')
  expect(errors).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/answer-script-correction.png', fullPage: true })
  if (process.env.RUN_EXAM_GRADING_BROWSER === '1') {
    await page.getByLabel('操作说明').fill('补页后重新核对第二位同名学生')
    await page.getByRole('button', { name: '核对学生并确认', exact: true }).click()
    await identity.getByLabel('参试学生').selectOption(secondTwin.value)
    await identity.getByRole('button', { name: '确认身份并保存新版本' }).click()
    await expect(identity).not.toBeVisible()
    await page.getByRole('button', { name: '开始解析', exact: true }).click()
    await expect(page.locator('.parse-state')).toHaveText('解析完成', { timeout: 90000 })
    await markExamAnswer(page, false)
    await expect(page.locator('.script-identity')).toContainText('N002')
    await page.screenshot({ path: 'test-results/exam-grading-second-student.png', fullPage: true })
  }

  for (const [width, height] of [
    [1366, 768],
    [1920, 1080],
  ]) {
    await page.setViewportSize({ width: width!, height: height! })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({
      path: `test-results/answer-scripts-${width}.png`,
      fullPage: true,
      animations: 'disabled',
    })
  }
  if (process.env.RUN_EXAM_RESULTS_BROWSER === '1') {
    await page.getByRole('link', { name: '成绩预览', exact: true }).click()
    await expect(page.getByRole('heading', { name: '整卷成绩预览' })).toBeVisible()
    const first = page.locator('.result-table tbody tr').filter({ hasText: 'N001' })
    const second = page.locator('.result-table tbody tr').filter({ hasText: 'N002' })
    await expect(first).toContainText('全部复核完成')
    await expect(first).toContainText('18.00')
    await expect(second).toContainText('未完成')
    await expect(second).toContainText('10.00')
    await page.screenshot({
      path: 'test-results/exam-results-preview.png',
      fullPage: true,
      animations: 'disabled',
    })
    await first.getByRole('button', { name: '查看各题与复核' }).click()
    const dialog = page.getByRole('dialog', { name: '逐题教师复核' })
    await expect(dialog).toContainText('8.00')
    await expect(dialog).toContainText('10.00')
    await expect(dialog).toContainText('修订2')
    await expect(dialog.getByRole('table')).toBeVisible()
    await dialog.screenshot({
      path: 'test-results/exam-results-question-review.png',
      animations: 'disabled',
    })
    await dialog.getByRole('button', { name: /Close this dialog|关闭此对话框/ }).click()
    const filter = page.locator('.result-filters select').nth(2)
    const oneClass = await filter
      .locator('option')
      .filter({ hasText: '合成1班' })
      .getAttribute('value')
    await filter.selectOption(oneClass!)
    await expect(page.locator('.result-table tbody tr')).toHaveCount(2)
    await expect(page.locator('.result-table')).not.toContainText('N002')
    await filter.selectOption('')
    await expect(page.locator('.result-table tbody tr')).toHaveCount(4)
    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出 Excel 预览' }).click()
    await (await download).saveAs('test-results/exam-results-preview.xlsx')
    expect(errors).toEqual([])
  }
})

async function markExamAnswer(page: Page, revise: boolean, question = 1) {
  const panel = page.getByRole('region', { name: 'AI 批改与复核', exact: true })
  await expect(panel).toBeVisible()
  if (question === 1)
    await panel.getByRole('button', { name: '准备题目作答核对', exact: true }).click()
  else {
    const selector = panel.locator('select').first()
    const value = await selector
      .locator('option')
      .filter({ hasText: '第2题' })
      .getAttribute('value')
    expect(value).toBeTruthy()
    await selector.selectOption(value!)
  }
  await expect(panel.getByRole('heading', { name: `第${question}题`, exact: true })).toBeVisible()
  const paragraph = panel
    .locator('.mark-node')
    .filter({ hasText: /Student response/i })
    .nth(question - 1)
  await expect(paragraph).toBeVisible()
  await paragraph.getByRole('button', { name: '选择此段', exact: true }).click()
  await panel.getByRole('checkbox', { name: '我已核对原页，人工指定所选内容属于本题' }).check()
  await panel.getByRole('button', { name: '确认学生作答', exact: true }).click()
  await expect(panel.getByText(/当前核对：已确认作答/)).toBeVisible()
  await panel.getByRole('button', { name: '请求 AI 评分', exact: true }).click()
  await expect(panel.getByRole('status').filter({ hasText: '建议已生成' })).toBeVisible({
    timeout: 30000,
  })
  await expect(panel).toContainText('synthetic-test-only')
  await panel.getByRole('button', { name: '接受模型建议', exact: true }).click()
  await expect(panel.getByText(/修订1 · 接受候选/)).toBeVisible()
  if (revise) {
    await panel.getByRole('button', { name: '载入完整分数快照', exact: true }).click()
    await panel
      .getByLabel(/^教师分 /)
      .first()
      .fill('3.00')
    await panel.getByLabel('审核说明').fill('合成验收：教师核对后修订过程分，保留完整评分快照')
    await panel.getByRole('button', { name: '提交完整修订', exact: true }).click()
    await expect(panel.getByText(/修订2 · 教师修订/)).toBeVisible()
    await expect(panel.getByText(/修订1 · 接受候选/)).toBeVisible()
    await panel.screenshot({ path: 'test-results/exam-grading-teacher-revision.png' })
  }
}

test('网页考试配置：创建场次、共享A/B卷、混班名单与版本化更正', async ({ page, request }) => {
  test.skip(process.env.RUN_EXAM_CONFIGURATION_BROWSER !== '1', 'configuration opt-in')
  test.setTimeout(120000)
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('dialog', (dialog) => dialog.accept())
  await signIn(page, '/exams')
  let lostBody = ''
  let recovered = false
  await page.route('**/api/v1/exam-configuration/events', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    if (!lostBody) {
      lostBody = route.request().postData()!
      const committed = await route.fetch()
      expect(committed.status()).toBe(201)
      await route.abort('failed')
    } else {
      expect(route.request().postData()).toBe(lostBody)
      recovered = true
      await route.continue()
    }
  })
  await page.getByRole('button', { name: '新建考试', exact: true }).click()
  const eventDialog = page.getByRole('dialog', { name: '新建考试', exact: true })
  await eventDialog.getByLabel('考试名称').fill('浏览器合成期中考试')
  await eventDialog
    .getByLabel('学年', { exact: true })
    .selectOption(process.env.WORKBENCH_CONFIG_YEAR!)
  await eventDialog
    .getByLabel('学期', { exact: true })
    .selectOption(process.env.WORKBENCH_CONFIG_TERM!)
  await eventDialog.getByRole('button', { name: '创建考试', exact: true }).click()
  await expect(eventDialog.getByText(/保存结果尚未核实/)).toBeVisible()
  await eventDialog.getByRole('button', { name: '重试原请求' }).click()
  await expect(eventDialog).not.toBeVisible()
  expect(recovered).toBe(true)
  await page.getByRole('button', { name: '新建科目场次', exact: true }).click()
  const sessionDialog = page.getByRole('dialog', { name: '新建科目场次', exact: true })
  await sessionDialog.getByLabel('场次名称').fill('网页数学场次')
  await sessionDialog
    .getByLabel('学科', { exact: true })
    .selectOption(process.env.WORKBENCH_CONFIG_SUBJECT!)
  await sessionDialog.getByLabel('开始时间（北京时间）').fill('2026-11-01T09:00')
  await sessionDialog.getByLabel('结束时间（北京时间）').fill('2026-11-01T11:00')
  await sessionDialog.getByLabel('用卷方式').selectOption('ab')
  await sessionDialog.getByRole('button', { name: '创建场次', exact: true }).click()
  await expect(sessionDialog).not.toBeVisible()
  await expect(page).toHaveURL(/\/exams\/sessions\//)
  const sid = new URL(page.url()).pathname.split('/').pop()!
  const paperPanel = page.getByRole('region', { name: '场次用卷' })
  await expect(paperPanel).toBeVisible()
  const selector = paperPanel.locator('select')
  await selector.nth(0).selectOption('A')
  await selector.nth(1).selectOption(process.env.WORKBENCH_CONFIG_PAPER_A!)
  await paperPanel.getByRole('button', { name: '绑定试卷', exact: true }).click()
  await expect(paperPanel.locator('tbody tr')).toHaveCount(1)
  await selector.nth(0).selectOption('B')
  await selector.nth(1).selectOption(process.env.WORKBENCH_CONFIG_PAPER_B!)
  await paperPanel.getByRole('button', { name: '绑定试卷', exact: true }).click()
  await expect(paperPanel.locator('tbody tr')).toHaveCount(2)
  await paperPanel.getByRole('button', { name: '标记用卷准备就绪' }).click()
  await expect(paperPanel.getByText(/用卷已就绪。仍需单独/)).toBeVisible()
  const rosterPanel = page.getByRole('region', { name: '参试名单配置' })
  await rosterPanel
    .locator(`input[type=checkbox][value="${process.env.WORKBENCH_CONFIG_CLASS_A}"]`)
    .check()
  await rosterPanel
    .locator(`input[type=checkbox][value="${process.env.WORKBENCH_CONFIG_CLASS_B}"]`)
    .check()
  await rosterPanel.getByRole('button', { name: '按考试日期预览学生' }).click()
  await expect(rosterPanel.locator('tbody tr')).toHaveCount(4)
  await expect(rosterPanel.locator('tbody tr').filter({ hasText: '合成同名' })).toHaveCount(2)
  await rosterPanel.getByRole('button', { name: '按当前顺序编排考号' }).click()
  const variantSelector = rosterPanel
    .locator('select')
    .filter({ has: page.locator('option', { hasText: '待核验' }) })
  const aOption = await variantSelector
    .first()
    .locator('option')
    .filter({ hasText: 'A卷' })
    .getAttribute('value')
  await variantSelector.first().selectOption(aOption!)
  const bOption = await variantSelector
    .first()
    .locator('option')
    .filter({ hasText: 'B卷' })
    .getAttribute('value')
  await rosterPanel.locator('tbody select').nth(1).selectOption(bOption!)
  await rosterPanel.getByLabel('名单操作说明').fill('浏览器核对两班参试名单')
  await rosterPanel.getByRole('button', { name: '保存名单草稿' }).click()
  await expect(rosterPanel.getByRole('button', { name: '冻结当前名单' })).toBeEnabled()
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({
    path: 'test-results/exam-configuration-roster-draft.png',
    fullPage: true,
    animations: 'disabled',
  })
  await rosterPanel.getByRole('button', { name: '冻结当前名单' }).click()
  await expect(rosterPanel.getByText(/本版已冻结，只读保留/)).toBeVisible()
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({
    path: 'test-results/exam-configuration-frozen.png',
    fullPage: true,
    animations: 'disabled',
  })
  await rosterPanel.getByLabel('名单操作说明').fill('浏览器创建更正版本')
  await rosterPanel.getByRole('button', { name: '创建更正草稿' }).click()
  await expect(rosterPanel.getByText(/当前是更正草稿/)).toBeVisible()
  const firstNumber = rosterPanel.locator('input[maxlength="32"]').first()
  await firstNumber.fill('R0001')
  await expect(rosterPanel.getByRole('button', { name: '冻结当前名单' })).toBeDisabled()
  await rosterPanel.getByRole('button', { name: '保存名单草稿' }).click()
  await rosterPanel.getByRole('button', { name: '冻结当前名单' }).click()
  await expect(rosterPanel.getByText(/本版已冻结，只读保留/)).toBeVisible()
  await expect(firstNumber).toHaveValue('R0001')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({
    path: 'test-results/exam-configuration-corrected.png',
    fullPage: true,
    animations: 'disabled',
  })
  await page.getByRole('link', { name: '扫描接收与页面预览 →' }).click()
  await expect(page.getByLabel('考试场次')).toHaveValue(sid)
  await expect(page.getByText('已具备冻结名单；页面身份仍须后续人工确认。')).toBeVisible()
  await page.getByRole('link', { name: '人工分卷与身份确认 →' }).click()
  await expect(page).toHaveURL(new RegExp(`/scans/sessions/${sid}/scripts$`))
  await expect(page.getByRole('heading', { name: '人工分卷与身份确认' })).toBeVisible()
  await page.getByRole('link', { name: '成绩预览', exact: true }).click()
  await expect(page.getByRole('heading', { name: '整卷成绩预览' })).toBeVisible()
  await expect(page.getByText('暂无已授权班级的考试场次，请联系考务负责人。')).toBeVisible()
  expect(errors).toEqual([])
})
