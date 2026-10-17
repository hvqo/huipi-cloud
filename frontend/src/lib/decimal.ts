/** Decimal scoring uses hundredths and BigInt; never round or infer missing precision. */
const DECIMAL = /^\d+(?:\.\d{1,2})?$/
function cents(input: string): bigint {
  const value = input.trim()
  if (!DECIMAL.test(value) || value.length > 32) throw new Error('分数应为最多两位小数的非负数')
  const [whole, fraction = ''] = value.split('.')
  return BigInt(whole!) * 100n + BigInt(fraction.padEnd(2, '0'))
}
function fixed(value: bigint): string {
  return `${value / 100n}.${(value % 100n).toString().padStart(2, '0')}`
}
export function isValidDecimal(value: string): boolean {
  try {
    cents(value)
    return true
  } catch {
    return false
  }
}
export function compareDecimals(a: string, b: string): -1 | 0 | 1 {
  const x = cents(a),
    y = cents(b)
  return x < y ? -1 : x > y ? 1 : 0
}
export function sumDecimals(values: string[]): string {
  return fixed(values.reduce((sum, value) => sum + cents(value), 0n))
}
export function addDecimals(a: string, b: string): string {
  return sumDecimals([a, b])
}
export function formatDecimal(value: string): string {
  return fixed(cents(value))
}
export function validateScoreInput(value: string, maximum: string): string {
  if (!value.trim()) return '请填写分数'
  if (!isValidDecimal(value)) return '分数应为最多两位小数的非负数'
  return compareDecimals(value, maximum) > 0 ? `不能超过该项上限 ${formatDecimal(maximum)} 分` : ''
}
