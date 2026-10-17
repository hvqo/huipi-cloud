/**
 * 评分 Decimal 字符串运算。
 *
 * 候选评分与人工修订一律使用十进制字符串（合同要求，禁止二进制浮点）。
 * 本模块基于 BigInt 定点实现，覆盖评分场景需要的解析、比较、求和与格式化。
 */

const MAX_SCALE = 4

export function isValidDecimal(input: string): boolean {
  return /^\d+(\.\d{1,4})?$/.test(input.trim()) && parse(input) !== null
}

/** 解析为 {value: 定点 BigInt, scale}；非法输入返回 null。 */
function parse(input: string): { value: bigint; scale: number } | null {
  const text = input.trim()
  if (!/^\d+(\.\d+)?$/.test(text)) return null
  const [intPart, fracPart = ''] = text.split('.')
  const scale = Math.min(fracPart.length, MAX_SCALE)
  const frac = fracPart.slice(0, MAX_SCALE).padEnd(MAX_SCALE, '0')
  return { value: BigInt(intPart + frac), scale }
}

function toScale(parsed: { value: bigint; scale: number }, target: number): bigint {
  const delta = target - parsed.scale
  return delta >= 0 ? parsed.value * 10n ** BigInt(delta) : parsed.value
}

/** 比较两个十进制字符串：-1 / 0 / 1。非法输入抛错。 */
export function compareDecimals(a: string, b: string): -1 | 0 | 1 {
  const pa = parse(a),
    pb = parse(b)
  if (!pa || !pb) throw new Error(`非法的数值：${!pa ? a : b}`)
  const scale = Math.max(pa.scale, pb.scale)
  const va = toScale(pa, scale),
    vb = toScale(pb, scale)
  return va < vb ? -1 : va > vb ? 1 : 0
}

/** 十进制加法，返回保留两位小数的字符串。 */
export function addDecimals(a: string, b: string): string {
  return formatDecimal(sumDecimals([a, b]))
}

/** 求和（空列表得 0.00）。 */
export function sumDecimals(list: string[]): string {
  let total = 0n
  let scale = 0
  const parsed = list.map((item) => {
    const p = parse(item)
    if (!p) throw new Error(`非法的数值：${item}`)
    scale = Math.max(scale, p.scale)
    return p
  })
  for (const p of parsed) total += toScale(p, scale)
  return formatFixed(total, scale)
}

/** 格式化为两位小数（评分展示口径），非法输入抛错。 */
export function formatDecimal(input: string): string {
  const p = parse(input)
  if (!p) throw new Error(`非法的数值：${input}`)
  return formatFixed(p.value, p.scale)
}

function formatFixed(value: bigint, scale: number): string {
  const target = 2
  const adjusted =
    scale >= target ? value / 10n ** BigInt(scale - target) : value * 10n ** BigInt(target - scale)
  const sign = adjusted < 0n ? '-' : ''
  const digits = (adjusted < 0n ? -adjusted : adjusted).toString().padStart(target + 1, '0')
  return `${sign}${digits.slice(0, digits.length - target)}.${digits.slice(digits.length - target)}`
}

/** 校验评分输入：合法数值且不超过上限。返回错误文案或空串。 */
export function validateScoreInput(input: string, max: string): string {
  const text = input.trim()
  if (!text) return '请填写分数'
  if (!isValidDecimal(text)) return '分数应为不超过 4 位小数的非负数值'
  try {
    if (compareDecimals(text, max) > 0) return `不能超过该项上限 ${formatDecimal(max)} 分`
  } catch {
    return '分数格式不正确'
  }
  return ''
}
