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

async function captureLayout(page: Page, name: string, anchor?: string) {
  const directory = process.env.LAYOUT_CAPTURE_DIR
  if (!directory) return
  const { mkdir } = await import('node:fs/promises')
  await mkdir(directory, { recursive: true })
  const previous = page.viewportSize(),
    scroll = await page.evaluate(() => window.scrollY)
  for (const [width, height] of [
    [1366, 768],
    [1440, 900],
    [1920, 1080],
  ]) {
    await page.setViewportSize({ width: width!, height: height! })
    await page.evaluate((selector) => {
      const element = selector ? document.querySelector(selector) : null
      window.scrollTo(0, element ? window.scrollY + element.getBoundingClientRect().top - 76 : 0)
    }, anchor || '')
    await page.screenshot({ path: `${directory}/${name}-${width}.png`, animations: 'disabled' })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(
      true,
    )
  }
  if (previous) await page.setViewportSize(previous)
  await page.evaluate((value) => window.scrollTo(0, value), scroll)
}

test('产品工作台：真实数据、导航折叠与作业视图适配', async ({ page, request }) => {
  test.skip(process.env.RUN_UI_PRODUCT_BROWSER !== '1', 'product workspace opt-in')
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  await signIn(page, '/dashboard')
  await expect(page.getByRole('heading', { name: '最近作业' })).toBeVisible()
  await expect(page.getByText('正在检查异常…')).not.toBeVisible()
  await expect(page.getByText('正在读取场次…')).not.toBeVisible()
  await expect(page.locator(`a[href="/assignments/${aid}/homework"]`).first()).toBeVisible()
  await captureLayout(page, 'dashboard')
  for (const [width, height] of [
    [1366, 768],
    [1440, 900],
    [1920, 1080],
  ]) {
    await page.setViewportSize({ width: width!, height: height! })
    await expect(page.getByRole('heading', { name: '我的阅卷任务' })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(
      true,
    )
    await page.screenshot({ path: `test-results/dashboard-${width}.png`, fullPage: true })
  }
  await page.getByRole('button', { name: '收起导航' }).click()
  await expect(page.getByRole('button', { name: '展开导航' })).toBeVisible()
  await page.getByRole('button', { name: '展开导航' }).click()
  await page
    .getByRole('navigation', { name: '功能导航' })
    .getByRole('link', { name: '我的作业', exact: true })
    .click()
  await page.getByRole('button', { name: '列表', exact: true }).click()
  await expect(page.locator('.assignment-list')).toBeVisible()
  await page.getByLabel('本页搜索作业').fill('不存在的作业名称')
  await expect(page.getByText('本页没有匹配的作业')).toBeVisible()
  await page.getByLabel('本页搜索作业').clear()
  await page.getByRole('button', { name: '卡片', exact: true }).click()
  await page.screenshot({ path: 'test-results/assignments-redesign.png', fullPage: true })
})

test('答题卡：打印、扫描、人工分卷、真实识别、教师确认与成绩预览', async ({
  page,
  request,
}, testInfo) => {
  test.skip(process.env.RUN_OMR_BROWSER !== '1', 'real OpenCV OMR opt-in')
  test.setTimeout(120000)
  page.setDefaultTimeout(10000)
  const { execFileSync } = await import('node:child_process')
  const sid = process.env.WORKBENCH_SCAN_SESSION!
  const paper = process.env.WORKBENCH_OMR_PAPER!
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  await signIn(page, '/omr')
  await expect(page.getByLabel('锁定试卷').locator(`option[value="${paper}"]`)).toHaveCount(1)
  await page.getByLabel('锁定试卷').selectOption(paper)
  await expect(page.getByText('第 1 题 · 10.00 分')).toBeVisible()
  await page.getByRole('button', { name: '创建答题卡' }).click()
  await expect(page.getByRole('button', { name: '下载 A4 打印版' })).toBeVisible()
  await page.screenshot({ path: 'test-results/omr-template.png', fullPage: true })
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 A4 打印版' }).click()
  const printPath = testInfo.outputPath('print.pdf'),
    scanPath = testInfo.outputPath('synthetic-filled.pdf')
  await (await downloaded).saveAs(printPath)
  const draw = `import sys\nfrom io import BytesIO\nimport pypdfium2 as pdfium\nfrom PIL import ImageDraw\nfrom huipi_cloud.modules.omr.engine import bubble_center\ndoc=pdfium.PdfDocument(sys.argv[1])\nim=doc[0].render(scale=150/72).to_pil().convert('RGB')\nd=ImageDraw.Draw(im)\nfor i,o in [(0,0),(1,1)]:\n x,y=bubble_center(i,o); d.ellipse((x-16,y-16,x+16,y+16),fill='black')\nim.save(sys.argv[2],format='PDF',resolution=150)\ndoc.close()\n`
  execFileSync(process.env.WORKBENCH_PYTHON!, ['-c', draw, printPath, scanPath])

  await page.getByRole('link', { name: '进入考试扫描 →' }).click()
  await page.getByLabel('考试场次').selectOption(sid)
  await page.getByLabel('批次名称').fill('OMR 合成扫描')
  await page.getByRole('button', { name: '新建扫描批次' }).click()
  await page.getByLabel('扫描 PDF', { exact: true }).setInputFiles(scanPath)
  await page.getByRole('button', { name: '上传并生成预览' }).click()
  await expect(page.getByRole('button', { name: '第 1 页', exact: true })).toBeVisible()
  await page.getByRole('link', { name: '人工分卷与身份确认 →' }).click()
  const source = page.locator('.script-page').filter({ hasText: 'synthetic-filled.pdf' })
  await expect(source).toBeVisible()
  await source.getByRole('button', { name: '加入扫描页 1' }).click()
  await page.getByLabel('已核对页数，答卷完整').check()
  await page.getByLabel('操作说明').fill('合成答题卡，人工核对页面')
  await page.getByRole('button', { name: '保存答卷草稿', exact: true }).click()
  await page.getByLabel('操作说明').fill('核对第三位学生身份')
  await page.getByRole('button', { name: '核对学生并确认', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '核对学生身份与实际卷别' })
  const student = dialog.getByLabel('参试学生').locator('option').filter({ hasText: '考号 N003' })
  await dialog.getByLabel('参试学生').selectOption((await student.getAttribute('value'))!)
  await dialog.getByRole('button', { name: '确认身份并保存新版本' }).click()
  await expect(dialog).not.toBeVisible()
  await page.getByRole('link', { name: '答题卡批改' }).click()
  await page.getByLabel('考试场次').selectOption(sid)
  await page.locator('.omr-scripts button').filter({ hasText: 'N003' }).click()
  await page.getByLabel('答题卡页面').selectOption({ label: 'synthetic-filled.pdf · 第 1 页' })
  await page.getByRole('button', { name: '识别该页' }).click()
  await expect(page.getByText('识别完成，请对照原图确认；识别建议还不是有效分数。')).toBeVisible()
  await expect(page.getByAltText('扫描原图与填涂区域')).toBeVisible()
  await expect(page.locator('.omr-table tbody tr')).toHaveCount(2)
  await page.screenshot({ path: 'test-results/omr-recognition.png', fullPage: true })
  await page.getByLabel('已核对该答卷').check()
  await page.getByRole('button', { name: '批量确认已核对的清晰题' }).click()
  await expect(page.getByRole('status')).toContainText('已确认 2 道清晰题')
  await page.screenshot({ path: 'test-results/omr-confirmed.png', fullPage: true })
  await page.getByRole('link', { name: '查看成绩预览 →' }).click()
  await expect(page.locator('.result-table tbody tr').filter({ hasText: 'N003' })).toContainText(
    '20.00',
  )
  await page.screenshot({ path: 'test-results/omr-results.png', fullPage: true })
})
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
  test.setTimeout(
    process.env.RUN_EXAM_GRADING_BROWSER === '1' || process.env.RUN_EXAM_VISUAL_BROWSER === '1'
      ? 240000
      : 120000,
  )
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
  // Consume the no-store response immediately at the response event, before later UI work.
  const received = page
    .waitForResponse(
      (r) =>
        /\/scan-batches\/[^/]+\/files$/.test(new URL(r.url()).pathname) &&
        r.request().method() === 'POST',
    )
    .then(async (response) => ({ status: response.status(), metadata: await response.json() }))
  await page.getByRole('button', { name: '上传并生成预览' }).click()
  const { status, metadata } = await received
  expect(status).toBe(201)
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
  await page.getByRole('button', { name: '放大扫描页', exact: true }).click()
  await expect(page.getByAltText('第2页扫描预览')).toHaveAttribute('style', /120%/)
  await page.getByRole('button', { name: '缩小扫描页', exact: true }).click()
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
  await captureLayout(page, 'scan-intake', '.scan-workspace')
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
  if (process.env.LAYOUT_CAPTURE_DIR) {
    await page.getByRole('button', { name: '预览扫描页 2', exact: true }).click()
    await expect(page.getByRole('button', { name: '预览扫描页 2', exact: true })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await expect(page.getByAltText('人工分卷页面预览')).toBeVisible()
    await captureLayout(page, 'answer-scripts', '.script-columns')
    await page.getByRole('button', { name: '放大原卷', exact: true }).click()
    await expect(page.getByAltText('人工分卷页面预览')).toHaveCSS('width', /\d+px/)
    await expect(page.getByAltText('人工分卷页面预览')).toHaveAttribute('style', /120%/)
    const oldDialog = page.getByRole('dialog', { name: '核对原始扫描页' })
    if (await oldDialog.isVisible()) await page.keyboard.press('Escape')
  }
  // Real API intent -> dedicated worker -> private PG/S3 Canonical result. No timed fake success.
  await page.getByRole('button', { name: '开始解析', exact: true }).click()
  await expect(page.locator('.parse-state')).toHaveText('解析完成', { timeout: 90000 })
  await page.getByRole('button', { name: '查看解析内容', exact: true }).click()
  await expect(page.getByRole('region', { name: '解析页面内容' })).toBeVisible()
  await expect(page.getByText('解析第 1 页 · 原扫描第 3 页', { exact: true })).toBeVisible()
  await expect(page.locator('.parsed-content')).not.toBeEmpty()
  await page.screenshot({ path: 'test-results/exam-protected-canonical.png', fullPage: true })
  if (process.env.RUN_EXAM_VISUAL_BROWSER === '1') await markVisualAnswer(page)
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
    if (process.env.RUN_EXAM_FINAL_BROWSER === '1') await markExamAnswer(page, false, 2)
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
  if (process.env.RUN_EXAM_RESULTS_BROWSER === '1' && process.env.RUN_EXAM_FINAL_BROWSER !== '1') {
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
  if (process.env.RUN_EXAM_VISUAL_BROWSER === '1') {
    await page.getByRole('link', { name: '成绩预览', exact: true }).click()
    await expect(page.getByRole('heading', { name: '整卷成绩预览' })).toBeVisible()
    const first = page.locator('.result-table tbody tr').filter({ hasText: 'N001' })
    await expect(first).toContainText('1 / 2')
    await expect(first).toContainText('未完成')
    await page.screenshot({ path: 'test-results/exam-visual-results.png', fullPage: true })
  }
  if (process.env.RUN_EXAM_FINAL_BROWSER === '1') await manageFinalResults(page)
})

async function manageFinalResults(page: Page) {
  await page.getByRole('link', { name: '成绩管理', exact: true }).click()
  await expect(page.getByRole('heading', { name: '成绩管理', exact: true })).toBeVisible()
  await page.getByLabel('考试场次').selectOption(process.env.WORKBENCH_SCAN_SESSION!)
  const attendance = page
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: '到考核对与批改进度' }) })
  for (const [number, status, label] of [
    ['N001', 'present', '到考'],
    ['N002', 'present', '到考'],
    ['N003', 'absent', '缺考'],
    ['N004', 'absent', '缺考'],
  ]) {
    const row = attendance.locator('tr').filter({ hasText: number })
    await expect(row).toBeVisible()
    await row.locator('select').selectOption(status!)
    const submitted = page.waitForRequest(
      (request) =>
        request.method() === 'POST' && request.url().endsWith('/final-results/attendance'),
    )
    await row.getByRole('button', { name: '确认' }).click()
    expect((await submitted).postDataJSON().status).toBe(status)
    await expect(row).toContainText(`已保存：${label}`)
  }
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: 'test-results/final-attendance.png', fullPage: true })
  await page.getByRole('button', { name: '生成待审批版本' }).click()
  const first = page.locator('tr').filter({ hasText: '第 1 版' })
  await expect(first).toContainText('待审批')
  await first.getByRole('button', { name: '查看待审批快照' }).click()
  await expect(page.getByRole('heading', { name: '待审批快照 · 第 1 版' })).toBeVisible()
  await first.getByRole('button', { name: '批准', exact: true }).click()
  await expect(first).toContainText('已批准待发布')
  await first.getByRole('button', { name: '发布', exact: true }).click()
  await expect(first).toContainText('已发布')
  await first.getByRole('button', { name: '查看正式成绩' }).click()
  const published = page
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: '已发布 · 第 1 版' }) })
  await expect(published).toContainText('18.00')
  await expect(published).toContainText('缺考')
  await expect(published).toContainText('平均')
  await page.evaluate(() => window.scrollTo(0, 0))
  await captureLayout(page, 'final-results', 'section:has(.page-heading)')
  await page.screenshot({ path: 'test-results/final-published-v1.png', fullPage: true })
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出正式成绩 Excel' }).click()
  await (await download).saveAs('test-results/final-published-v1.xlsx')
  const student = published.locator('tr').filter({ hasText: 'N001' })
  await student.getByRole('button', { name: '查看详情' }).click()
  const studentDetails = page.getByRole('dialog', { name: '学生成绩详情' })
  await expect(studentDetails).toContainText('18.00')
  await expect(studentDetails).toContainText('第1题')
  await captureLayout(page, 'student-detail')
  await page.keyboard.press('Escape')
  await expect(studentDetails).not.toBeVisible()
  // The formal snapshot is unchanged while the teacher corrects the source review.
  await student.getByRole('link', { name: '进入批改' }).click()
  const panel = page.getByRole('region', { name: 'AI 批改与复核', exact: true })
  await expect(panel).toBeVisible()
  await expect(panel.getByRole('heading', { name: '第1题', exact: true })).toBeVisible()
  await panel.getByRole('button', { name: '载入完整分数快照', exact: true }).click()
  await panel
    .getByLabel(/^教师分 /)
    .first()
    .fill('2.00')
  await panel.getByLabel('审核说明').fill('合成成绩更正：再次核对步骤')
  await panel.getByRole('button', { name: '提交完整修订', exact: true }).click()
  await expect(panel.getByText(/修订3 · 教师修订/)).toBeVisible()
  await page.getByRole('link', { name: '成绩管理', exact: true }).click()
  await page.getByLabel('考试场次').selectOption(process.env.WORKBENCH_SCAN_SESSION!)
  await page.getByRole('button', { name: '生成更正版本' }).click()
  const second = page.locator('tr').filter({ hasText: '第 2 版' })
  await expect(second).toContainText('待审批')
  await second.getByRole('button', { name: '批准', exact: true }).click()
  await expect(second).toContainText('已批准待发布')
  await second.getByRole('button', { name: '发布', exact: true }).click()
  await expect(second).toContainText('已发布')
  await second.getByRole('button', { name: '查看正式成绩' }).click()
  await expect(page.getByRole('heading', { name: '已发布 · 第 2 版' })).toBeVisible()
  await expect(
    page
      .locator('section')
      .filter({ has: page.getByRole('heading', { name: '已发布 · 第 2 版' }) }),
  ).toContainText('17.00')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: 'test-results/final-published-v2.png', fullPage: true })
  await first.getByRole('button', { name: '查看正式成绩' }).click()
  await expect(page.getByRole('heading', { name: '已发布 · 第 1 版' })).toBeVisible()
  await expect(
    page
      .locator('section')
      .filter({ has: page.getByRole('heading', { name: '已发布 · 第 1 版' }) }),
  ).toContainText('18.00')
}

async function markVisualAnswer(page: Page) {
  const panel = page.getByRole('region', { name: 'AI 批改与复核', exact: true })
  await expect(panel).toBeVisible()
  await panel.getByRole('button', { name: '准备题目作答核对', exact: true }).click()
  await expect(panel.getByRole('heading', { name: '第1题', exact: true })).toBeVisible()
  const visual = panel.getByRole('region', { name: '图像作答区域与多模态评分' })
  for (const pageNumber of [3, 1]) {
    await visual.getByRole('button', { name: `原文件第${pageNumber}页` }).click()
    const canvas = visual.locator('.visual-image')
    await expect(canvas).toBeVisible()
    await canvas.scrollIntoViewIfNeeded()
    const box = await canvas.boundingBox()
    expect(box).not.toBeNull()
    await page.mouse.move(box!.x + box!.width * 0.07, box!.y + box!.height * 0.11)
    await page.mouse.down()
    await page.mouse.move(box!.x + box!.width * 0.86, box!.y + box!.height * 0.62, { steps: 8 })
    await page.mouse.up()
  }
  await expect(visual.locator('ol li')).toHaveCount(2)
  await visual.screenshot({ path: 'test-results/exam-visual-selection.png' })
  await visual.getByRole('button', { name: '确认图像作答区域' }).click()
  await expect(visual.getByText(/已确认（版本 1）/)).toBeVisible()
  await visual.getByRole('button', { name: '请求图文 AI 评分' }).click()
  await expect(visual.getByText(/评分状态：建议已生成/)).toBeVisible({ timeout: 120000 })
  await expect(visual.getByText('仅为候选，需教师复核', { exact: false })).toBeVisible()
  await expect(visual.getByRole('button', { name: /查看原图区域/ }).first()).toBeVisible()
  await visual.screenshot({ path: 'test-results/exam-visual-candidate.png' })
  await visual.getByRole('button', { name: '接受建议', exact: true }).click()
  await expect(visual.getByText(/修订1 ·/)).toBeVisible()
  await visual.screenshot({ path: 'test-results/exam-visual-review.png' })
}

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
    if (process.env.LAYOUT_CAPTURE_DIR) {
      await panel.locator('.mark-thumbs button').first().click()
      await expect(panel.getByAltText('当前答卷扫描原页')).toBeVisible()
      await captureLayout(page, 'exam-grading', '.exam-marking')
      await panel.getByRole('button', { name: '放大批改原卷' }).click()
      await expect(panel.getByAltText('当前答卷扫描原页')).toHaveAttribute('style', /120%/)
    }
    await panel.screenshot({ path: 'test-results/exam-grading-teacher-revision.png' })
  }
}

test('网页考试配置：创建场次、共享A/B卷、混班名单与版本化更正', async ({ page, request }) => {
  test.skip(process.env.RUN_EXAM_CONFIGURATION_BROWSER !== '1', 'configuration opt-in')
  test.setTimeout(120000)
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  let discardChanges = true
  page.on('dialog', (dialog) => (discardChanges ? dialog.accept() : dialog.dismiss()))
  let paperA = process.env.WORKBENCH_CONFIG_PAPER_A!
  let paperB = process.env.WORKBENCH_CONFIG_PAPER_B!
  if (process.env.RUN_PAPER_WORKBENCH_BROWSER === '1') {
    await signIn(page, '/papers')
    await page.getByRole('button', { name: '从已发布作业创建', exact: true }).click()
    const importing = page.getByRole('dialog', { name: '创建试卷快照' })
    await importing.getByLabel('已发布来源').selectOption(process.env.WORKBENCH_PAPER_SOURCE!)
    await importing.getByLabel('试卷名称').fill('浏览器教师命题')
    await importing.getByRole('button', { name: '创建草稿快照' }).click()
    await expect(importing).not.toBeVisible()
    await expect(page).toHaveURL(/\/papers\//)
    paperA = new URL(page.url()).pathname.split('/').pop()!
    const editor = page.getByRole('main', { name: '试卷编辑工作区' })
    await editor.getByLabel('题目正文', { exact: true }).fill('临时未保存内容')
    discardChanges = false
    await page
      .getByRole('navigation', { name: '题目导航' })
      .getByRole('button', { name: '切换至2' })
      .click()
    await expect(editor.getByLabel('题目正文', { exact: true })).toHaveValue('临时未保存内容')
    discardChanges = true
    await editor.getByLabel('题目正文', { exact: true }).fill('合成题1：计算3+3，并写出过程。')
    await editor.getByLabel('教师标准答案', { exact: true }).fill('3+3=6')
    await editor.getByLabel('评分要求').first().fill('正确列出计算过程')
    await editor.getByRole('button', { name: '保存题目与评分标准', exact: true }).click()
    await expect(editor.getByLabel('教师标准答案', { exact: true })).toHaveValue('3+3=6')
    const order = page.getByRole('region', { name: '题序与显示题号' })
    await order.getByRole('button', { name: '下移', exact: true }).first().click()
    await order.getByRole('button', { name: '保存题序与题号', exact: true }).click()
    await expect(editor.getByRole('button', { name: '检查并锁定试卷' })).toBeEnabled()
    await captureLayout(page, 'paper-workbench', '.paper-workspace')
    await page.screenshot({
      path: 'test-results/paper-workbench-draft.png',
      fullPage: true,
      animations: 'disabled',
    })
    await editor.getByRole('button', { name: '检查并锁定试卷' }).click()
    await expect(editor.getByText(/本版本只读保留/)).toBeVisible()
    await expect(editor.getByRole('button', { name: '刷新版本', exact: true })).toBeEnabled()
    await expect(editor.getByLabel('教师标准答案', { exact: true })).toBeDisabled()
    await page.screenshot({
      path: 'test-results/paper-workbench-locked.png',
      fullPage: true,
      animations: 'disabled',
    })
    await editor.getByRole('button', { name: '创建后继草稿' }).click()
    await expect(page).not.toHaveURL(new RegExp(paperA + '$'))
    paperB = new URL(page.url()).pathname.split('/').pop()!
    await expect(editor.getByRole('button', { name: '检查并锁定试卷' })).toBeVisible()
    await editor.getByRole('button', { name: '检查并锁定试卷' }).click()
    await expect(editor.getByText(/本版本只读保留/)).toBeVisible()
    await expect(editor.getByRole('button', { name: '刷新版本', exact: true })).toBeEnabled()
    await page.screenshot({
      path: 'test-results/paper-workbench-successor.png',
      fullPage: true,
      animations: 'disabled',
    })
    await page.getByRole('link', { name: '考试配置 →', exact: true }).click()
  } else await signIn(page, '/exams')
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
  await selector.nth(1).selectOption(paperA)
  await paperPanel.getByRole('button', { name: '绑定试卷', exact: true }).click()
  await expect(paperPanel.locator('tbody tr')).toHaveCount(1)
  await selector.nth(0).selectOption('B')
  await selector.nth(1).selectOption(paperB)
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
  await expect(
    page
      .getByRole('navigation', { name: '功能导航' })
      .getByRole('link', { name: '成绩预览', exact: true }),
  ).toHaveCount(0)
  await signIn(page, '/exam-results')
  await expect(page.getByRole('heading', { name: '整卷成绩预览' })).toBeVisible()
  await expect(page.getByText('暂无已授权班级的考试场次，请联系考务负责人。')).toBeVisible()
  expect(errors).toEqual([])
})

test('普通命题教师：最小建卷角色与新卷对象授权闭环', async ({ page, request }) => {
  test.skip(process.env.RUN_PAPER_CREATOR_BROWSER !== '1', 'paper creator opt-in')
  test.setTimeout(90000)
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/creator`, {
    ignoreHTTPSErrors: true,
  })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('dialog', (dialog) => dialog.accept())
  await page.setViewportSize({ width: 1440, height: 1000 })
  await signIn(page, '/papers')
  await page.getByRole('button', { name: '从已发布作业创建', exact: true }).click()
  const importing = page.getByRole('dialog', { name: '创建试卷快照' })
  const sources = importing.getByLabel('已发布来源')
  await expect(sources.locator('option')).toHaveCount(2) // placeholder + own source only
  await sources.selectOption(process.env.WORKBENCH_CREATOR_SOURCE!)
  await importing.getByLabel('试卷名称').fill('普通教师独立命题')
  await importing.getByRole('button', { name: '创建草稿快照' }).click()
  await expect(importing).not.toBeVisible()
  await expect(page).toHaveURL(/\/papers\//)
  const pid = new URL(page.url()).pathname.split('/').pop()!
  const editor = page.getByRole('main', { name: '试卷编辑工作区' })
  await expect(editor.getByLabel('教师标准答案', { exact: true })).toBeEnabled()
  await editor.getByLabel('教师标准答案', { exact: true }).fill('3+3=6')
  await editor.getByLabel('评分要求').first().fill('列式与计算过程正确')
  await editor.getByRole('button', { name: '保存题目与评分标准', exact: true }).click()
  await expect(editor.getByRole('button', { name: '刷新版本', exact: true })).toBeEnabled()
  const order = page.getByRole('region', { name: '题序与显示题号' })
  await order.getByRole('button', { name: '下移', exact: true }).first().click()
  await order.getByRole('button', { name: '保存题序与题号', exact: true }).click()
  await expect(editor.getByRole('button', { name: '检查并锁定试卷' })).toBeEnabled()
  await page.screenshot({ path: 'test-results/paper-creator-draft.png', fullPage: true })
  await editor.getByRole('button', { name: '检查并锁定试卷' }).click()
  await expect(editor.getByText(/本版本只读保留/)).toBeVisible()
  await expect(editor.getByRole('button', { name: '刷新版本', exact: true })).toBeEnabled()
  await expect(editor.getByLabel('教师标准答案', { exact: true })).toBeDisabled()
  await page.screenshot({ path: 'test-results/paper-creator-locked.png', fullPage: true })
  await editor.getByRole('button', { name: '创建后继草稿' }).click()
  await expect(page).not.toHaveURL(new RegExp(pid + '$'))
  await expect(editor.getByLabel('教师标准答案', { exact: true })).toBeEnabled()
  await expect(editor.getByLabel('教师标准答案', { exact: true })).toHaveValue('6') // reordered first item
  await expect(editor.getByRole('button', { name: '检查并锁定试卷' })).toBeVisible()
  await expect(editor.getByRole('button', { name: '刷新版本', exact: true })).toBeEnabled()
  await page.screenshot({ path: 'test-results/paper-creator-successor.png', fullPage: true })
  expect(errors).toEqual([])
})

test('学校管理：真实CSV预检确认、两班同名学生与冻结名单联动', async ({
  page,
  request,
  browser,
}) => {
  test.skip(process.env.RUN_SCHOOL_WORKBENCH_BROWSER !== '1', 'school workbench opt-in')
  test.setTimeout(120000)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => d.accept())
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/school_admin`, {
    ignoreHTTPSErrors: true,
  })
  await signIn(page, '/school')
  await expect(page.getByRole('link', { name: '学校管理', exact: true })).toBeVisible()
  const scope = page.getByRole('region', { name: '基础组织', exact: true })
  const created: Record<string, string> = {}
  async function create(kind: string, name: string, fields: Record<string, string>) {
    await scope.getByLabel('新增类型').selectOption(kind)
    await scope.getByRole('button', { name: '新建' + name, exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '新建' + name, exact: true })
    for (const [label, value] of Object.entries(fields)) {
      if (['学年状态', '所属学年', '所属届别'].includes(label))
        await dialog.getByRole('combobox', { name: label, exact: true }).selectOption(value)
      else await dialog.getByLabel(label, { exact: true }).fill(value)
    }
    // Read the real backend response before forwarding it, avoiding Chromium's no-store
    // response-body eviction race. No synthetic response or extra write is introduced.
    const path = '**/api/v1/organization/' + kind
    await page.route(path, async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      const response = await route.fetch()
      expect(response.status()).toBe(201)
      const result = await response.json()
      created[fields['编码']!] = result.id
      await route.fulfill({ response })
    })
    await dialog.getByRole('button', { name: '保存' + name, exact: true }).click()
    await expect(dialog).not.toBeVisible()
    await expect(page.getByText('组织信息已保存。')).toBeVisible()
    expect(created[fields['编码']!]).toBeTruthy()
    await page.unroute(path)
  }
  await create('academic-years', '学年', {
    编码: 'SCHOOL2026',
    名称: '网页2026学年',
    开始日期: '2026-09-01',
    结束日期: '2027-07-10',
    学年状态: 'active',
  })
  await create('academic-terms', '学期', {
    编码: 'SCHOOLFALL',
    所属学年: created.SCHOOL2026!,
    开始日期: '2026-09-01',
    结束日期: '2027-01-31',
    学期序号: '1',
  })
  await create('grade-cohorts', '入学届别', {
    编码: 'SCHOOLCOHORT',
    名称: '网页2026入学',
    入学年份: '2026',
  })
  await create('subjects', '学科', { 编码: 'SCHOOLMATH', 名称: '网页数学' })
  await create('classes', '行政班', {
    编码: 'WEB01',
    名称: '网页高一1班',
    所属届别: created.SCHOOLCOHORT!,
  })
  await create('classes', '行政班', {
    编码: 'WEB02',
    名称: '网页高一2班',
    所属届别: created.SCHOOLCOHORT!,
  })
  await page.screenshot({ path: 'test-results/school-dictionaries.png', fullPage: true })
  await page.getByRole('button', { name: '学生导入', exact: true }).click()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载CSV模板' }).click()
  const downloaded = await download
  const stream = await downloaded.createReadStream()
  const bytes: Buffer[] = []
  for await (const chunk of stream!) bytes.push(Buffer.from(chunk))
  expect(
    Buffer.concat(bytes)
      .toString('utf8')
      .replace(/^\uFEFF/, ''),
  ).toBe('student_code,name,class_code,valid_from\r\n')
  const header = 'student_code,name,class_code,valid_from\n'
  async function upload(content: string) {
    await page.getByLabel('导入学年').selectOption(created.SCHOOL2026!)
    await page
      .getByLabel('CSV文件')
      .setInputFiles({ name: 'synthetic.csv', mimeType: 'text/csv', buffer: Buffer.from(content) })
    await page.getByRole('button', { name: '预检CSV', exact: true }).click()
    await expect(page.getByRole('heading', { name: '预检结果 · 尚未导入' })).toBeVisible()
  }
  await upload(
    header +
      '001,同名合成,WEB01,2026-09-01\n002,同名合成,WEB02,2026-09-01\n003,合成丙,WEB01,2026-09-01\n004,合成丁,WEB02,2026-09-01\n',
  )
  await expect(page.getByRole('region', { name: '导入预检结果' }).locator('tbody tr')).toHaveCount(
    4,
  )
  await page.screenshot({ path: 'test-results/school-import-preview.png', fullPage: true })
  let original = ''
  let recovered = false
  await page.route('**/api/v1/organization/student-imports/*/confirm', async (route) => {
    if (!original) {
      original = route.request().postData()!
      const committed = await route.fetch()
      expect(committed.status()).toBe(200)
      await route.abort('failed')
    } else {
      expect(route.request().postData()).toBe(original)
      recovered = true
      await route.continue()
    }
  })
  await page.getByRole('button', { name: '确认正式导入', exact: true }).click()
  await expect(page.getByText(/保存结果尚未核实，请勿重复提交/)).toBeVisible()
  await page.getByRole('button', { name: '重试原请求' }).click()
  await expect(page.getByRole('heading', { name: '已正式导入', exact: true })).toBeVisible()
  expect(recovered).toBe(true)
  await page.unroute('**/api/v1/organization/student-imports/*/confirm')
  await page.screenshot({ path: 'test-results/school-import-confirmed.png', fullPage: true })
  await page.getByRole('button', { name: '学生名册', exact: true }).click()
  await page.getByLabel('学号或姓名').fill('同名合成')
  await page.getByRole('button', { name: '查询学生', exact: true }).click()
  await expect(page.getByRole('region', { name: '学生名册' }).locator('tbody tr')).toHaveCount(2)
  await expect(
    page
      .getByRole('region', { name: '学生名册' })
      .locator('tbody tr')
      .filter({ hasText: '同名合成' }),
  ).toHaveCount(2)
  await page.getByRole('link', { name: '前往考试配置 →', exact: true }).click()
  await page.getByRole('button', { name: '新建考试', exact: true }).click()
  const ed = page.getByRole('dialog', { name: '新建考试', exact: true })
  await ed.getByLabel('考试名称').fill('学校资料联动合成考试')
  await ed.getByLabel('学年', { exact: true }).selectOption(created.SCHOOL2026!)
  await ed.getByLabel('学期', { exact: true }).selectOption(created.SCHOOLFALL!)
  await ed.getByRole('button', { name: '创建考试', exact: true }).click()
  await expect(ed).not.toBeVisible()
  await page.getByRole('button', { name: '新建科目场次', exact: true }).click()
  const sd = page.getByRole('dialog', { name: '新建科目场次', exact: true })
  await sd.getByLabel('场次名称').fill('学校联动数学')
  await sd.getByLabel('学科', { exact: true }).selectOption(created.SCHOOLMATH!)
  await sd.getByLabel('开始时间（北京时间）').fill('2026-11-01T09:00')
  await sd.getByLabel('结束时间（北京时间）').fill('2026-11-01T11:00')
  await sd.getByLabel('用卷方式').selectOption('single')
  await sd.getByRole('button', { name: '创建场次', exact: true }).click()
  await expect(sd).not.toBeVisible()
  await expect(page).toHaveURL(/\/exams\/sessions\//)
  const sessionUrl = page.url()
  const paper = page.getByRole('region', { name: '场次用卷' })
  await paper.locator('select').last().selectOption(process.env.WORKBENCH_CONFIG_PAPER_A!)
  await paper.getByRole('button', { name: '绑定试卷', exact: true }).click()
  await expect(paper.locator('tbody tr')).toHaveCount(1)
  await paper.getByRole('button', { name: '标记用卷准备就绪' }).click()
  await expect(paper.getByText(/用卷已就绪/)).toBeVisible()
  const roster = page.getByRole('region', { name: '参试名单配置' })
  await roster.locator(`input[type=checkbox][value="${created.WEB01}"]`).check()
  await roster.locator(`input[type=checkbox][value="${created.WEB02}"]`).check()
  await roster.getByRole('button', { name: '按考试日期预览学生' }).click()
  await expect(roster.locator('tbody tr')).toHaveCount(4)
  await expect(roster.locator('tbody tr').filter({ hasText: '同名合成' })).toHaveCount(2)
  await roster.getByRole('button', { name: '按当前顺序编排考号' }).click()
  await roster.getByLabel('名单操作说明').fill('合成两班逐人核对；卷别保持待核验')
  await roster.getByRole('button', { name: '保存名单草稿' }).click()
  await expect(roster.getByRole('button', { name: '冻结当前名单' })).toBeEnabled()
  await roster.getByRole('button', { name: '冻结当前名单' }).click()
  await expect(roster.getByText(/本版已冻结，只读保留/)).toBeVisible()
  await page.screenshot({ path: 'test-results/school-frozen-roster.png', fullPage: true })
  await page.getByRole('link', { name: '学校管理', exact: true }).click()
  await page.getByRole('button', { name: '学生导入', exact: true }).click()
  await upload(header + '005,后续合成,WEB01,2026-09-01\n')
  await page.getByRole('button', { name: '确认正式导入', exact: true }).click()
  await expect(page.getByRole('heading', { name: '已正式导入', exact: true })).toBeVisible()
  await page.getByRole('link', { name: '前往考试配置 →', exact: true }).click()
  await page
    .locator('article')
    .filter({ has: page.getByRole('heading', { name: '学校联动数学', exact: true }) })
    .getByRole('button', { name: '配置场次 →', exact: true })
    .click()
  await expect(page).toHaveURL(sessionUrl)
  await expect(roster.getByText(/本版已冻结，只读保留/)).toBeVisible()
  await expect(roster.locator('tbody tr')).toHaveCount(4)
  await expect(roster.getByText('后续合成')).toHaveCount(0)
  // Different signed identities, separate browser sessions; no token role substitution.
  for (const actor of ['school_teacher', 'school_previewer']) {
    await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/${actor}`, {
      ignoreHTTPSErrors: true,
    })
    const context = await browser.newContext({ ignoreHTTPSErrors: true })
    const other = await context.newPage()
    await signIn(other, '/school')
    if (actor === 'school_teacher') {
      await expect(other.getByRole('link', { name: '学校管理', exact: true })).toHaveCount(0)
      await expect(other.getByText(/当前没有学校组织读取权限/)).toBeVisible()
      await expect(other.getByRole('button', { name: '新建学年', exact: true })).toHaveCount(0)
    } else {
      await other.getByRole('button', { name: '导入记录', exact: true }).click()
      await other.getByRole('button', { name: '查看批次', exact: true }).first().click()
      await expect(other.getByRole('button', { name: '确认正式导入', exact: true })).toHaveCount(0)
    }
    await context.close()
  }
  expect(errors).toEqual([])
})

test('协同阅卷：负责人跨班分配、两名教师提交与成绩预览', async ({ page, request, browser }) => {
  test.skip(process.env.RUN_MARKING_TASKS_BROWSER !== '1', 'collaborative marking opt-in')
  test.setTimeout(150000)
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  page.on('dialog', (d) => d.accept())
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await signIn(page, '/marking/manage')
  await page.getByLabel('考试与科目场次').selectOption(process.env.WORKBENCH_SCAN_SESSION!)
  const first = page.locator('.mark-script').filter({ hasText: '合成1班' })
  const second = page.locator('.mark-script').filter({ hasText: '合成2班' })
  await expect(first).toHaveCount(1)
  await expect(second).toHaveCount(1)
  await first.getByRole('button', { name: '选择整卷', exact: true }).click()
  await page.getByLabel('阅卷教师').selectOption(process.env.WORKBENCH_MARKER_A!)
  await page.getByRole('button', { name: '分配选中题目（2）', exact: true }).click()
  await expect(page.getByText('操作已保存。')).toBeVisible()
  await second.getByRole('checkbox').first().check()
  await page.getByLabel('阅卷教师').selectOption(process.env.WORKBENCH_MARKER_B!)
  await page.getByRole('button', { name: '分配选中题目（1）', exact: true }).click()
  await expect(page.getByText('操作已保存。')).toBeVisible()
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: 'test-results/marking-management.png', fullPage: true })
  async function teacher(actor: string, count: number) {
    await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/${actor}`, {
      ignoreHTTPSErrors: true,
    })
    const context = await browser.newContext({
      ignoreHTTPSErrors: true,
      viewport: { width: 1440, height: 1000 },
    })
    const tab = await context.newPage()
    tab.on('dialog', (d) => d.accept())
    tab.on('pageerror', (e) => errors.push(e.message))
    await signIn(tab, '/marking/my')
    const taskList = tab.getByRole('region', { name: '个人任务列表' })
    await expect(taskList.locator('tbody tr')).toHaveCount(count)
    await taskList.getByRole('button', { name: '已完成', exact: true }).click()
    await expect(taskList.getByText('本页没有此状态的任务，可切换筛选或查看其他页。')).toBeVisible()
    await taskList.getByRole('button', { name: '全部', exact: true }).click()
    await expect(taskList.locator('tbody tr')).toHaveCount(count)
    for (let index = 0; index < count; index++) {
      if (index === 0) await tab.getByRole('link', { name: '进入任务 →' }).first().click()
      await tab.getByRole('button', { name: '领取并开始', exact: true }).click()
      const panel = tab.getByRole('region', { name: 'AI 批改与复核' })
      await expect(panel.getByRole('button', { name: '接受模型建议', exact: true })).toBeEnabled()
      await panel.getByRole('button', { name: '接受模型建议', exact: true }).click()
      await expect(panel.getByText('已保存，请核对当前结果。尚未发布正式成绩。')).toBeVisible()
      if (index === 0) {
        await panel.locator('.mark-thumbs button').first().click()
        await expect(panel.getByAltText('当前答卷扫描原页')).toBeVisible()
        if (actor === 'marker_a') {
          await tab.evaluate(() => {
            const panel = document.querySelector('.exam-marking')!
            window.scrollTo(0, window.scrollY + panel.getBoundingClientRect().top - 76)
          })
          const taskBar = tab.getByRole('region', { name: '当前阅卷任务' })
          await expect(taskBar.getByRole('button', { name: '提交当前任务' })).toBeInViewport()
          const taskBox = await taskBar.boundingBox()
          const previewBox = await panel.locator('.grading-preview').boundingBox()
          expect(taskBox && previewBox && previewBox.y >= taskBox.y + taskBox.height - 1).toBe(true)
          await captureLayout(tab, 'marking-task', '.exam-marking')
        }
        await tab.evaluate(() => window.scrollTo(0, 0))
        await tab.screenshot({ path: `test-results/marking-${actor}-review.png`, fullPage: true })
      }
      await tab.getByRole('button', { name: '提交当前任务', exact: true }).click()
      await expect(tab.getByText('任务已提交，可进入下一题。')).toBeVisible()
      await expect(
        tab.getByRole('region', { name: '当前阅卷任务' }).getByText(/已完成/),
      ).toBeVisible()
      if (index < count - 1)
        await tab.getByRole('button', { name: '下一道待批题目', exact: true }).click()
    }
    await context.close()
  }
  // Assignment does not itself create a final score from an existing candidate/review.
  await page.getByRole('link', { name: '成绩预览', exact: true }).click()
  const firstResult = page.locator('.result-table tbody tr').filter({ hasText: 'N001' })
  await expect(firstResult).toContainText('未完成')
  await teacher('marker_a', 2)
  await teacher('marker_b', 1)
  // The supervisor's in-memory session is unchanged by the two other browser contexts.
  await page.getByRole('link', { name: '阅卷进度', exact: true }).click()
  await page.getByLabel('考试与科目场次').selectOption(process.env.WORKBENCH_SCAN_SESSION!)
  const progress = page.getByRole('region', { name: '任务明细' })
  await expect(progress.locator('tbody tr')).toHaveCount(3)
  for (const row of await progress.locator('tbody tr').all())
    await expect(row).toContainText('已完成')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: 'test-results/marking-progress.png', fullPage: true })
  await page.getByRole('link', { name: '整卷成绩预览（需独立班级授权） →', exact: true }).click()
  await expect(firstResult).toContainText('全部复核完成')
  await expect(firstResult).toContainText('20.00')
  const secondResult = page.locator('.result-table tbody tr').filter({ hasText: 'N002' })
  await expect(secondResult).toContainText('未完成')
  await expect(secondResult.locator('td').nth(5)).toContainText('未完成')
  await expect(secondResult).toContainText('10.00')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: 'test-results/marking-results-preview.png', fullPage: true })
  expect(errors).toEqual([])
})

test('异常补救：缺页身份待定、失败文件原请求恢复、跨批补页、解析失败重认后送回阅卷', async ({
  page,
  request,
}) => {
  test.skip(process.env.RUN_RECOVERY_BROWSER !== '1', 'recovery real business opt-in')
  test.setTimeout(160000)
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  const sid = process.env.WORKBENCH_SCAN_SESSION!
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => d.accept())
  await signIn(page, `/scans/sessions/${sid}/scripts`)
  await page.locator('.script-saved-item').filter({ hasText: '合成1班' }).click()
  await expect(page.locator('.script-identity')).toContainText('N001')
  await page.getByRole('button', { name: '移除第2项', exact: true }).click()
  await page.getByLabel('已核对页数，答卷完整').uncheck()
  await page.getByLabel('操作说明').fill('异常补救：发现漏页，保留未确认草稿')
  const saved = page
    .waitForResponse((r) => r.url().endsWith('/versions') && r.request().method() === 'POST')
    .then((r) => r.json())
  await page.getByRole('button', { name: '保存为新版本草稿', exact: true }).click()
  const { script_id: aid } = await saved
  await page.getByRole('link', { name: '异常处理', exact: true }).click()
  await page.getByLabel('考试与科目场次').selectOption(sid)
  await expect(
    page.locator(`tr[data-kind="identity_pending"][data-resource="${aid}"]`),
  ).toBeVisible()
  await page.screenshot({ path: 'test-results/recovery-missing-identity.png', fullPage: true })
  // Real failure after the file reservation, before private S3 storage succeeds.
  await page.getByRole('link', { name: '新建补扫批次 →', exact: true }).click()
  await page.getByLabel('批次名称').fill('浏览器补扫批次')
  await page.getByRole('button', { name: '新建扫描批次', exact: true }).click()
  await page
    .getByLabel('扫描 PDF', { exact: true })
    .setInputFiles(process.env.WORKBENCH_RECOVERY_PDF!)
  await page.getByRole('button', { name: '上传并生成预览', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('link', { name: '异常处理', exact: true }).click()
  await page.getByLabel('考试与科目场次').selectOption(sid)
  const failed = page.locator('tr[data-kind="upload_failed"]')
  await expect(failed).toHaveCount(1)
  await failed.locator('input[type=file]').setInputFiles(process.env.WORKBENCH_RECOVERY_PDF!)
  await expect(page.getByText('重传已完成，页面仍待人工分卷及身份确认。')).toBeVisible()
  await expect(page.locator('tr[data-kind="upload_failed"]')).toHaveCount(0)
  await page.screenshot({ path: 'test-results/recovery-upload-restored.png', fullPage: true })
  await page
    .locator(`tr[data-resource="${aid}"]`)
    .getByRole('link', { name: '补页、调序或重新确认', exact: true })
    .click()
  await expect(page.locator('.script-status')).toContainText('待身份确认')
  await expect(page.locator('.script-order li')).toHaveCount(1)
  const original = page.locator('.script-page').filter({ hasText: 'mixed-scan.pdf' })
  await original.getByRole('button', { name: '加入扫描页 1', exact: true }).click()
  const supplement = page.locator('.script-page').filter({ hasText: 'recover-upload.pdf' })
  await supplement.getByRole('button', { name: '加入扫描页 1', exact: true }).click()
  await page.getByRole('button', { name: '上移第3项', exact: true }).click()
  await page.getByLabel('已核对页数，答卷完整').check()
  await page.getByLabel('操作说明').fill('异常补救：测试解析资源故障')
  await page.getByRole('button', { name: '核对学生并确认', exact: true }).click()
  const identity = page.getByRole('dialog', { name: '核对学生身份与实际卷别' })
  await identity.getByLabel('参试学生').selectOption({
    label: await identity
      .getByLabel('参试学生')
      .locator('option')
      .filter({ hasText: '考号 N001' })
      .innerText(),
  })
  await identity.getByRole('button', { name: '确认身份并保存新版本' }).click()
  await expect(identity).not.toBeVisible()
  await page.getByRole('button', { name: '开始解析', exact: true }).click()
  await expect(page.locator('.parse-state')).toHaveText('解析失败', { timeout: 40000 })
  await page.getByRole('link', { name: '异常处理', exact: true }).click()
  await page.getByLabel('考试与科目场次').selectOption(sid)
  const parsing = page.locator(`tr[data-kind="parsing_failed"][data-resource="${aid}"]`)
  await expect(parsing).toBeVisible()
  await expect(parsing).toContainText('本地解析资源暂不可用')
  await page.screenshot({ path: 'test-results/recovery-parse-failed.png', fullPage: true })
  await parsing.getByRole('link', { name: '核对答卷与重新解析', exact: true }).click()
  await expect(page.locator('.script-order li')).toHaveCount(3)
  await page.getByLabel('操作说明').fill('资源修复后重新确认，保持三页顺序')
  await page.getByRole('button', { name: '核对学生并确认', exact: true }).click()
  await identity.getByLabel('参试学生').selectOption({
    label: await identity
      .getByLabel('参试学生')
      .locator('option')
      .filter({ hasText: '考号 N001' })
      .innerText(),
  })
  await identity.getByRole('button', { name: '确认身份并保存新版本' }).click()
  await expect(identity).not.toBeVisible()
  await page.getByRole('button', { name: '开始解析', exact: true }).click()
  await expect(page.locator('.parse-state')).toHaveText('解析完成', { timeout: 60000 })
  await page.getByRole('button', { name: '查看解析内容', exact: true }).click()
  await expect(page.locator('.parsed-content')).not.toBeEmpty()
  await page.screenshot({ path: 'test-results/recovery-canonical-restored.png', fullPage: true })
  await page.getByRole('link', { name: '异常处理', exact: true }).click()
  await page.getByLabel('考试与科目场次').selectOption(sid)
  await page.getByRole('link', { name: '送回阅卷流程 →', exact: true }).click()
  // The repaired version receives new workspaces; none of the old scores is revived.
  await page.getByRole('button', { name: '准备待批题目', exact: true }).first().click()
  await page.getByLabel('阅卷教师').selectOption(process.env.WORKBENCH_MARKER_A!)
  const paper = page.locator('.mark-script').filter({ hasText: '合成1班' })
  await paper.locator('input[type=checkbox]').first().check()
  await page.getByRole('button', { name: /分配选中题目/ }).click()
  await expect(page.getByText('操作已保存。', { exact: true })).toBeVisible()
  await page.screenshot({ path: 'test-results/recovery-return-to-marking.png', fullPage: true })
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/marker_a`, {
    ignoreHTTPSErrors: true,
  })
  const teacherContext = await page
    .context()
    .browser()!
    .newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 } })
  const teacherPage = await teacherContext.newPage()
  await signIn(teacherPage, '/marking/my')
  await teacherPage.getByRole('link', { name: '进入任务 →' }).click()
  await teacherPage.getByRole('button', { name: '领取并开始', exact: true }).click()
  await expect(teacherPage.getByRole('region', { name: 'AI 批改与复核' })).toBeVisible()
  const resumed = teacherPage.getByRole('region', { name: 'AI 批改与复核' })
  await expect(resumed.getByRole('heading', { name: '第1题', exact: true })).toBeVisible()
  await expect(resumed.locator('.mark-thumbs button')).toHaveCount(3)
  await resumed.locator('.mark-thumbs button').first().scrollIntoViewIfNeeded()
  await expect
    .poll(() =>
      resumed
        .locator('.mark-thumbs img')
        .first()
        .evaluate((img) => (img as HTMLImageElement).naturalWidth),
    )
    .toBeGreaterThan(0)

  await teacherPage.screenshot({
    path: 'test-results/recovery-teacher-resumed.png',
    fullPage: true,
  })
  await teacherContext.close()
  await page.getByRole('link', { name: '成绩预览', exact: true }).click()
  const correctedResult = page.locator('.result-table tbody tr').filter({ hasText: 'N001' })
  await expect(correctedResult).toContainText('未完成')
  await expect(correctedResult).not.toContainText('18.00')
  await page.screenshot({ path: 'test-results/recovery-old-score-invalid.png', fullPage: true })

  await page.getByRole('link', { name: '异常处理', exact: true }).click()
  await page.getByLabel('考试与科目场次').selectOption(sid)
  await expect(page.locator(`tr[data-kind="parsing_failed"][data-resource="${aid}"]`)).toHaveCount(
    0,
  )
  expect(errors).toEqual([])
})

test('日常作业：两班布置、同名学生代收重交与教师批改', async ({ page, request }) => {
  test.skip(process.env.RUN_HOMEWORK_BROWSER !== '1', 'real homework workflow opt-in')
  test.setTimeout(120000)
  page.setDefaultTimeout(10000)
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  await signIn(page, '/assignments')
  await page.getByRole('button', { name: /新建作业/ }).click()
  const create = page.getByRole('dialog', { name: '新建作业' })
  await create.getByLabel('作业名称').fill('两班合成日常作业')
  await create.getByLabel('学科').fill('合成数学')
  await create.getByLabel('年级').fill('高二')
  await create.getByRole('button', { name: '创建草稿' }).click()
  await page.getByRole('link', { name: '布置班级作业与查看完成情况 →' }).click()
  await page.getByLabel('题号').fill('1')
  await page.getByLabel('题目', { exact: true }).fill('计算 2+2')
  await page.getByLabel('满分').fill('10.00')
  await page.getByLabel('标准答案').fill('4')
  await page.getByLabel('评分标准').fill('正确计算结果得满分')
  await page.getByRole('button', { name: '保存题目' }).click()
  await expect(page.getByText('当前已配置 1 题。')).toBeVisible()
  await page.getByRole('button', { name: '发布作业内容' }).click()
  await expect(page.getByLabel('学期与学科').locator('option')).toHaveCount(2)
  await page.getByLabel('学期与学科').selectOption({ label: 'fall · 合成数学' })
  await page.getByLabel('合成1班').check()
  await page.getByLabel('合成2班').check()
  const due = new Date(Date.now() + 86400000 * 3)
  const local = new Date(due.getTime() - due.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
  await page.getByLabel('截止时间').fill(local)
  await page.getByRole('button', { name: '布置作业', exact: true }).click()
  await expect(page.getByRole('heading', { name: '班级完成情况' })).toBeVisible()
  await expect(page.getByText(/合成1班.*应交 2，已交 0，未交 2/)).toBeVisible()
  await expect(page.getByText(/合成2班.*应交 2，已交 0，未交 2/)).toBeVisible()
  const sameName = page.locator('tbody tr').filter({ hasText: '合成同名' })
  await expect(sameName).toHaveCount(2)
  const firstCode = (await sameName.first().locator('td').nth(1).textContent())!.trim()
  await sameName.first().locator('input[type=file]').setInputFiles(process.env.WORKBENCH_SCAN_PDF!)
  await page.getByRole('button', { name: '确认代收' }).click()
  await expect(page.locator('tbody tr').filter({ hasText: firstCode })).toContainText('第 1 次')
  await page
    .locator('tbody tr')
    .filter({ hasText: firstCode })
    .locator('input[type=file]')
    .setInputFiles(process.env.WORKBENCH_SCAN_PDF!)
  await page.getByRole('button', { name: '确认代收' }).click()
  const student = page.locator('tbody tr').filter({ hasText: firstCode })
  await expect(student).toContainText('第 2 次')
  await page.getByLabel('筛选本页提交状态').selectOption('not_submitted')
  await expect(page.locator('tbody tr')).toHaveCount(3)
  await expect(page.locator('tbody')).not.toContainText(firstCode)
  await page.getByLabel('筛选本页提交状态').selectOption('submitted')
  await expect(page.locator('tbody tr')).toHaveCount(1)
  await expect(page.locator('tbody')).toContainText(firstCode)
  await page.getByLabel('筛选本页提交状态').selectOption('all')
  await student.getByRole('button', { name: '提交历史' }).click()
  await expect(page.getByText('第 1 次 · 历史版本')).toBeVisible()
  await page.evaluate(() => window.scrollTo(0, 0))
  await captureLayout(page, 'homework', '.page-heading')
  await page.screenshot({ path: 'test-results/homework-dashboard.png', fullPage: true })
  await expect
    .poll(
      async () => {
        await page.getByRole('button', { name: '刷新', exact: true }).click()
        return await page.locator('tbody tr').filter({ hasText: firstCode }).innerText()
      },
      { timeout: 30000, intervals: [500, 1000] },
    )
    .toContain('完成')
  await student.getByRole('link', { name: '进入批改' }).click()
  await page.getByRole('button', { name: '准备作答核对' }).click()
  await page.getByRole('link', { name: '核对作答 →' }).click()
  await select(page)
  await save(page)
  await expect(page.getByRole('status')).toContainText('已确认保存')
  await page.getByRole('button', { name: '请求 AI 评分建议' }).click()
  await expect(page.getByText(/建议总分 10/)).toBeVisible()
  await page.getByLabel(/^教师分 /).fill('')
  await page.getByRole('button', { name: '提交修订' }).click()
  await expect(page.getByText('请填写每个评分项的教师分值，空白不会自动计零。')).toBeVisible()
  await page.getByLabel(/^教师分 /).fill('10.00')
  await page.getByRole('button', { name: '接受建议' }).click()
  await expect(page.getByText('教师复核已保存，不代表正式成绩发布。')).toBeVisible()
  await expect(page.getByText(/当前复核：已接受/)).toBeVisible()
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: 'test-results/homework-review.png', fullPage: true })
  await page.getByRole('link', { name: '← 返回学生提交' }).click()
  await page.getByRole('link', { name: '← 返回作业' }).click()
  await page.getByRole('link', { name: '布置班级作业与查看完成情况 →' }).click()
  await expect(page.locator('tbody tr').filter({ hasText: firstCode })).toContainText('1 / 1')
})

test('学校管理员登记和撤销真实任教关系', async ({ page, request }) => {
  test.skip(process.env.RUN_HOMEWORK_BROWSER !== '1', 'real homework workflow opt-in')
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/admin`, {
    ignoreHTTPSErrors: true,
  })
  await signIn(page, '/school/teaching')
  const teacher = process.env.WORKBENCH_HOMEWORK_TEACHER_B!
  await page.getByLabel('教师账号').selectOption(teacher)
  await page.getByLabel('学期').selectOption({ label: 'fall' })
  await page.getByLabel('学科').selectOption({ label: '合成数学' })
  await page.getByLabel('行政班').selectOption({ label: '合成1班' })
  await page.getByRole('button', { name: '保存任教关系' }).click()
  await expect(page.getByRole('status')).toContainText('任教关系已保存')
  const row = page.locator('tbody tr').filter({ hasText: teacher })
  await expect(row).toContainText('合成1班')
  await page.screenshot({ path: 'test-results/homework-teaching.png', fullPage: true })
  page.once('dialog', (dialog) => dialog.accept())
  await row.getByRole('button', { name: '撤销' }).click()
  await expect(row).toHaveCount(0)
})

test('日常作业：图片区域评分复核及重交失效', async ({ page, request }) => {
  test.skip(process.env.RUN_HOMEWORK_VISUAL_BROWSER !== '1', 'real homework visual opt-in')
  test.setTimeout(180000)
  page.setDefaultTimeout(15000)
  await request.post(`${process.env.WORKBENCH_OIDC}/__test__/actor/a`, { ignoreHTTPSErrors: true })
  await signIn(page, '/assignments')
  await page.getByRole('button', { name: /新建作业/ }).click()
  const create = page.getByRole('dialog', { name: '新建作业' })
  await create.getByLabel('作业名称').fill('两班图片作业合成验收')
  await create.getByLabel('学科').fill('合成数学')
  await create.getByLabel('年级').fill('高二')
  await create.getByRole('button', { name: '创建草稿' }).click()
  await page.getByRole('link', { name: '布置班级作业与查看完成情况 →' }).click()
  await page.getByLabel('题号').fill('1')
  await page.getByLabel('题目', { exact: true }).fill('计算 1+1')
  await page.getByLabel('满分').fill('2.00')
  await page.getByLabel('标准答案').fill('2')
  await page.getByLabel('评分标准').fill('学生写出正确结果得2分')
  await page.getByRole('button', { name: '保存题目' }).click()
  await page.getByRole('button', { name: '发布作业内容' }).click()
  await page.getByLabel('学期与学科').selectOption({ label: 'fall · 合成数学' })
  await page.getByLabel('合成1班').check()
  await page.getByLabel('合成2班').check()
  const due = new Date(Date.now() + 86400000 * 3)
  await page
    .getByLabel('截止时间')
    .fill(new Date(due.getTime() - due.getTimezoneOffset() * 60000).toISOString().slice(0, 16))
  await page.getByRole('button', { name: '布置作业', exact: true }).click()
  const same = page.locator('tbody tr').filter({ hasText: '合成同名' })
  await expect(same).toHaveCount(2)
  const code = (await same.first().locator('td').nth(1).textContent())!.trim()
  const row = page.locator('tbody tr').filter({ hasText: code })
  await row.locator('input[type=file]').setInputFiles(process.env.WORKBENCH_HOMEWORK_IMAGE!)
  await page.getByRole('button', { name: '确认代收' }).click()
  await expect(row).toContainText('第 1 次')
  await row.getByRole('link', { name: '进入批改' }).click()
  await page.getByText('图片作答批改', { exact: true }).first().click()
  const panel = page.getByRole('region', { name: '图片作答批改' })
  const image = panel.getByAltText('学生作答原页，在图上拖动选择本题作答区域')
  await expect(image).toBeVisible()
  const box = (await image.boundingBox())!
  await page.mouse.move(box.x + box.width * 0.08, box.y + box.height * 0.12)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width * 0.88, box.y + box.height * 0.75)
  await page.mouse.up()
  await expect(panel.getByText(/区域 1 · 第 1 页/)).toBeVisible()
  await panel.getByRole('button', { name: '确认图片区域' }).click()
  await expect(panel.getByRole('status')).toContainText('图片作答区域已确认')
  await panel.getByRole('button', { name: '请求图片 AI 评分' }).click()
  await expect(panel.getByRole('status')).toContainText('评分请求已结束', { timeout: 120000 })
  await expect(panel.getByText(/建议总分|模型弃权/)).toBeVisible({ timeout: 15000 })
  if (await panel.getByRole('button', { name: '接受图片建议' }).isVisible()) {
    await expect(panel.getByText(/区域 1：/)).toBeVisible()
    await panel.getByRole('button', { name: '接受图片建议' }).click()
    await expect(panel.getByRole('status')).toContainText('教师复核已保存')
    await expect(panel.getByText(/教师复核：已接受/)).toBeVisible()
    await page.screenshot({ path: 'test-results/homework-visual-review.png', fullPage: true })
    await page.getByRole('link', { name: '← 返回作业' }).click()
    await page.getByRole('link', { name: '布置班级作业与查看完成情况 →' }).click()
    await expect(page.locator('tbody tr').filter({ hasText: code })).toContainText('1 / 1')
    await page
      .locator('tbody tr')
      .filter({ hasText: code })
      .locator('input[type=file]')
      .setInputFiles(process.env.WORKBENCH_HOMEWORK_IMAGE!)
    await page.getByRole('button', { name: '确认代收' }).click()
    await expect(page.locator('tbody tr').filter({ hasText: code })).toContainText('第 2 次')
    await expect(page.locator('tbody tr').filter({ hasText: code })).toContainText('0 / 1')
    await page.screenshot({ path: 'test-results/homework-visual-resubmission.png', fullPage: true })
  } else {
    await expect(panel.getByText(/模型弃权/)).toBeVisible()
    await page.screenshot({ path: 'test-results/homework-visual-abstention.png', fullPage: true })
  }
})
