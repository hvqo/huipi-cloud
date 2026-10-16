import type { Region } from '../api/client'
export function utf16ToCodePoint(text: string, offset: number): number {
  if (!Number.isInteger(offset) || offset < 0 || offset > text.length)
    throw new Error('选区超出文本范围。')
  if (
    offset > 0 &&
    offset < text.length &&
    /[\uD800-\uDBFF]/.test(text[offset - 1]!) &&
    /[\uDC00-\uDFFF]/.test(text[offset]!)
  )
    throw new Error('选区不能截断一个 Unicode 字符。')
  return Array.from(text.slice(0, offset)).length
}
function host(node: globalThis.Node | null): HTMLElement | null {
  return (node instanceof Element ? node : node?.parentElement)?.closest(
    '[data-source-pointer]',
  ) as HTMLElement | null
}
export function selectedRegion(
  selection: Selection | null,
  source: ReadonlyMap<string, string>,
): Region {
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed)
    throw new Error('请先在来源文本中选取证据。')
  const range = selection.getRangeAt(0)
  const start = host(range.startContainer),
    end = host(range.endContainer)
  if (!start || start !== end) throw new Error('一次只能选择一个来源节点，请分次添加多个区域。')
  const pointer = start.dataset.sourcePointer!
  const text = source.get(pointer)
  if (text === undefined || start.textContent !== text)
    throw new Error('显示内容与原文不一致，请刷新后重新选择。')
  const before = range.cloneRange()
  before.selectNodeContents(start)
  before.setEnd(range.startContainer, range.startOffset)
  const through = range.cloneRange()
  through.selectNodeContents(start)
  through.setEnd(range.endContainer, range.endOffset)
  const a = utf16ToCodePoint(text, before.toString().length),
    b = utf16ToCodePoint(text, through.toString().length)
  if (b <= a || Array.from(text).slice(a, b).join('') !== range.toString())
    throw new Error('选区无法准确映射到来源，请重新选择。')
  return { content_pointer: pointer, text_start: a, text_end: b }
}
export function addRegion(prior: Region[], next: Region) {
  if (prior.length >= 100) throw new Error('最多选择 100 个证据区域。')
  if (
    prior.some(
      (r) =>
        r.content_pointer === next.content_pointer &&
        (r.text_start == null ||
          next.text_start == null ||
          Math.max(r.text_start, next.text_start) < Math.min(r.text_end!, next.text_end!)),
    )
  )
    throw new Error('证据区域重复或重叠，请保留独立范围。')
  return [...prior, next]
}
