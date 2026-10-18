import { DEMO_MODE } from '../app/mode'
import { createRealServices } from './real'
import type { HuipiServices } from './types'

export * from './types'

/**
 * 数据访问层入口。页面统一通过 services() 获取业务服务，
 * 不直接写死合成 JSON，也不在真实接口失败时回退到演示数据。
 *
 * 真实 Provider 静态导入（仅包装既有 API client）；演示实现（./mock）
 * 通过动态 import 加载：真实模式与生产构建中 VITE_DEMO_MODE 为 false，
 * 该分支在构建期即被消除，演示代码不进入产物。
 */
let cached: Promise<HuipiServices> | null = null

async function load(): Promise<HuipiServices> {
  if (DEMO_MODE) {
    if (import.meta.env.PROD) {
      throw new Error('生产构建禁止加载演示数据服务。')
    }
    const { createMockServices } = await import('./mock')
    return createMockServices()
  }
  return createRealServices()
}

export function services(): Promise<HuipiServices> {
  cached ??= load()
  return cached
}
