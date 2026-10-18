/**
 * 演示模式的合成试卷图片。
 *
 * 通过 Canvas 绘制生成真实 PNG Blob，经与生产完全一致的
 * 认证读取 → Blob URL → <img> 预览管线展示；图片带“演示合成”水印，
 * 不会被误认为真实生产文件（真实模式仍读取受保护的学生原文件）。
 */

export interface SyntheticExamPage {
  title: string
  subject: string
  studentRef: string
  questions: { number: number; stem: string; answer?: string }[]
}

const PAGE_W = 1000
const PAGE_H = 1414

function wrapText(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  maxWidth: number,
  lineHeight: number,
): number {
  let lineY = y
  for (const paragraph of text.split('\n')) {
    let line = ''
    for (const char of paragraph) {
      const test = line + char
      if (ctx.measureText(test).width > maxWidth && line) {
        ctx.fillText(line, x, lineY)
        line = char
        lineY += lineHeight
      } else {
        line = test
      }
    }
    ctx.fillText(line, x, lineY)
    lineY += lineHeight
  }
  return lineY
}

export function renderExamPage(page: SyntheticExamPage): Promise<Blob> {
  const canvas = document.createElement('canvas')
  canvas.width = PAGE_W
  canvas.height = PAGE_H
  const ctx = canvas.getContext('2d')!

  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, PAGE_W, PAGE_H)

  // 页眉
  ctx.fillStyle = '#8b95a6'
  ctx.font = '18px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'
  ctx.textAlign = 'center'
  ctx.fillText('慧批实验中学 · 课后作业', PAGE_W / 2, 60)
  ctx.font = 'bold 26px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'
  ctx.fillStyle = '#22304a'
  ctx.fillText(page.title, PAGE_W / 2, 100)
  ctx.font = '16px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'
  ctx.fillStyle = '#8b95a6'
  ctx.fillText(`${page.subject} · 学生 ${page.studentRef}`, PAGE_W / 2, 128)
  ctx.strokeStyle = '#d7dde7'
  ctx.beginPath()
  ctx.moveTo(70, 148)
  ctx.lineTo(PAGE_W - 70, 148)
  ctx.stroke()

  // 题目与手写作答
  ctx.textAlign = 'left'
  let y = 200
  for (const question of page.questions) {
    ctx.fillStyle = '#22304a'
    ctx.font = '19px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'
    y = wrapText(ctx, `${question.stem}`, 80, y, PAGE_W - 160, 30)
    y += 8
    if (question.answer != null) {
      const boxTop = y - 6
      ctx.fillStyle = '#2c3e63'
      ctx.font = '21px "KaiTi", "AR PL UKai CN", "Noto Sans CJK SC", cursive'
      y = wrapText(ctx, question.answer, 110, y + 18, PAGE_W - 220, 34)
      ctx.setLineDash([6, 5])
      ctx.strokeStyle = '#c8d3e4'
      ctx.strokeRect(95, boxTop, PAGE_W - 190, y - boxTop + 12)
      ctx.setLineDash([])
      y += 26
    } else {
      ctx.fillStyle = '#b7c0cf'
      ctx.font = 'italic 17px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'
      ctx.fillText('（未作答）', 110, y + 14)
      y += 52
    }
    if (y > PAGE_H - 140) break
  }

  // 页脚与水印
  ctx.fillStyle = '#9aa4b4'
  ctx.font = '14px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'
  ctx.textAlign = 'center'
  ctx.fillText('第 1 页 · 共 1 页', PAGE_W / 2, PAGE_H - 40)
  ctx.save()
  ctx.globalAlpha = 0.07
  ctx.translate(PAGE_W / 2, PAGE_H / 2)
  ctx.rotate(-Math.PI / 7)
  ctx.font = 'bold 64px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'
  ctx.fillStyle = '#2e6be6'
  ctx.fillText('演示合成试卷', 0, 0)
  ctx.restore()

  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob)
      else reject(new Error('合成试卷生成失败（演示环境）。'))
    }, 'image/png')
  })
}

/** 手写辅助图（题目配图）占位。 */
export function renderHandDrawing(label: string): Promise<Blob> {
  const canvas = document.createElement('canvas')
  canvas.width = 480
  canvas.height = 320
  const ctx = canvas.getContext('2d')!
  ctx.fillStyle = '#fdfefe'
  ctx.fillRect(0, 0, 480, 320)
  ctx.strokeStyle = '#7f95c4'
  ctx.lineWidth = 2.5
  ctx.beginPath()
  ctx.moveTo(40, 280)
  ctx.bezierCurveTo(140, 40, 300, 300, 440, 90)
  ctx.stroke()
  ctx.strokeStyle = '#e0a63e'
  ctx.setLineDash([8, 6])
  ctx.beginPath()
  ctx.moveTo(180, 40)
  ctx.lineTo(180, 300)
  ctx.stroke()
  ctx.setLineDash([])
  ctx.fillStyle = '#54658a'
  ctx.font = '20px "KaiTi", "AR PL UKai CN", cursive'
  ctx.fillText(label, 36, 30)
  ctx.fillStyle = '#9aa4b4'
  ctx.font = '13px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'
  ctx.fillText('演示合成手绘图', 36, 306)
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob)
      else reject(new Error('合成手绘图生成失败（演示环境）。'))
    }, 'image/png')
  })
}
