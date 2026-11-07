// 照片编辑器（嵌在照片窗口里）：裁剪、旋转、翻转、拉直、调色、标注。
//
// 页面上只改参数、看预览：预览是缩小的底图按同一套变换画在 canvas 上，调色用 SVG 的
// feColorMatrix 套与后端相同的颜色矩阵。保存时把参数交给后端 photo_edit.py 按原图
// 分辨率重做一遍，结果作为一步调整，原片不动。处理顺序（与后端一致）：
//   翻转 → 顺时针转 90° 的倍数 → 拉直（绕中心转并放大到填满）→ 调色 → 标注 → 裁剪
// 标注与裁剪框的坐标都是「画幅」（转正、拉直之后）的比例 0~1。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { App as AntApp, Button, InputNumber, Segmented, Slider, Tooltip } from 'antd'

import type { TaskInfo } from '../api'
import { Icon } from '../theme'
import { fw, photoUrl } from './fwapi'
import type { FwPhoto } from './fwapi'

type Tab = 'crop' | 'color' | 'mark'
type AnnoType = 'arrow' | 'line' | 'rect' | 'ellipse' | 'text'
interface Anno { id: number; type: AnnoType; pts: [number, number][]; color: string; width: number; text?: string; size?: number }
type Rect = [number, number, number, number]
interface Color { exposure: number; contrast: number; saturation: number; warmth: number }

const NO_COLOR: Color = { exposure: 0, contrast: 0, saturation: 0, warmth: 0 }
const FULL: Rect = [0, 0, 1, 1]

const ASPECTS: { key: string; label: string; ratio: number | 'orig' | null }[] = [
  { key: 'free', label: '自由', ratio: null },
  { key: 'orig', label: '原始', ratio: 'orig' },
  { key: '1:1', label: '1:1', ratio: 1 },
  { key: '4:3', label: '4:3', ratio: 4 / 3 },
  { key: '3:4', label: '3:4', ratio: 3 / 4 },
  { key: '3:2', label: '3:2', ratio: 3 / 2 },
  { key: '2:3', label: '2:3', ratio: 2 / 3 },
  { key: '16:9', label: '16:9', ratio: 16 / 9 },
]

const COLOR_ITEMS: { key: keyof Color; label: string; hint: string }[] = [
  { key: 'exposure', label: '曝光', hint: '整体明暗，±1 档' },
  { key: 'contrast', label: '对比度', hint: '' },
  { key: 'saturation', label: '饱和度', hint: '' },
  { key: 'warmth', label: '色温', hint: '向右偏暖，向左偏冷' },
]

const ANNO_TOOLS: { key: AnnoType; label: string }[] = [
  { key: 'arrow', label: '箭头' }, { key: 'line', label: '直线' }, { key: 'rect', label: '矩形' },
  { key: 'ellipse', label: '椭圆' }, { key: 'text', label: '文字' },
]
const SWATCHES = ['#E53935', '#FFD400', '#FFFFFF', '#111111', '#1E88E5', '#43A047']

/** 与后端 photo_edit.color_matrix 同一算法：返回 3×4 矩阵（行优先） */
function colorMatrix(c: Color): number[][] {
  const e = 2 ** (c.exposure / 100)
  const k = 1 + c.contrast / 100
  const s = 1 + c.saturation / 100
  const w = c.warmth / 100
  const L = [0.2126, 0.7152, 0.0722]
  const mul = (a: number[][], b: number[][]) => a.map((r) => b[0].map((_, j) => r.reduce((acc, v, i) => acc + v * b[i][j], 0)))
  const aff = (lin: number[][], off: number[]) => [
    [...lin[0], off[0]], [...lin[1], off[1]], [...lin[2], off[2]], [0, 0, 0, 1]]
  const I = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
  const exposure = aff(I.map((r) => r.map((v) => v * e)), [0, 0, 0])
  const contrast = aff(I.map((r) => r.map((v) => v * k)), [0.5 * (1 - k), 0.5 * (1 - k), 0.5 * (1 - k)])
  const sat = I.map((r) => r.map((v, j) => (1 - s) * L[j] + s * v))
  const saturation = aff(sat, [0, 0, 0])
  const warmth = aff([[1 + 0.12 * w, 0, 0], [0, 1, 0], [0, 0, 1 - 0.12 * w]], [0, 0, 0])
  return mul(warmth, mul(saturation, mul(contrast, exposure))).slice(0, 3)
}

function straightenZoom(w: number, h: number, angle: number): number {
  const a = (Math.abs(angle) * Math.PI) / 180
  return Math.max((w * Math.cos(a) + h * Math.sin(a)) / w, (w * Math.sin(a) + h * Math.cos(a)) / h)
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

interface Props {
  photo: FwPhoto
  onClose: () => void
  onSaved: (p: FwPhoto) => void
  onTask: <T>(start: () => Promise<TaskInfo>, failTitle: string) => Promise<T | null>
}

export default function ImageEditor({ photo, onClose, onSaved, onTask }: Props) {
  const { message, modal } = AntApp.useApp()
  const [tab, setTab] = useState<Tab>('crop')
  const [img, setImg] = useState<HTMLImageElement | null>(null)
  const [rot90, setRot90] = useState(0)
  const [flipX, setFlipX] = useState(false)
  const [flipY, setFlipY] = useState(false)
  const [angle, setAngle] = useState(0)
  const [crop, setCrop] = useState<Rect>(FULL)
  const [aspect, setAspect] = useState('free')
  const [color, setColor] = useState<Color>(NO_COLOR)
  const [annos, setAnnos] = useState<Anno[]>([])
  const [tool, setTool] = useState<AnnoType>('arrow')
  const [ink, setInk] = useState(SWATCHES[0])
  const [lineW, setLineW] = useState(0.006)
  const [textSize, setTextSize] = useState(0.045)
  const [selAnno, setSelAnno] = useState<number | null>(null)
  const [typing, setTyping] = useState<{ at: [number, number]; text: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const nextId = useRef(1)

  useEffect(() => {
    const im = new window.Image()
    im.onload = () => setImg(im)
    im.src = photoUrl(photo, 1600, 'base')
    return () => { im.onload = null }
  }, [photo])

  // ------------------------------------------------------------- 画幅与舞台
  const stageRef = useRef<HTMLDivElement>(null)
  const [box, setBox] = useState({ w: 800, h: 600 })
  useEffect(() => {
    const el = stageRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setBox({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const iw = img?.naturalWidth ?? photo.width ?? 4
  const ih = img?.naturalHeight ?? photo.height ?? 3
  const turned = rot90 % 180 !== 0
  const fw0 = turned ? ih : iw                              // 画幅（预览像素）
  const fh0 = turned ? iw : ih
  const pad = 40                                            // 四周留白：裁剪框的把手不会被挡住
  const k = Math.min((box.w - pad * 2) / fw0, (box.h - pad * 2) / fh0)
  const W = Math.max(1, fw0 * k)
  const H = Math.max(1, fh0 * k)
  const ox = (box.w - W) / 2
  const oy = (box.h - H) / 2

  // ------------------------------------------------------------- 预览
  const canvasRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = canvasRef.current
    if (!cv || !img) return
    const dpr = window.devicePixelRatio || 1
    cv.width = Math.round(W * dpr)
    cv.height = Math.round(H * dpr)
    const ctx = cv.getContext('2d')!
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, cv.width, cv.height)
    ctx.imageSmoothingQuality = 'high'
    const s = straightenZoom(W, H, angle)
    ctx.translate(cv.width / 2, cv.height / 2)
    ctx.rotate((angle * Math.PI) / 180)
    ctx.scale(s * dpr, s * dpr)
    ctx.rotate((rot90 * Math.PI) / 180)
    ctx.scale(flipX ? -1 : 1, flipY ? -1 : 1)
    const dw = iw * k
    const dh = ih * k
    ctx.drawImage(img, -dw / 2, -dh / 2, dw, dh)
  }, [img, W, H, angle, rot90, flipX, flipY, iw, ih, k])

  const matrix = useMemo(() => colorMatrix(color), [color])
  // feColorMatrix 要 4×5：每行 R G B A 偏移
  const matrixValues = useMemo(() => [
    ...matrix.flatMap((r) => [r[0], r[1], r[2], 0, r[3]]),
    0, 0, 0, 1, 0,
  ].join(' '), [matrix])
  const colored = Object.values(color).some((v) => v !== 0)

  // ------------------------------------------------------------- 裁剪框拖动
  const ratioOf = (key: string): number | null => {
    const a = ASPECTS.find((x) => x.key === key)?.ratio ?? null
    return a === 'orig' ? fw0 / fh0 : a
  }
  /** 按比例在画幅里取一个居中的最大框（比例是像素宽高比） */
  const fitAspect = (key: string): Rect => {
    const r = ratioOf(key)
    if (!r) return FULL
    const fr = fw0 / fh0
    if (r > fr) { const hh = fr / r; return [0, (1 - hh) / 2, 1, (1 + hh) / 2] }
    const ww = r / fr
    return [(1 - ww) / 2, 0, (1 + ww) / 2, 1]
  }
  const pickAspect = (key: string) => {
    setAspect(key)
    setCrop(fitAspect(key))
  }

  const drag = useRef<{ mode: string; sx: number; sy: number; start: Rect } | null>(null)
  const onCropDown = (e: React.PointerEvent, mode: string) => {
    e.stopPropagation()
    e.preventDefault()
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    drag.current = { mode, sx: e.clientX, sy: e.clientY, start: crop }
  }
  const onCropMove = (e: React.PointerEvent) => {
    const g = drag.current
    if (!g) return
    const dx = (e.clientX - g.sx) / W
    const dy = (e.clientY - g.sy) / H
    let [x0, y0, x1, y1] = g.start
    const min = 0.04
    if (g.mode === 'move') {
      const w = x1 - x0
      const h = y1 - y0
      x0 = clamp(x0 + dx, 0, 1 - w); y0 = clamp(y0 + dy, 0, 1 - h)
      return setCrop([x0, y0, x0 + w, y0 + h])
    }
    if (g.mode.includes('w')) x0 = clamp(x0 + dx, 0, x1 - min)
    if (g.mode.includes('e')) x1 = clamp(x1 + dx, x0 + min, 1)
    if (g.mode.includes('n')) y0 = clamp(y0 + dy, 0, y1 - min)
    if (g.mode.includes('s')) y1 = clamp(y1 + dy, y0 + min, 1)
    const r = ratioOf(aspect)
    if (r) {
      // 固定比例：以拖动的那条边为准，另一向跟着调；角把手按宽度定高度
      const fr = fw0 / fh0
      const wantH = ((x1 - x0) * fr) / r
      if (g.mode === 'n' || g.mode === 's') {
        const wantW = ((y1 - y0) * r) / fr
        const cx = (g.start[0] + g.start[2]) / 2
        x0 = clamp(cx - wantW / 2, 0, 1); x1 = clamp(cx + wantW / 2, 0, 1)
      } else if (g.mode.includes('n')) y0 = clamp(y1 - wantH, 0, y1 - min)
      else if (g.mode.includes('s')) y1 = clamp(y0 + wantH, y0 + min, 1)
      else {
        const cy = (g.start[1] + g.start[3]) / 2
        y0 = clamp(cy - wantH / 2, 0, 1); y1 = clamp(cy + wantH / 2, 0, 1)
      }
    }
    setCrop([x0, y0, x1, y1])
  }
  const onCropUp = () => { drag.current = null }

  // ------------------------------------------------------------- 标注
  const toFrame = (e: React.PointerEvent | React.MouseEvent): [number, number] => {
    const r = stageRef.current!.getBoundingClientRect()
    return [clamp((e.clientX - r.left - ox) / W, 0, 1), clamp((e.clientY - r.top - oy) / H, 0, 1)]
  }
  const drawing = useRef<number | null>(null)
  const textAt = useRef<[number, number] | null>(null)
  const onMarkDown = (e: React.PointerEvent) => {
    if (tab !== 'mark' || e.button !== 0 || typing) return
    const p = toFrame(e)
    if (tool === 'text') {
      // 松手时再放文字框：按下时就放，松手那一下会把焦点从新输入框上抢走
      e.preventDefault()
      textAt.current = p
      return
    }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    const id = nextId.current++
    drawing.current = id
    setAnnos((xs) => [...xs, { id, type: tool, pts: [p, p], color: ink, width: lineW }])
    setSelAnno(id)
  }
  const onMarkMove = (e: React.PointerEvent) => {
    const id = drawing.current
    if (id == null) return
    const p = toFrame(e)
    setAnnos((xs) => xs.map((a) => (a.id === id ? { ...a, pts: [a.pts[0], p] } : a)))
  }
  const onMarkUp = () => {
    if (textAt.current) {
      const at = textAt.current
      textAt.current = null
      setTyping({ at, text: '' })
      return
    }
    const id = drawing.current
    drawing.current = null
    if (id == null) return
    // 太短的当成误点，丢掉
    setAnnos((xs) => xs.filter((a) => a.id !== id || Math.hypot((a.pts[1][0] - a.pts[0][0]) * W, (a.pts[1][1] - a.pts[0][1]) * H) > 6))
  }
  const commitText = () => {
    if (typing && typing.text.trim()) {
      const id = nextId.current++
      setAnnos((xs) => [...xs, { id, type: 'text', pts: [typing.at], color: ink, width: lineW, text: typing.text.trim(), size: textSize }])
      setSelAnno(id)
    }
    setTyping(null)
  }
  const patchSel = (changes: Partial<Anno>) => {
    if (selAnno == null) return
    setAnnos((xs) => xs.map((a) => (a.id === selAnno ? { ...a, ...changes } : a)))
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest?.('input, textarea')) return
      if ((e.key === 'Delete' || e.key === 'Backspace') && selAnno != null && tab === 'mark') {
        e.preventDefault()
        setAnnos((xs) => xs.filter((a) => a.id !== selAnno))
        setSelAnno(null)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selAnno, tab])

  // ------------------------------------------------------------- 几何操作
  const turn = (deg: 90 | -90) => {
    setRot90((r) => (r + deg + 360) % 360)
    // 转 90° 之后画幅横竖互换，裁剪框与标注都按新画幅重来最不容易出错
    setCrop(FULL)
    setAspect('free')
    if (annos.length) message.info('旋转 90° 后已清空裁剪框；标注按新画幅重新绘制。')
    setAnnos([])
  }

  const changed = rot90 !== 0 || flipX || flipY || angle !== 0 || colored || annos.length > 0
    || crop.some((v, i) => Math.abs(v - FULL[i]) > 1e-4)

  const reset = () => {
    setRot90(0); setFlipX(false); setFlipY(false); setAngle(0); setCrop(FULL); setAspect('free')
    setColor(NO_COLOR); setAnnos([]); setSelAnno(null)
  }

  const cancel = () => {
    if (!changed) return onClose()
    modal.confirm({
      title: '放弃这次编辑？', content: '未保存的裁剪、调色、标注将丢失。',
      okText: '放弃', okButtonProps: { danger: true }, cancelText: '继续编辑', onOk: onClose,
    })
  }

  const save = useCallback(async () => {
    setSaving(true)
    try {
      const spec = {
        rot90, flip_x: flipX, flip_y: flipY, angle, crop, color,
        annotations: annos.map(({ type, pts, color: c, width, text, size }) => ({ type, pts, color: c, width, text, size })),
      }
      const r = await onTask<{ photo: FwPhoto }>(() => fw.editPhoto(photo.uid, spec), '编辑照片失败')
      if (r) onSaved(r.photo)
    } finally {
      setSaving(false)
    }
  }, [rot90, flipX, flipY, angle, crop, color, annos, photo.uid, onTask, onSaved])

  // ------------------------------------------------------------- 渲染
  const [cx0, cy0, cx1, cy1] = crop
  const cropPx = { left: ox + cx0 * W, top: oy + cy0 * H, width: (cx1 - cx0) * W, height: (cy1 - cy0) * H }
  const showHandles = tab === 'crop'
  const short = Math.min(W, H)

  const annoSvg = (a: Anno) => {
    const p = a.pts.map(([x, y]) => [x * W, y * H])
    const sw = Math.max(1, a.width * short)
    const sel = a.id === selAnno && tab === 'mark'
    const common = { stroke: a.color, strokeWidth: sw, fill: 'none', strokeLinecap: 'round' as const }
    let el: React.ReactNode = null
    if (a.type === 'text') {
      const fs = (a.size ?? 0.045) * short
      const dark = a.color === '#111111'
      el = <text x={p[0][0]} y={p[0][1]} dominantBaseline="text-before-edge" fontSize={fs} fontWeight={700}
        fill={a.color} stroke={dark ? 'rgba(255,255,255,.9)' : 'rgba(0,0,0,.8)'} strokeWidth={Math.max(1, fs * 0.08)}
        paintOrder="stroke" style={{ fontFamily: 'var(--font-ui)' }}>{a.text}</text>
    } else if (a.type === 'line' || a.type === 'arrow') {
      const [[x0, y0], [x1, y1]] = p
      const ang = Math.atan2(y1 - y0, x1 - x0)
      const hs = sw * 4.5
      const head = [[x1, y1], [x1 - hs * Math.cos(ang - 0.42), y1 - hs * Math.sin(ang - 0.42)],
        [x1 - hs * 0.72 * Math.cos(ang), y1 - hs * 0.72 * Math.sin(ang)], [x1 - hs * Math.cos(ang + 0.42), y1 - hs * Math.sin(ang + 0.42)]]
      el = (
        <>
          <line x1={x0} y1={y0} x2={x1} y2={y1} {...common} />
          {a.type === 'arrow' ? <polygon points={head.map((q) => q.join(',')).join(' ')} fill={a.color} /> : null}
        </>
      )
    } else {
      const [[x0, y0], [x1, y1]] = p
      const x = Math.min(x0, x1), y = Math.min(y0, y1), w = Math.abs(x1 - x0), h = Math.abs(y1 - y0)
      el = a.type === 'rect' ? <rect x={x} y={y} width={w} height={h} {...common} strokeLinecap="butt" />
        : <ellipse cx={x + w / 2} cy={y + h / 2} rx={w / 2} ry={h / 2} {...common} />
    }
    return (
      <g key={a.id} className={'ie-anno' + (sel ? ' sel' : '')}
        onPointerDown={(e) => { if (tab === 'mark' && !drawing.current) { e.stopPropagation(); setSelAnno(a.id) } }}>
        {el}
      </g>
    )
  }

  const sel = annos.find((a) => a.id === selAnno) ?? null

  return (
    <div className="ie">
      <svg width="0" height="0" style={{ position: 'absolute' }} aria-hidden>
        <filter id="ie-color" colorInterpolationFilters="sRGB">
          <feColorMatrix type="matrix" values={matrixValues} />
        </filter>
      </svg>

      <div ref={stageRef} className={'ie-stage' + (tab === 'mark' ? ' marking' : '')}
        onPointerDown={onMarkDown} onPointerMove={onMarkMove} onPointerUp={onMarkUp}>
        {!img ? <div className="ie-loading">正在载入底图…</div> : null}
        <canvas ref={canvasRef} className="ie-canvas"
          style={{ left: ox, top: oy, width: W, height: H, filter: colored ? 'url(#ie-color)' : undefined }} />
        <svg className="ie-annos" style={{ left: ox, top: oy }} width={W} height={H}>
          {annos.map(annoSvg)}
        </svg>
        {/* 裁剪框外压暗；裁剪页之外压得更暗、不显示把手 */}
        <div className={'ie-crop' + (showHandles ? ' active' : '')} style={cropPx}
          onPointerDown={showHandles ? (e) => onCropDown(e, 'move') : undefined}
          onPointerMove={onCropMove} onPointerUp={onCropUp}>
          {showHandles ? (
            <>
              <div className="ie-thirds" />
              {['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].map((h) => (
                <div key={h} className={'ie-handle ' + h} onPointerDown={(e) => onCropDown(e, h)}
                  onPointerMove={onCropMove} onPointerUp={onCropUp} />
              ))}
            </>
          ) : null}
        </div>
        {typing ? (
          <input className="ie-typing" autoFocus value={typing.text} placeholder="输入文字，回车确定"
            style={{ left: ox + typing.at[0] * W, top: oy + typing.at[1] * H, color: ink, fontSize: Math.max(12, textSize * short) }}
            onPointerDown={(e) => e.stopPropagation()}
            onChange={(e) => setTyping({ ...typing, text: e.target.value })}
            onKeyDown={(e) => { if (e.key === 'Enter') commitText(); else if (e.key === 'Escape') setTyping(null) }}
            onBlur={commitText} />
        ) : null}
        <div className="ie-size mono">
          {Math.round((cx1 - cx0) * (turned ? photo.height ?? ih : photo.width ?? iw))} × {Math.round((cy1 - cy0) * (turned ? photo.width ?? iw : photo.height ?? ih))}
        </div>
      </div>

      <aside className="ie-side">
        <div className="ie-tabs">
          <Segmented block value={tab} onChange={(v) => { setTab(v as Tab); setTyping(null) }}
            options={[{ value: 'crop', label: '裁剪旋转' }, { value: 'color', label: '调色' }, { value: 'mark', label: '标注' }]} />
        </div>
        <div className="ie-panel">
          {tab === 'crop' ? (
            <>
              <section className="ie-section">
                <div className="ie-label">裁剪比例</div>
                <div className="ie-chips">
                  {ASPECTS.map((a) => (
                    <button key={a.key} className={'ie-chip' + (aspect === a.key ? ' on' : '')} onClick={() => pickAspect(a.key)}>{a.label}</button>
                  ))}
                </div>
                <div className="caption">拖动框内移动，拖动四角与边线调整大小。</div>
              </section>
              <section className="ie-section">
                <div className="ie-label">旋转与翻转</div>
                <div className="ie-row-btns">
                  <Tooltip title="向左转 90°"><button className="ie-icon-btn" onClick={() => turn(-90)}><span className="ie-glyph">⟲</span>左转</button></Tooltip>
                  <Tooltip title="向右转 90°"><button className="ie-icon-btn" onClick={() => turn(90)}><span className="ie-glyph">⟳</span>右转</button></Tooltip>
                  <button className={'ie-icon-btn' + (flipX ? ' on' : '')} onClick={() => setFlipX((v) => !v)}><span className="ie-glyph">⇋</span>水平</button>
                  <button className={'ie-icon-btn' + (flipY ? ' on' : '')} onClick={() => setFlipY((v) => !v)}><span className="ie-glyph ie-rot">⇋</span>垂直</button>
                </div>
              </section>
              <section className="ie-section">
                <div className="ie-label-row">
                  <span className="ie-label">拉直</span>
                  <InputNumber size="small" min={-45} max={45} step={0.1} value={angle} addonAfter="°" style={{ width: 104 }}
                    onChange={(v) => setAngle(clamp(Number(v ?? 0), -45, 45))} />
                </div>
                <Slider min={-45} max={45} step={0.1} value={angle} onChange={setAngle}
                  marks={{ '-45': '-45°', 0: '0', 45: '45°' }} tooltip={{ formatter: (v) => `${v}°` }} />
                <div className="caption">按地平线、层理把照片转正，四角自动放大填满。</div>
              </section>
            </>
          ) : tab === 'color' ? (
            <section className="ie-section">
              {COLOR_ITEMS.map((c) => (
                <div key={c.key} className="ie-slider">
                  <div className="ie-label-row">
                    <span className="ie-label">{c.label}</span>
                    <span className="caption mono">{color[c.key] > 0 ? '+' : ''}{color[c.key]}</span>
                  </div>
                  <div onDoubleClick={() => setColor((x) => ({ ...x, [c.key]: 0 }))} title="双击归零">
                    <Slider min={-100} max={100} value={color[c.key]} included={false}
                      onChange={(v) => setColor((x) => ({ ...x, [c.key]: v }))} />
                  </div>
                  {c.hint ? <div className="caption">{c.hint}</div> : null}
                </div>
              ))}
              <Button size="small" disabled={!colored} onClick={() => setColor(NO_COLOR)}>恢复默认颜色</Button>
            </section>
          ) : (
            <>
              <section className="ie-section">
                <div className="ie-label">工具</div>
                <div className="ie-chips">
                  {ANNO_TOOLS.map((t) => (
                    <button key={t.key} className={'ie-chip' + (tool === t.key ? ' on' : '')} onClick={() => setTool(t.key)}>{t.label}</button>
                  ))}
                </div>
                <div className="caption">{tool === 'text' ? '在照片上单击放置文字。' : '在照片上拖动绘制。'}单击已画的标注可选中，Delete 删除。</div>
              </section>
              <section className="ie-section">
                <div className="ie-label">颜色</div>
                <div className="ie-swatches">
                  {SWATCHES.map((c) => (
                    <button key={c} className={'ie-swatch' + (ink === c ? ' on' : '')} style={{ background: c }}
                      onClick={() => { setInk(c); patchSel({ color: c }) }} aria-label={c} />
                  ))}
                </div>
                <div className="ie-label-row" style={{ marginTop: 12 }}>
                  <span className="ie-label">{tool === 'text' || sel?.type === 'text' ? '字号' : '线宽'}</span>
                </div>
                {tool === 'text' || sel?.type === 'text' ? (
                  <Slider min={0.015} max={0.12} step={0.001} value={sel?.type === 'text' ? sel.size ?? textSize : textSize}
                    tooltip={{ formatter: null }}
                    onChange={(v) => { setTextSize(v); if (sel?.type === 'text') patchSel({ size: v }) }} />
                ) : (
                  <Slider min={0.002} max={0.02} step={0.0005} value={sel ? sel.width : lineW}
                    tooltip={{ formatter: null }}
                    onChange={(v) => { setLineW(v); if (sel) patchSel({ width: v }) }} />
                )}
              </section>
              {annos.length ? (
                <section className="ie-section">
                  <div className="ie-label">已添加</div>
                  <div className="ie-list">
                    {annos.map((a) => (
                      <div key={a.id} className={'ie-list-row' + (a.id === selAnno ? ' sel' : '')} onClick={() => setSelAnno(a.id)}>
                        <span className="ie-dot" style={{ background: a.color }} />
                        <span className="ie-list-name">{ANNO_TOOLS.find((t) => t.key === a.type)?.label}{a.text ? `：${a.text}` : ''}</span>
                        <button className="ibtn xs" onClick={(e) => { e.stopPropagation(); setAnnos((xs) => xs.filter((x) => x.id !== a.id)); if (selAnno === a.id) setSelAnno(null) }}>
                          <Icon name="trash" size={13} />
                        </button>
                      </div>
                    ))}
                  </div>
                </section>
              ) : null}
            </>
          )}
        </div>
        <div className="ie-foot">
          <Button disabled={!changed} onClick={reset}>还原</Button>
          <div className="spacer" />
          <Button onClick={cancel}>取消</Button>
          <Button type="primary" disabled={!changed || !img} loading={saving} onClick={save}>保存</Button>
        </div>
      </aside>
    </div>
  )
}
