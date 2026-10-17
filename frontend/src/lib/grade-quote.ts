import type { Region, Schema } from '../api/client'
/** Canonical offsets are not GradeInput offsets; only use a returned, verified mapping. */
export function mapGradeQuote(
  region: Region,
  texts: ReadonlyMap<string, string>,
  locations: Schema['GradeEvidenceLocation'][],
): Schema['QuoteEvidenceReference'] {
  const start = region.text_start,
    end = region.text_end,
    text = texts.get(region.content_pointer)
  if (
    start == null ||
    end == null ||
    text === undefined ||
    start < 0 ||
    end <= start ||
    end > Array.from(text).length
  )
    throw new Error('引文范围无效。')
  const location = locations.find(
    (e) =>
      e.content_pointer === region.content_pointer &&
      start >= e.canonical_text_start &&
      end <= e.canonical_text_end,
  )
  if (!location) throw new Error('此片段缺少可验证的评分输入映射，请在已定位的候选引文范围内选择。')
  const quote = Array.from(text).slice(start, end).join('')
  if (Array.from(quote).length > 2000) throw new Error('单条引文不能超过2000个字符，请缩小选区。')
  return {
    kind: 'quote',
    segment_index: location.segment_index,
    text_start: location.text_start + start - location.canonical_text_start,
    text_end: location.text_start + end - location.canonical_text_start,
    quote,
  }
}
