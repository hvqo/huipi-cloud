/**
 * 生产构建防泄漏校验：dist 中不得出现演示数据服务代码。
 *
 * 校验的是“演示业务代码”（合成数据、mock 服务、种子数据），
 * 而不是被构建期常量消除后不可达的界面文案。
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const FORBIDDEN = [
  'huipi.demo.user',
  'u-teacher-li',
  'createMockServices',
  'resetDemoState',
  'seedDemoState',
  'renderExamPage',
  '慧批实验中学',
  '演示合成试卷',
  'STU-001',
  'demo-aligner',
]

const assets = readdirSync('dist/assets')
const offenders = []
for (const file of assets) {
  if (!file.endsWith('.js')) continue
  const content = readFileSync(join('dist/assets', file), 'utf8')
  for (const marker of FORBIDDEN) {
    if (content.includes(marker)) offenders.push(`${file}: ${marker}`)
  }
}
if (offenders.length) {
  console.error('生产构建包含演示数据服务代码：')
  for (const item of offenders) console.error(`  - ${item}`)
  process.exit(1)
}
console.log(`✓ 生产构建校验通过：${assets.length} 个资源中未发现演示数据服务代码。`)
