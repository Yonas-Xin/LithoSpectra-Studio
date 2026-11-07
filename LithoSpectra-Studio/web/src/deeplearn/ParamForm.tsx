// 按 Worker 的参数声明生成表单。界面里没有一行"某个任务有哪些参数"的硬编码：
// Worker 加了参数、换了默认值，这里自动跟上。个别参数（样本集、骨干、模型、影像）
// 由调用方给专门的控件，其余按类型生成。

import { useState } from 'react'
import { Button, Checkbox, Input, InputNumber, Select } from 'antd'
import { desktop } from '../desktop'
import type { ParamDecl } from './dlapi'

export type Values = Record<string, unknown>
export type Renderer = (value: unknown, onChange: (v: unknown) => void, p: ParamDecl) => React.ReactNode

export function defaults(params: ParamDecl[]): Values {
  return Object.fromEntries(params.map((p) => [p.name, p.default ?? (p.kind === 'bool' ? false : '')]))
}

function visible(p: ParamDecl, values: Values): boolean {
  return p.visible_when.every((c) => c.values.includes(values[c.param]))
}

export function PathInput(props: { value: string; onChange: (v: string) => void; mode: string; kind?: 'image' | 'vector' | 'any'; placeholder?: string }) {
  const pick = async () => {
    const dt = desktop()
    if (!dt) return
    const r = props.mode === 'directory' ? await dt.pick_folder(props.value || undefined)
      : await dt.pick_file(props.kind ?? 'any')
    if (r) props.onChange(r)
  }
  return (
    <div className="inline">
      <Input value={props.value} placeholder={props.placeholder} onChange={(e) => props.onChange(e.target.value)} />
      {desktop() ? <Button onClick={pick}>浏览…</Button> : null}
    </div>
  )
}

function Field({ p, value, onChange }: { p: ParamDecl; value: unknown; onChange: (v: unknown) => void }) {
  if (p.kind === 'bool') {
    return <Checkbox checked={!!value} onChange={(e) => onChange(e.target.checked)}>{p.label}</Checkbox>
  }
  if (p.kind === 'choice') {
    return <Select value={value as string} onChange={onChange} options={p.choices.map((c) => ({ value: c.value, label: c.label }))} />
  }
  if (p.kind === 'int' || p.kind === 'float') {
    const small = p.kind === 'float' && typeof p.default === 'number' && p.default !== 0 && Math.abs(p.default) < 0.01
    return (
      <InputNumber style={{ width: '100%' }} value={value as number} min={p.minimum ?? undefined} max={p.maximum ?? undefined}
        step={p.kind === 'int' ? 1 : small ? Number(p.default) / 2 : 0.05}
        stringMode={small} onChange={(v) => onChange(v === null ? p.default : Number(v))} />
    )
  }
  if (p.kind === 'path') {
    const kind = p.extensions.some((e) => ['shp', 'gpkg', 'geojson', 'kml'].includes(e)) ? 'vector'
      : p.extensions.some((e) => ['tif'].includes(e)) ? 'image' : 'any'
    return <PathInput value={String(value ?? '')} onChange={onChange} mode={p.path_mode} kind={kind}
      placeholder={p.optional ? '可留空' : ''} />
  }
  return <Input value={String(value ?? '')} placeholder={p.optional ? '可留空' : ''} onChange={(e) => onChange(e.target.value)} />
}

export default function ParamForm(props: {
  params: ParamDecl[]; values: Values; onChange: (v: Values) => void
  renderers?: Record<string, Renderer>; hide?: string[]; labelWidth?: number
}) {
  const [showAdvanced, setShowAdvanced] = useState(false)
  const set = (name: string) => (v: unknown) => props.onChange({ ...props.values, [name]: v })
  const shown = props.params.filter((p) => !props.hide?.includes(p.name) && visible(p, props.values))
  const row = (p: ParamDecl) => {
    const custom = props.renderers?.[p.name]
    const control = custom ? custom(props.values[p.name], set(p.name), p) : <Field p={p} value={props.values[p.name]} onChange={set(p.name)} />
    if (p.kind === 'bool' && !custom) {
      return <div key={p.name} className="dl-form-row bool" title={p.help}>{control}{p.help ? <span className="caption">{p.help}</span> : null}</div>
    }
    return (
      <div key={p.name} className="dl-form-row" title={p.help}>
        <label style={{ width: props.labelWidth ?? 108 }}>{p.label}</label>
        <div className="dl-form-ctl">
          {control}
          {p.help && !custom ? <div className="caption">{p.help}</div> : null}
        </div>
      </div>
    )
  }
  const basic = shown.filter((p) => !p.advanced)
  const advanced = shown.filter((p) => p.advanced)
  return (
    <div className="dl-form">
      {basic.map(row)}
      {advanced.length ? (
        <div className="dl-advanced">
          <button className="dl-link" onClick={() => setShowAdvanced((v) => !v)}>
            {showAdvanced ? '收起高级参数' : `展开高级参数（${advanced.length} 项）`}
          </button>
          {showAdvanced ? advanced.map(row) : null}
        </div>
      ) : null}
    </div>
  )
}
