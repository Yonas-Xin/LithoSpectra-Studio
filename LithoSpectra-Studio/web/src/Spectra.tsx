// 光谱模块的界面：曲线窗口、光谱库窗口、按类别取平均谱。
//
// 曲线窗口：上面一行工具（导入导出、光谱库、绑定影像、钉住清空），不常动的样式与版式
// 收进「样式」面板；中间是图，鼠标浮标的读数跟着十字线走；底下一行选横纵轴、范围、
// 取谱窗口与平滑。曲线清单是可收起的侧栏。图由后端用 matplotlib 画（plot.draw_into），
// 这里只负责操作。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { App as AntApp, Button, Dropdown, Input, InputNumber, Modal, Popover, Segmented, Select, Slider, Switch, Table, Tooltip, Tree, TreeSelect } from 'antd'
import type { MenuProps, TreeDataNode } from 'antd'
import { call, get, post } from './api'
import type { TaskInfo } from './api'
import { desktop } from './desktop'
import Float from './Float'
import { Chevron, Icon } from './theme'
import { useTaskRunner } from './tasks'

// =============================================================================
// 接口
// =============================================================================

export interface Curve {
  id: number
  label: string
  color: string
  custom_color: boolean
  visible: boolean
  live: boolean
  origin: string
  wavelength: boolean
  valid: number
  coord: string
  lon: number
  lat: number
  note: string
  layer: string
}

export interface BoardState {
  layer_id: number | null
  layer_name: string
  curves: Curve[]
  x_band: boolean
  x_options: { wave: boolean; band: boolean }
  unit: string
  unit_active: string
  unit_label: string
  window: number
  smooth: boolean
  smooth_window: number
  smooth_order: number
  style: Record<string, unknown>
  range: string
  dpi: number
  note: string
  marks: { lon: number; lat: number; color: string; live: boolean }[]
  message?: string
}

interface Meta {
  units: { key: string; menu: string }[]
  windows: number[]
  ranges: { key: string; span: number[] | null; feature: string }[]
  edge_tolerance: number[]
  presets: { name: string; w: number; h: number }[]
  color_sets: { key: string; name: string; colors: string[] }[]
  gap_modes: { key: string; name: string }[]
  feature_modes: { key: string; name: string }[]
  marker_modes: { key: string; name: string }[]
  legend_places: { key: string; name: string }[]
  fonts: number[]
  dpis: number[]
  line_width: number[]
  marker_size: number[]
  smooth_window: number[]
  smooth_order: number[]
  algorithms: { key: string; name: string; tip: string }[]
  library: boolean
}

interface PlotMeta {
  png: string
  axes?: number[]
  xlim?: number[]
  ylim?: number[]
  series?: { label: string; color: string; x: (number | null)[]; y: (number | null)[] }[]
}

/** 一条匹配结果。lo / hi 是实际参与比较的波长段，coverage 是它占参考曲线有效波段的百分比 */
interface Hit {
  id: string
  display: string
  score: number
  metric: string
  bands: number
  lo: number | null
  hi: number | null
  coverage: number
}

interface Entry {
  id: string
  name: string
  cn: string
  display: string
  library: string
  source: string
  sample: string
  wl: number[]
  count: number
  cls: string
  formula: string
}

export const sapi = {
  meta: () => get<Meta>('/api/spectra/meta'),
  state: () => get<BoardState>('/api/spectra/state'),
  bind: (layer_id: number | null) => post<BoardState>('/api/spectra/bind', { layer_id }),
  pick: (lon: number, lat: number, append: boolean) => post<BoardState>('/api/spectra/pick', { lon, lat, append }),
  pin: () => post<BoardState>('/api/spectra/pin'),
  options: (body: Record<string, unknown>) => post<BoardState>('/api/spectra/options', body),
  patchCurve: (id: number, body: { label?: string; color?: string; visible?: boolean }) =>
    call<BoardState>('PATCH', `/api/spectra/curves/${id}`, body),
  deleteCurves: (ids: number[]) => post<BoardState>('/api/spectra/curves/delete', { ids }),
  clear: () => post<BoardState>('/api/spectra/clear'),
  plot: (w: number, h: number, scale: number) => get<PlotMeta>(`/api/spectra/plot?width=${w}&height=${h}&scale=${scale}`),
  export: (path: string, kind: 'figure' | 'csv') => post<{ path: string }>('/api/spectra/export', { path, kind }),
  importFile: (path: string) => post<BoardState & { added: number; notes: string[] }>('/api/spectra/import', { path }),
  tree: () => get<{ categories: { path: string; label: string; cn: string; count: number; depth: number }[]; groups: { key: string; cn?: string; name?: string; count: number }[]; total: number; grouped: number; user_folders: string[]; user_tree: { path: string; name: string; count: number }[]; user_count: number; user_root: string; user_root_cn: string }>('/api/speclib/tree'),
  search: (body: { text?: string; category?: string; group?: string; library?: string; limit?: number }) => post<Entry[]>('/api/speclib/search', body),
  entry: (id: string) => get<{ id: string; title: string; detail: string; x: (number | null)[]; y: (number | null)[]; library: string; name: string; cn: string; note: string }>(`/api/speclib/entries/${encodeURIComponent(id)}`),
  addToPlot: (ids: string[]) => post<BoardState & { added: number; message: string }>('/api/speclib/add-to-plot', { ids }),
  match: (body: { curve_id: number; text?: string; category?: string; group?: string; library?: string; algorithm: string; continuum: boolean; mask_water: boolean; span?: number[] | null; full_cover: boolean; edge_tolerance: number }) => post<{ task: TaskInfo }>('/api/speclib/match', body),
  saveCurves: (curve_ids: number[], folder: string) => post<{ saved: number }>('/api/speclib/user/save', { curve_ids, folder }),
  patchEntry: (id: string, body: { name?: string; note?: string; cls?: string }) => call<{ ok: boolean }>('PATCH', `/api/speclib/user/entries/${encodeURIComponent(id)}`, body),
  deleteEntries: (ids: string[]) => post<{ removed: number }>('/api/speclib/user/delete', { ids }),
  moveEntries: (ids: string[], folder: string) => post<{ moved: number }>('/api/speclib/user/move', { ids, folder }),
  addFolder: (path: string) => post<{ path: string }>('/api/speclib/user/folders', { path }),
  renameFolder: (path: string, name: string) => post<{ path: string }>('/api/speclib/user/folders/rename', { path, name }),
  deleteFolder: (path: string, mode: '' | 'move' | 'delete' = '') => post<{ ok: boolean }>('/api/speclib/user/folders/delete', { path, mode }),
  moveFolder: (path: string, parent: string) => post<{ path: string }>('/api/speclib/user/folders/move', { path, parent }),
  folderUsage: (path: string) => get<{ primary: number; tagged: number; subfolders: number }>(`/api/speclib/user/folders/usage?path=${encodeURIComponent(path)}`),
  importLib: (path: string, folder = '') => post<{ added: number; skipped: number; notes: string[] }>('/api/speclib/user/import', { path, folder }),
  exportLib: (ids: string[], path: string, kind: string) => post<{ exported: number; path: string }>('/api/speclib/export', { ids, path, kind }),
  groups: () => get<{ key: string; name: string; count: number; color: string; parent: string; detail: string }[]>('/api/spectra/groups'),
  groupMeans: (keys: string[]) => post<{ task: TaskInfo }>('/api/spectra/group-means', { keys }),
}

/** 问一句话。WebView 里浏览器自带的输入框不可靠，一律走界面里的对话框。 */
function useAsk() {
  const { modal } = AntApp.useApp()
  const ask = (title: string, initial = ''): Promise<string | null> =>
    new Promise((resolve) => {
      let v = initial
      modal.confirm({
        title, icon: null, okText: '确定',
        content: <Input autoFocus defaultValue={initial} onChange={(e) => (v = e.target.value)} />,
        onOk: () => resolve(v), onCancel: () => resolve(null),
      })
    })
  const confirm = (title: string): Promise<boolean> =>
    new Promise((resolve) => modal.confirm({ title, okText: '确定', okButtonProps: { danger: true }, onOk: () => resolve(true), onCancel: () => resolve(false) }))
  return { ask, confirm }
}

// =============================================================================
// 曲线窗口
// =============================================================================

export function SpectrumWindow(props: {
  board: BoardState
  setBoard: (b: BoardState) => void
  rasters: { id: number; name: string }[]
  onClose: () => void
  onOpenLibrary: () => void
  onGroupMeans: () => void
  onStatus: (s: string, path?: string) => void
}) {
  const { message } = AntApp.useApp()
  const { ask } = useAsk()
  const { board, setBoard } = props
  const [meta, setMeta] = useState<Meta | null>(null)
  const [plot, setPlot] = useState<PlotMeta | null>(null)
  const [showList, setShowList] = useState(false)
  const [picked, setPicked] = useState<number[]>([])
  const [hover, setHover] = useState<{ x: number; px: number; rows: [string, string, string][] } | null>(null)
  const [dragX, setDragX] = useState<[number, number] | null>(null)
  const plotRef = useRef<HTMLDivElement>(null)
  const imgRef = useRef<HTMLImageElement>(null)
  const size = useRef({ w: 800, h: 360 })
  const timer = useRef(0)
  const st = board.style as Record<string, number | string | boolean | null>

  useEffect(() => { sapi.meta().then(setMeta) }, [])

  const redraw = useCallback(async () => {
    const { w, h } = size.current
    if (w < 50 || h < 50) return
    try {
      setPlot(await sapi.plot(Math.round(w), Math.round(h), window.devicePixelRatio || 1))
    } catch (e) {
      message.error((e as Error).message)
    }
  }, [message])

  // 面板大小变了：等停手再按新尺寸重画一次（拉一次窗口重排几十次没有意义）
  useEffect(() => {
    const el = plotRef.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => {
      size.current = { w: e.contentRect.width, h: e.contentRect.height }
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(redraw, 120)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [redraw])
  useEffect(() => { redraw() }, [board, redraw])

  const apply = async (fn: () => Promise<BoardState>) => {
    try {
      const s = await fn()
      setBoard(s)
      if (s.message) props.onStatus(s.message)
    } catch (e) {
      message.error((e as Error).message)
    }
  }
  const setStyle = (style: Record<string, unknown>) => apply(() => sapi.options({ style }))

  // ------------------------------------------------------------- 浮标与拖范围
  /** 鼠标位置 -> 数据坐标。轴在图上的位置由后端随图给出（图左下为原点的分数）。 */
  const toData = (clientX: number) => {
    const img = imgRef.current
    if (!img || !plot?.axes || !plot.xlim) return null
    const r = img.getBoundingClientRect()
    const fx = (clientX - r.left) / r.width
    const [x0, , x1] = plot.axes
    if (fx < x0 || fx > x1) return null
    return plot.xlim[0] + ((fx - x0) / (x1 - x0)) * (plot.xlim[1] - plot.xlim[0])
  }
  const toPx = (x: number) => {
    const img = imgRef.current
    if (!img || !plot?.axes || !plot.xlim) return 0
    const r = img.getBoundingClientRect()
    const [x0, , x1] = plot.axes
    return (x0 + ((x - plot.xlim[0]) / (plot.xlim[1] - plot.xlim[0])) * (x1 - x0)) * r.width + (r.left - (plotRef.current?.getBoundingClientRect().left ?? 0))
  }
  const onMove = (e: React.MouseEvent) => {
    const x = toData(e.clientX)
    if (dragX) {
      setDragX([dragX[0], e.clientX])
      return
    }
    if (x === null || !plot?.series?.length) return setHover(null)
    // 浮标对齐到采样点：光谱的横轴是离散波段，读一个插值出来的数没有意义
    let snap = x
    let best = Infinity
    for (const s of plot.series) for (const v of s.x) if (v !== null && Math.abs(v - x) < best) { best = Math.abs(v - x); snap = v }
    const rows: [string, string, string][] = []
    for (const s of plot.series.slice(0, 10)) {
      const i = s.x.findIndex((v) => v === snap)
      const y = i >= 0 ? s.y[i] : null
      rows.push([s.label, s.color, y === null || y === undefined ? '—' : Number(y).toPrecision(4)])
    }
    setHover({ x: snap, px: toPx(snap), rows })
  }
  const onDown = (e: React.MouseEvent) => {
    if (e.button !== 0 || toData(e.clientX) === null) return
    setDragX([e.clientX, e.clientX])
  }
  const onUp = (e: React.MouseEvent) => {
    if (!dragX) return
    const [a] = dragX
    setDragX(null)
    // 按下之后挪出六个像素才算拖
    if (Math.abs(e.clientX - a) < 6 || !board.x_options.wave || board.x_band) return
    const lo = toData(Math.min(a, e.clientX))
    const hi = toData(Math.max(a, e.clientX))
    if (lo !== null && hi !== null) apply(() => sapi.options({ custom_range: [lo, hi] }))
  }

  // ------------------------------------------------------------- 菜单
  const exportFigure = async () => {
    const dt = desktop()
    const p = dt ? await dt.save_file('spectra.png') : await ask('导出图（.png / .pdf / .svg / .eps / .tif）', 'spectra.png')
    if (!p) return
    try {
      const r = await sapi.export(p, 'figure')
      props.onStatus(`已导出图：${r.path}`, r.path)
    } catch (e) { message.error((e as Error).message) }
  }
  const exportCsv = async () => {
    const dt = desktop()
    const p = dt ? await dt.save_file('spectra.csv') : await ask('导出数据（CSV）', 'spectra.csv')
    if (!p) return
    try {
      const r = await sapi.export(p, 'csv')
      props.onStatus(`已导出数据：${r.path}`, r.path)
    } catch (e) { message.error((e as Error).message) }
  }
  const importAny = async () => {
    const dt = desktop()
    const p = dt ? await dt.pick_file('any') : await ask('导入曲线（本窗口导出的 CSV、光谱仪文本表、ENVI 光谱库）')
    if (!p) return
    try {
      const r = await sapi.importFile(p)
      setBoard(r)
      props.onStatus(`已导入 ${r.added} 条曲线。${r.notes.join('；')}`)
    } catch (e) { message.error((e as Error).message) }
  }
  const saveToLibrary = async (ids: number[]) => {
    if (!ids.length) return props.onStatus('图中暂无曲线。请先在影像上取谱，或从文件导入。')
    const folder = await ask('保存到「我的光谱」的分类（留空则保存在根目录，可填写路径，如 我的分类/海德乌拉）', '')
    if (folder === null) return
    try {
      const r = await sapi.saveCurves(ids, folder)
      props.onStatus(`已保存 ${r.saved} 条到「我的光谱」。`)
    } catch (e) { message.error((e as Error).message) }
  }

  // ------------------------------------------------------------- 样式面板
  const styleTitle = async () => {
    const t = await ask('图标题（留空则不显示）', String(st.title ?? ''))
    if (t !== null) setStyle({ title: t })
  }
  const preset = meta?.presets.find((p) => p.w === st.width_mm && p.h === st.height_mm)?.name ?? ''
  const stylePanel = meta ? (
    <div className="pop-panel spec-style">
      <section>
        <h4>曲线</h4>
        <div className="pop-row"><label>线宽</label>
          <Slider min={meta.line_width[0]} max={meta.line_width[1]} step={0.05} defaultValue={Number(st.curve_lw)}
            key={'lw' + st.curve_lw} onChangeComplete={(v: number) => setStyle({ curve_lw: v })} /></div>
        <div className="pop-row"><label>数据点</label>
          <Select size="small" style={{ width: 96 }} value={String(st.marker_mode)} options={meta.marker_modes.map((m) => ({ value: m.key, label: m.name }))}
            onChange={(v) => setStyle({ marker_mode: v })} />
          <InputNumber size="small" style={{ width: 70 }} min={meta.marker_size[0]} max={meta.marker_size[1]} step={0.5} value={Number(st.marker_size)}
            onChange={(v) => v && setStyle({ marker_size: Number(v) })} /></div>
        <h4>配色</h4>
        <div className="color-sets">
          {meta.color_sets.map((c) => (
            <button key={c.key} className={'color-set' + (c.key === st.color_set ? ' on' : '')} onClick={() => setStyle({ color_set: c.key })}>
              <span className="swatches">{c.colors.slice(0, 8).map((x, i) => <i key={i} style={{ background: x }} />)}</span>
              <span className="color-set-name">{c.name}</span>
            </button>
          ))}
        </div>
      </section>
      <section>
        <h4>图幅与版式</h4>
        <div className="pop-row"><label>图幅</label>
          <Select size="small" style={{ flex: 1 }} value={preset || undefined} placeholder={`${st.width_mm}×${st.height_mm} mm`}
            options={meta.presets.map((p) => ({ value: p.name, label: p.name }))} onChange={(v) => apply(() => sapi.options({ preset: v }))} /></div>
        <div className="pop-row"><label>字号</label>
          <Segmented size="small" value={Number(st.font_pt)} options={meta.fonts.map((f) => ({ value: f, label: String(f) }))}
            onChange={(v) => setStyle({ font_pt: Number(v) })} /></div>
        <div className="pop-row"><label>图例</label>
          <Switch size="small" checked={!!st.legend} onChange={(v) => setStyle({ legend: v })} />
          <Select size="small" style={{ flex: 1 }} disabled={!st.legend} value={String(st.legend_place)} popupMatchSelectWidth={false}
            options={meta.legend_places.map((g) => ({ value: g.key, label: g.name }))} onChange={(v) => setStyle({ legend_place: v })} />
          <Select size="small" style={{ width: 72 }} disabled={!st.legend} value={Number(st.legend_cols)}
            options={[0, 1, 2, 3, 4].map((n) => ({ value: n, label: n ? `${n} 列` : '自动' }))} onChange={(v) => setStyle({ legend_cols: v })} /></div>
        <div className="toggles">
          {([['grid', '网格'], ['box_frame', '坐标全框'], ['minor_ticks', '次刻度'], ['serif', '衬线字体']] as const).map(([k, t]) => (
            <label key={k} className="toggle"><Switch size="small" checked={!!st[k]} onChange={(v) => setStyle({ [k]: v })} />{t}</label>
          ))}
        </div>
        <h4>标注</h4>
        <div className="pop-row"><label>水汽波段</label>
          <Segmented size="small" value={String(st.gap_mode)} options={meta.gap_modes.map((g) => ({ value: g.key, label: g.name }))}
            onChange={(v) => setStyle({ gap_mode: v })} /></div>
        <div className="pop-row"><label>诊断吸收</label>
          <Select size="small" style={{ flex: 1 }} value={String(st.feature_mode)} options={meta.feature_modes.map((g) => ({ value: g.key, label: g.name }))}
            onChange={(v) => setStyle({ feature_mode: v })} /></div>
        <div className="pop-row"><label>图标题</label>
          <button className="link-btn" onClick={styleTitle}>{st.title ? String(st.title) : '无'}<Icon name="pencil" size={12} /></button></div>
        <div className="pop-row"><label>位图精度</label>
          <Segmented size="small" value={board.dpi} options={meta.dpis.map((d) => ({ value: d, label: `${d}` }))}
            onChange={(v) => apply(() => sapi.options({ dpi: Number(v) }))} /><span className="caption">dpi</span></div>
      </section>
    </div>
  ) : null

  if (!meta) return null
  const curves = board.curves
  const live = curves.find((c) => c.live)
  const stageLeft = plotRef.current?.getBoundingClientRect().width ?? 0
  const renameCurve = async () => {
    const c = curves.find((x) => x.id === picked[0])
    const n = c && await ask('曲线名称', c.label)
    if (c && n) apply(() => sapi.patchCurve(c.id, { label: n }))
  }

  return (
    <Float id="光谱曲线" title={<>光谱曲线{board.layer_name ? <span className="float-sub">{board.layer_name}</span> : null}</>} onClose={props.onClose}
      initial={{ x: Math.max(40, window.innerWidth - 800), y: Math.max(60, window.innerHeight - 540), w: 780, h: 500 }}>
      <div className="spec">
        <div className="win-toolbar">
          <Dropdown trigger={['click']} menu={{ items: [{ key: 'csv', label: '从文件导入曲线…' }], onClick: importAny }}>
            <button className="gbtn"><Icon name="folder-open" size={15} />导入<Chevron size={10} /></button>
          </Dropdown>
          <Dropdown trigger={['click']} menu={{ items: [{ key: 'fig', label: '导出图（PNG / PDF / SVG / EPS / TIFF）…' }, { key: 'csv', label: '导出数据（CSV）…' }],
            onClick: ({ key }) => (key === 'fig' ? exportFigure() : exportCsv()) }}>
            <button className="gbtn"><Icon name="download" size={15} />导出<Chevron size={10} /></button>
          </Dropdown>
          <span className="vsep" />
          {meta.library ? <button className="gbtn" onClick={props.onOpenLibrary}><Icon name="database" size={15} />光谱库</button> : null}
          <button className="gbtn" onClick={props.onGroupMeans} title="在绑定影像上按样本类别计算平均光谱"><Icon name="layers" size={15} />类别平均谱</button>
          <div className="spacer" />
          <Select size="small" className="bind-select" placeholder="绑定影像…" value={board.layer_id ?? undefined} allowClear
            prefix={<Icon name="image" size={14} />} popupMatchSelectWidth={false}
            options={props.rasters.map((r) => ({ value: r.id, label: r.name }))}
            onChange={(v?: number) => apply(() => sapi.bind(v ?? null))} />
          <span className="vsep" />
          <Tooltip title="保留当前曲线（也可在影像上 Shift 单击）"><button className="ibtn" disabled={!live} onClick={() => apply(sapi.pin)}><Icon name="map-pin" size={16} /></button></Tooltip>
          <Tooltip title="清空曲线"><button className="ibtn" disabled={!curves.length} onClick={() => apply(sapi.clear)}><Icon name="trash" size={16} /></button></Tooltip>
          <Popover trigger="click" placement="bottomRight" content={stylePanel} arrow={false}>
            <Tooltip title="样式与版式"><button className="ibtn"><Icon name="settings" size={16} /></button></Tooltip>
          </Popover>
          <Tooltip title={showList ? '收起曲线清单' : '曲线清单'}>
            <button className={'ibtn' + (showList ? ' on' : '')} onClick={() => setShowList(!showList)}>
              <Icon name="table" size={16} />{curves.length ? <span className="ibtn-badge">{curves.length}</span> : null}
            </button>
          </Tooltip>
        </div>

        <div className="spec-main">
          <div className="spec-stage" ref={plotRef} onMouseMove={onMove} onMouseLeave={() => { setHover(null); setDragX(null) }}
            onMouseDown={onDown} onMouseUp={onUp}
            onDoubleClick={() => apply(() => sapi.options({ range: '全谱' }))}>
            {plot ? <img ref={imgRef} src={`data:image/png;base64,${plot.png}`} alt="" draggable={false} /> : null}
            {hover && !dragX ? <div className="spec-cross" style={{ left: hover.px }} /> : null}
            {hover && !dragX && hover.rows.length ? (
              <div className={'spec-readout-card' + (hover.px > stageLeft - 200 ? ' flip' : '')} style={{ left: hover.px }}>
                <div className="readout-x">{board.x_band ? `波段 ${Number(hover.x).toFixed(0)}` : `${Number(hover.x).toFixed(1)} nm`}</div>
                {hover.rows.map(([l, c, v]) => (
                  <div key={l} className="readout-row"><i style={{ background: c }} /><span className="readout-name">{l}</span><b>{v}</b></div>
                ))}
              </div>
            ) : null}
            {dragX ? <div className="spec-drag" style={{ left: Math.min(dragX[0], dragX[1]) - (plotRef.current?.getBoundingClientRect().left ?? 0), width: Math.abs(dragX[1] - dragX[0]) }} /> : null}
            {!curves.length ? <div className="spec-empty">在影像上单击取谱，或从光谱库、文件中添加曲线</div> : null}
          </div>
          {showList ? (
            <aside className="spec-side">
              <div className="side-head">
                <span>曲线 <span className="caption">{curves.length}</span></span>
                <div className="spacer" />
                <Tooltip title="改名"><button className="ibtn sm" disabled={picked.length !== 1} onClick={renameCurve}><Icon name="pencil" size={14} /></button></Tooltip>
                <Tooltip title="删除选中"><button className="ibtn sm" disabled={!picked.length} onClick={() => { apply(() => sapi.deleteCurves(picked)); setPicked([]) }}><Icon name="trash" size={14} /></button></Tooltip>
                <Tooltip title={picked.length ? '将选中曲线保存到「我的光谱」' : '将全部曲线保存到「我的光谱」'}>
                  <button className="ibtn sm" disabled={!curves.length} onClick={() => saveToLibrary(picked.length ? picked : curves.filter((c) => c.origin !== 'library').map((c) => c.id))}><Icon name="save" size={14} /></button>
                </Tooltip>
              </div>
              <div className="side-rows">
                {curves.map((c) => (
                  <Dropdown key={c.id} trigger={['contextMenu']} menu={{
                    items: [{ key: 'save', label: '保存到「我的光谱」…' }, { key: 'reset', label: '恢复默认颜色', disabled: !c.custom_color }],
                    onClick: ({ key }) => key === 'save' ? saveToLibrary([c.id]) : apply(() => sapi.patchCurve(c.id, { color: '' })),
                  }}>
                    <div className={'curve-row' + (picked.includes(c.id) ? ' on' : '') + (c.visible ? '' : ' off')}
                      onClick={(e) => setPicked(e.metaKey || e.shiftKey ? (picked.includes(c.id) ? picked.filter((x) => x !== c.id) : [...picked, c.id]) : [c.id])}>
                      <label className="curve-swatch" style={{ background: c.color }} onClick={(e) => e.stopPropagation()} title="修改颜色">
                        <input type="color" value={c.color} onChange={(e) => apply(() => sapi.patchCurve(c.id, { color: e.target.value }))} />
                      </label>
                      <span className="curve-name" title={[c.coord, c.layer, c.note].filter(Boolean).join('\n')}>
                        {c.label}{c.live ? <span className="live-tag">当前</span> : null}
                      </span>
                      <button className="ibtn xs" title={c.visible ? '隐藏' : '显示'}
                        onClick={(e) => { e.stopPropagation(); apply(() => sapi.patchCurve(c.id, { visible: !c.visible })) }}>
                        <Icon name="eye" size={14} />
                      </button>
                    </div>
                  </Dropdown>
                ))}
                {!curves.length ? <div className="hint side-empty">暂无曲线</div> : null}
              </div>
            </aside>
          ) : null}
        </div>

        <div className="win-footer">
          <div className="foot-field"><label>横轴</label>
            <Select size="small" variant="borderless" style={{ width: 104 }} value={board.x_band ? 'band' : 'wave'}
              disabled={!(board.x_options.wave && board.x_options.band)}
              options={[{ value: 'wave', label: '波长 (nm)' }, { value: 'band', label: '波段序号' }]}
              onChange={(v) => apply(() => sapi.options({ x: v }))} /></div>
          <div className="foot-field"><label>纵轴</label>
            <Select size="small" variant="borderless" style={{ width: 168 }} value={board.unit} popupMatchSelectWidth={false}
              options={meta.units.map((u) => ({ value: u.key, label: u.key === 'auto' ? `自动（${board.unit_label}）` : u.menu }))}
              onChange={(v) => apply(() => sapi.options({ unit: v }))} /></div>
          <div className="foot-field"><label>范围</label>
            <Select size="small" variant="borderless" style={{ width: 128 }} value={board.range} popupMatchSelectWidth={false}
              disabled={board.x_band}
              options={[...meta.ranges.map((r) => ({ value: r.key, label: r.key })), { value: '自定义', label: '自定义…' }]}
              onChange={async (v) => {
                if (v === '自定义') {
                  const t = await ask('波长范围（nm），如 2000-2450', '2000-2450')
                  const m = t?.match(/([\d.]+)\D+([\d.]+)/)
                  if (m) apply(() => sapi.options({ range: '自定义', custom_range: [Number(m[1]), Number(m[2])] }))
                } else apply(() => sapi.options({ range: v }))
              }} /></div>
          <div className="foot-field"><label>取谱窗口</label>
            <Select size="small" variant="borderless" style={{ width: 70 }} value={board.window}
              options={meta.windows.map((w) => ({ value: w, label: `${w}×${w}` }))}
              onChange={(v) => apply(() => sapi.options({ window: v }))} /></div>
          <div className="foot-field"><label>平滑</label>
            <Switch size="small" checked={board.smooth} onChange={(v) => apply(() => sapi.options({ smooth: v }))} />
            {board.smooth ? (
              <>
                <InputNumber size="small" variant="borderless" style={{ width: 48 }} min={meta.smooth_window[0]} max={meta.smooth_window[1]} step={2} value={board.smooth_window}
                  onChange={(v) => v && apply(() => sapi.options({ smooth_window: Number(v) }))} title="SG 窗口（奇数）" />
                <InputNumber size="small" variant="borderless" style={{ width: 40 }} min={meta.smooth_order[0]} max={meta.smooth_order[1]} value={board.smooth_order}
                  onChange={(v) => v && apply(() => sapi.options({ smooth_order: Number(v) }))} title="SG 阶数" />
              </>
            ) : null}
          </div>
        </div>
        {board.note ? <div className="caption footer-note">{board.note}</div> : null}
      </div>
    </Float>
  )
}

// =============================================================================
// 光谱库窗口
// =============================================================================

/** 光谱名：中文名在前，英文名与样品号随后（灰色）。没有中文名的只写英文。 */
function EntryName({ e }: { e: Entry }) {
  if (!e.cn || e.cn === e.name) return <span title={e.display}>{e.display}</span>
  return (
    <span title={`${e.cn}  ${e.display}`}>
      <span className="entry-cn">{e.cn}</span><span className="entry-en">{e.display}</span>
    </span>
  )
}

function MiniPlot({ x, y }: { x: (number | null)[]; y: (number | null)[] }) {
  const pts: [number, number][] = []
  for (let i = 0; i < x.length; i++) if (x[i] !== null && y[i] !== null) pts.push([x[i]!, y[i]!])
  if (pts.length < 2) return <div className="hint">该条光谱没有有效值。</div>
  const xs = pts.map((p) => p[0]); const ys = pts.map((p) => p[1])
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]
  const W = 420, H = 150, pad = 4
  const d = pts.map(([a, b], i) => `${i ? 'L' : 'M'}${(pad + ((a - x0) / (x1 - x0 || 1)) * (W - 2 * pad)).toFixed(1)},${(H - pad - ((b - y0) / (y1 - y0 || 1)) * (H - 2 * pad)).toFixed(1)}`).join('')
  return (
    <svg className="mini-plot" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
      <path d={d} fill="none" stroke="#D62728" strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
      <text x={pad} y={H - 2} className="mini-tick">{x0.toFixed(0)} nm</text>
      <text x={W - pad} y={H - 2} textAnchor="end" className="mini-tick">{x1.toFixed(0)} nm</text>
    </svg>
  )
}

export function LibraryWindow(props: {
  curves: Curve[]
  setBoard: (b: BoardState) => void
  onClose: () => void
  onStatus: (s: string, path?: string) => void
}) {
  const { message, modal } = AntApp.useApp()
  const { ask, confirm } = useAsk()
  const runTask = useTaskRunner()
  const [tree, setTree] = useState<Awaited<ReturnType<typeof sapi.tree>> | null>(null)
  const [scope, setScope] = useState<{ category?: string; group?: string; library?: string }>({})
  const [text, setText] = useState('')
  const [rows, setRows] = useState<Entry[]>([])
  const [sel, setSel] = useState<string[]>([])
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof sapi.entry>> | null>(null)
  const [algos, setAlgos] = useState<Meta['algorithms']>([])
  const [algo, setAlgo] = useState('sam')
  const [continuum, setContinuum] = useState(true)
  const [maskWater, setMaskWater] = useState(true)
  const [queryCurve, setQueryCurve] = useState<number | null>(null)
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [ranges, setRanges] = useState<Meta['ranges']>([])
  const [span, setSpan] = useState<{ key: string; span: number[] | null }>({ key: '全谱', span: null })
  const [fullCover, setFullCover] = useState(true)
  const [edgeTol, setEdgeTol] = useState(20)
  const [tolMax, setTolMax] = useState(50)
  const [hitHead, setHitHead] = useState('')
  // 「我的光谱」的管理：树上就地新建 / 改名，右键菜单，拖动挪分类与光谱
  const [expanded, setExpanded] = useState<React.Key[]>(['u'])
  const [editing, setEditing] = useState<{ kind: 'new'; parent: string } | { kind: 'rename'; path: string } | null>(null)
  const [editValue, setEditValue] = useState('')
  const [rowMenu, setRowMenu] = useState<{ x: number; y: number; ids: string[] } | null>(null)
  const [moveDlg, setMoveDlg] = useState<{ ids: string[]; target: string; sub: string } | null>(null)
  const [delDlg, setDelDlg] = useState<{ path: string; primary: number; tagged: number; subfolders: number } | null>(null)
  const [dropKey, setDropKey] = useState<string | null>(null)
  // 表格的虚拟滚动要一个确切的高度：按容器实际大小算，窗口拉大拉小都跟着变
  const tableBox = useRef<HTMLDivElement>(null)
  const [tableH, setTableH] = useState(240)
  useEffect(() => {
    const el = tableBox.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setTableH(Math.max(80, Math.floor(e.contentRect.height) - 34)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const loadTree = useCallback(() => sapi.tree().then(setTree), [])
  useEffect(() => {
    loadTree()
    sapi.meta().then((m) => {
      setAlgos(m.algorithms)
      setRanges(m.ranges)
      setEdgeTol(m.edge_tolerance[0])
      setTolMax(m.edge_tolerance[1])
    })
  }, [loadTree])
  const search = useCallback(async () => {
    try {
      setRows(await sapi.search({ text, ...scope, limit: 1000000 }))
    } catch (e) { message.error((e as Error).message) }
  }, [text, scope, message])
  useEffect(() => { const t = setTimeout(search, 200); return () => clearTimeout(t) }, [search])
  useEffect(() => {
    if (!queryCurve && props.curves.length) setQueryCurve((props.curves.find((c) => c.live) ?? props.curves[0]).id)
  }, [props.curves, queryCurve])
  const refNoWl = props.curves.find((c) => c.id === queryCurve)?.wavelength === false

  const treeData: TreeDataNode[] = useMemo(() => {
    if (!tree) return []
    const byPath = new Map<string, TreeDataNode>()
    const roots: TreeDataNode[] = []
    // 我的光谱：按路径逐级挂到父分类下
    const userBy = new Map<string, TreeDataNode>()
    const userNodes: TreeDataNode[] = []
    for (const f of tree.user_tree) {
      const node: TreeDataNode = { key: 'u:' + f.path, title: `${f.name}（${f.count}）`, children: [] }
      userBy.set(f.path, node)
      const parent = f.path.includes('/') ? userBy.get(f.path.slice(0, f.path.lastIndexOf('/'))) : undefined
      if (parent) parent.children!.push(node)
      else userNodes.push(node)
    }
    if (editing?.kind === 'new') {
      const temp: TreeDataNode = { key: 'new:', title: '', isLeaf: true, selectable: false }
      const parent = editing.parent ? userBy.get(editing.parent) : undefined
      if (parent) parent.children!.unshift(temp)
      else userNodes.unshift(temp)
    }
    for (const n of userBy.values()) if (!n.children!.length) delete n.children
    for (const c of tree.categories) {
      const node: TreeDataNode = { key: 'c:' + c.path, title: `${c.cn}（${c.count}）`, children: [] }
      byPath.set(c.path, node)
      const parent = c.path.includes('/') ? byPath.get(c.path.slice(0, c.path.lastIndexOf('/'))) : undefined
      if (parent) parent.children!.push(node)
      else roots.push(node)
    }
    return [
      { key: 'all', title: `全部（${tree.total}）` },
      ...roots,
      { key: 'g', title: `蚀变组合（${tree.grouped}）`, selectable: false, children: tree.groups.map((g) => ({ key: 'g:' + g.key, title: `${g.cn || g.name || g.key}（${g.count}）` })) },
      { key: 'u', title: `${tree.user_root_cn}（${tree.user_count}）`, children: userNodes },
    ]
  }, [tree, editing])

  const onSelectTree = (k: string) => {
    if (k === 'all') setScope({})
    else if (k.startsWith('c:')) setScope({ category: k.slice(2) })
    else if (k.startsWith('g:')) setScope({ group: k.slice(2) })
    else if (k === 'u') setScope({ library: 'user' })
    else if (k.startsWith('u:')) setScope({ library: 'user', category: k.slice(2) })
    setHits(null)
  }

  const addToPlot = async (ids: string[]) => {
    if (!ids.length) return
    try {
      const r = await sapi.addToPlot(ids)
      props.setBoard(r)
      props.onStatus(r.message || `已添加 ${r.added} 条到曲线图。`)
    } catch (e) { message.error((e as Error).message) }
  }
  const runMatch = async () => {
    if (!queryCurve) return props.onStatus('曲线图中暂无曲线。请先在影像上取谱，或从光谱库添加。')
    try {
      const { result } = await runTask<{ curve: string; algorithm: string; metric_name: string; hits: Hit[]; compared: number; skipped_cover: number; skipped_bands: number; lo: number | null; hi: number | null; tolerance: number; full_cover: boolean }>(
        async () => (await sapi.match({ curve_id: queryCurve, text, ...scope, algorithm: algo, continuum, mask_water: maskWater, span: span.span, full_cover: fullCover, edge_tolerance: edgeTol })).task)
      setHits(result.hits)
      const band = result.lo != null && result.hi != null ? `在 ${result.lo.toFixed(0)}–${result.hi.toFixed(0)} nm ` : ''
      const left: string[] = []
      if (result.skipped_cover) left.push(`${result.skipped_cover} 条未完整覆盖比较范围（端点容差 ${result.tolerance.toFixed(0)} nm）`)
      if (result.skipped_bands) left.push(`${result.skipped_bands} 条有效重叠波段不足`)
      setHitHead(`参考曲线「${result.curve}」${band}与当前列表 ${result.compared} 条光谱的匹配结果（${result.algorithm}${continuum ? '，已去包络' : ''}）`
        + (left.length ? `。另有 ${left.join('、')}，未参与排名` : ''))
    } catch (e) { modal.error({ title: '匹配失败', content: (e as Error).message }) }
  }

  // ------------------------------------------------------------- 我的光谱：分类管理
  const fail = (e: unknown) => message.error((e as Error).message)
  const pathOf = (key: string) => (key === 'u' ? '' : key.slice(2))
  const nameOf = (path: string) => (tree?.user_tree.find((f) => f.path === path)?.name ?? path.split('/').pop() ?? path)
  const refreshAll = () => { loadTree(); search() }

  const startNew = (parent: string) => {
    setEditing({ kind: 'new', parent })
    setEditValue('')
    setExpanded((ks) => [...new Set([...ks, 'u', ...(parent ? ['u:' + parent] : [])])])
  }
  const startRename = (path: string) => {
    setEditing({ kind: 'rename', path })
    setEditValue(path.split('/').pop() ?? path)
  }
  const commitEdit = async () => {
    const ed = editing
    const v = editValue.trim()
    setEditing(null)
    if (!ed || !v) return
    if (v.includes('/')) return message.warning('分类名不能包含斜杠。如需建立下一级，请在该分类上右键选择「新建子分类」。')
    try {
      if (ed.kind === 'new') {
        const path = ed.parent ? `${ed.parent}/${v}` : v
        await sapi.addFolder(path)
        props.onStatus(`已新建分类「${v}」`)
      } else if (v !== ed.path.split('/').pop()) {
        const np = (await sapi.renameFolder(ed.path, v)).path
        if (scope.category === ed.path) setScope({ library: 'user', category: np })
        props.onStatus(`分类已改名为「${v}」`)
      }
      refreshAll()
    } catch (e) { fail(e) }
  }

  const askDelete = async (path: string) => {
    try {
      const u = await sapi.folderUsage(path)
      if (!u.primary && !u.tagged) {
        await sapi.deleteFolder(path)
        if (scope.category === path) setScope({ library: 'user' })
        props.onStatus(`已删除分类「${nameOf(path)}」`)
        return refreshAll()
      }
      setDelDlg({ path, ...u })
    } catch (e) { fail(e) }
  }
  const doDelete = async (mode: 'move' | 'delete') => {
    const d = delDlg!
    setDelDlg(null)
    try {
      await sapi.deleteFolder(d.path, mode)
      if (scope.category && (scope.category === d.path || scope.category.startsWith(d.path + '/'))) setScope({ library: 'user' })
      props.onStatus(mode === 'delete' ? `已删除分类「${nameOf(d.path)}」及其中 ${d.primary} 条光谱`
        : `已删除分类「${nameOf(d.path)}」，其中的光谱已移到上一级`)
      setSel([])
      refreshAll()
    } catch (e) { fail(e) }
  }

  const moveFolderTo = async (path: string, parent: string) => {
    try {
      const np = (await sapi.moveFolder(path, parent)).path
      setExpanded((ks) => [...new Set([...ks, 'u', ...(parent ? ['u:' + parent] : [])])])
      props.onStatus(`已将分类「${nameOf(path)}」移到「${parent ? nameOf(parent) : '我的光谱'}」下`)
      if (scope.category === path) setScope({ library: 'user', category: np })
      refreshAll()
    } catch (e) { fail(e) }
  }
  const moveEntriesTo = async (ids: string[], folder: string) => {
    try {
      const r = await sapi.moveEntries(ids, folder)
      props.onStatus(`已将 ${r.moved} 条光谱移到「${folder ? nameOf(folder) : '我的光谱'}」`)
      setSel([])
      refreshAll()
    } catch (e) { fail(e) }
  }

  const importInto = async (folder: string) => {
    try {
      const dt = desktop()
      const p = dt ? await dt.pick_file('any') : await ask('导入文件（光谱仪文本、ENVI 光谱库或 .slb）')
      if (!p) return
      const r = await sapi.importLib(p, folder)
      props.onStatus(`导入 ${r.added} 条到「${folder ? nameOf(folder) : '我的光谱'}」，跳过 ${r.skipped} 条。${r.notes.join('；')}`)
      setExpanded((ks) => [...new Set([...ks, 'u', ...(folder ? ['u:' + folder] : [])])])
      refreshAll()
    } catch (e) { fail(e) }
  }

  const folderMenu = (path: string): MenuProps['items'] => path ? [
    { key: 'new', label: '新建子分类' },
    { key: 'rename', label: '重命名' },
    { key: 'import', label: '导入光谱到此分类…' },
    { type: 'divider' as const },
    { key: 'delete', label: '删除分类…', danger: true },
  ] : [
    { key: 'new', label: '新建分类' },
    { key: 'import', label: '导入光谱…' },
  ]
  const onFolderMenu = (key: string, path: string) => {
    if (key === 'new') startNew(path)
    else if (key === 'rename') startRename(path)
    else if (key === 'import') importInto(path)
    else if (key === 'delete') askDelete(path)
  }

  // ------------------------------------------------------------- 光谱条目
  const userIds = (ids: string[]) => ids.filter((id) => rows.find((r) => r.id === id)?.library === 'user')
  const entryOps = async (key: string, ids: string[]) => {
    setRowMenu(null)
    try {
      if (key === 'plot') return addToPlot(ids)
      if (key === 'move') return setMoveDlg({ ids: userIds(ids), target: scope.library === 'user' ? scope.category ?? '' : '', sub: '' })
      if (key === 'rename' && ids.length === 1) {
        const e = rows.find((r) => r.id === ids[0])
        const n = e && await ask('光谱改名', e.name)
        if (n && n.trim()) { await sapi.patchEntry(ids[0], { name: n.trim() }); search() }
        return
      }
      if (key === 'delete') {
        const mine = userIds(ids)
        if (!mine.length) return
        if (await confirm(`从「我的光谱」中删除 ${mine.length} 条光谱？删除后无法恢复。`)) {
          await sapi.deleteEntries(mine); setSel([]); refreshAll()
          props.onStatus(`已删除 ${mine.length} 条光谱`)
        }
        return
      }
      if (key.startsWith('export:')) {
        const kind = key.slice(7)
        const dt = desktop()
        const p = dt ? await dt.save_file(`spectra.${kind}`) : await ask('导出到', `spectra.${kind}`)
        if (p) { const r = await sapi.exportLib(ids, p, kind); props.onStatus(`已导出 ${r.exported} 条 → ${r.path}`, r.path) }
      }
    } catch (e) { fail(e) }
  }
  const rowMenuItems = (ids: string[]): MenuProps['items'] => {
    const mine = userIds(ids).length
    return [
      { key: 'plot', label: `加到曲线图${ids.length > 1 ? `（${ids.length} 条）` : ''}` },
      ...(mine ? [
        { type: 'divider' as const },
        { key: 'move', label: '移动到分类…' },
        { key: 'rename', label: '改名…', disabled: ids.length !== 1 },
        { key: 'delete', label: '删除', danger: true },
      ] : []),
      { type: 'divider' as const },
      { key: 'export', label: '导出', children: EXPORT_ITEMS },
    ]
  }

  // Delete 键删除选中的「我的光谱」条目（焦点在光谱库窗口里时）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (t.closest('input, textarea') || !t.closest('.lib')) return
      if ((e.key === 'Delete' || e.key === 'Backspace') && sel.length && userIds(sel).length) {
        e.preventDefault()
        entryOps('delete', sel)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  /** 树上一个节点的标题：「我的光谱」的分类带右键菜单、能接住拖过来的光谱、能就地改名 */
  const renderTitle = (node: TreeDataNode) => {
    const key = String(node.key)
    if (key === 'new:') {
      return (
        <Input size="small" autoFocus className="lib-tree-input" placeholder="分类名称，回车确定" value={editValue}
          onChange={(e) => setEditValue(e.target.value)} onPressEnter={commitEdit} onBlur={commitEdit}
          onKeyDown={(e) => { e.stopPropagation(); if (e.key === 'Escape') setEditing(null) }} onClick={(e) => e.stopPropagation()}
          // 焦点事件别冒泡到 Tree：它会去滚一个不存在的虚拟列表而报错
          onFocus={(e) => e.stopPropagation()} />
      )
    }
    if (key !== 'u' && !key.startsWith('u:')) return <span>{String(node.title)}</span>
    const path = pathOf(key)
    if (editing?.kind === 'rename' && editing.path === path) {
      return (
        <Input size="small" autoFocus className="lib-tree-input" value={editValue}
          onChange={(e) => setEditValue(e.target.value)} onPressEnter={commitEdit} onBlur={commitEdit}
          onKeyDown={(e) => { e.stopPropagation(); if (e.key === 'Escape') setEditing(null) }} onClick={(e) => e.stopPropagation()}
          // 焦点事件别冒泡到 Tree：它会去滚一个不存在的虚拟列表而报错
          onFocus={(e) => e.stopPropagation()} />
      )
    }
    return (
      <Dropdown trigger={['contextMenu']} menu={{ items: folderMenu(path), onClick: ({ key: k, domEvent }) => { domEvent.stopPropagation(); onFolderMenu(k, path) } }}>
        <span className={'lib-folder' + (dropKey === key ? ' drop' : '')}
          // 分类名可以拖：拖到另一个分类上就整枝挪过去（子分类、光谱跟着走）
          draggable={!!path && !editing}
          onDragStart={(e) => {
            e.stopPropagation()
            e.dataTransfer.setData(FOLDER_MIME, path)
            e.dataTransfer.effectAllowed = 'move'
          }}
          onDragOver={(e) => {
            const t = e.dataTransfer.types
            if (t.includes(SPEC_MIME) || t.includes(FOLDER_MIME)) { e.preventDefault(); e.stopPropagation(); setDropKey(key) }
          }}
          onDragLeave={() => setDropKey((k) => (k === key ? null : k))}
          onDrop={(e) => {
            const ids = e.dataTransfer.getData(SPEC_MIME)
            const folder = e.dataTransfer.getData(FOLDER_MIME)
            setDropKey(null)
            if (!ids && !folder) return
            e.preventDefault()
            e.stopPropagation()
            if (ids) return moveEntriesTo(JSON.parse(ids) as string[], path)
            const parentOf = folder.includes('/') ? folder.slice(0, folder.lastIndexOf('/')) : ''
            if (folder === path || path.startsWith(folder + '/')) return message.warning('不能将分类移到其自身或其子分类中。')
            if (path !== parentOf) moveFolderTo(folder, path)
          }}
          onDoubleClick={(e) => { if (path) { e.stopPropagation(); startRename(path) } }}>
          {String(node.title)}
        </span>
      </Dropdown>
    )
  }

  const hitOf = useMemo(() => new Map((hits ?? []).map((h) => [h.id, h])), [hits])
  const shown = useMemo(() => {
    if (!hits) return rows
    const byId = new Map(rows.map((r) => [r.id, r]))
    return hits.map((h) => byId.get(h.id)).filter((r): r is Entry => !!r)
  }, [hits, rows])

  return (
    <Float title="光谱库" onClose={props.onClose} initial={{ x: 100, y: 90, w: 1120, h: 660 }}>
      <div className="lib">
        <aside className="lib-side">
          <div className="side-head">
            <span>分类</span>
            <Tooltip title="在「我的光谱」中新建分类">
              <button className="icon-btn" onClick={() => startNew(scope.library === 'user' ? scope.category ?? '' : '')}><Icon name="plus" size={14} /></button>
            </Tooltip>
          </div>
          <div className="lib-tree">
            <Tree blockNode treeData={treeData} defaultSelectedKeys={['all']}
              expandedKeys={expanded} onExpand={(ks) => setExpanded(ks)}
              switcherIcon={<span className="tree-chev"><Chevron size={10} /></span>}
              titleRender={renderTitle}
              onSelect={(keys) => keys[0] && onSelectTree(String(keys[0]))} />
          </div>
          <div className="lib-side-hint">「我的光谱」：右键分类可新建、重命名、删除；拖动分类或列表里的光谱到另一个分类上即可移动，双击分类名改名。</div>
        </aside>
        <div className="lib-main">
          <div className="win-toolbar flat">
            <Input className="search-input" allowClear prefix={<Icon name="search" size={14} />} placeholder="搜索中文名、英文名、样品号或化学式"
              value={text} onChange={(e) => setText(e.target.value)} />
            <span className="count-chip">{shown.length} 条</span>
            <div className="spacer" />
            <button className="gbtn" disabled={!sel.length} onClick={() => addToPlot(sel)} title="将选中的光谱添加到曲线图（也可双击行）">
              <Icon name="chart-line" size={15} />加到曲线图{sel.length ? <span className="ibtn-badge inline">{sel.length}</span> : null}
            </button>
            {userIds(sel).length ? (
              <button className="gbtn" onClick={() => entryOps('move', sel)} title="把选中的光谱移到另一个分类（也可直接拖到左侧分类上）">
                <Icon name="folder-open" size={15} />移动到…
              </button>
            ) : null}
            <Dropdown trigger={['click']} disabled={!sel.length}
              menu={{ items: EXPORT_ITEMS, onClick: ({ key }) => entryOps(key, sel) }}>
              <button className="gbtn" disabled={!sel.length}><Icon name="download" size={15} />导出<Chevron size={10} /></button>
            </Dropdown>
            <button className="gbtn" onClick={() => importInto(scope.library === 'user' ? scope.category ?? '' : '')}
              title={scope.library === 'user' && scope.category ? `导入到「${nameOf(scope.category)}」` : '导入到「我的光谱」'}>
              <Icon name="upload" size={15} />导入…
            </button>
          </div>
          {hits ? (
            <div className="hits-bar">
              <Icon name="target" size={14} /><span>{hitHead}</span><div className="spacer" />
              <button className="link-btn" onClick={() => setHits(null)}>回到列表</button>
            </div>
          ) : null}
          <div className="lib-table" ref={tableBox}>
            <Table<Entry> size="small" rowKey="id" pagination={false} virtual scroll={{ y: tableH }} dataSource={shown}
              rowSelection={{ selectedRowKeys: sel, onChange: (k) => setSel(k as string[]), columnWidth: 36 }}
              rowClassName={(r) => (preview?.id === r.id ? 'row-previewed' : '')}
              onRow={(r) => ({
                onClick: () => sapi.entry(r.id).then(setPreview),
                onDoubleClick: () => addToPlot([r.id]),
                // 右键：对选中的几条（右键的那条不在选中里时只对它）
                onContextMenu: (e: React.MouseEvent) => {
                  e.preventDefault()
                  const ids = sel.includes(r.id) ? sel : [r.id]
                  if (!sel.includes(r.id)) setSel([r.id])
                  setRowMenu({ x: e.clientX, y: e.clientY, ids })
                },
                // 「我的光谱」的条目可以拖到左侧分类上
                draggable: r.library === 'user',
                onDragStart: (e: React.DragEvent) => {
                  const ids = userIds(sel.includes(r.id) ? sel : [r.id])
                  e.dataTransfer.setData(SPEC_MIME, JSON.stringify(ids))
                  e.dataTransfer.effectAllowed = 'move'
                },
              })}
              columns={hits ? [
                { title: '得分', width: 70, render: (_, r) => <b className="score">{hitOf.get(r.id)?.score.toFixed(1)}</b> },
                { title: '名称', dataIndex: 'display', ellipsis: true, render: (_, r) => <EntryName e={r} /> },
                { title: '度量', width: 110, render: (_, r) => <span className="caption">{hitOf.get(r.id)?.metric}</span> },
                {
                  title: '比较波段', width: 150, render: (_, r) => {
                    const h = hitOf.get(r.id)
                    if (!h || h.lo == null || h.hi == null) return null
                    return (
                      <span className="mono caption" title={`实际参与比较 ${h.bands} 个波段，占参考曲线有效波段的 ${h.coverage.toFixed(0)}%`}>
                        {h.lo.toFixed(0)}–{h.hi.toFixed(0)}{h.coverage < 95 ? ` · ${h.coverage.toFixed(0)}%` : ''}
                      </span>
                    )
                  },
                },
              ] : [
                { title: '名称', dataIndex: 'display', ellipsis: true, render: (_, r) => <EntryName e={r} /> },
                { title: '来源', dataIndex: 'source', width: 110, ellipsis: true, render: (v: string, r) => <span className="source-tag">{r.library === 'user' ? '我的光谱' : v}</span> },
                { title: '波长 (nm)', width: 120, render: (_, r) => <span className="mono caption">{r.wl[0].toFixed(0)}–{r.wl[1].toFixed(0)}</span> },
              ]} />
          </div>
          <div className="lib-cards">
            <section className="card lib-preview">
              <header><span>预览</span>{preview ? <span className="card-sub" title={preview.title}>{preview.title}</span> : null}</header>
              {preview ? (
                <>
                  <MiniPlot x={preview.x} y={preview.y} />
                  <div className="caption pre lib-detail">{preview.detail}</div>
                </>
              ) : <div className="hint card-empty">单击行预览，双击添加到曲线图</div>}
            </section>
            <section className="card lib-match">
              <header><span>光谱匹配</span></header>
              <div className="match-body">
                <div className="pop-row"><label>参考曲线</label>
                  <Select size="small" style={{ flex: 1, minWidth: 0 }} value={queryCurve ?? undefined} placeholder="从曲线图中选择" onChange={setQueryCurve}
                    options={props.curves.map((c) => ({ value: c.id, label: c.label + (c.live ? '（当前）' : '') }))} /></div>
                <div className="pop-row"><label>匹配算法</label>
                  <Select size="small" style={{ flex: 1, minWidth: 0 }} value={algo} onChange={setAlgo}
                    options={algos.map((a) => ({ value: a.key, label: <span title={a.tip}>{a.name}</span> }))} /></div>
                <div className="pop-row"><label>比较范围</label>
                  <Select size="small" style={{ flex: 1, minWidth: 0 }} value={span.key} popupMatchSelectWidth={false}
                    options={[...ranges.map((r) => ({ value: r.key, label: r.key })),
                      { value: '自定义', label: span.key === '自定义' && span.span ? `自定义  ${span.span[0]} ~ ${span.span[1]} nm` : '自定义…' }]}
                    onChange={async (v) => {
                      if (v === '自定义') {
                        const t = await ask('比较范围（nm），如 2000-2450', span.span ? `${span.span[0]}-${span.span[1]}` : '2000-2450')
                        const m = t?.match(/([\d.]+)\D+([\d.]+)/)
                        if (m) setSpan({ key: '自定义', span: [Number(m[1]), Number(m[2])].sort((a, b) => a - b) })
                      } else setSpan({ key: v, span: ranges.find((r) => r.key === v)?.span ?? null })
                    }} /></div>
                <div className="toggles">
                  <label className="toggle"><Switch size="small" checked={continuum} onChange={setContinuum} />去除包络线</label>
                  <label className="toggle"><Switch size="small" checked={maskWater} onChange={setMaskWater} />排除水汽吸收波段</label>
                  <label className="toggle" title="开启时只有完整覆盖比较范围的库谱参与排名，各条目比较的是同一段波长。关闭后部分重叠的库谱也参与，结果中标出实际比较的波段与覆盖率。">
                    <Switch size="small" checked={fullCover} onChange={setFullCover} />仅限完整覆盖
                  </label>
                  {fullCover ? (
                    <span className="toggle" title={`比较范围两端允许库谱缺少的波长，用来容忍不同仪器的量程差异。下限为参考曲线的两个波段间隔，上限 ${tolMax} nm，再大会漏掉两端的诊断吸收；不需要两端时请缩小比较范围。`}>
                      端点容差
                      <InputNumber size="small" style={{ width: 56 }} min={0} max={tolMax} step={5} value={edgeTol} controls={false}
                        onChange={(v) => setEdgeTol(Math.min(tolMax, Math.max(0, Number(v ?? 0))))} />
                      nm
                    </span>
                  ) : null}
                </div>
              </div>
              {refNoWl ? <div className="caption">参考曲线没有波长信息，无法与光谱库匹配。请先在影像的「图层属性」中加载波长表。</div> : null}
              <Button type="primary" className="cta block" disabled={!rows.length || !props.curves.length || refNoWl} onClick={runMatch}
                title="建议先在左侧选定分类（如 矿物 / 层状硅酸盐）缩小范围，匹配结果更有参考价值。库谱与参考曲线分辨率不同时，会先把较细的一方降到较粗的一方再比较。">
                与列表中 {rows.length} 条光谱匹配
              </Button>
            </section>
          </div>
        </div>
      </div>
      {rowMenu ? (
        <Dropdown open trigger={['contextMenu']} onOpenChange={(o) => { if (!o) setRowMenu(null) }}
          menu={{ items: rowMenuItems(rowMenu.ids), onClick: ({ key }) => entryOps(key, rowMenu.ids) }}>
          <span className="ctx-anchor" style={{ left: rowMenu.x, top: rowMenu.y }} />
        </Dropdown>
      ) : null}

      <Modal open={!!moveDlg} title={`移动 ${moveDlg?.ids.length ?? 0} 条光谱`} okText="移动" cancelText="取消" width={440}
        onCancel={() => setMoveDlg(null)}
        onOk={() => {
          const d = moveDlg!
          const sub = d.sub.trim().replace(/\//g, '')
          const target = sub ? (d.target ? `${d.target}/${sub}` : sub) : d.target
          setMoveDlg(null)
          moveEntriesTo(d.ids, target)
        }}>
        {moveDlg ? (
          <div className="dlg">
            <div className="form-row">
              <span>目标分类</span>
              <TreeSelect style={{ flex: 1 }} value={moveDlg.target} treeDefaultExpandAll showSearch
                treeNodeFilterProp="title" onChange={(v) => setMoveDlg({ ...moveDlg, target: v })}
                treeData={[{ value: '', title: '我的光谱（根）', children: folderTree(tree?.user_tree ?? []) }]} />
            </div>
            <div className="form-row" style={{ marginTop: 10 }}>
              <span>新建子分类</span>
              <Input style={{ flex: 1 }} placeholder="可选：在目标分类下新建并移入" value={moveDlg.sub}
                onChange={(e) => setMoveDlg({ ...moveDlg, sub: e.target.value })} />
            </div>
            <div className="hint" style={{ marginTop: 10 }}>也可以直接把选中的光谱拖到左侧的分类上。兼属的矿物类型分类不变。</div>
          </div>
        ) : null}
      </Modal>

      <Modal open={!!delDlg} title={`删除分类「${delDlg ? nameOf(delDlg.path) : ''}」`} width={480}
        onCancel={() => setDelDlg(null)}
        footer={delDlg ? [
          <Button key="c" onClick={() => setDelDlg(null)}>取消</Button>,
          delDlg.primary ? <Button key="d" danger onClick={() => doDelete('delete')}>连同 {delDlg.primary} 条光谱一起删除</Button> : null,
          <Button key="m" type="primary" onClick={() => doDelete('move')}>{delDlg.primary ? '删除分类，光谱移到上一级' : '删除分类'}</Button>,
        ] : null}>
        {delDlg ? (
          <div className="dlg">
            <div>
              这个分类{delDlg.subfolders ? `连同 ${delDlg.subfolders} 个子分类` : ''}下有 {delDlg.primary} 条光谱
              {delDlg.tagged ? `，另有 ${delDlg.tagged} 条光谱兼属于它（只摘掉这个兼属，光谱本身保留）` : ''}。
            </div>
            <div className="hint" style={{ marginTop: 8 }}>
              「移到上一级」会把光谱放进「{delDlg.path.includes('/') ? nameOf(delDlg.path.slice(0, delDlg.path.lastIndexOf('/'))) : '我的光谱'}」，不丢数据。
            </div>
          </div>
        ) : null}
      </Modal>
    </Float>
  )
}

/** 「我的光谱」的分类树（TreeSelect 用） */
function folderTree(list: { path: string; name: string }[]) {
  interface N { value: string; title: string; children: N[] }
  const by = new Map<string, N>()
  const roots: N[] = []
  for (const f of list) {
    const n: N = { value: f.path, title: f.name, children: [] }
    by.set(f.path, n)
    const parent = f.path.includes('/') ? by.get(f.path.slice(0, f.path.lastIndexOf('/'))) : undefined
    if (parent) parent.children.push(n)
    else roots.push(n)
  }
  return roots
}

const SPEC_MIME = 'application/x-litho-spectra'
const FOLDER_MIME = 'application/x-litho-spec-folder'

const EXPORT_ITEMS: MenuProps['items'] = [
  { key: 'export:slb', label: '.slb（带分类与中文名）…' },
  { key: 'export:sli', label: 'ENVI 光谱库…' },
  { key: 'export:csv', label: 'CSV…' },
]

// =============================================================================
// 按类别取平均谱
// =============================================================================

export function GroupMeansDialog(props: { open: boolean; onClose: () => void; onDone: (added: number, problems: string[]) => void }) {
  const { message } = AntApp.useApp()
  const runTask = useTaskRunner()
  const [groups, setGroups] = useState<Awaited<ReturnType<typeof sapi.groups>>>([])
  const [checked, setChecked] = useState<string[]>([])
  const [err, setErr] = useState('')
  useEffect(() => {
    if (!props.open) return
    setErr('')
    setChecked([])
    sapi.groups().then(setGroups).catch((e) => setErr((e as Error).message))
  }, [props.open])
  const treeData = useMemo(() => {
    const nodes = new Map<string, TreeDataNode & { children: TreeDataNode[] }>()
    for (const g of groups) nodes.set(g.key, { key: g.key, title: <span>{g.color ? <i className="dot" style={{ background: g.color }} /> : null}{g.name}<span className="muted">　{g.count || ''}</span></span>, children: [] })
    const roots: TreeDataNode[] = []
    for (const g of groups) {
      const p = g.parent ? nodes.get(g.parent) : undefined
      if (p) p.children.push(nodes.get(g.key)!)
      else roots.push(nodes.get(g.key)!)
    }
    return roots
  }, [groups])
  const run = async () => {
    const leaf = checked.filter((k) => groups.find((g) => g.key === k)?.count)
    if (!leaf.length) return message.warning('请勾选要计算平均谱的类别。')
    props.onClose()
    try {
      const { result } = await runTask<{ added: number; problems: string[] }>(async () => (await sapi.groupMeans(leaf)).task)
      props.onDone(result.added, result.problems)
    } catch (e) { message.error((e as Error).message) }
  }
  return (
    <Modal open={props.open} title="按类别取平均谱" width={520} onCancel={props.onClose}
      footer={[<Button key="c" onClick={props.onClose}>取消</Button>, <Button key="ok" className="cta" disabled={!!err} onClick={run}>开始计算</Button>]}>
      <div className="dlg">
        {err ? <div className="caption warn">{err}</div> : (
          <>
            <div className="caption">仅列出位于绑定影像范围内的类别。每个类别对其样本覆盖的像元取平均，生成一条光谱添加到曲线图。</div>
            <div className="pick-list tall">
              <Tree checkable defaultExpandAll treeData={treeData} checkedKeys={checked} onCheck={(k) => setChecked((Array.isArray(k) ? k : k.checked).map(String))} />
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}
