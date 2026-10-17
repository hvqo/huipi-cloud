import type { S } from '../types'
import type { DemoCanonical } from './state'

/** Canonical 节点与页面的构建工具（种子数据与演示上传解析共用）。 */

export interface ScriptEntry {
  qid: string
  number: number
  stem: string
  answer?: string
  imageAsset?: string
}

export function textNode(value: string): S['CanonicalContentNode'] {
  return {
    normalized_type: 'text',
    source_type: 'demo_text',
    source_index: 0,
    bbox: null,
    content_kind: 'scalar',
    value,
    children: [],
    asset_refs: [],
  }
}

export function imageNode(assetId: string): S['CanonicalContentNode'] {
  return {
    normalized_type: 'image',
    source_type: 'demo_image',
    source_index: 0,
    bbox: [120, 420, 480, 700],
    content_kind: 'scalar',
    value: null,
    children: [],
    asset_refs: [
      {
        kind: 'stored',
        asset_id: assetId,
        uri: null,
        sha256: `asset-${assetId}`,
        size_bytes: 38_214,
        content_type: 'image/png',
        source_pointer: null,
        source_encoding: null,
        source_occurrence_index: 0,
      },
    ],
  }
}

/** 每页 3 题：题干块 + 作答块（+ 可选手写图块）。 */
export function buildCanonical(
  sid: string,
  script: ScriptEntry[],
): {
  canonical: DemoCanonical
  answerPointers: Record<string, { pointer: string; page: number; text: string }>
  stemPointers: Record<string, string>
} {
  const documentId = `cdoc-${sid}`
  const pages: S['CanonicalPageRead'][] = []
  const pointers: DemoCanonical['pointers'] = {}
  const answerPointers: Record<string, { pointer: string; page: number; text: string }> = {}
  const stemPointers: Record<string, string> = {}
  const perPage = 3
  const pageCount = Math.ceil(script.length / perPage)
  let blockCount = 0
  for (let pageIndex = 0; pageIndex < pageCount; pageIndex++) {
    let blockIndex = 0
    const blocks: S['CanonicalBlock'][] = []
    const entries = script.slice(pageIndex * perPage, (pageIndex + 1) * perPage)
    for (const entry of entries) {
      const stemPointer = `/pages/${pageIndex}/blocks/${blockIndex}/content`
      const stemBlock: S['CanonicalBlock'] = {
        block_id: `blk-${sid}-${pageIndex}-${blockIndex}`,
        normalized_type: 'text',
        reading_order: blockIndex,
        page_index: pageIndex,
        page_number: pageIndex + 1,
        source_page_idx: pageIndex,
        source_block_index: blockIndex,
        source_type: 'demo_text',
        bbox: [64, 90, 730, 150],
        content: textNode(entry.stem),
      }
      blocks.push(stemBlock)
      pointers[stemPointer] = {
        page_number: pageIndex + 1,
        block_id: stemBlock.block_id,
        normalized_type: 'text',
        text: entry.stem,
      }
      stemPointers[entry.qid] = stemPointer
      blockIndex++
      blockCount++
      if (entry.answer != null) {
        const answerPointer = `/pages/${pageIndex}/blocks/${blockIndex}/content`
        const answerBlock: S['CanonicalBlock'] = {
          block_id: `blk-${sid}-${pageIndex}-${blockIndex}`,
          normalized_type: 'text',
          reading_order: blockIndex,
          page_index: pageIndex,
          page_number: pageIndex + 1,
          source_page_idx: pageIndex,
          source_block_index: blockIndex,
          source_type: 'demo_text',
          bbox: [64, 150, 730, 260],
          content: textNode(entry.answer),
        }
        blocks.push(answerBlock)
        pointers[answerPointer] = {
          page_number: pageIndex + 1,
          block_id: answerBlock.block_id,
          normalized_type: 'text',
          text: entry.answer,
        }
        answerPointers[entry.qid] = {
          pointer: answerPointer,
          page: pageIndex + 1,
          text: entry.answer,
        }
        blockIndex++
        blockCount++
      }
      if (entry.imageAsset) {
        const imagePointer = `/pages/${pageIndex}/blocks/${blockIndex}/content`
        const imageBlock: S['CanonicalBlock'] = {
          block_id: `blk-${sid}-${pageIndex}-${blockIndex}`,
          normalized_type: 'image',
          reading_order: blockIndex,
          page_index: pageIndex,
          page_number: pageIndex + 1,
          source_page_idx: pageIndex,
          source_block_index: blockIndex,
          source_type: 'demo_image',
          bbox: [120, 420, 480, 700],
          content: imageNode(entry.imageAsset),
        }
        blocks.push(imageBlock)
        pointers[imagePointer] = {
          page_number: pageIndex + 1,
          block_id: imageBlock.block_id,
          normalized_type: 'image',
          text: null,
        }
        blockIndex++
        blockCount++
      }
    }
    pages.push({
      document_id: documentId,
      submission_id: sid,
      source_artifact_id: `art-${sid}`,
      schema_version: 'demo-v1',
      bbox_coordinate_space: 'page_normalized',
      page: {
        page_index: pageIndex,
        page_number: pageIndex + 1,
        source_page_idx: pageIndex,
        blocks,
      },
    })
  }
  const canonical: DemoCanonical = {
    document_id: documentId,
    source_artifact_id: `art-${sid}`,
    canonical_sha256: `sha-c-${sid}`,
    aligner_version: 'demo-aligner-1.2',
    alignment_artifact_id: `aln-${sid}`,
    alignment_sha256: `sha-a-${sid}`,
    questions_digest: `sha-q-${sid}`,
    page_count: pageCount,
    block_count: blockCount,
    pages,
    aligned: {},
    pointers,
  }
  return { canonical, answerPointers, stemPointers }
}
