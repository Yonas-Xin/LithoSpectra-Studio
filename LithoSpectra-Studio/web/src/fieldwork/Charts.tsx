// 面板里的两张小图：轨迹高程剖面、链接光谱的曲线。都是纯 SVG，跟着主题变量走。
// 要论文样式出图，用光谱模块的曲线窗口；这里只求一眼看清形状。

import { useMemo, useState } from 'react'

const W = 480
const H = 150
const PAD = { l: 44, r: 10, t: 10, b: 24 }

function ticks(lo: number, hi: number, n = 5): number[] {
  if (!(hi > lo)) return [lo]
  const raw = (hi - lo) / n
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const out = []
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Number(v.toPrecision(12)))
  return out
}

function Axes(props: { x0: number; x1: number; y0: number; y1: number; xFmt: (v: number) => string; yFmt: (v: number) => string }) {
  const { x0, x1, y0, y1 } = props
  const sx = (v: number) => PAD.l + ((v - x0) / (x1 - x0 || 1)) * (W - PAD.l - PAD.r)
  const sy = (v: number) => H - PAD.b - ((v - y0) / (y1 - y0 || 1)) * (H - PAD.t - PAD.b)
  return (
    <g className="fw-axes">
      {ticks(y0, y1, 4).map((v) => (
        <g key={'y' + v}>
          <line x1={PAD.l} x2={W - PAD.r} y1={sy(v)} y2={sy(v)} className="fw-grid" />
          <text x={PAD.l - 4} y={sy(v) + 3} textAnchor="end">{props.yFmt(v)}</text>
        </g>
      ))}
      {ticks(x0, x1, 6).map((v) => (
        <text key={'x' + v} x={sx(v)} y={H - 6} textAnchor="middle">{props.xFmt(v)}</text>
      ))}
      <line x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b} className="fw-axis" />
    </g>
  )
}

/** 高程对累计距离。坐标是 [经度, 纬度, 高程, 时间]。鼠标移上去读数，并把位置报给 onHover（画布上标出来）。 */
export function ElevationProfile({ coords, onHover }: {
  coords: [number, number, number, number][]
  onHover?: (lonlat: [number, number] | null) => void
}) {
  const [hover, setHover] = useState<number | null>(null)
  const data = useMemo(() => {
    const R = 6371008.8
    let d = 0
    const out: { km: number; z: number; ll: [number, number] }[] = []
    coords.forEach((c, i) => {
      if (i) {
        const [lo1, la1] = coords[i - 1]
        const p1 = (la1 * Math.PI) / 180
        const p2 = (c[1] * Math.PI) / 180
        const dp = p2 - p1
        const dl = ((c[0] - lo1) * Math.PI) / 180
        const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2
        d += 2 * R * Math.asin(Math.sqrt(a))
      }
      out.push({ km: d / 1000, z: c[2], ll: [c[0], c[1]] })
    })
    // 点太多时抽稀，SVG 路径不必上万个点
    const step = Math.max(1, Math.floor(out.length / 800))
    return out.filter((_, i) => i % step === 0 || i === out.length - 1)
  }, [coords])
  if (data.length < 2) return null
  const xs = data.map((p) => p.km)
  const ys = data.map((p) => p.z)
  const x0 = 0
  const x1 = Math.max(...xs)
  const y0 = Math.floor(Math.min(...ys) / 50) * 50
  const y1 = Math.ceil(Math.max(...ys) / 50) * 50
  const sx = (v: number) => PAD.l + ((v - x0) / (x1 - x0 || 1)) * (W - PAD.l - PAD.r)
  const sy = (v: number) => H - PAD.b - ((v - y0) / (y1 - y0 || 1)) * (H - PAD.t - PAD.b)
  const path = data.map((p, i) => `${i ? 'L' : 'M'}${sx(p.km).toFixed(1)},${sy(p.z).toFixed(1)}`).join('')
  const cur = hover != null ? data[hover] : null
  const leave = () => {
    setHover(null)
    onHover?.(null)
  }
  return (
    <div className="fw-chart-wrap">
      <svg className="fw-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect()
          const km = x0 + ((((e.clientX - r.left) / r.width) * W - PAD.l) / (W - PAD.l - PAD.r)) * (x1 - x0)
          if (km < x0 || km > x1) return leave()
          let best = 0
          data.forEach((p, i) => { if (Math.abs(p.km - km) < Math.abs(data[best].km - km)) best = i })
          if (best === hover) return
          setHover(best)
          onHover?.(data[best].ll)
        }}
        onMouseLeave={leave}>
        <Axes x0={x0} x1={x1} y0={y0} y1={y1} xFmt={(v) => `${v} km`} yFmt={(v) => `${v}`} />
        <path d={path} className="fw-line" />
        {cur ? <line x1={sx(cur.km)} x2={sx(cur.km)} y1={PAD.t} y2={H - PAD.b} className="fw-cursor" /> : null}
      </svg>
      <div className="fw-readout mono">
        {cur ? `${cur.km.toFixed(2)} km  高程 ${Math.round(cur.z)} m` : '距离 km · 高程 m；鼠标移到图上，画布中标出对应位置'}
      </div>
    </div>
  )
}

export interface ChartCurve {
  id: string
  label: string
  x: number[]
  y: (number | null)[]
  color: string
}

/** 反射率曲线（波长 nm）。鼠标移上去读数。 */
export function SpectrumChart({ curves, height = H }: { curves: ChartCurve[]; height?: number }) {
  const [hover, setHover] = useState<number | null>(null)
  const all = curves.flatMap((c) => c.x.map((x, i) => [x, c.y[i]] as [number, number | null]))
  const valid = all.filter((p) => p[1] != null) as [number, number][]
  if (!valid.length) return <div className="caption">无可显示的数据。</div>
  const x0 = Math.min(...valid.map((p) => p[0]))
  const x1 = Math.max(...valid.map((p) => p[0]))
  const y0 = 0
  const y1 = Math.max(0.1, Math.ceil(Math.max(...valid.map((p) => p[1])) * 10) / 10)
  const h = height
  const sx = (v: number) => PAD.l + ((v - x0) / (x1 - x0 || 1)) * (W - PAD.l - PAD.r)
  const sy = (v: number) => h - PAD.b - ((v - y0) / (y1 - y0 || 1)) * (h - PAD.t - PAD.b)
  const pathOf = (c: ChartCurve) => {
    let d = ''
    let pen = false
    c.x.forEach((x, i) => {
      const y = c.y[i]
      if (y == null) { pen = false; return }
      d += `${pen ? 'L' : 'M'}${sx(x).toFixed(1)},${sy(y).toFixed(1)}`
      pen = true
    })
    return d
  }
  const readout = hover != null ? curves.map((c) => {
    let best = 0
    c.x.forEach((x, i) => { if (Math.abs(x - hover) < Math.abs(c.x[best] - hover)) best = i })
    return { c, v: c.y[best] }
  }) : []
  return (
    <div className="fw-chart-wrap">
      <svg className="fw-chart" viewBox={`0 0 ${W} ${h}`} preserveAspectRatio="none"
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect()
          const px = ((e.clientX - r.left) / r.width) * W
          const v = x0 + ((px - PAD.l) / (W - PAD.l - PAD.r)) * (x1 - x0)
          setHover(v >= x0 && v <= x1 ? v : null)
        }}
        onMouseLeave={() => setHover(null)}>
        <g className="fw-axes" style={{ fontSize: 10 }}>
          {ticks(y0, y1, 4).map((v) => (
            <g key={'y' + v}>
              <line x1={PAD.l} x2={W - PAD.r} y1={sy(v)} y2={sy(v)} className="fw-grid" />
              <text x={PAD.l - 4} y={sy(v) + 3} textAnchor="end">{v.toFixed(2)}</text>
            </g>
          ))}
          {ticks(x0, x1, 6).map((v) => <text key={'x' + v} x={sx(v)} y={h - 6} textAnchor="middle">{v}</text>)}
          <line x1={PAD.l} x2={W - PAD.r} y1={h - PAD.b} y2={h - PAD.b} className="fw-axis" />
        </g>
        {curves.map((c) => <path key={c.id} d={pathOf(c)} className="fw-curve" style={{ stroke: c.color }} />)}
        {hover != null ? <line x1={sx(hover)} x2={sx(hover)} y1={PAD.t} y2={h - PAD.b} className="fw-cursor" /> : null}
      </svg>
      <div className="fw-readout mono">
        {hover != null
          ? `${hover.toFixed(0)} nm  ` + readout.map((r) => `${r.c.label}: ${r.v == null ? '—' : r.v.toFixed(4)}`).join('  ')
          : '波长 nm · 反射率'}
      </div>
    </div>
  )
}
