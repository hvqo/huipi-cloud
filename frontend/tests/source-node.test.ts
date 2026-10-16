import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import SourceNode from '../src/components/SourceNode.vue'
import type { Node } from '../src/api/client'
const node = (value: string): Node => ({
  normalized_type: 'text',
  source_type: 'paragraph',
  content_kind: 'scalar',
  value,
  children: [],
  asset_refs: [],
})
it('renders source HTML and external assets as inert text and metadata', () => {
  const input = node('<img src="https://example.invalid/private" onerror="alert(1)">学生文字')
  input.asset_refs = [{ kind: 'external', uri: 'https://example.invalid/private' }]
  const wrapper = mount(SourceNode, {
    props: {
      node: input,
      pointer: '/p',
      related: new Set<string>(),
      disabled: false,
      confirmed: [],
    },
    global: { stubs: { ElButton: true } },
  })
  expect(wrapper.find('img').exists()).toBe(false)
  expect(wrapper.find('a').exists()).toBe(false)
  expect(wrapper.get('[data-source-pointer]').element.textContent).toBe(input.value)
  wrapper.unmount()
})
it('highlights confirmed code-point ranges without changing the canonical text', () => {
  const input = node('题😀𠀀答案 é')
  const wrapper = mount(SourceNode, {
    props: {
      node: input,
      pointer: '/p',
      related: new Set<string>(),
      disabled: false,
      confirmed: [
        {
          role: 'response_regions',
          content_pointer: '/p',
          page_number: 1,
          text_start: 1,
          text_end: 3,
        },
      ],
    },
    global: { stubs: { ElButton: true } },
  })
  expect(wrapper.get('mark').text()).toBe('😀𠀀')
  expect(wrapper.get('[data-source-pointer]').element.textContent).toBe(input.value)
  wrapper.unmount()
})
