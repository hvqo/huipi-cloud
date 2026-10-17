/**
 * 运行模式开关。
 *
 * 演示模式只能通过 `npm run dev:demo`（vite --mode demo）显式启用：
 * `.env.demo` 仅在该模式下加载，`npm run build`（mode=production）时
 * VITE_DEMO_MODE 为 undefined，演示数据服务不会进入产物。
 */
export const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === 'true'

export function assertRealMode(feature: string): void {
  if (DEMO_MODE) throw new Error(`演示模式不支持真实功能：${feature}`)
}
