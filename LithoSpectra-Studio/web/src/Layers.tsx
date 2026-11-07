// 图层面板与图层相关的对话框：右键菜单、影像属性、唯一值赋色。

import { useEffect, useRef, useState } from 'react'
import { App as AntApp, Button, Checkbox, ColorPicker, Dropdown, InputNumber, Modal, Radio, Select, Tooltip } from 'antd'
import type { MenuProps } from 'antd'
import { api } from './api'
import type { LayerInfo } from './api'
import { desktop } from './desktop'
import { Icon } from './theme'

export type LayerId = number | 'samples'

export interface SamplesLayerState {
  visible: boolean
  opacity: number
}

const KIND_ICON: Record<string, 'image' | 'shapes' | 'crop' | 'layers'> = {
  raster: 'image', image: 'image', vector: 'shapes', drawn: 'crop',
}

// =============================================================================
// 图层面板
// =============================================================================

export function LayerPanel(props: {
  layers: LayerInfo[]
  order: LayerId[]
  samples: SamplesLayerState
  selected: LayerId | null
  /** Tab 键切换显示的那个图层（最近在面板里点过的） */
  tabTarget?: LayerId | null
  modules: string[]
  onSelect: (id: LayerId | null) => void
  /** 双击图层：缩放到它的范围 */
  onZoom: (id: LayerId) => void
  onToggle: (id: LayerId, visible: boolean) => void
  onReorder: (order: LayerId[]) => void
  onAction: (action: string, layer: LayerInfo) => void
  onAdd: (kind: 'raster' | 'image' | 'vector') => void
}) {
  const [dragging, setDragging] = useState<LayerId | null>(null)
  // 行是 draggable 的，WebKit 里收不到 dblclick，按两次单击的间隔自己认双击
  const lastClick = useRef<{ id: LayerId; t: number } | null>(null)
  const clickRow = (id: LayerId) => {
    const now = Date.now()
    const prev = lastClick.current
    props.onSelect(id)
    if (prev && prev.id === id && now - prev.t < 400) {
      lastClick.current = null
      props.onZoom(id)
    } else lastClick.current = { id, t: now }
  }
  const byId = new Map(props.layers.map((l) => [l.id, l]))

  const menuOf = (l: LayerInfo): MenuProps['items'] => {
    const raster = l.kind === 'raster' || l.kind === 'image'
    const styled = l.kind === 'vector' || l.kind === 'drawn'
    const items: MenuProps['items'] = [
      { key: 'zoom', label: '缩放到图层' },
      { key: 'props', label: '图层属性…', disabled: !raster },
    ]
    if (styled && (l.feature_count ?? 0) > 0) items.push({ key: 'select', label: '按图层范围选择样本…' })
    if (raster) {
      if (props.modules.includes('spectra') && (l.band_count ?? 0) > 1) items.push({ key: 'spectrum', label: '光谱曲线…' })
      items.push({ key: 'extent', label: '生成范围图层…' })
      if (props.modules.includes('toolbox')) items.push({ key: 'toolbox', label: '用工具箱处理…' })
    }
    if (l.kind === 'drawn') {
      items.push({ key: 'save', label: '导出范围为矢量…', disabled: !(l.feature_count ?? 0) })
      items.push({ key: 'clear', label: '清空手绘范围', disabled: !(l.feature_count ?? 0) })
    }
    items.push({ type: 'divider' }, { key: 'rename', label: '重命名…' })
    if (styled) items.push({ key: 'color', label: '设置颜色…' })
    if (l.kind === 'vector') {
      items.push({ key: 'unique', label: '按属性唯一值赋色…' })
      if (l.color_field) items.push({ key: 'unique-clear', label: '取消唯一值赋色' })
      items.push({
        key: 'fill',
        label: (
          <span className="menu-check">
            <span className="menu-check-mark">{l.fill ? <Icon name="check" size={14} /> : null}</span>填充面
          </span>
        ),
      })
    }
    items.push({ type: 'divider' }, { key: 'up', label: '上移' }, { key: 'down', label: '下移' },
      { type: 'divider' }, { key: 'remove', label: '移除图层', danger: true })
    return items
  }

  const move = (id: LayerId, delta: number) => {
    const i = props.order.indexOf(id)
    const j = Math.max(0, Math.min(props.order.length - 1, i + delta))
    if (i === j) return
    const next = [...props.order]
    next.splice(i, 1)
    next.splice(j, 0, id)
    props.onReorder(next)
  }

  const drop = (target: LayerId) => {
    if (dragging === null || dragging === target) return
    const next = props.order.filter((x) => x !== dragging)
    next.splice(next.indexOf(target), 0, dragging)
    props.onReorder(next)
    setDragging(null)
  }

  const addMenu: MenuProps['items'] = [
    { key: 'raster', label: '添加影像（ENVI / GeoTIFF）…' },
    { key: 'image', label: '添加配准图片（JPG / PNG）…' },
    { key: 'vector', label: '添加参考矢量…' },
  ]

  return (
    <div className="layer-panel">
      <div className="layer-head">
        <span className="caption">图层</span>
        <Dropdown trigger={['click']} menu={{ items: addMenu, onClick: ({ key }) => props.onAdd(key as 'raster') }}>
          <Tooltip title="添加图层"><button className="icon-btn"><Icon name="plus" size={14} /></button></Tooltip>
        </Dropdown>
      </div>
      <div className="layer-list">
        {props.order.map((id) => {
          const l = id === 'samples' ? null : byId.get(id)
          if (id !== 'samples' && !l) return null
          const visible = l ? l.visible : props.samples.visible
          const row = (
            <div
              key={String(id)}
              className={'layer-row' + (props.selected === id ? ' on' : '') + (dragging === id ? ' dragging' : '')}
              draggable
              onDragStart={() => setDragging(id)}
              onDragOver={(e) => e.preventDefault()}
              onDrop={() => drop(id)}
              onDragEnd={() => setDragging(null)}
              onClick={() => clickRow(id)}
              title={(l ? [l.path, ...l.info].filter(Boolean).join('\n') : '样本库中的样本，不能移除。') + '\n双击缩放到图层范围'}
            >
              <Checkbox checked={visible} onClick={(e) => e.stopPropagation()} onChange={(e) => props.onToggle(id, e.target.checked)} />
              <span className="layer-icon"><Icon name={l ? KIND_ICON[l.kind] ?? 'layers' : 'database'} size={14} /></span>
              <span className={'layer-name' + (l ? '' : ' strong')}>{l ? l.name : '样本库 · 样本'}</span>
              {props.tabTarget === id ? <span className="layer-tab" title="按 Tab 键切换这个图层的显示">Tab</span> : null}
              {l?.error ? <span className="layer-err" title={l.error}>!</span> : null}
              {l?.preparing != null ? (
                <span className="layer-prep" title="首次显示大图，正在后台生成显示金字塔，完成后缩放与平移会更流畅。生成期间可正常使用。">
                  准备中 {Math.round(l.preparing * 100)}%
                </span>
              ) : null}
            </div>
          )
          if (!l) return row
          return (
            <Dropdown key={String(id)} trigger={['contextMenu']}
              menu={{
                items: menuOf(l),
                onClick: ({ key }) => {
                  if (key === 'up') move(id, -1)
                  else if (key === 'down') move(id, 1)
                  else props.onAction(key, l)
                },
              }}>
              {row}
            </Dropdown>
          )
        })}
        {props.order.length <= 1 ? (
          <div className="hint layer-hint">将影像或矢量文件拖入窗口叠加显示，或点击右上角的 + 添加图层。</div>
        ) : null}
      </div>
    </div>
  )
}

// =============================================================================
// 影像属性：波段、拉伸、颜色表、波长表
// =============================================================================

export function RasterPropsDialog(props: {
  layer: LayerInfo | null
  onCancel: () => void
  onOk: (patch: { bands: number[]; stretch: number[]; use_color_table: boolean; wavelength_file?: string }) => void
}) {
  const l = props.layer
  const [bands, setBands] = useState<number[]>([])
  const [lo, setLo] = useState(2)
  const [hi, setHi] = useState(98)
  const [useCt, setUseCt] = useState(false)
  const [wl, setWl] = useState<string | undefined>(undefined)
  const [wlLabel, setWlLabel] = useState('')

  useEffect(() => {
    if (!l) return
    const b = l.bands ?? [1]
    setBands([b[0], b[1] ?? b[b.length - 1], b[2] ?? b[b.length - 1]])
    setLo(l.stretch?.[0] ?? 2)
    setHi(l.stretch?.[1] ?? 98)
    setUseCt(!!l.use_color_table)
    setWl(undefined)
    setWlLabel(l.wavelength_source ?? '')
  }, [l])

  if (!l) return <Modal open={false} />
  const single = (l.band_count ?? 1) < 3
  const bandOpts = (l.band_labels ?? []).map((t, i) => ({ value: i + 1, label: t }))
  const presets = Object.entries(l.wavelength_presets ?? {})
  const ctOn = l.has_color_table && useCt

  const browseWl = async () => {
    const dt = desktop()
    const p = dt ? await dt.pick_file('any') : window.prompt('波长表文件路径')
    if (p) {
      setWl(p)
      setWlLabel(p.split(/[\\/]/).pop() ?? p)
    }
  }

  return (
    <Modal open title={`图层属性 · ${l.name}`} width={480} onCancel={props.onCancel}
      footer={[
        <Button key="c" onClick={props.onCancel}>取消</Button>,
        <Button key="ok" type="primary" onClick={() => props.onOk({
          bands: single ? [bands[0]] : bands, stretch: [lo, hi], use_color_table: useCt,
          ...(wl !== undefined ? { wavelength_file: wl } : {}),
        })}>确定</Button>,
      ]}>
      <div className="dlg">
        <div className="caption pre">{[`${l.name}（${l.kind_cn}）`, ...l.info].join('\n')}</div>
        {l.has_color_table ? (
          <Checkbox checked={useCt} onChange={(e) => setUseCt(e.target.checked)}
            title="关闭后将类别值作为灰度拉伸显示，通常仅在核对原始数值时使用">
            按影像自带的颜色表显示（{l.color_table_size} 项）
          </Checkbox>
        ) : null}
        {(single ? ['灰度波段'] : ['红 波段', '绿 波段', '蓝 波段']).map((label, i) => (
          <div className="form-row" key={label}>
            <label>{label}</label>
            <div className="form-ctl">
              <Select disabled={ctOn} showSearch optionFilterProp="label" value={bands[i]} options={bandOpts}
                onChange={(v: number) => setBands(bands.map((b, j) => (j === i ? v : b)))} />
            </div>
          </div>
        ))}
        <div className="form-row">
          <label>拉伸分位数</label>
          <div className="form-ctl inline">
            <InputNumber disabled={ctOn} min={0} max={49} step={0.5} value={lo} onChange={(v) => setLo(Number(v) || 0)} />
            <span>% ~</span>
            <InputNumber disabled={ctOn} min={51} max={100} step={0.5} value={hi} onChange={(v) => setHi(Number(v) || 100)} />
            <span>%</span>
          </div>
        </div>
        <div className="form-row">
          <label>波长表</label>
          <div className="form-ctl inline">
            <Select style={{ flex: 1 }} value={wl === undefined ? (l.wavelength_source && !presets.some(([k]) => k === l.wavelength_source) ? '__keep' : presets.find(([k]) => k === l.wavelength_source)?.[1] ?? '') : wl}
              onChange={(v: string) => { if (v !== '__keep') { setWl(v); setWlLabel(presets.find(([, p]) => p === v)?.[0] ?? '') } }}
              options={[
                { value: '', label: '不使用（按波段号显示）' },
                ...(l.wavelength_source && !presets.some(([k]) => k === l.wavelength_source) ? [{ value: '__keep', label: `${l.wavelength_source}（当前）` }] : []),
                ...presets.map(([k, p]) => ({ value: p, label: k })),
                ...(wl && !presets.some(([, p]) => p === wl) ? [{ value: wl, label: wlLabel }] : []),
              ]} />
            <Button onClick={browseWl}>从文件…</Button>
          </div>
        </div>
      </div>
    </Modal>
  )
}

// =============================================================================
// 唯一值赋色
// =============================================================================

const MAX_ROWS = 300

export function UniqueColorsDialog(props: { layer: LayerInfo | null; onCancel: () => void; onOk: (field: string, colors: Record<string, string>) => void }) {
  const { message, modal } = AntApp.useApp()
  const l = props.layer
  const [field, setField] = useState('')
  const [rows, setRows] = useState<{ value: string; color: string }[]>([])
  const [hits, setHits] = useState<Record<string, { matched: string; rule: string; path: string }>>({})
  const [paletteSize, setPaletteSize] = useState(256)

  useEffect(() => {
    if (!l) return
    setField(l.color_field || l.fields?.[0] || '')
    setHits({})
  }, [l])

  useEffect(() => {
    if (!l || !field) return setRows([])
    api.layerValues(l.id, field).then((r) => {
      setRows(r.values)
      setPaletteSize(r.palette_size)
    }).catch((e) => message.error((e as Error).message))
  }, [l, field, message])

  if (!l) return <Modal open={false} />
  const legend = async () => {
    try {
      const r = await api.legendColors(l.id, field)
      setRows(rows.map((x) => ({ ...x, color: r.colors[x.value] ?? x.color })))
      setHits(r.hits)
      modal.info({
        title: '按岩性图例赋色',
        content: `${r.total} 个取值中识别出 ${Object.keys(r.colors).length} 个，已按标准岩性图例配色。未识别的 ${r.unknown.length} 个保持原颜色${r.unknown.length ? '：' + r.unknown.slice(0, 8).join('、') + (r.unknown.length > 8 ? '…' : '') : '。'}\n\n将鼠标悬停在某一行上可查看识别结果。`,
      })
    } catch (e) {
      modal.warning({ title: '未找到岩性图例库', content: (e as Error).message })
    }
  }
  const recolor = async () => {
    const r = await api.layerValues(l.id, field)
    setRows(r.values.map((x, i) => ({ ...x, color: r.values[i].color })))
    setHits({})
  }
  let hint = field ? `字段「${field}」共 ${rows.length} 个唯一值，点击色块修改颜色。` : '整个图层使用同一颜色（可在图层菜单「设置颜色」中修改）。'
  if (rows.length > MAX_ROWS) hint += `列表仅显示前 ${MAX_ROWS} 个。`
  if (rows.length > paletteSize) hint += `配色方案只有 ${paletteSize} 种颜色，超出部分将重复使用。`

  return (
    <Modal open title={`唯一值赋色 · ${l.name}`} width={520} onCancel={props.onCancel}
      footer={[
        <Button key="lg" onClick={legend} disabled={!field} style={{ float: 'left' }}>按岩性图例</Button>,
        <Button key="re" onClick={recolor} disabled={!field} style={{ float: 'left' }}>重新配色</Button>,
        <Button key="c" onClick={props.onCancel}>取消</Button>,
        <Button key="ok" type="primary" onClick={() => props.onOk(field, Object.fromEntries(rows.map((r) => [r.value, r.color])))}>确定</Button>,
      ]}>
      <div className="dlg">
        <div className="form-row">
          <label>赋色字段</label>
          <div className="form-ctl">
            <Select value={field} onChange={setField}
              options={[{ value: '', label: '（不按字段，整个图层使用同一颜色）' }, ...(l.fields ?? []).map((f) => ({ value: f, label: f }))]} />
          </div>
        </div>
        <div className="caption">{hint}</div>
        <div className="pick-list tall">
          {rows.slice(0, MAX_ROWS).map((r, i) => {
            const h = hits[r.value]
            return (
              <div key={r.value} className="pick-item" title={h ? `识别为「${h.matched}」（${h.rule}），归入 ${h.path}` : undefined}>
                <ColorPicker size="small" value={r.color} disabledAlpha
                  onChangeComplete={(c) => {
                    setRows(rows.map((x, j) => (j === i ? { ...x, color: c.toHexString() } : x)))
                    const { [r.value]: _drop, ...rest } = hits
                    void _drop
                    setHits(rest)
                  }} />
                <span>{r.value}</span>
                {h ? <span className="muted">→ {h.matched}</span> : null}
              </div>
            )
          })}
        </div>
      </div>
    </Modal>
  )
}

// =============================================================================
// 按范围选择样本
// =============================================================================

export const ROI_MODES: [string, string][] = [
  ['intersect', '相交（样本与范围有交集）'],
  ['within', '完全包含（样本完全落入范围）'],
  ['centroid', '中心点在内（按样本中心判断）'],
]

export function RoiSelectDialog(props: {
  name: string | null
  keyword: boolean
  onCancel: () => void
  onOk: (mode: string, limit: boolean, add: boolean) => void
}) {
  const [mode, setMode] = useState('intersect')
  const [limit, setLimit] = useState(false)
  const [add, setAdd] = useState(false)
  useEffect(() => {
    setLimit(false)
    setAdd(false)
  }, [props.name])
  return (
    <Modal open={props.name !== null} title="按范围选择样本" width={440} onCancel={props.onCancel}
      footer={[
        <Button key="c" onClick={props.onCancel}>取消</Button>,
        <Button key="ok" type="primary" onClick={() => props.onOk(mode, limit, add)}>选择</Button>,
      ]}>
      <div className="dlg">
        {props.name ? <div>按「{props.name}」的范围选择样本。</div> : null}
        <div className="form-row">
          <label>选择方式</label>
          <div className="form-ctl">
            <Radio.Group className="radios" value={mode} onChange={(e) => setMode(e.target.value)}>
              {ROI_MODES.map(([k, t]) => <Radio key={k} value={k}>{t}</Radio>)}
            </Radio.Group>
          </div>
        </div>
        <Checkbox checked={limit} disabled={!props.keyword} onChange={(e) => setLimit(e.target.checked)}>仅在当前检索结果的类别中选择</Checkbox>
        <Checkbox checked={add} onChange={(e) => setAdd(e.target.checked)}>追加到现有选择（不清空已选）</Checkbox>
      </div>
    </Modal>
  )
}
