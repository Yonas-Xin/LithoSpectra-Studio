// 导入、导出、还原三个对话框。

import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Checkbox, Input, InputNumber, Modal, Radio, Select } from 'antd'
import { api } from './api'
import type { AttrProfile, BackupEntry, ExportOptions } from './api'
import { desktop } from './desktop'

const CIRCLED = '①②③④⑤⑥⑦⑧⑨'

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="form-row">
      <label>{label}</label>
      <div className="form-ctl">{children}</div>
    </div>
  )
}

/** 主按钮：陶土 CTA。对话框里"开始导入 / 开始导出"用它。 */
function Primary(props: { disabled?: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <Button className="cta" disabled={props.disabled} onClick={props.onClick}>
      {props.children}
    </Button>
  )
}

// =============================================================================
// 按属性导入
// =============================================================================

export interface AttrImportOptions {
  class_fields: string[]
  region: string
  name_field: string
  code_field: string
  sample_type: string
}

/**
 * 字段的**勾选顺序就是层级顺序**：先勾「岩性大」再勾「代号」，得到的是
 * 岩性大 → 代号 两级。列表上直接把序号标出来（①②③）。
 */
export function AttrImportDialog(props: {
  profile: AttrProfile | null
  onCancel: () => void
  onOk: (opts: AttrImportOptions) => void
}) {
  const p = props.profile
  const [levels, setLevels] = useState<string[]>([])
  const [region, setRegion] = useState('')
  const [nameField, setNameField] = useState('')
  const [codeField, setCodeField] = useState('')
  const [sampleType, setSampleType] = useState('')
  const [preview, setPreview] = useState('尚未选择分级字段。')
  const timer = useRef<number>(0)

  useEffect(() => {
    if (!p) return
    setLevels(p.suggested.filter((s) => p.fields.some((f) => f.name === s)))
    setRegion(p.default_region)
    setNameField('')
    setCodeField('')
    setSampleType('')
  }, [p])

  useEffect(() => {
    if (!p) return
    if (!levels.length) {
      setPreview('尚未选择分级字段。')
      return
    }
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(async () => {
      try {
        const r = await api.attrPreview(p.path, levels)
        let text = `按 ${levels.join(' / ')} 分级 → 末级类别 ${r.leaves} 个，中间分组 ${r.groups} 个`
        if (r.examples.length) text += `\n示例：${r.examples.join('；')}`
        setPreview(text)
      } catch (e) {
        setPreview(`预览失败：${(e as Error).message}`)
      }
    }, 220)
  }, [levels, p])

  const toggle = (name: string) =>
    setLevels((cur) => (cur.includes(name) ? cur.filter((x) => x !== name) : [...cur, name]))
  const names = [{ value: '', label: '（留空）' }, ...(p?.fields ?? []).map((f) => ({ value: f.name, label: f.name }))]
  const blocker = !levels.length ? '' : !region.trim() ? '请填写工区名称。' : ''

  return (
    <Modal
      open={!!p}
      title="按属性导入样本"
      width={680}
      onCancel={props.onCancel}
      footer={[
        <Button key="c" onClick={props.onCancel}>取消</Button>,
        <Primary key="ok" disabled={!levels.length || !region.trim()}
          onClick={() => props.onOk({ class_fields: levels, region: region.trim(), name_field: nameField, code_field: codeField, sample_type: sampleType.trim() })}>
          开始导入
        </Primary>,
      ]}
    >
      {p ? (
        <div className="dlg">
          <div className="dlg-subtitle">{p.name}</div>
          <div className="caption">
            图层 {p.info.layer ?? '-'}　要素 {p.info.features ?? '-'} 条　坐标系 {p.info.crs_name || p.info.crs || '未定义'}
          </div>
          <div className="caption">依次勾选字段左侧的方框，序号即层级顺序（从上层到末级）。再次点击可取消，后续序号自动调整。</div>
          <div className="field-list">
            {p.fields.map((f) => {
              const lv = levels.indexOf(f.name)
              const fill = f.total ? Math.round((f.non_null / f.total) * 100) : 0
              return (
                <div key={f.name} className={'field-item' + (lv >= 0 ? ' on' : '')} onClick={() => toggle(f.name)}
                  title={`类型 ${f.type}　填充率 ${fill}%　唯一值 ${f.unique}\n示例：${f.samples.join('、')}`}>
                  <Checkbox checked={lv >= 0} />
                  <span className="badge">{lv >= 0 ? (lv < 9 ? CIRCLED[lv] : `(${lv + 1})`) : ''}</span>
                  <span className="fname">{f.name}</span>
                  <span className="muted">{f.type}　唯一值 {f.unique}</span>
                </div>
              )
            })}
          </div>
          <Row label="工区名称"><Input value={region} placeholder="如 青海海德乌拉" onChange={(e) => setRegion(e.target.value)} /></Row>
          <Row label="末级类别名取自"><Select value={nameField} options={names} onChange={setNameField} /></Row>
          <Row label="类别号取自"><Select value={codeField} options={names} onChange={setCodeField} /></Row>
          <Row label="样本类型"><Input value={sampleType} placeholder="如 lithology、alteration" onChange={(e) => setSampleType(e.target.value)} /></Row>
          <div className="caption pre">{blocker || preview}</div>
        </div>
      ) : null}
    </Modal>
  )
}

// =============================================================================
// 导出
// =============================================================================

export interface ExportRequest {
  out_dir: string
  driver: string
  template: string
  target_crs: string
  level: string
  restore_attrs: boolean
  buffer_radius: number
  buffer_class_ids: number[] | null
}

/**
 * 「导出层级」决定一个矢量文件代表哪一层类别；树上按分支标了合并 / 拆分时，
 * 默认走那份标记。顶上一行随层级实时变：会导出几个类别、几个文件当场看得见。
 */
export function ExportDialog(props: { opts: ExportOptions | null; onCancel: () => void; onOk: (r: ExportRequest) => void }) {
  const o = props.opts
  const [outDir, setOutDir] = useState('')
  const [driver, setDriver] = useState('')
  const [template, setTemplate] = useState('')
  const [useCrs, setUseCrs] = useState(false)
  const [crs, setCrs] = useState('')
  const [level, setLevel] = useState('leaf')
  const [restore, setRestore] = useState(true)
  const [buffer, setBuffer] = useState(false)
  const [radius, setRadius] = useState(30)
  const [bufIds, setBufIds] = useState<number[]>([])
  const [preview, setPreview] = useState<{ units: number; files: number; samples: number; notes: string[] } | null>(null)

  useEffect(() => {
    if (!o) return
    setOutDir(o.last_dir)
    setDriver(o.driver)
    setTemplate(o.template)
    setUseCrs(false)
    setCrs('')
    setLevel(o.units.length ? 'custom' : 'leaf')
    setRestore(true)
    setBuffer(false)
    setRadius(o.buffer_radius)
    setBufIds(o.point_classes.map((c) => c.id))
  }, [o])

  useEffect(() => {
    if (!o || !o.selected) return setPreview(null)
    api.exportPreview(level).then(setPreview).catch(() => setPreview(null))
  }, [o, level])

  const blocker = useMemo(() => {
    if (!o) return ''
    if (!o.selected) return '未勾选任何样本。请先在样本树中勾选要导出的类别。'
    if (!outDir.trim()) return '请选择导出目录。'
    if (level === 'custom' && !o.units.length) return '样本树中没有标记导出分支，请选择其他导出层级。'
    if (buffer && !bufIds.length) return '已启用点要素缓冲，但未选择任何类别。'
    return ''
  }, [o, outDir, level, buffer, bufIds])

  if (!o) return <Modal open={false} />
  const note = preview?.notes[0] ?? ''
  const head = !o.selected
    ? '未勾选任何样本。'
    : level === 'custom' && !o.units.length
      ? `当前勾选 ${o.selected} 条样本，涉及 ${o.selected_classes} 个类别。`
      : preview
        ? (
          <>
            当前勾选 {o.selected} 条样本，涉及 {o.selected_classes} 个类别 → 按此层级将导出 <b>{preview.units} 个类别</b>，
            {preview.files === preview.units ? `${preview.files} 个文件` : `${preview.files} 个文件（点面分开）`}，共 {preview.samples} 条样本。
          </>
        )
        : ''
  const hint = blocker
    ? blocker
    : level === 'custom'
      ? `按样本树中标记的分支导出：${o.units.slice(0, 6).map((u) => u.name).join('、')}${o.units.length > 6 ? '…' : ''}${note ? '　' + note : ''}`
      : `按固定层级汇总，忽略样本树中的分支标记。${note ? '　' + note : ''}`

  const browse = async () => {
    const dt = desktop()
    const p = dt ? await dt.pick_folder(outDir) : window.prompt('导出目录', outDir)
    if (p) setOutDir(p)
  }
  const pointCls = o.point_classes

  return (
    <Modal
      open
      title="导出样本"
      width={600}
      onCancel={props.onCancel}
      footer={[
        <Button key="c" onClick={props.onCancel}>取消</Button>,
        <Primary key="ok" disabled={!!blocker}
          onClick={() => props.onOk({
            out_dir: outDir.trim(), driver, template, target_crs: useCrs ? crs.trim() : '', level,
            restore_attrs: restore, buffer_radius: buffer ? radius : 0,
            buffer_class_ids: buffer && bufIds.length < pointCls.length ? bufIds : null,
          })}>
          开始导出
        </Primary>,
      ]}
    >
      <div className="dlg">
        <div className="dlg-subtitle">{head}</div>
        <Row label="导出目录">
          <div className="inline">
            <Input value={outDir} placeholder="选择导出目录…" onChange={(e) => setOutDir(e.target.value)} />
            <Button onClick={browse}>浏览…</Button>
          </div>
        </Row>
        <Row label="文件格式"><Select value={driver} options={o.drivers.map((d) => ({ value: d, label: d }))} onChange={setDriver} /></Row>
        <Row label="命名模板">
          <Input value={template} onChange={(e) => setTemplate(e.target.value)}
            title={'可用字段：{label} {name} {region} {type} {date} {parent} {path}\n{label} 为本次导出的标签号，按导出单元从 0 起连续编号，与文件中的 class 字段一致。'} />
        </Row>
        <Row label="">
          <div className="inline">
            <Checkbox checked={useCrs} onChange={(e) => setUseCrs(e.target.checked)}>投影到指定坐标系</Checkbox>
            <Input disabled={!useCrs} value={crs} placeholder="如 EPSG:32646；留空则还原为导入时的坐标系" onChange={(e) => setCrs(e.target.value)} />
          </div>
        </Row>
        <fieldset className="group">
          <legend>导出层级</legend>
          <Radio.Group value={level} onChange={(e) => setLevel(e.target.value)} className="radios">
            <Radio value="custom" disabled={!o.units.length} title="按样本树中为各分支设置的「整支合并 / 按子类分别输出」导出">
              按样本树中标记的分支层级（推荐）
            </Radio>
            <Radio value="leaf">每个末级类别一个文件</Radio>
            <Radio value="1">按第 1 层类别汇总</Radio>
            <Radio value="2" disabled={o.max_depth < 2}>按第 2 层类别汇总</Radio>
          </Radio.Group>
        </fieldset>
        <Checkbox checked={restore} onChange={(e) => setRestore(e.target.checked)}>还原导入时的原始属性字段</Checkbox>
        <fieldset className="group">
          <legend>点要素缓冲</legend>
          <div className="inline">
            <Checkbox disabled={!pointCls.length} checked={buffer} onChange={(e) => setBuffer(e.target.checked)}
              title="仅处理点要素，面要素不受影响；重叠的圆合并为一个面。">将点缓冲为半径</Checkbox>
            <InputNumber disabled={!buffer} min={0.1} max={100000} step={5} value={radius} addonAfter="m"
              onChange={(v) => setRadius(Number(v) || 0.1)} style={{ width: 130 }} />
            <span>的圆，重叠的合并为一个面</span>
          </div>
          <div className="caption">
            {!pointCls.length
              ? '所选类别中没有点样本，无需缓冲。'
              : buffer
                ? `仅对勾选的类别缓冲（共 ${pointCls.length} 个含点样本的类别）。`
                : '不缓冲，点样本按原样导出。'}
          </div>
          {pointCls.length ? (
            <div className={'check-list' + (buffer ? '' : ' off')}>
              {pointCls.map((c) => (
                <Checkbox key={c.id} disabled={!buffer} checked={bufIds.includes(c.id)}
                  onChange={(e) => setBufIds(e.target.checked ? [...bufIds, c.id] : bufIds.filter((x) => x !== c.id))}>
                  {c.path}
                </Checkbox>
              ))}
            </div>
          ) : null}
        </fieldset>
        <div className={'caption' + (blocker ? ' warn' : '')}>{hint}</div>
      </div>
    </Modal>
  )
}

// =============================================================================
// 从备份还原
// =============================================================================

export function RestoreDialog(props: {
  backups: BackupEntry[] | null
  onCancel: () => void
  onPick: (path: string) => void
  onFromFile: () => void
}) {
  const [pick, setPick] = useState<string | null>(null)
  useEffect(() => setPick(null), [props.backups])
  return (
    <Modal
      open={!!props.backups}
      title="从备份还原"
      width={560}
      onCancel={props.onCancel}
      footer={[
        <Button key="f" onClick={props.onFromFile} style={{ float: 'left' }}>从其他文件还原…</Button>,
        <Button key="c" onClick={props.onCancel}>取消</Button>,
        <Button key="ok" danger disabled={!pick} onClick={() => pick && props.onPick(pick)}>还原</Button>,
      ]}
    >
      <div className="dlg">
        <div className="caption">还原将用所选备份替换当前样本库。替换前会自动备份当前状态，可随时恢复。</div>
        <div className="dlg-subtitle small">自动备份（样本库旁的 backups 文件夹）</div>
        <div className="pick-list">
          {props.backups?.length ? (
            props.backups.map((b) => (
              <div key={b.path} className={'pick-item mono' + (pick === b.path ? ' on' : '')} onClick={() => setPick(b.path)}>
                {b.time}　{b.name}　{b.size_mb.toFixed(1)} MB
              </div>
            ))
          ) : (
            <div className="caption" style={{ padding: 8 }}>暂无自动备份。如需从手动备份的文件还原，请点击左下角「从其他文件还原」。</div>
          )}
        </div>
      </div>
    </Modal>
  )
}
