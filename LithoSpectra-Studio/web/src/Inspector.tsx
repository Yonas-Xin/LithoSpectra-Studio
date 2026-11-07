import { useEffect, useState } from 'react'
import { Button, ColorPicker, Input, InputNumber, Popconfirm, Slider, TreeSelect } from 'antd'
import { api } from './api'
import type { Feature, LayerInfo, LayerPatch, LibState, SampleClass } from './api'
import type { SamplesLayerState } from './Layers'
import { Icon } from './theme'

const KIND_LABEL: Record<string, string> = { point: '点', polygon: '面', group: '分组', mixed: '点+面', '': '未定' }

interface Props {
  state: LibState | null
  classes: SampleClass[]
  totalSamples: number
  cls: SampleClass | null
  selected: string[]
  feature: Feature | null
  /** 图层清单里选中的图层；'samples' 是样本图层自己 */
  layer: LayerInfo | 'samples' | null
  samplesLayer: SamplesLayerState
  onPatchLayer: (id: number | 'samples', patch: LayerPatch) => void
  onLayerAction: (action: string, layer: LayerInfo) => void
  onRename: (title: string, label: string, initial: string) => Promise<string | null>
  onPatchClass: (id: number, body: { name?: string; color?: string; description?: string }) => void
  onLabel: (key: string, label: string) => void
  onMove: (keys: string[], classId: number) => void
  onDelete: (keys: string[]) => void
}

/** 失焦或回车才提交的输入框，免得每敲一个字就写一次库。 */
function CommitInput(props: { value: string; placeholder?: string; area?: boolean; onCommit: (v: string) => void }) {
  const [v, setV] = useState(props.value)
  useEffect(() => setV(props.value), [props.value])
  const commit = () => {
    if (v !== props.value) props.onCommit(v)
  }
  return props.area ? (
    <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} value={v} placeholder={props.placeholder}
      onChange={(e) => setV(e.target.value)} onBlur={commit} />
  ) : (
    <Input value={v} placeholder={props.placeholder} onChange={(e) => setV(e.target.value)} onBlur={commit} onPressEnter={commit} />
  )
}

/** 检查器的字段：小标题 + 值 + 发丝下划线。 */
function Field({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div className="field">
      <div className="field-label">{label}</div>
      <div className={'field-value' + (mono ? ' mono' : '')}>{children}</div>
    </div>
  )
}

function classTree(classes: SampleClass[], leafOnly: (c: SampleClass) => boolean) {
  interface N { value: number; title: string; selectable: boolean; children: N[] }
  const nodes = new Map<number, N>()
  for (const c of classes) nodes.set(c.id, { value: c.id, title: c.name, selectable: leafOnly(c), children: [] })
  const roots: N[] = []
  for (const c of classes) {
    const parent = c.parent_id != null ? nodes.get(c.parent_id) : undefined
    if (parent) parent.children.push(nodes.get(c.id)!)
    else roots.push(nodes.get(c.id)!)
  }
  return roots
}

export default function Inspector(props: Props) {
  const { cls, feature, selected, classes } = props

  let title = ''
  let kind = ''
  let body: React.ReactNode = null

  if (selected.length > 1) {
    const kinds = new Set(selected.map((k) => k.split(':')[0]))
    const kindOne = kinds.size === 1 ? [...kinds][0] : ''
    title = `已选 ${selected.length} 个要素`
    kind = kindOne === 'point' ? '点' : kindOne === 'polygon' ? '面' : '点 + 面'
    body = (
      <>
        <Field label="改为类别">
          {kindOne ? (
            <TreeSelect style={{ width: '100%' }} placeholder="选择基础类别" treeDefaultExpandAll showSearch
              treeNodeFilterProp="title" popupMatchSelectWidth={false}
              treeData={classTree(classes, (c) => !c.is_group && (c.kind === kindOne || c.kind === ''))}
              onChange={(id: number) => props.onMove(selected, id)} />
          ) : (
            <span className="muted">选中要素同时包含点和面，一个类别只能包含一种几何类型，请分开改归。</span>
          )}
        </Field>
        <div className="actions">
          <Popconfirm title={`删除这 ${selected.length} 个要素？`} description="保存前可撤销。" onConfirm={() => props.onDelete(selected)}>
            <Button danger className="btn-danger">删除选中要素</Button>
          </Popconfirm>
        </div>
      </>
    )
  } else if (feature) {
    const p = feature.properties
    const owner = classes.find((c) => c.id === p.class_id)
    title = p.label || `${p.kind === 'point' ? '点' : '面'} #${Math.abs(Number(p.key.split(':')[1]))}`
    kind = `${p.kind === 'point' ? '点' : '面'}样本${p.draft ? ' · 未保存' : ''}`
    body = (
      <>
        <Field label="名称">
          <CommitInput value={p.label} placeholder="可留空" onCommit={(v) => props.onLabel(p.key, v)} />
        </Field>
        <Field label="所属类别">
          <TreeSelect style={{ width: '100%' }} value={p.class_id} treeDefaultExpandAll showSearch
            treeNodeFilterProp="title" popupMatchSelectWidth={false}
            treeData={classTree(classes, (c) => !c.is_group && (c.kind === p.kind || c.kind === '' || c.id === p.class_id))}
            onChange={(id: number) => id !== p.class_id && props.onMove([p.key], id)} />
        </Field>
        <Field label="工区">{owner?.region || '—'}</Field>
        <Field label="来源">{p.source || '—'}</Field>
        <Field label="范围" mono>{p.bbox ? p.bbox.map((x) => x.toFixed(5)).join(', ') : '—'}</Field>
        <div className="actions">
          <Popconfirm title="删除该样本？" description="保存前可撤销。" onConfirm={() => props.onDelete([p.key])}>
            <Button className="btn-quiet" icon={<Icon name="trash" size={14} />}>删除</Button>
          </Popconfirm>
        </div>
      </>
    )
  } else if (props.layer === 'samples') {
    title = '样本库 · 样本'
    kind = '样本图层'
    body = (
      <>
        <Field label={`不透明度 ${Math.round(props.samplesLayer.opacity * 100)}%`}>
          <Slider min={0} max={100} value={Math.round(props.samplesLayer.opacity * 100)}
            onChange={(v: number) => props.onPatchLayer('samples', { opacity: v / 100 })} />
        </Field>
        <div className="hint">样本图层不能移除；如需隐藏，取消勾选即可。</div>
      </>
    )
  } else if (props.layer) {
    const l = props.layer
    const raster = l.kind === 'raster' || l.kind === 'image'
    const styled = l.kind === 'vector' || l.kind === 'drawn'
    title = l.name
    kind = l.kind_cn
    body = (
      <>
        <Field label={`不透明度 ${Math.round(l.opacity * 100)}%`}>
          <Slider min={0} max={100} value={Math.round(l.opacity * 100)}
            onChange={(v: number) => props.onPatchLayer(l.id, { opacity: v / 100 })} />
        </Field>
        {styled ? (
          <>
            {/* 面填充单独一条：拉满图层不透明度仍觉得"透"的，是填充 */}
            <Field label={`面填充 ${Math.round(((l.fill_alpha ?? 70) / 255) * 100)}%`}>
              <Slider min={0} max={100} disabled={l.fill === false} value={Math.round(((l.fill_alpha ?? 70) / 255) * 100)}
                onChange={(v: number) => props.onPatchLayer(l.id, { fill_alpha: Math.round((v / 100) * 255) })} />
            </Field>
            <Field label="颜色">
              {l.color_field ? (
                <span>按「{l.color_field}」唯一值赋色　<a onClick={() => props.onLayerAction('unique', l)}>修改…</a></span>
              ) : (
                <ColorPicker value={l.color} disabledAlpha showText onChangeComplete={(c) => props.onPatchLayer(l.id, { color: c.toHexString() })} />
              )}
            </Field>
            <Field label="线宽">
              <InputNumber min={0.2} max={10} step={0.2} value={l.line_width}
                onChange={(v) => v && props.onPatchLayer(l.id, { line_width: Number(v) })} />
            </Field>
            <Field label="要素">{l.feature_count} 个</Field>
          </>
        ) : null}
        {l.info.map((line, i) => {
          const [k, ...rest] = line.split('：')
          return rest.length ? <Field key={i} label={k}>{rest.join('：')}</Field> : <div key={i} className="hint">{line}</div>
        })}
        {l.path ? <Field label="文件"><span className="path" title={l.path}>{l.path}</span></Field> : null}
        <div className="actions">
          {raster ? <Button onClick={() => props.onLayerAction('props', l)}>图层属性…</Button> : null}
          <Button className="btn-quiet" onClick={() => props.onLayerAction('zoom', l)}>缩放到图层</Button>
        </div>
      </>
    )
  } else if (cls) {
    title = cls.name
    kind = cls.is_group ? '分组' : `基础类别 · ${KIND_LABEL[cls.kind] ?? '未定'}`
    body = (
      <>
        <Field label="名称">
          <CommitInput value={cls.name} onCommit={(v) => v.trim() && props.onPatchClass(cls.id, { name: v.trim() })} />
        </Field>
        {cls.is_group ? null : (
          <Field label="颜色">
            <ColorPicker value={cls.color} disabledAlpha showText disabled={cls.draft}
              onChangeComplete={(c) => props.onPatchClass(cls.id, { color: c.toHexString() })} />
          </Field>
        )}
        <Field label="说明">
          <CommitInput area value={cls.description} onCommit={(v) => props.onPatchClass(cls.id, { description: v })} />
        </Field>
        <Field label="工区">{cls.region || '—'}</Field>
        <Field label="样本">{cls.is_group ? `${cls.total} 个（含下级）` : `${cls.count} 个`}</Field>
        <Field label="来源">{cls.source || '—'}</Field>
        {Object.entries(cls.attrs ?? {}).map(([k, v]) => (
          <Field key={k} label={k}>{v == null ? '' : String(v)}</Field>
        ))}
      </>
    )
  } else if (props.state?.open) {
    title = props.state.name
    kind = '样本库'
    body = (
      <>
        <Field label="路径"><span className="path" title={props.state.path}>{props.state.path}</span></Field>
        <Field label="类别">{classes.length} 个</Field>
        <Field label="样本">{props.totalSamples} 个</Field>
        <Field label="存储坐标系">EPSG:4326</Field>
      </>
    )
  } else {
    title = '未打开样本库'
  }

  return (
    <div className="panel">
      <div className="panel-title">检查器</div>
      <div className="panel-body inspector">
        <div className="insp-title">{title}</div>
        {kind ? <div className="insp-kind">{kind}</div> : null}
        {body}
      </div>
    </div>
  )
}

interface IdentItem {
  id: string
  swatch?: string
  title: string
  sub: string
  rows: [string, string][]
}

/**
 * 要素信息：画布上单击一处，那里压着的矢量要素连同全部字段列在这里——样本与参考
 * 矢量、手绘范围都算，影像不算（像元光谱用取谱工具）。样本在前，参考图层按图层
 * 清单从上到下。
 */
export function IdentifyPanel({ keys, classes, layers }: { keys: string[]; classes: SampleClass[]; layers: LayerInfo[] }) {
  const [items, setItems] = useState<IdentItem[]>([])
  const [current, setCurrent] = useState(0)
  useEffect(() => {
    let alive = true
    const byId = new Map(classes.map((c) => [c.id, c]))
    const layerById = new Map(layers.map((l) => [l.id, l]))
    const str = (v: unknown) => (v == null ? '' : String(v))
    Promise.all(
      keys.slice(0, 30).map(async (k): Promise<IdentItem | null> => {
        try {
          if (k.startsWith('L:')) {
            const [, lid, fid] = k.split(':')
            const l = layerById.get(Number(lid))
            const r = await api.layerFeature(Number(lid), Number(fid))
            return { id: k, swatch: l?.color, title: l?.name ?? '图层', sub: `要素 ${fid}`,
              rows: Object.entries(r.attrs).map(([a, v]) => [a, str(v)]) }
          }
          const f = await api.feature(k)
          const p = f.properties
          const cls = byId.get(p.class_id)
          return { id: k, swatch: cls?.color, title: p.label || cls?.name || '样本',
            sub: `${p.kind === 'point' ? '点' : '面'} #${Math.abs(Number(k.split(':')[1]))}`,
            rows: [['类别', cls?.name ?? ''], ['名称', p.label], ['来源', p.source ?? ''],
              ...Object.entries(p.attrs ?? {}).map(([a, v]) => [a, str(v)] as [string, string])] }
        } catch {
          return null
        }
      }),
    ).then((list) => {
      if (!alive) return
      setItems(list.filter((x): x is IdentItem => !!x))
      setCurrent(0)
    })
    return () => {
      alive = false
    }
  }, [keys, classes, layers])

  const f = items[current]
  return (
    <div className="panel">
      <div className="panel-title">要素信息</div>
      <div className="panel-body identify">
        {!keys.length || !f ? (
          <div className="hint">
            使用平移或选择工具在画布上单击，此处显示该位置要素的属性。
            <br />
            多个要素重叠时，样本排在前面，参考图层按图层列表顺序排列。
          </div>
        ) : (
          <>
            {items.length > 1 ? (
              <div className="identify-list">
                {items.map((it, i) => (
                  <div key={it.id} className={'identify-item' + (i === current ? ' on' : '')} onClick={() => setCurrent(i)}>
                    <span className="swatch" style={{ background: it.swatch }} />
                    {it.title}
                    <span className="muted"> · {it.sub}</span>
                  </div>
                ))}
              </div>
            ) : null}
            {items.length === 1 ? <div className="identify-one">{f.title}<span className="muted"> · {f.sub}</span></div> : null}
            <table className="attr-table">
              <tbody>
                {f.rows.map(([k, v], i) => (
                  <tr key={i}><th>{k}</th><td>{v}</td></tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  )
}
