import { beforeEach, describe, expect, it } from 'vitest'
import { addRegion, selectedRegion, utf16ToCodePoint } from '../src/lib/selection'

describe('Unicode code point half-open selections', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    window.getSelection()?.removeAllRanges()
  })
  it.each(['中文', 'ASCII', '😀𠀀', 'e\u0301', '行一\n行二', '中A😀𠀀e\u0301\n末'])(
    'counts %s without UTF-8 or UTF-16 confusion',
    (text) => {
      expect(utf16ToCodePoint(text, text.length)).toBe(Array.from(text).length)
    },
  )
  it('rejects half a surrogate and invalid offsets', () => {
    expect(() => utf16ToCodePoint('😀', 1)).toThrow()
    for (const offset of [-1, 3, 0.5]) expect(() => utf16ToCodePoint('😀', offset)).toThrow()
  })
  it('maps an actual DOM Range across TextNodes inside one Canonical node', () => {
    const el = document.createElement('div')
    el.dataset.sourcePointer = '/pages/0/blocks/0/content'
    el.innerHTML = '<span>题干😀</span><span>学生𠀀é\n作答</span>'
    document.body.append(el)
    const text = el.textContent!,
      first = el.children[0]!.firstChild!,
      last = el.children[1]!.firstChild!
    const range = document.createRange()
    range.setStart(first, 2)
    range.setEnd(last, 7)
    const selection = window.getSelection()!
    selection.addRange(range)
    const result = selectedRegion(selection, new Map([[el.dataset.sourcePointer, text]]))
    expect(result.text_start).toBe(2)
    expect(Array.from(text).slice(result.text_start!, result.text_end!).join('')).toBe(
      range.toString(),
    )
    expect(result.text_end).toBe(9)
  })
  it('does not merge separate ContentNodes, even with identical text', () => {
    document.body.innerHTML =
      '<span data-source-pointer="/p/0">学生作答</span><span data-source-pointer="/p/1">学生作答</span>'
    const range = document.createRange()
    range.setStart(document.body.children[0]!.firstChild!, 0)
    range.setEnd(document.body.children[1]!.firstChild!, 4)
    window.getSelection()!.addRange(range)
    expect(() =>
      selectedRegion(
        window.getSelection(),
        new Map([
          ['/p/0', '学生作答'],
          ['/p/1', '学生作答'],
        ]),
      ),
    ).toThrow('一个来源节点')
  })
  it('rejects stale rendered text instead of accepting a truncated answer', () => {
    document.body.innerHTML = '<span data-source-pointer="/p">已变化文本</span>'
    const range = document.createRange()
    range.selectNodeContents(document.body.firstChild!)
    window.getSelection()!.addRange(range)
    expect(() => selectedRegion(window.getSelection(), new Map([['/p', '原始文本']]))).toThrow(
      '不一致',
    )
  })
  it('allows adjacent and independent regions but rejects overlaps', () => {
    const prior = [{ content_pointer: '/a', text_start: 0, text_end: 2 }]
    expect(addRegion(prior, { content_pointer: '/a', text_start: 2, text_end: 4 })).toHaveLength(2)
    expect(addRegion(prior, { content_pointer: '/b', text_start: 0, text_end: 2 })).toHaveLength(2)
    expect(() => addRegion(prior, { content_pointer: '/a', text_start: 1, text_end: 3 })).toThrow(
      '重叠',
    )
  })
})
