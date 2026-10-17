/**
 * 运行模式开关。
 *
 * `__HP_DEMO__` 由 vite.config.ts 在构建期注入：仅 `npm run dev:demo`
 * （vite --mode demo）为 true；`npm run dev` 与 `npm run build` 均为 false，
 * 演示数据服务的所有分支在构建期即被消除，不进入产物。
 */
export const DEMO_MODE = __HP_DEMO__

export function assertRealMode(feature: string): void {
  if (DEMO_MODE) throw new Error(`演示模式不支持真实功能：${feature}`)
}
