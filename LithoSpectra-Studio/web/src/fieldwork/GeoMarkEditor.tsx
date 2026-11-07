// 地质水印编辑器（嵌在照片浏览器里）：指北针、比例尺、镜向、文字是可选组件，需要哪个加哪个，
// 拖到哪儿成品就画在哪儿。
//
// 预览由后端 geomark.py 画（透明水印层 + 各组件包围框），与导出的成品同一套绘制代码，
// 所见即所得。位置是相对照片宽高的比例，大小是相对短边的比例，比例尺按原图像素标定，
// 预览缩放多少都不影响成品。
//
// 比例尺要先「标定」：在照片里的参照物（地质锤、记号笔、硬币、罗盘）上拉一条线，
// 填它的实际长度，算出原图每毫米多少像素；比例尺的整数长度按 1-2-5 自动挑，也可以手改。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { App as AntApp, Button, Input, InputNumber, Radio, Segmented, Select, Slider, Switch, Tooltip } from 'antd'

import { Icon } from '../theme'
import { directionName, fmtTime, fw, photoUrl } from './fwapi'
import type { FwPhoto, FwPointDetail, GeomarkAnchor, GeomarkDefaults, GeomarkItem, GeomarkPreview, GeomarkSpec, GeomarkType } from './fwapi'

const UNITS = ['mm', 'cm', 'm']

/** 指北针样式，字符码对应 ESRI North 字体（assets/fonts/esri_north.ttf） */
const NORTH_DESIGNS = [
  { value: 'star', code: 53, label: '八角星' },
  { value: 'rose', code: 193, label: '罗盘' },
  { value: 'arrow', code: 177, label: '箭头' },
  { value: 'circle', code: 111, label: '圆形' },
]
const UNIT_MM: Record<string, number> = { mm: 1, cm: 10, m: 1000 }

const KIND: Record<GeomarkType, { zh: string; en: string; glyph: ReactNode }> = {
  north: { zh: '指北针', en: 'North arrow', glyph: <NorthGlyph /> },
  scale: { zh: '比例尺', en: 'Scale bar', glyph: <ScaleGlyph /> },
  view: { zh: '镜向', en: 'Viewing direction', glyph: <ViewGlyph /> },
  text: { zh: '文字', en: 'Text', glyph: <TextGlyph /> },
}

/** 在 1-2-5 序列里挑一个画出来接近 target 像素的长度（与后端 nice_length 同规则） */
function niceLength(pxPerUnit: number, target: number): number {
  if (pxPerUnit <= 0) return 1
  const raw = target / pxPerUnit
  const e0 = Math.floor(Math.log10(raw))
  let best = 1
  for (const e of [e0 - 1, e0, e0 + 1]) {
    for (const m of [1, 2, 5]) {
      const v = m * 10 ** e
      if (Math.abs(v * pxPerUnit - target) < Math.abs(best * pxPerUnit - target)) best = Number(v.toPrecision(6))
    }
  }
  return best
}

/** 象限方位，与后端 geomark.bearing 同规则：N45°E、S42°E */
function bearing(az: number): string {
  const a = ((az % 360) + 360) % 360
  const d = (v: number) => String(Number(v.toFixed(1)))
  if (a === 0) return 'N'
  if (a === 90) return 'E'
  if (a === 180) return 'S'
  if (a === 270) return 'W'
  if (a < 90) return `N${d(a)}°E`
  if (a < 180) return `S${d(180 - a)}°E`
  if (a < 270) return `S${d(a - 180)}°W`
  return `N${d(360 - a)}°W`
}

/** 默认值里记的东西：样式、大小、摆放；内容（镜向度数、文字、标定）每张照片各自的，不记 */
const STYLE_KEYS: (keyof GeomarkItem)[] = ['size', 'anchor', 'design', 'auto', 'label', 'label_format', 'segments', 'unit', 'format']

function pickStyle(it: Partial<GeomarkItem>): Partial<GeomarkItem> {
  const out: Partial<GeomarkItem> = {}
  for (const k of STYLE_KEYS) if (it[k] !== undefined) (out as Record<string, unknown>)[k] = it[k]
  return out
}

/** 包围框（相对宽高的比例）→ 离最近两条边的距离，以照片短边为单位 */
function anchorOf(x: number, y: number, bw: number, bh: number, pw: number, ph: number): GeomarkAnchor {
  const short = Math.min(pw, ph)
  const left = x * pw
  const right = (1 - x - bw) * pw
  const top = y * ph
  const bottom = (1 - y - bh) * ph
  const r = (v: number) => Math.round(Math.max(0, v) / short * 10000) / 10000
  return {
    h: x + bw / 2 < 0.5 ? 'left' : 'right', dx: r(x + bw / 2 < 0.5 ? left : right),
    v: y + bh / 2 < 0.5 ? 'top' : 'bottom', dy: r(y + bh / 2 < 0.5 ? top : bottom),
  }
}

function newId(type: GeomarkType, items: GeomarkItem[]): string {
  let i = 1
  while (items.some((it) => it.id === `${type[0]}${i}`)) i++
  return `${type[0]}${i}`
}

interface Props {
  photo: FwPhoto
  point: FwPointDetail | null
  onClose: () => void
  onSaved: (p: FwPhoto) => void
}

type Calib = { itemId: string; pts: number[] | null; ask: boolean; len: number; unit: string }
type Drag = { id: string; sx: number; sy: number; x0: number; y0: number; moved: boolean }

export default function GeoMarkEditor({ photo, point, onClose, onSaved }: Props) {
  const { message } = AntApp.useApp()
  const prev = photo.overlay?.version === 2 ? photo.overlay : null
  const azLocked = photo.locked?.includes('azimuth')
  // 照片方位角：指北针自动旋转、镜向都按它来。原片没有时可在这里补记，保存时一并记入照片信息
  const [azimuth, setAzimuth] = useState<number | null>(photo.azimuth)

  const [lang, setLang] = useState<'en' | 'zh'>(prev?.lang ?? 'en')
  const [theme, setTheme] = useState<'light' | 'dark'>(prev?.theme ?? 'light')
  const [panel, setPanel] = useState<boolean>(prev?.panel ?? false)
  const [items, setItems] = useState<GeomarkItem[]>(() => prev?.items.map((it) => ({ ...it })) ?? [])
  const [sel, setSel] = useState<string | null>(items[0]?.id ?? null)
  const [calib, setCalib] = useState<Calib | null>(null)
  const [saving, setSaving] = useState(false)
  // 各类组件上次的样式、大小与摆放：新加的组件照此摆，同一批照片出图一致
  const [defaults, setDefaults] = useState<GeomarkDefaults>({})
  useEffect(() => {
    fw.geomarkDefaults().then((d) => {
      setDefaults(d)
      if (!prev) {
        if (d.lang) setLang(d.lang)
        if (d.theme) setTheme(d.theme)
        if (d.panel != null) setPanel(d.panel)
      }
    }).catch(() => undefined)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------- 舞台尺寸
  const boxRef = useRef<HTMLDivElement>(null)
  const [box, setBox] = useState({ w: 900, h: 640 })
  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setBox({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const pw = photo.width ?? 1600
  const ph = photo.height ?? 1200
  const pad = 24
  const d = Math.min((box.w - pad * 2) / pw, (box.h - pad * 2) / ph)
  const W = Math.max(1, pw * d)
  const H = Math.max(1, ph * d)
  const ox = (box.w - W) / 2
  const oy = (box.h - H) / 2

  // ------------------------------------------------------------- 预览（后端画，最新一次为准）
  const spec: GeomarkSpec = useMemo(() => ({ version: 2, lang, theme, panel, items }), [lang, theme, panel, items])
  const [preview, setPreview] = useState<GeomarkPreview | null>(null)
  const azRef = useRef(azimuth)
  azRef.current = azimuth
  const seq = useRef(0)
  const busy = useRef(false)
  const queued = useRef<GeomarkSpec | null>(null)
  const previewW = Math.round(Math.min(pw, Math.max(400, W * (window.devicePixelRatio || 1)), 2000))

  const request = useCallback((s: GeomarkSpec) => {
    if (busy.current) { queued.current = s; return }
    busy.current = true
    const id = ++seq.current
    fw.geomarkPreview(photo.uid, s, previewW, azRef.current)
      .then((r) => { if (id === seq.current) setPreview(r) })
      .catch((e) => message.error((e as Error).message))
      .finally(() => {
        busy.current = false
        const next = queued.current
        queued.current = null
        if (next) request(next)
      })
  }, [photo.uid, previewW, message])

  useEffect(() => {
    const t = window.setTimeout(() => request(spec), 30)
    return () => window.clearTimeout(t)
  }, [spec, azimuth, request])

  // ------------------------------------------------------------- 组件
  const patch = (id: string, changes: Partial<GeomarkItem>) =>
    setItems((xs) => xs.map((it) => (it.id === id ? { ...it, ...changes } : it)))
  const remove = (id: string) => {
    setItems((xs) => xs.filter((it) => it.id !== id))
    if (sel === id) setSel(null)
  }
  const selected = items.find((it) => it.id === sel) ?? null

  const add = (type: GeomarkType) => {
    const id = newId(type, items)
    const used = (x: number, y: number) => items.some((it) => Math.abs(it.x - x) < 0.04 && Math.abs(it.y - y) < 0.04)
    const at = (x: number, y: number) => (used(x, y) ? { x, y: Math.min(0.9, y + 0.08) } : { x, y })
    let it: GeomarkItem
    if (type === 'north') {
      it = { id, type, ...at(0.82, 0.04), size: 0.14, design: 'star', rotation: 0, auto: true }
    } else if (type === 'scale') {
      const other = items.find((x) => x.type === 'scale' && (x.px_per_mm ?? 0) > 0)
      it = { id, type, ...at(0.04, 0.88), size: 0.032, design: 'bar', px_per_mm: other?.px_per_mm ?? 0,
        length: other?.length ?? 10, unit: other?.unit ?? 'cm', segments: 2 }
    } else if (type === 'view') {
      it = { id, type, ...at(0.04, 0.04), size: 0.03, azimuth: null, format: 'bearing' }
    } else {
      it = { id, type, ...at(0.04, 0.115), size: 0.028, lines: defaultLines(photo, point, lang) }
    }
    const def = defaults.items?.[type]
    if (def) it = { ...it, ...pickStyle(def) }
    setItems((xs) => [...xs, it])
    setSel(id)
    if (type === 'scale' && !(it.px_per_mm && it.px_per_mm > 0)) startCalib(id)
  }

  // ------------------------------------------------------------- 拖动
  const drag = useRef<Drag | null>(null)
  const [dragPos, setDragPos] = useState<{ id: string; x: number; y: number } | null>(null)
  const onBoxDown = (e: React.PointerEvent, it: GeomarkItem) => {
    if (calib || e.button !== 0) return
    e.stopPropagation()
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
    setSel(it.id)
    // 起点取实际画出来的位置：按离边距离摆的组件，x / y 不一定是它现在的位置
    const b = preview?.boxes[it.id]
    drag.current = { id: it.id, sx: e.clientX, sy: e.clientY, x0: b ? b[0] : it.x, y0: b ? b[1] : it.y, moved: false }
  }
  const onBoxMove = (e: React.PointerEvent) => {
    const g = drag.current
    if (!g) return
    const dx = (e.clientX - g.sx) / W
    const dy = (e.clientY - g.sy) / H
    if (!g.moved && Math.hypot(e.clientX - g.sx, e.clientY - g.sy) < 3) return
    g.moved = true
    const x = Math.min(0.98, Math.max(0, g.x0 + dx))
    const y = Math.min(0.98, Math.max(0, g.y0 + dy))
    setDragPos({ id: g.id, x, y })
    patch(g.id, { x, y, anchor: undefined })
  }
  const onBoxUp = () => {
    const g = drag.current
    if (g?.moved && dragPos) placeAt(g.id, dragPos.x, dragPos.y)
    drag.current = null
    setDragPos(null)
  }

  /** 摆到 (x, y)（包围框左上角，相对宽高的比例），换算成离最近两条边的距离存下 */
  const placeAt = (id: string, x: number, y: number) => {
    const b = preview?.boxes[id]
    const bw = b ? b[2] - b[0] : 0
    const bh = b ? b[3] - b[1] : 0
    patch(id, { x, y, anchor: anchorOf(x, y, bw, bh, pw, ph) })
  }

  // 方向键微调、Delete 删除选中的组件
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!sel || calib) return
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA') return
      const it = items.find((x) => x.id === sel)
      if (!it) return
      const step = e.shiftKey ? 0.02 : 0.004
      const moves: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }
      if (moves[e.key]) {
        e.preventDefault()
        const b = preview?.boxes[sel]
        const x0 = b ? b[0] : it.x
        const y0 = b ? b[1] : it.y
        placeAt(sel, Math.min(0.98, Math.max(0, x0 + moves[e.key][0])), Math.min(0.98, Math.max(0, y0 + moves[e.key][1])))
      } else if (e.key === 'Delete' || e.key === 'Backspace') {
        e.preventDefault()
        remove(sel)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // ------------------------------------------------------------- 标定
  function startCalib(itemId: string) {
    setCalib({ itemId, pts: null, ask: false, len: 14, unit: 'cm' })
  }
  const stagePt = (e: React.PointerEvent) => {
    const r = boxRef.current!.getBoundingClientRect()
    return [e.clientX - r.left, e.clientY - r.top]
  }
  const onStageDown = (e: React.PointerEvent) => {
    if (!calib || calib.ask) {
      if (!calib) setSel(null)
      return
    }
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
    const [x, y] = stagePt(e)
    setCalib({ ...calib, pts: [x, y, x, y] })
  }
  const onStageMove = (e: React.PointerEvent) => {
    if (!calib?.pts || calib.ask) return
    const [x, y] = stagePt(e)
    setCalib({ ...calib, pts: [calib.pts[0], calib.pts[1], x, y] })
  }
  const onStageUp = (e: React.PointerEvent) => {
    if (!calib?.pts || calib.ask) return
    const [x, y] = stagePt(e)
    const [a, b] = calib.pts
    if (Math.hypot(x - a, y - b) < 8) return setCalib({ ...calib, pts: null })
    setCalib({ ...calib, pts: [a, b, x, y], ask: true })
  }
  const finishCalib = () => {
    const c = calib!
    const [a, b, x2, y2] = c.pts!
    const lenPhotoPx = Math.hypot(x2 - a, y2 - b) / d
    const mm = c.len * UNIT_MM[c.unit]
    if (!(mm > 0)) return message.warning('请填写参照物的实际长度。')
    const ppm = lenPhotoPx / mm
    // 比例尺长度挑一个画出来约占照片宽度 1/5 的整数
    const unit = mm >= 1000 ? 'm' : 'cm'
    const length = niceLength(ppm * UNIT_MM[unit], pw * 0.2)
    // 同一张照片的比例尺共用一次标定
    setItems((xs) => xs.map((it) => (it.type === 'scale'
      ? { ...it, px_per_mm: ppm, ...(it.id === c.itemId || !(it.px_per_mm && it.px_per_mm > 0) ? { unit, length } : {}) }
      : it)))
    setSel(c.itemId)
    setCalib(null)
  }
  const cancelCalib = () => {
    const c = calib
    setCalib(null)
    // 刚加上、还没标定过的比例尺没法画，取消标定就把它去掉
    if (c) {
      const it = items.find((x) => x.id === c.itemId)
      if (it && !(it.px_per_mm && it.px_per_mm > 0)) remove(c.itemId)
    }
  }

  // ------------------------------------------------------------- 保存
  const save = async () => {
    setSaving(true)
    try {
      if (!azLocked && azimuth !== photo.azimuth) await fw.patchPhoto(photo.uid, { azimuth, azimuth_src: 'manual' })
      // 每个组件都按离边距离存：换到宽窄、横竖不同的照片上也贴在同一个角
      const anchored = items.map((it) => {
        if (it.anchor) return it
        const b = preview?.boxes[it.id]
        return b ? { ...it, anchor: anchorOf(b[0], b[1], b[2] - b[0], b[3] - b[1], pw, ph) } : it
      })
      const saved = await fw.geomark(photo.uid, { ...spec, items: anchored })
      // 这次的样式、大小与摆放记为默认，下一张照片新加组件时照此摆
      const next: GeomarkDefaults = { lang, theme, panel, items: { ...(defaults.items ?? {}) } }
      for (const it of anchored) next.items![it.type] = pickStyle(it)
      fw.setGeomarkDefaults(next).catch(() => undefined)
      onSaved(saved)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const switchLang = (v: 'en' | 'zh') => {
    setLang(v)
    // 文字组件里还是自动生成的默认内容时，跟着换语言
    setItems((xs) => xs.map((it) => (it.type === 'text' && sameLines(it.lines, defaultLines(photo, point, v === 'en' ? 'zh' : 'en'))
      ? { ...it, lines: defaultLines(photo, point, v) } : it)))
  }

  const baseUrl = photoUrl(photo, 1600, 'base')
  const boxes = preview?.boxes ?? {}
  const calibrating = !!calib && !calib.ask

  return (
    <>
      <div className="fw-geo">
        <div ref={boxRef} className={'fw-geo-stage' + (calibrating ? ' calibrating' : '')}
          onPointerDown={onStageDown} onPointerMove={onStageMove} onPointerUp={onStageUp}>
          <img className="fw-geo-img" src={baseUrl} alt="" draggable={false}
            style={{ left: ox, top: oy, width: W, height: H }} />
          {preview ? (
            <img className="fw-geo-img" src={preview.image} alt="" draggable={false}
              style={{ left: ox, top: oy, width: W, height: H }} />
          ) : null}
          {!calib ? items.map((it) => {
            const b = boxes[it.id]
            if (!b) return null
            // 拖动时包围框跟着指针走，水印层稍后由后端重画
            const dx = dragPos?.id === it.id ? dragPos.x - b[0] : 0
            const dy = dragPos?.id === it.id ? dragPos.y - b[1] : 0
            return (
              <div key={it.id} className={'fw-geo-box' + (it.id === sel ? ' sel' : '')}
                style={{ left: ox + (b[0] + dx) * W, top: oy + (b[1] + dy) * H, width: (b[2] - b[0]) * W, height: (b[3] - b[1]) * H }}
                onPointerDown={(e) => onBoxDown(e, it)} onPointerMove={onBoxMove} onPointerUp={onBoxUp}
                title={KIND[it.type].zh} />
            )
          }) : null}
          {calib?.pts ? (
            <svg className="fw-geo-calib" width={box.w} height={box.h}>
              <line x1={calib.pts[0]} y1={calib.pts[1]} x2={calib.pts[2]} y2={calib.pts[3]} />
              <circle cx={calib.pts[0]} cy={calib.pts[1]} r={4} />
              <circle cx={calib.pts[2]} cy={calib.pts[3]} r={4} />
            </svg>
          ) : null}
          {calib?.ask ? (
            <div className="fw-geo-ask" onPointerDown={(e) => e.stopPropagation()}>
              <span>参照物实际长度</span>
              <InputNumber size="small" min={0.01} value={calib.len} autoFocus style={{ width: 90 }}
                onChange={(v) => setCalib({ ...calib, len: v ?? 0 })} onPressEnter={finishCalib} />
              <Select size="small" value={calib.unit} style={{ width: 68 }} onChange={(u) => setCalib({ ...calib, unit: u })}
                options={UNITS.map((u) => ({ value: u, label: u }))} />
              <Button size="small" onClick={() => setCalib({ ...calib, pts: null, ask: false })}>重画</Button>
              <Button size="small" type="primary" onClick={finishCalib}>确定</Button>
              <span className="caption">记号笔约 14 cm，地质锤约 33 cm，一元硬币 2.5 cm</span>
            </div>
          ) : null}
          {calibrating ? (
            <div className="fw-geo-tip">
              在照片中的参照物上，从一端拖到另一端。<a onClick={cancelCalib}>取消</a>
            </div>
          ) : null}
          {!items.length ? <div className="fw-geo-empty">从右侧添加指北针、比例尺等组件</div> : null}
        </div>

        <div className="fw-geo-side">
          <div className="fw-geo-side-scroll">
          <div className="fw-section" style={{ marginTop: 4 }}>
            <div className="fw-section-head"><span>添加组件</span></div>
            <div className="fw-geo-add">
              {(Object.keys(KIND) as GeomarkType[]).map((k) => (
                <button key={k} className="fw-geo-chip" onClick={() => add(k)} disabled={!!calib}>
                  <span className="fw-geo-glyph">{KIND[k].glyph}</span>
                  <span>{KIND[k].zh}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="fw-section">
            <div className="fw-section-head"><span>照片方位角</span></div>
            <div className="fw-form" style={{ marginTop: 0 }}>
              <label>方位角</label>
              {azLocked ? (
                <span>
                  <span className="mono">{azimuth}°</span>
                  <span className="caption"> {lang === 'en' ? bearing(azimuth ?? 0) : directionName(azimuth ?? 0)}</span>
                  <span className="fw-lock">原片</span>
                </span>
              ) : (
                <span className="fw-inline">
                  <InputNumber size="small" min={0} max={359.9} value={azimuth ?? undefined} placeholder="未记录" addonAfter="°"
                    style={{ width: 110, flex: 'none' }} onChange={(v) => setAzimuth(v ?? null)} />
                  {azimuth != null ? <span className="caption nowrap">{lang === 'en' ? bearing(azimuth) : directionName(azimuth)}</span> : null}
                </span>
              )}
            </div>
            <div className="caption" style={{ marginTop: 4 }}>
              {azLocked ? '取自照片 EXIF，指北针与镜向按此自动设置。'
                : azimuth == null ? '照片没有记录拍摄方向。填写相机朝向的方位角后，指北针会自动旋转，保存时记入照片信息。'
                  : '指北针与镜向按此自动设置，保存时记入照片信息。'}
            </div>
          </div>

          {items.length ? (
            <div className="fw-section">
              <div className="fw-section-head"><span>已添加</span></div>
              <div className="fw-geo-list">
                {items.map((it) => (
                  <div key={it.id} className={'fw-geo-row' + (it.id === sel ? ' sel' : '')} onClick={() => setSel(it.id)}>
                    <span className="fw-geo-glyph">{KIND[it.type].glyph}</span>
                    <span className="fw-geo-row-name">{KIND[it.type].zh}</span>
                    <span className="caption fw-geo-row-sub">{summary(it, lang, azimuth)}</span>
                    <Tooltip title="删除">
                      <button className="ibtn xs" onClick={(e) => { e.stopPropagation(); remove(it.id) }}><Icon name="trash" size={13} /></button>
                    </Tooltip>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {selected ? (
            <div className="fw-section">
              <div className="fw-section-head"><span>{KIND[selected.type].zh}</span></div>
              <ItemProps it={selected} lang={lang} pw={pw} azimuth={azimuth}
                onChange={(c) => patch(selected.id, c)} onCalib={() => startCalib(selected.id)} />
            </div>
          ) : null}

          <div className="fw-section">
            <div className="fw-section-head"><span>样式</span></div>
            <div className="fw-form" style={{ marginTop: 0 }}>
              <label>语言</label>
              <Segmented size="small" block value={lang} onChange={(v) => switchLang(v as 'en' | 'zh')}
                options={[{ value: 'en', label: 'English' }, { value: 'zh', label: '中文' }]} />
              <label>配色</label>
              <Segmented size="small" block value={theme} onChange={(v) => setTheme(v as 'light' | 'dark')}
                options={[{ value: 'light', label: '白色' }, { value: 'dark', label: '黑色' }]} />
              <label>底板</label>
              <span className="fw-inline">
                <Switch size="small" checked={panel} onChange={setPanel} />
                <span className="caption">组件背后衬半透明底板</span>
              </span>
            </div>
          </div>
          <div className="caption" style={{ marginTop: 14 }}>
            只保存水印参数，原片不变；导出时按原图分辨率绘制。拖动组件调整位置，方向键微调，Delete 删除。
          </div>
          </div>
          <div className="fw-geo-actions">
            <Button onClick={onClose}>取消</Button>
            <Button type="primary" loading={saving} onClick={save}>保存水印</Button>
          </div>
        </div>
      </div>

    </>
  )
}

// ---------------------------------------------------------------- 组件属性

function ItemProps({ it, lang, pw, azimuth, onChange, onCalib }: {
  it: GeomarkItem
  lang: 'en' | 'zh'
  pw: number
  azimuth: number | null
  onChange: (c: Partial<GeomarkItem>) => void
  onCalib: () => void
}) {
  const sizeRow = (min: number, max: number) => (
    <>
      <label>大小</label>
      <Slider min={min} max={max} step={0.001} value={it.size} onChange={(v) => onChange({ size: v })}
        tooltip={{ formatter: (v) => `${Math.round((v ?? 0) * 1000) / 10}%` }} />
    </>
  )

  if (it.type === 'north') {
    const auto = it.auto !== false
    const rot = auto && azimuth != null ? Math.round(((-azimuth % 360) + 360) % 360) : (it.rotation ?? 0)
    return (
      <div className="fw-form" style={{ marginTop: 0 }}>
        <label>样式</label>
        <div className="fw-north-designs">
          {NORTH_DESIGNS.map((d) => (
            <button key={d.value} className={'fw-north-design' + ((it.design ?? 'rose') === d.value ? ' on' : '')}
              title={d.label} onClick={() => onChange({ design: d.value })}>
              <span className="fw-north-glyph">{String.fromCharCode(d.code)}</span>
            </button>
          ))}
        </div>
        <label>方向</label>
        <span className="fw-inline">
          <Switch size="small" checked={auto} onChange={(v) => onChange({ auto: v, rotation: rot })} />
          <span className="caption">按照片方位角自动旋转</span>
        </span>
        {auto ? (
          <>
            <label />
            <div className="caption">
              {azimuth != null ? `北向在画面中顺时针 ${rot}°。` : '照片没有方位角，指北针暂时朝上；在上方填写方位角后自动旋转。'}
            </div>
          </>
        ) : (
          <>
            <label>旋转</label>
            <span className="fw-inline">
              <Slider style={{ flex: 1 }} min={-180} max={180} step={1} value={it.rotation ?? 0} onChange={(v) => onChange({ rotation: v })} />
              <InputNumber size="small" min={-180} max={180} value={it.rotation ?? 0} addonAfter="°" style={{ width: 92, flex: 'none' }}
                onChange={(v) => onChange({ rotation: v ?? 0 })} />
            </span>
          </>
        )}
        <label>标注</label>
        <span className="fw-inline">
          <Switch size="small" checked={it.label !== false} onChange={(v) => onChange({ label: v })} />
          <span className="caption">下方标注镜向</span>
        </span>
        {it.label !== false ? (
          <>
            <label />
            <Segmented size="small" block value={it.label_format ?? 'azimuth'}
              onChange={(v) => onChange({ label_format: v as 'bearing' | 'azimuth' })}
              options={[
                { value: 'azimuth', label: `方位角 ${azimuth != null ? Math.round(azimuth) % 360 : 230}°` },
                { value: 'bearing', label: `象限角 ${bearing(azimuth ?? 230)}` },
              ]} />
            {azimuth == null ? (<><label /><div className="caption">照片没有方位角时不显示标注。</div></>) : null}
          </>
        ) : null}
        {sizeRow(0.06, 0.32)}
      </div>
    )
  }

  if (it.type === 'scale') {
    const ok = (it.px_per_mm ?? 0) > 0
    return (
      <div className="fw-form" style={{ marginTop: 0 }}>
        <label>标定</label>
        <span className="fw-inline">
          <Button size="small" onClick={onCalib}>{ok ? '重新标定' : '标定…'}</Button>
          {ok ? <span className="caption mono">{(it.px_per_mm! * 10).toFixed(1)} px/cm</span> : null}
        </span>
        {ok ? (
          <>
            <label>长度</label>
            <span className="fw-inline">
              <InputNumber size="small" min={0.1} value={it.length} style={{ width: 90 }}
                onChange={(v) => onChange({ length: v ?? it.length })} />
              <Select size="small" value={it.unit} style={{ width: 70 }} onChange={(u) => onChange({ unit: u })}
                options={UNITS.map((u) => ({ value: u, label: u }))} />
              <Tooltip title="按照片宽度的 1/5 取整">
                <Button size="small" onClick={() => onChange({ length: niceLength(it.px_per_mm! * UNIT_MM[it.unit ?? 'cm'], pw * 0.2) })}>自动</Button>
              </Tooltip>
            </span>
            <label>样式</label>
            <Segmented size="small" block value={it.design ?? 'bar'} onChange={(v) => onChange({ design: v as string })}
              options={[{ value: 'bar', label: '分段条' }, { value: 'line', label: '线段' }]} />
            {(it.design ?? 'bar') === 'bar' ? (
              <>
                <label>分段</label>
                <Radio.Group size="small" value={it.segments ?? 2} onChange={(e) => onChange({ segments: e.target.value })}
                  options={[1, 2, 4, 5].map((n) => ({ value: n, label: String(n) }))} optionType="button" />
              </>
            ) : null}
            {sizeRow(0.015, 0.08)}
          </>
        ) : (
          <>
            <label />
            <div className="caption">在照片中的参照物（地质锤、记号笔、硬币、罗盘）上画一条线，再填写其实际长度。</div>
          </>
        )}
      </div>
    )
  }

  if (it.type === 'view') {
    const az = it.azimuth ?? azimuth
    return (
      <div className="fw-form" style={{ marginTop: 0 }}>
        <label>镜向</label>
        <span className="caption">{az != null ? `${Math.round(az)}°，取自照片方位角` : '照片没有方位角，请在上方填写'}</span>
        <label>格式</label>
        <Segmented size="small" block value={it.format ?? 'bearing'} onChange={(v) => onChange({ format: v as 'bearing' | 'azimuth' })}
          options={[{ value: 'bearing', label: lang === 'en' ? (az != null ? bearing(az) : 'N45°E') : '象限' },
            { value: 'azimuth', label: az != null ? `${String(Math.round(az) % 360).padStart(3, '0')}°` : '045°' }]} />
        {sizeRow(0.015, 0.06)}
      </div>
    )
  }

  return (
    <div className="fw-form" style={{ marginTop: 0 }}>
      <label>内容</label>
      <Input.TextArea size="small" autoSize={{ minRows: 2, maxRows: 8 }} value={(it.lines ?? []).join('\n')}
        onChange={(e) => onChange({ lines: e.target.value.split('\n') })} />
      {sizeRow(0.012, 0.06)}
    </div>
  )
}

// ---------------------------------------------------------------- 辅助

function summary(it: GeomarkItem, lang: 'en' | 'zh', azimuth: number | null): string {
  if (it.type === 'north') return (NORTH_DESIGNS.find((d) => d.value === (it.design ?? 'rose'))?.label ?? '') + (it.auto === false ? ` · ${it.rotation ?? 0}°` : ' · 自动')
  if (it.type === 'scale') return (it.px_per_mm ?? 0) > 0 ? `${it.length} ${it.unit}` : '未标定'
  if (it.type === 'view') {
    if (it.azimuth == null && azimuth == null) return '缺方位角'
    const az = it.azimuth ?? azimuth ?? 0
    return it.format === 'azimuth' ? `${String(Math.round(az) % 360).padStart(3, '0')}°` : (lang === 'en' ? bearing(az) : directionName(az))
  }
  return (it.lines ?? []).find((l) => l.trim()) ?? ''
}

function sameLines(a: string[] | undefined, b: string[]): boolean {
  const x = (a ?? []).filter((l) => l.trim())
  return x.length === b.length && x.every((l, i) => l === b[i])
}

function defaultLines(photo: FwPhoto, point: FwPointDetail | null, lang: 'en' | 'zh'): string[] {
  const lines: string[] = []
  if (point?.name) lines.push(point.name)
  const lon = photo.lon ?? point?.lon
  const lat = photo.lat ?? point?.lat
  if (lon != null && lat != null) {
    const ns = lat >= 0 ? 'N' : 'S'
    const ew = lon >= 0 ? 'E' : 'W'
    lines.push(lang === 'en'
      ? `${Math.abs(lat).toFixed(5)}°${ns}, ${Math.abs(lon).toFixed(5)}°${ew}`
      : `${Math.abs(lon).toFixed(5)}°${ew}  ${Math.abs(lat).toFixed(5)}°${ns}`)
  }
  const alt = photo.altitude ?? point?.elevation
  if (alt != null) lines.push(lang === 'en' ? `Elevation ${Math.round(alt)} m` : `海拔 ${Math.round(alt)} m`)
  if (photo.taken) lines.push(fmtTime(photo.taken).slice(0, 10))
  return lines
}

// 组件图标：与成品同一种画法的缩略

function NorthGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18">
      <path d="M12 3 L17 19 L12 15.5 Z" fill="currentColor" />
      <path d="M12 3 L7 19 L12 15.5 Z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  )
}

function ScaleGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18">
      <rect x="3" y="9" width="18" height="5" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="3" y="9" width="4.5" height="5" fill="currentColor" />
      <rect x="12" y="9" width="4.5" height="5" fill="currentColor" />
    </svg>
  )
}

function ViewGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 18 L12 6 L20 18" />
      <path d="M12 6 V2.5" />
      <circle cx="12" cy="13.5" r="2" />
    </svg>
  )
}

function TextGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M5 6 H19 M12 6 V19 M9 19 H15" />
    </svg>
  )
}
