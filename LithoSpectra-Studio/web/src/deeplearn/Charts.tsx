// 训练监控的图：指标曲线与混淆矩阵。纯 SVG，颜色只取主题变量。

import { useMemo, useState } from 'react'
import type { ClassRow } from './dlapi'

const PAD = { l: 44, r: 12, t: 10, b: 24 }

function ticks(lo: number, hi: number, n = 5): number[] {
  if (!(hi > lo)) return [lo]
  const raw = (hi - lo) / n
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const out = []
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Number(v.toPrecision(12)))
  return out
}

export interface Series {
  name: string
  color: string
  points: [number, number][]
  dashed?: boolean
}

/** 多条折线，横轴为轮次。悬停显示该轮各曲线的取值；``mark`` 标出最佳一轮。 */
export function LineChart(props: {
  series: Series[]; height?: number; width?: number; yFmt?: (v: number) => string
  yRange?: [number, number]; mark?: number | null; title: string
}) {
  const W = props.width ?? 420
  const H = props.height ?? 170
  const [hover, setHover] = useState<number | null>(null)
  const all = props.series.flatMap((s) => s.points)
  const xs = all.map((p) => p[0])
  const ys = all.map((p) => p[1]).filter((v) => isFinite(v))
  const x0 = xs.length ? Math.min(...xs) : 0
  const x1 = xs.length ? Math.max(...xs, x0 + 1) : 1
  let [y0, y1] = props.yRange ?? [ys.length ? Math.min(...ys) : 0, ys.length ? Math.max(...ys) : 1]
  if (!props.yRange) {
    const span = y1 - y0 || Math.abs(y1) || 1
    y0 -= span * 0.05
    y1 += span * 0.05
  }
  const sx = (v: number) => PAD.l + ((v - x0) / (x1 - x0 || 1)) * (W - PAD.l - PAD.r)
  const sy = (v: number) => H - PAD.b - ((v - y0) / (y1 - y0 || 1)) * (H - PAD.t - PAD.b)
  const yTicks = ticks(y0, y1, 4)
  // 刻度按步长取同样的小数位（0.00、0.05、0.10），悬停读数多给一位有效数字
  const step = yTicks.length > 1 ? yTicks[1] - yTicks[0] : 0
  let digits = 0
  while (digits < 6 && step > 0 && Math.abs(Math.round(step * 10 ** digits) - step * 10 ** digits) > 1e-6) digits++
  const tickFmt = props.yFmt ?? ((v: number) => v.toFixed(digits))
  const fmt = props.yFmt ?? ((v: number) => (Math.abs(v) >= 100 ? v.toFixed(0) : v !== 0 && Math.abs(v) < 1e-3 ? v.toExponential(2) : v.toPrecision(4)))
  const xTicks = ticks(x0, x1, 6).filter((v) => Number.isInteger(v))
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const x = ((e.clientX - box.left) / box.width) * W
    const v = x0 + ((x - PAD.l) / (W - PAD.l - PAD.r)) * (x1 - x0)
    setHover(Math.round(Math.max(x0, Math.min(x1, v))))
  }
  return (
    <div className="dl-chart">
      <div className="dl-chart-head">
        <span className="dl-chart-title">{props.title}</span>
        <span className="dl-legend">
          {props.series.map((s) => (
            <span key={s.name}><i style={{ background: s.color, opacity: s.dashed ? 0.6 : 1 }} />{s.name}</span>
          ))}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="dl-svg" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <g className="dl-axes">
          {yTicks.map((v) => (
            <g key={'y' + v}>
              <line x1={PAD.l} x2={W - PAD.r} y1={sy(v)} y2={sy(v)} className="dl-gridline" />
              <text x={PAD.l - 4} y={sy(v) + 3} textAnchor="end">{tickFmt(v)}</text>
            </g>
          ))}
          {xTicks.map((v) => <text key={'x' + v} x={sx(v)} y={H - 6} textAnchor="middle">{v}</text>)}
          <line x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b} className="dl-axis" />
        </g>
        {props.mark ? <line x1={sx(props.mark)} x2={sx(props.mark)} y1={PAD.t} y2={H - PAD.b} className="dl-mark" /> : null}
        {props.series.map((s) => (
          <g key={s.name}>
            <polyline fill="none" stroke={s.color} strokeWidth={1.6} strokeDasharray={s.dashed ? '4 3' : undefined}
              points={s.points.filter((p) => isFinite(p[1])).map((p) => `${sx(p[0])},${sy(p[1])}`).join(' ')} />
            {s.points.length <= 40 ? s.points.filter((p) => isFinite(p[1])).map((p) => (
              <circle key={p[0]} cx={sx(p[0])} cy={sy(p[1])} r={1.8} fill={s.color} />
            )) : null}
          </g>
        ))}
        {hover !== null && all.length ? (
          <g>
            <line x1={sx(hover)} x2={sx(hover)} y1={PAD.t} y2={H - PAD.b} className="dl-hover" />
            <text x={Math.min(sx(hover) + 6, W - 140)} y={PAD.t + 10} className="dl-tip">
              <tspan>第 {hover} 轮</tspan>
              {props.series.map((s) => {
                const p = s.points.find((q) => q[0] === hover)
                return p ? <tspan key={s.name} x={Math.min(sx(hover) + 6, W - 140)} dy={13} fill={s.color}>{s.name} {fmt(p[1])}</tspan> : null
              })}
            </text>
          </g>
        ) : null}
      </svg>
    </div>
  )
}

/** 混淆矩阵：行为真值，列为预测，颜色按行归一化（各类的召回分布）。单击单元格回调。 */
export function ConfusionMatrix(props: {
  matrix: number[][]; classes: ClassRow[]; selected?: [number, number] | null
  onPick?: (truth: number, pred: number) => void
}) {
  const n = props.matrix.length
  const cell = n > 16 ? 18 : n > 10 ? 24 : 32
  const label = 96
  const names = useMemo(() => props.matrix.map((_, i) => props.classes.find((c) => c.label === i)?.name ?? String(i)), [props.matrix, props.classes])
  const W = label + n * cell + 4
  const H = label + n * cell + 4
  return (
    <div className="dl-cm-wrap">
      <svg width={W} height={H} className="dl-cm">
        {names.map((nm, j) => (
          <text key={'c' + j} transform={`translate(${label + j * cell + cell / 2 + 3},${label - 4}) rotate(-50)`} className="dl-cm-name">{nm.length > 10 ? nm.slice(0, 9) + '…' : nm}</text>
        ))}
        {props.matrix.map((row, i) => {
          const total = row.reduce((a, b) => a + b, 0) || 1
          return (
            <g key={'r' + i}>
              <text x={label - 6} y={label + i * cell + cell / 2 + 4} textAnchor="end" className="dl-cm-name">{names[i].length > 10 ? names[i].slice(0, 9) + '…' : names[i]}</text>
              {row.map((v, j) => {
                const frac = v / total
                const diag = i === j
                const sel = props.selected && props.selected[0] === i && props.selected[1] === j
                return (
                  <g key={j} className="dl-cm-cell" onClick={() => props.onPick?.(i, j)}>
                    <rect x={label + j * cell} y={label + i * cell} width={cell - 2} height={cell - 2} rx={3}
                      className={diag ? 'diag' : 'off'} style={{ fillOpacity: v ? 0.12 + 0.88 * frac : 0.04 }}
                      stroke={sel ? 'var(--text)' : 'none'} strokeWidth={sel ? 2 : 0}>
                      <title>{`真值 ${names[i]} → 预测 ${names[j]}：${v}（${(frac * 100).toFixed(1)}%）`}</title>
                    </rect>
                    {cell >= 24 && v ? (
                      <text x={label + j * cell + (cell - 2) / 2} y={label + i * cell + cell / 2 + 3} textAnchor="middle"
                        className={'dl-cm-num' + (frac > 0.55 ? ' on' : '')}>{v > 999 ? `${(v / 1000).toFixed(1)}k` : v}</text>
                    ) : null}
                  </g>
                )
              })}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
