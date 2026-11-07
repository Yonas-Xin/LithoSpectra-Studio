// 照片窗口：在主界面上弹出的一个窗口。左侧工具栏切换「信息 / 修补 / 编辑 / 地质水印」，
// 所有调整都在这个窗口里完成，不再另开对话框；下面一排缩略图翻页。
//
// 照片是原始资料，原片不改。去水印、框选修补、编辑器的结果是一步步的「调整」，可以
// 撤销上一步或全部清除；地质水印只记参数，随时回来改；朝向与说明只记在库里。这些都
// 在「导出」时才落到文件上。

import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { App as AntApp, Button, Input, InputNumber, Segmented, Select, Spin, Tooltip } from 'antd'

import type { TaskInfo } from '../api'
import { Icon } from '../theme'
import type { IconName } from '../generated/icons'
import { directionName, fmtTime, fw, photoUrl } from './fwapi'
import type { FwPhoto, FwPoint, FwPointDetail, FwStatus, OvitalMark, PhotoExif } from './fwapi'
import type { ExportRequest } from './ExportDialog'

const GeoMarkEditor = lazy(() => import('./GeoMarkEditor'))
const ImageEditor = lazy(() => import('./ImageEditor'))

const OP_NAMES: Record<string, string> = {
  'ovmark-remove': '去除奥维水印', inpaint: '框选修补', edit: '图片编辑',
  geomark: '地质水印（旧版）', azimuth: '写入朝向（旧版）', legacy: '旧版编辑',
}

type Mode = 'info' | 'repair' | 'edit' | 'mark'

const MODES: { key: Mode; icon: IconName; label: string; tip: string }[] = [
  { key: 'info', icon: 'info', label: '信息', tip: '照片信息与调整记录' },
  { key: 'repair', icon: 'eraser', label: '修补', tip: '去除奥维水印、框选修补' },
  { key: 'edit', icon: 'crop', label: '编辑', tip: '裁剪、旋转、调色、标注' },
  { key: 'mark', icon: 'layers', label: '水印', tip: '指北针、比例尺、镜向、文字' },
]

interface Props {
  photos: FwPhoto[]
  index: number
  point: FwPointDetail | null
  points: FwPoint[]
  status: FwStatus | null
  onIndex: (i: number) => void
  onClose: () => void
  onChanged: (ph: FwPhoto | null, removedUid?: string) => void
  onTask: <T>(start: () => Promise<TaskInfo>, failTitle: string) => Promise<T | null>
  onStatus: (t: string) => void
  onStatusRefresh: () => void
  onExport: (req: ExportRequest) => void
}

type View = { scale: number; x: number; y: number }
type Mark = { mark: OvitalMark; exif: PhotoExif }

export default function PhotoViewer(props: Props) {
  const { message, modal } = AntApp.useApp()
  const ph = props.photos[props.index]
  const stageRef = useRef<HTMLDivElement>(null)
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null)
  const [view, setView] = useState<View>({ scale: 1, x: 0, y: 0 })
  const [fit, setFit] = useState(1)
  const [showOriginal, setShowOriginal] = useState(false)
  const [full, setFull] = useState(false)
  const [fullW, setFullW] = useState<number | null>(null)
  const [exif, setExif] = useState<PhotoExif | null>(null)
  const [mode, setMode] = useState<Mode>('info')
  const [mark, setMark] = useState<Mark | null>(null)
  const [markAz, setMarkAz] = useState<number | null>(null)
  const [analysing, setAnalysing] = useState(false)
  const [rects, setRects] = useState<number[][]>([])
  const [drawing, setDrawing] = useState<number[] | null>(null)
  const [caption, setCaption] = useState(ph?.caption ?? '')
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null)
  const space = useRef(false)
  const stripRef = useRef<HTMLElement>(null)

  // 用方向键翻页时，下面一排缩略图跟着滚，当前这张始终看得见
  useEffect(() => {
    stripRef.current?.querySelector<HTMLElement>('.pv-thumb.cur')?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [props.index, props.photos.length])

  useEffect(() => {
    setCaption(ph?.caption ?? '')
    setShowOriginal(false)
    setFull(false)
    setFullW(null)
    setRects([])
    setDrawing(null)
    setMark(null)
    setExif(null)
    if (ph) fw.photoInfo(ph.uid).then(setExif).catch(() => setExif(null))
  }, [ph?.uid, ph?.file])

  // 编辑器与水印编辑各占整块区域，换照片时退回信息页
  useEffect(() => {
    setMode((m) => (m === 'edit' || m === 'mark' ? 'info' : m))
  }, [ph?.uid])

  const fitView = useCallback((nw: number, nh: number) => {
    const el = stageRef.current
    if (!el) return
    const s = Math.min(el.clientWidth / nw, el.clientHeight / nh, 1) * 0.96
    setFit(s)
    setView({ scale: s, x: (el.clientWidth - nw * s) / 2, y: (el.clientHeight - nh * s) / 2 })
  }, [])

  // 窗口大小变了重新适配
  useEffect(() => {
    const el = stageRef.current
    if (!el || !natural) return
    const ro = new ResizeObserver(() => fitView(natural.w, natural.h))
    ro.observe(el)
    return () => ro.disconnect()
  }, [natural, fitView, mode])

  // 放大过了显示档位就换原图
  useEffect(() => {
    if (natural && !full && view.scale > fit * 1.6) setFull(true)
  }, [view.scale, fit, natural, full])

  const busyEditor = mode === 'edit' || mode === 'mark'

  // 键盘
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest?.('input, textarea, .ant-select')) return
      if (modalShown()) return
      if (e.key === ' ') { space.current = true; return }
      if (busyEditor) return
      if (e.key === 'ArrowLeft' && props.index > 0) props.onIndex(props.index - 1)
      else if (e.key === 'ArrowRight' && props.index < props.photos.length - 1) props.onIndex(props.index + 1)
      else if (e.key === 'Escape') {
        if (drawing || rects.length) { setDrawing(null); setRects([]) }
        else if (mark) setMark(null)
        else if (mode !== 'info') setMode('info')
        else props.onClose()
      } else if (e.key === '0' && natural) fitView(natural.w, natural.h)
      else if ((e.key === '+' || e.key === '=' || e.key === '-') && natural && stageRef.current) {
        // 以看图区中心为准放大、缩小一档，与滚轮同样的上下限
        const el = stageRef.current
        const mx = el.clientWidth / 2
        const my = el.clientHeight / 2
        setView((v) => {
          const scale = Math.min(Math.max(v.scale * (e.key === '-' ? 1 / 1.25 : 1.25), fit * 0.5), 8)
          const f = scale / v.scale
          return { scale, x: mx - (mx - v.x) * f, y: my - (my - v.y) * f }
        })
      }
    }
    const onUpKey = (e: KeyboardEvent) => { if (e.key === ' ') space.current = false }
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onUpKey)
    return () => { window.removeEventListener('keydown', onKey); window.removeEventListener('keyup', onUpKey) }
  }, [props, busyEditor, mark, drawing, rects, mode, natural, fitView, fit])

  if (!ph) return null

  // 显示的图与照片原尺寸（已转正）的比例：框选修补要换算回原图像素
  const toPhoto = natural && ph.width ? ph.width / natural.w : 1
  // 修补是在底图上做的：修补页显示不含地质水印的底图
  const variant = showOriginal ? 'original' : mode === 'repair' ? 'base' : 'adjusted'
  const baseSrc = photoUrl(ph, 1600, variant)
  const fullSrc = photoUrl(ph, 0, variant)
  const imgStyle = (k: number): React.CSSProperties => ({ transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale * k})` })
  const erasing = mode === 'repair' && !mark && !showOriginal

  const clientToImage = (cx: number, cy: number) => {
    const r = stageRef.current!.getBoundingClientRect()
    return [(cx - r.left - view.x) / view.scale, (cy - r.top - view.y) / view.scale]
  }

  const onWheel = (e: React.WheelEvent) => {
    const r = stageRef.current!.getBoundingClientRect()
    const mx = e.clientX - r.left
    const my = e.clientY - r.top
    const k = Math.exp(-e.deltaY * 0.0015)
    const scale = Math.min(Math.max(view.scale * k, fit * 0.5), 8)
    const f = scale / view.scale
    setView({ scale, x: mx - (mx - view.x) * f, y: my - (my - view.y) * f })
  }

  // 左键：修补页框选，其余拖动平移；中键或按住空格始终平移
  const onDown = (e: React.MouseEvent) => {
    const pan = e.button === 1 || (e.button === 0 && (space.current || !erasing))
    if (pan) {
      e.preventDefault()
      drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }
      return
    }
    if (e.button === 0 && erasing) {
      const [x, y] = clientToImage(e.clientX, e.clientY)
      setDrawing([x, y, x, y])
    }
  }
  const onMove = (e: React.MouseEvent) => {
    if (drawing) {
      const [x, y] = clientToImage(e.clientX, e.clientY)
      setDrawing([drawing[0], drawing[1], x, y])
      return
    }
    const d = drag.current
    if (d) setView({ ...view, x: d.vx + e.clientX - d.x, y: d.vy + e.clientY - d.y })
  }
  const onUp = () => {
    if (drawing) {
      const [a, b, c, d] = drawing
      const r = [Math.min(a, c), Math.min(b, d), Math.max(a, c), Math.max(b, d)]
      if (r[2] - r[0] > 3 && r[3] - r[1] > 3) setRects([...rects, r])
      setDrawing(null)
    }
    drag.current = null
  }

  // ------------------------------------------------------------- 动作
  const changed = (p: FwPhoto | null, removed?: string) => props.onChanged(p, removed)

  const analyse = async () => {
    setAnalysing(true)
    try {
      const r = await fw.analyseMark(ph.uid)
      if (!r.mark.found) {
        message.info('照片左下角未识别到奥维水印。其他水印可在照片上框选后修补。')
        return
      }
      setRects([])
      setMark(r)
      setMarkAz(r.mark.azimuth)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setAnalysing(false)
    }
  }

  const removeMark = async () => {
    const r = mark!
    setMark(null)
    const override = markAz != null && markAz !== r.mark.azimuth ? markAz : (r.mark.azimuth_ok ? null : markAz)
    const res = await props.onTask<{ photo: FwPhoto; engine: string }>(() => fw.removeMark(ph.uid, override), '去除奥维水印失败')
    if (!res) return
    changed(res.photo)
    props.onStatus(`已去除奥维水印（${res.engine === 'lama' ? 'LaMa 修补' : 'OpenCV 修补'}）` +
      (res.photo.azimuth != null ? `，朝向记为 ${directionName(res.photo.azimuth)} ${Math.round(res.photo.azimuth)}°` : '') + '。原片未改动。')
  }

  const runErase = async () => {
    const list = rects.map((r) => r.map((v) => v * toPhoto))
    setRects([])
    const res = await props.onTask<{ photo: FwPhoto }>(() => fw.inpaintRects(ph.uid, list), '修补失败')
    if (res) { changed(res.photo); props.onStatus(`已修补 ${list.length} 处，可撤销；原片未改动。`) }
  }

  const downloadModel = () => modal.confirm({
    title: '下载 LaMa 修补模型？',
    content: `约 ${Math.round((props.status?.inpaint.model_size ?? 208e6) / 1e6)} MB，从 Hugging Face（国内使用 hf-mirror 镜像）下载，保存到 ${props.status?.inpaint.model_path}。LaMa 修补出的纹理比默认的 OpenCV 更自然。`,
    okText: '下载', cancelText: '暂不下载',
    onOk: async () => {
      const r = await props.onTask(() => fw.downloadModel(), '模型下载失败')
      if (r) { props.onStatusRefresh(); message.success('模型已就绪。') }
    },
  })

  const saveAzimuth = async (v: number | null) => {
    if (v === ph.azimuth) return
    try {
      changed(await fw.patchPhoto(ph.uid, { azimuth: v, azimuth_src: 'manual' }))
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const undoStep = async () => {
    const last = ph.edits[ph.edits.length - 1]
    changed(await fw.undoStep(ph.uid))
    props.onStatus(`已撤销：${OP_NAMES[last?.op] ?? last?.op ?? '上一步'}`)
  }
  const removeGeomark = async () => {
    changed(await fw.removeGeomark(ph.uid))
    props.onStatus('已移除地质水印。')
  }
  const clearAll = () => modal.confirm({
    title: '清除这张照片的全部调整？',
    content: '将去除全部去水印、修补、编辑与地质水印，恢复为原片。朝向与说明保留。',
    okText: '清除', okButtonProps: { danger: true }, cancelText: '取消',
    onOk: async () => { changed(await fw.revertPhoto(ph.uid)); props.onStatus('已清除全部调整，恢复为原片。') },
  })

  const remove = () => modal.confirm({
    title: '从库中移除这张照片？',
    content: '仅从库中移除该记录。媒体文件夹中的原片保留，计算机上的源文件不受影响。',
    okText: '移除', okButtonProps: { danger: true }, cancelText: '取消',
    onOk: async () => { await fw.deletePhoto(ph.uid); changed(null, ph.uid) },
  })

  const exportThis = () => {
    const pt = props.point
    props.onExport({
      title: `${pt?.name ?? '照片'} · ${ph.orig_name ?? ''}`, count: 1, uids: [ph.uid],
      sample: { point: pt?.name || '未关联点位', orig: ph.orig_name ?? 'IMG', date: (ph.taken ?? '').slice(0, 10).replace(/-/g, ''), survey: pt?.survey ?? '' },
    })
  }

  const moveTo = async (uid: string | null) => {
    await fw.movePhoto(ph.uid, uid)
    changed(null, ph.uid)
    props.onStatus(uid ? '照片已关联到所选点位。' : '已取消照片与点位的关联。')
  }

  const switchMode = (m: Mode) => {
    if (m === mode) return
    setMark(null)
    setRects([])
    setDrawing(null)
    setShowOriginal(false)
    setMode(m)
  }

  const az = ph.azimuth
  const locked = new Set(ph.locked ?? [])
  const edits = ph.edits ?? []
  const lama = props.status?.inpaint.model && props.status?.inpaint.onnxruntime
  const shownRects = [...rects, ...(drawing ? [[
    Math.min(drawing[0], drawing[2]), Math.min(drawing[1], drawing[3]),
    Math.max(drawing[0], drawing[2]), Math.max(drawing[1], drawing[3])]] : [])]

  // ------------------------------------------------------------- 右侧面板
  const infoPanel = (
    <>
      <Section title="照片信息">
        <div className="pv-kv">
          <label>拍摄时间</label>
          <span className="mono">{fmtTime(ph.taken) || '—'}{locked.has('taken') ? <Lock /> : null}</span>
          <label>位置</label>
          <span className="mono">
            {ph.lon != null ? `${ph.lon.toFixed(6)}°E ${ph.lat!.toFixed(6)}°N` : '—'}
            {locked.has('position') ? <Lock /> : ph.lon != null ? <Lock text="补记" /> : null}
          </span>
          <label>海拔</label>
          <span className="mono">
            {ph.altitude != null ? `${ph.altitude} m` : '—'}
            {locked.has('altitude') ? <Lock /> : ph.altitude != null ? <Lock text="补记" /> : null}
          </span>
          <label>方位角</label>
          {locked.has('azimuth') ? (
            <span><span className="mono">{az}°</span><span className="caption"> {directionName(az ?? 0)}</span><Lock /></span>
          ) : (
            <span className="fw-inline">
              <InputNumber size="small" min={0} max={359.9} step={1} value={az ?? undefined} placeholder="未记录"
                addonAfter="°" style={{ width: 100, flex: 'none' }}
                onBlur={(e) => { const v = e.target.value === '' ? null : parseFloat(e.target.value); if (v === null || !Number.isNaN(v)) saveAzimuth(v) }}
                onPressEnter={(e) => (e.target as HTMLInputElement).blur()} />
              {az != null ? <span className="caption nowrap">{directionName(az)} · {srcName(ph.azimuth_src)}</span> : null}
            </span>
          )}
          <label>尺寸</label>
          <span className="mono">{ph.width ? `${ph.width} × ${ph.height}` : '—'}</span>
          {exif?.model ? (<><label>相机</label><span>{exif.make} {exif.model}</span></>) : null}
          <label>说明</label>
          <Input.TextArea size="small" autoSize={{ minRows: 2, maxRows: 5 }} value={caption} placeholder="岩性、产状、现象…"
            onChange={(e) => setCaption(e.target.value)}
            onBlur={async () => { if (caption !== (ph.caption ?? '')) changed(await fw.patchPhoto(ph.uid, { caption })) }} />
        </div>
        <div className="caption pv-note">标「原片」的信息来自照片 EXIF，不可修改。补记的信息保存在库中，导出时可补写进 EXIF。</div>
      </Section>

      <Section title="调整记录" extra={ph.adjusted ? <button className="tb-btn" onClick={clearAll}>全部清除</button> : null}>
        {ph.adjusted ? (
          <div className="fw-steps">
            {edits.map((e, i) => (
              <div key={i} className="fw-step">
                <span className="fw-step-no mono">{i + 1}</span>
                <span className="fw-step-name">{OP_NAMES[e.op] ?? e.op}</span>
                <span className="caption mono">{fmtTime(e.time).slice(5)}</span>
                {i === edits.length - 1 ? (
                  <Tooltip title="撤销这一步"><button className="ibtn xs" onClick={undoStep}><Icon name="undo" size={13} /></button></Tooltip>
                ) : <span className="fw-step-pad" />}
              </div>
            ))}
            {ph.overlay ? (
              <div className="fw-step">
                <span className="fw-step-no"><Icon name="layers" size={12} /></span>
                <span className="fw-step-name">地质水印</span>
                <Tooltip title="修改"><button className="ibtn xs" onClick={() => switchMode('mark')}><Icon name="pencil" size={13} /></button></Tooltip>
                <Tooltip title="移除"><button className="ibtn xs" onClick={removeGeomark}><Icon name="trash" size={13} /></button></Tooltip>
              </div>
            ) : null}
          </div>
        ) : <div className="caption">尚无调整，当前显示原片。用左侧的修补、编辑、水印工具添加调整。</div>}
      </Section>

      <Section title="所属点位">
        <Select size="small" style={{ width: '100%' }} showSearch optionFilterProp="label"
          value={ph.point_uid ?? '__none'}
          onChange={(v) => v !== (ph.point_uid ?? '__none') && moveTo(v === '__none' ? null : v)}
          options={[{ value: '__none', label: '（不关联点位）' },
            ...props.points.map((p) => ({ value: p.uid, label: `${p.name || '未命名'}${p.survey ? '（' + p.survey + '）' : ''}` }))]} />
      </Section>

      <div className="pv-actions">
        <Button className="btn-danger" onClick={remove}>从库中移除</Button>
        <Button type="primary" onClick={exportThis}><Icon name="download" size={14} /> 导出这张</Button>
      </div>
    </>
  )

  const repairPanel = (
    <>
      <Section title="奥维水印">
        {mark ? (
          <>
            <div className="pre fw-mark-text">{mark.mark.text}</div>
            <div className="pv-kv" style={{ marginTop: 10 }}>
              <label>朝向</label>
              <span className="fw-inline">
                <InputNumber size="small" min={0} max={359.9} value={markAz ?? undefined} addonAfter="°"
                  status={!mark.mark.azimuth_ok ? 'warning' : undefined}
                  onChange={(v) => setMarkAz(v ?? null)} style={{ width: 110, flex: 'none' }} />
                {markAz != null ? <span className="caption">{directionName(markAz)}</span> : null}
              </span>
            </div>
            {mark.mark.notes.map((n, i) => <div key={i} className="fw-warn" style={{ marginTop: 6 }}>{n}</div>)}
            <div className="caption pv-note">
              照片上的框是识别出的文字，只去除框内的白色笔画。位置与海拔优先采用照片自带的 GPS。
            </div>
            <div className="pv-row-btns">
              <Button size="small" onClick={() => setMark(null)}>取消</Button>
              <Button size="small" type="primary" onClick={removeMark}>去除水印</Button>
            </div>
          </>
        ) : props.status?.ocr ? (
          <>
            <div className="caption">识别左下角奥维相机的水印文字（经纬度、海拔、朝向），记下朝向后抹掉文字。</div>
            <div className="pv-row-btns"><Button size="small" loading={analysing} onClick={analyse}>识别水印</Button></div>
          </>
        ) : <div className="caption">需要 macOS 与 ocrmac 才能识别水印文字。</div>}
      </Section>

      <Section title="框选修补">
        <div className="caption">
          在照片上拖动框选要去除的区域（标签、杂物、其他水印），可框选多处。按住空格或用中键拖动平移，滚轮缩放。
        </div>
        <div className="pv-row-btns">
          <Button size="small" disabled={!rects.length} onClick={() => setRects([])}>清空</Button>
          <Button size="small" type="primary" disabled={!rects.length} onClick={runErase}>
            修补{rects.length ? ` ${rects.length} 处` : ''}
          </Button>
        </div>
        <div className="caption pv-note">
          修补引擎：{lama ? 'LaMa（纹理自然）' : 'OpenCV'}
          {!lama && props.status?.inpaint.onnxruntime ? <>。<a onClick={downloadModel}>下载 LaMa 模型</a>后修补的纹理更自然。</> : null}
        </div>
      </Section>

      <Section title="调整记录" extra={ph.adjusted ? <button className="tb-btn" onClick={clearAll}>全部清除</button> : null}>
        {edits.length ? (
          <div className="fw-steps">
            {edits.map((e, i) => (
              <div key={i} className="fw-step">
                <span className="fw-step-no mono">{i + 1}</span>
                <span className="fw-step-name">{OP_NAMES[e.op] ?? e.op}</span>
                {i === edits.length - 1 ? (
                  <Tooltip title="撤销这一步"><button className="ibtn xs" onClick={undoStep}><Icon name="undo" size={13} /></button></Tooltip>
                ) : <span className="fw-step-pad" />}
              </div>
            ))}
          </div>
        ) : <div className="caption">尚无像素修补。</div>}
      </Section>
    </>
  )

  // ------------------------------------------------------------- 布局
  const stage = (
    <div className="pv-stage-col">
      <div ref={stageRef} className={'fw-stage pv-stage' + (erasing ? ' erasing' : '')} onWheel={onWheel}
        onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={onUp}
        onDoubleClick={() => {
          if (!natural || erasing) return
          if (view.scale > fit * 1.05) fitView(natural.w, natural.h)
          else {
            const el = stageRef.current!
            const s = fit * 3
            setView({ scale: s, x: (el.clientWidth - natural.w * s) / 2, y: (el.clientHeight - natural.h * s) / 2 })
          }
        }}>
        <img key={baseSrc} src={baseSrc} alt="" draggable={false} style={imgStyle(1)}
          onLoad={(e) => {
            // 每换一张（含调整后的新版本）都按它自己的尺寸重新适配；
            // 不在 effect 里清空：缓存命中时 onLoad 会比 effect 先到
            const im = e.currentTarget
            setNatural({ w: im.naturalWidth, h: im.naturalHeight })
            fitView(im.naturalWidth, im.naturalHeight)
          }} />
        {full && natural ? (
          // 放大后叠一张原图上去；没加载完之前透明，底下那张先顶着
          <img key={fullSrc} src={fullSrc} alt="" draggable={false}
            style={{ ...imgStyle(fullW ? natural.w / fullW : 1), opacity: fullW ? 1 : 0 }}
            onLoad={(e) => setFullW(e.currentTarget.naturalWidth)} />
        ) : null}
        {mark ? mark.mark.items.map((it, i) => (
          <div key={i} className="fw-mark-box" style={boxStyle(it.box, view, natural && ph.width ? natural.w / ph.width : 1)} />
        )) : null}
        {mode === 'repair' ? shownRects.map((r, i) => (
          <div key={i} className="fw-erase-box" style={boxStyle(r, view, 1)} />
        )) : null}
        {!natural ? <div className="fw-stage-loading"><Spin /></div> : null}
        {ph.adjusted && mode !== 'repair' ? (
          <Segmented size="small" className="pv-variant" value={showOriginal ? 'original' : 'adjusted'}
            options={[{ value: 'adjusted', label: '调整后' }, { value: 'original', label: '原片' }]}
            onChange={(v) => { setShowOriginal(v === 'original'); setFullW(null) }} />
        ) : null}
        <div className="pv-zoom">
          <span className="mono">{natural && ph.width ? Math.round(view.scale * natural.w / ph.width * 100) : 100}%</span>
          <Tooltip title="适配窗口（0）；+ / − 放大、缩小"><button className="pv-zoom-btn" onClick={() => natural && fitView(natural.w, natural.h)}><Icon name="maximize" size={13} /></button></Tooltip>
        </div>
      </div>
    </div>
  )

  return (
    <div className="pv-backdrop">
      <div className="pv-window" role="dialog" aria-label="照片">
        <header className="pv-head">
          <div className="pv-title">
            <span className="pv-title-main">{props.point?.name ?? '未关联点位'}</span>
            <span className="pv-title-sub">{ph.orig_name ?? ''}</span>
          </div>
          <div className="pv-nav">
            <button className="pv-nav-btn" disabled={props.index <= 0} onClick={() => props.onIndex(props.index - 1)} aria-label="上一张">‹</button>
            <span className="mono">{props.index + 1} / {props.photos.length}</span>
            <button className="pv-nav-btn" disabled={props.index >= props.photos.length - 1} onClick={() => props.onIndex(props.index + 1)} aria-label="下一张">›</button>
          </div>
          <div className="spacer" />
          {ph.adjusted ? <span className="pv-badge">已调整</span> : <span className="pv-badge plain">原片</span>}
          <Tooltip title="导出这张照片"><button className="pv-head-btn" onClick={exportThis}><Icon name="download" size={15} />导出</button></Tooltip>
          <Tooltip title="关闭（Esc）"><button className="pv-head-btn icon" onClick={props.onClose} aria-label="关闭">×</button></Tooltip>
        </header>

        <div className="pv-body">
          <nav className="pv-rail">
            {MODES.map((m) => (
              <Tooltip key={m.key} title={m.tip} placement="right">
                <button className={'pv-rail-btn' + (mode === m.key ? ' on' : '')} onClick={() => switchMode(m.key)}>
                  <Icon name={m.icon} size={18} />
                  <span>{m.label}</span>
                </button>
              </Tooltip>
            ))}
          </nav>

          {mode === 'edit' ? (
            <div className="pv-editor">
              <Suspense fallback={<div className="fw-stage-loading"><Spin /></div>}>
                <ImageEditor photo={ph} onClose={() => setMode('info')} onTask={props.onTask}
                  onSaved={(p) => { setMode('info'); changed(p); props.onStatus('编辑已保存为一步调整，可撤销；原片未改动。') }} />
              </Suspense>
            </div>
          ) : mode === 'mark' ? (
            <div className="pv-editor">
              <Suspense fallback={<div className="fw-stage-loading"><Spin /></div>}>
                <GeoMarkEditor photo={ph} point={props.point} onClose={() => setMode('info')}
                  onSaved={(p) => { setMode('info'); changed(p); props.onStatus('地质水印已保存，可随时修改或移除。') }} />
              </Suspense>
            </div>
          ) : (
            <>
              {stage}
              <aside className="pv-side">
                <div className="pv-side-head">{MODES.find((m) => m.key === mode)?.tip}</div>
                <div className="pv-side-scroll">{mode === 'repair' ? repairPanel : infoPanel}</div>
              </aside>
            </>
          )}
        </div>

        <footer className="pv-strip" ref={stripRef}>
          {props.photos.map((p, i) => (
            <button key={p.uid} className={'pv-thumb' + (i === props.index ? ' cur' : '')} onClick={() => props.onIndex(i)}
              title={[p.orig_name, fmtTime(p.taken)].filter(Boolean).join('\n')}>
              <img src={photoUrl(p, 160)} alt="" draggable={false} />
              {p.adjusted ? <span className="pv-thumb-dot" /> : null}
            </button>
          ))}
        </footer>
      </div>
    </div>
  )
}

function Section({ title, extra, children }: { title: string; extra?: ReactNode; children: ReactNode }) {
  return (
    <section className="pv-section">
      <div className="pv-section-head"><span>{title}</span>{extra}</div>
      {children}
    </section>
  )
}

function Lock({ text = '原片' }: { text?: string }) {
  return <span className="fw-lock">{text}</span>
}

/** 有没有开着的对话框（antd 关掉的对话框还留在 DOM 里，只是隐藏了） */
function modalShown(): boolean {
  return [...document.querySelectorAll('.ant-modal-wrap')].some((e) => getComputedStyle(e).display !== 'none')
}

function srcName(s: string | null): string {
  return { exif: '照片 EXIF', ocr: '水印识别', manual: '手动' }[s ?? ''] ?? ''
}

/** 原图像素框 → 屏幕上的绝对定位。k 是显示图与原图的比例。 */
function boxStyle(b: number[], v: View, k: number): React.CSSProperties {
  return {
    left: v.x + b[0] * k * v.scale, top: v.y + b[1] * k * v.scale,
    width: (b[2] - b[0]) * k * v.scale, height: (b[3] - b[1]) * k * v.scale,
  }
}
