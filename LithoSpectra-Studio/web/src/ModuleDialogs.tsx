// 样本裁剪对话框与预处理工具箱窗口。工具箱的参数面板按工具自带的 Param 声明生成。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { App as AntApp, Button, Checkbox, Input, InputNumber, Modal, Radio, Select } from 'antd'
import { get, post } from './api'
import type { TaskInfo } from './api'
import { desktop } from './desktop'
import Float from './Float'

// =============================================================================
// 裁剪
// =============================================================================

interface ClipOptions {
  patch_size: number
  format: string
  formats: string[]
  train_ratio: number
  split_by: string
  buffer_radius: number
  max_depth: number
  units: { id: number; name: string }[]
  selected: number
  selected_classes: number
  total: number
  point_classes_selected: { id: number; path: string }[]
  point_classes_all: { id: number; path: string }[]
  last_image: string
  last_dir: string
  rasters: { id: number; name: string; path: string }[]
}

export interface ClipRequest {
  image_path: string
  out_dir: string
  patch_size: number
  output_format: string
  train_ratio: number
  split_by: string
  only_selected: boolean
  level: string
  buffer_radius: number
  buffer_class_ids: number[] | null
}

export const clipApi = {
  options: () => get<ClipOptions>('/api/clip/options'),
  preview: (level: string, only_selected: boolean) =>
    post<{ units: number; files: number; samples: number; notes: string[]; classes: number }>('/api/clip/preview', { level, only_selected }),
  run: (body: ClipRequest) => post<{ task: TaskInfo }>('/api/clip', body),
}

/**
 * 把样本裁成训练用的影像块。「裁剪层级」与导出同一套口径：一个单元就是一个标签，
 * 导出的矢量文件与裁出来的样本块，类别划分方式一致。
 */
export function ClipDialog(props: { open: boolean; onCancel: () => void; onOk: (r: ClipRequest) => void; lockFormat?: string; title?: string }) {
  const [o, setO] = useState<ClipOptions | null>(null)
  const [image, setImage] = useState('')
  const [outDir, setOutDir] = useState('')
  const [patch, setPatch] = useState(17)
  const [fmt, setFmt] = useState('bin')
  const [ratio, setRatio] = useState(0.6)
  const [split, setSplit] = useState('feature')
  const [only, setOnly] = useState(true)
  const [level, setLevel] = useState('leaf')
  const [buffer, setBuffer] = useState(false)
  const [radius, setRadius] = useState(30)
  const [bufIds, setBufIds] = useState<number[]>([])
  const [head, setHead] = useState<React.ReactNode>('')
  const [note, setNote] = useState('')

  useEffect(() => {
    if (!props.open) return
    clipApi.options().then((x) => {
      setO(x)
      setImage(x.last_image || x.rasters[0]?.path || '')
      setOutDir(x.last_dir)
      setPatch(x.patch_size)
      setFmt(props.lockFormat ?? x.format)
      setRatio(x.train_ratio)
      setSplit(x.split_by)
      setOnly(true)
      setLevel(x.units.length ? 'custom' : 'leaf')
      setBuffer(false)
      setRadius(x.buffer_radius)
      setBufIds(x.point_classes_selected.map((c) => c.id))
    })
  }, [props.open, props.lockFormat])

  const pointCls = o ? (only ? o.point_classes_selected : o.point_classes_all) : []
  useEffect(() => setBufIds(pointCls.map((c) => c.id)), [only, o]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!o) return
    if (only && !o.selected) { setHead('未勾选任何样本。'); setNote(''); return }
    if (level === 'custom' && !o.units.length) { setHead(`当前勾选 ${o.selected} 条样本，涉及 ${o.selected_classes} 个类别。`); return }
    clipApi.preview(level, only).then((info) => {
      const scope = only ? `当前勾选 ${o.selected} 条样本，涉及 ${o.selected_classes} 个类别` : `整个样本库 ${o.total} 条样本，${info.classes} 个类别`
      setHead(<>{scope} → 按此层级将裁剪出 <b>{info.units} 个标签</b>（类别单元），共 {info.samples} 条样本。</>)
      setNote(info.notes[0] ?? '')
    })
  }, [o, level, only])

  const blocker = useMemo(() => {
    if (!o) return ''
    if (only && !o.selected) return '未勾选任何样本。请先在样本树中勾选，或取消「仅裁剪已勾选的样本」。'
    if (level === 'custom' && !o.units.length) return '样本树中没有标记裁剪分支，请选择其他裁剪层级。'
    if (!image.trim()) return '请选择要裁剪的影像。'
    if (!outDir.trim()) return '请选择输出目录。'
    if (buffer && !bufIds.length) return '已启用点要素缓冲，但未选择任何类别。'
    return ''
  }, [o, only, level, image, outDir, buffer, bufIds])

  const hints = [blocker || note]
  if (!blocker) {
    if (ratio >= 1) hints.push('不划分测试集，全部写入 train_dataset。')
    else if (split === 'pixel') hints.push('按像元划分时，同一露头的相邻像元可能同时进入训练集与测试集，测试精度会偏高，仅建议在复现旧结果时使用。')
    if (!only) hints.push('将裁剪整个样本库，而非仅已勾选的样本。')
  }

  if (!props.open || !o) return <Modal open={false} />
  const browseImage = async () => {
    const p = desktop() ? await desktop()!.pick_file('image') : window.prompt('影像路径', image)
    if (p) setImage(p)
  }
  const browseDir = async () => {
    const p = desktop() ? await desktop()!.pick_folder(outDir) : window.prompt('输出目录', outDir)
    if (p) setOutDir(p)
  }
  return (
    <Modal open title={props.title ?? '裁剪样本块'} width={620} onCancel={props.onCancel}
      footer={[
        <Button key="c" onClick={props.onCancel}>取消</Button>,
        <Button key="ok" className="cta" disabled={!!blocker} onClick={() => props.onOk({
          image_path: image.trim(), out_dir: outDir.trim(), patch_size: patch, output_format: fmt, train_ratio: ratio,
          split_by: split, only_selected: only, level, buffer_radius: buffer ? radius : 0,
          buffer_class_ids: buffer && bufIds.length < pointCls.length ? bufIds : null,
        })}>开始裁剪</Button>,
      ]}>
      <div className="dlg">
        <div className="dlg-subtitle">{head}</div>
        <div className="form-row"><label>影像</label><div className="form-ctl inline">
          <Select style={{ flex: 1 }} showSearch value={image || undefined} placeholder="选择画布中的影像或浏览文件" onChange={setImage}
            options={[...o.rasters.map((r) => ({ value: r.path, label: r.name })), ...(image && !o.rasters.some((r) => r.path === image) ? [{ value: image, label: image }] : [])]} />
          <Button onClick={browseImage}>浏览…</Button></div></div>
        <div className="form-row"><label>输出目录</label><div className="form-ctl inline">
          <Input value={outDir} placeholder="选择输出目录…" onChange={(e) => setOutDir(e.target.value)} />
          <Button onClick={browseDir}>浏览…</Button></div></div>
        <div className="form-row"><label>块大小</label><div className="form-ctl">
          <InputNumber min={1} max={512} value={patch} onChange={(v) => setPatch(Number(v) || 1)} title="以样本位置为中心的方块边长（像元），常用 13–30" /></div></div>
        <div className="form-row"><label>输出格式</label><div className="form-ctl">
          <Select value={fmt} onChange={setFmt} disabled={!!props.lockFormat} options={o.formats.map((f) => ({ value: f, label: f }))}
            title="h5：所有块保存在一个文件中，便于管理，推荐用于训练；bin：每块一个文件，体积小但文件数量多；tif：每块一个文件，带地理信息，便于逐块核对" /></div></div>
        <div className="form-row"><label>训练集比例</label><div className="form-ctl">
          <InputNumber min={0.05} max={1} step={0.05} value={ratio} onChange={(v) => setRatio(Number(v) || 0.05)} title="1.00 表示不划分，全部作为训练集" /></div></div>
        <div className="form-row"><label>划分方式</label><div className="form-ctl">
          <Select disabled={ratio >= 1} value={split} onChange={setSplit}
            options={[{ value: 'feature', label: '按要素划分（推荐）' }, { value: 'pixel', label: '按像元划分' }]} /></div></div>
        <Checkbox checked={only} onChange={(e) => setOnly(e.target.checked)}>仅裁剪已勾选的样本</Checkbox>
        <fieldset className="group">
          <legend>裁剪层级（一个单元一个标签）</legend>
          <Radio.Group className="radios" value={level} onChange={(e) => setLevel(e.target.value)}>
            <Radio value="custom" disabled={!o.units.length}>按样本树中标记的分支层级（推荐）</Radio>
            <Radio value="leaf">每个末级类别一个标签</Radio>
            <Radio value="1">按第 1 层类别汇总</Radio>
            <Radio value="2" disabled={o.max_depth < 2}>按第 2 层类别汇总</Radio>
          </Radio.Group>
        </fieldset>
        <fieldset className="group">
          <legend>点要素缓冲</legend>
          <div className="inline">
            <Checkbox className="nowrap" disabled={!pointCls.length} checked={buffer} onChange={(e) => setBuffer(e.target.checked)}>将点缓冲为半径</Checkbox>
            <InputNumber disabled={!buffer} min={0.1} max={100000} step={5} value={radius} addonAfter="m" onChange={(v) => setRadius(Number(v) || 0.1)} style={{ width: 130 }} />
            <span className="nowrap">的圆</span>
          </div>
          <div className="caption">{!pointCls.length ? '所选类别中没有点样本，无需缓冲。' : buffer ? `圆内每个像元裁剪一块，重叠部分合并。仅对勾选的类别缓冲（共 ${pointCls.length} 个含点样本的类别）。` : '不缓冲，每个点裁剪一块。'}</div>
          {pointCls.length ? (
            <div className={'check-list' + (buffer ? '' : ' off')}>
              {pointCls.map((c) => (
                <Checkbox key={c.id} disabled={!buffer} checked={bufIds.includes(c.id)}
                  onChange={(e) => setBufIds(e.target.checked ? [...bufIds, c.id] : bufIds.filter((x) => x !== c.id))}>{c.path}</Checkbox>
              ))}
            </div>
          ) : null}
        </fieldset>
        <div className={'caption' + (blocker ? ' warn' : '')}>{hints.filter(Boolean).join('　')}</div>
      </div>
    </Modal>
  )
}

// =============================================================================
// 工具箱
// =============================================================================

interface ToolParam {
  name: string
  label: string
  default: unknown
  kind: 'str' | 'int' | 'float' | 'bool' | 'choice' | 'path' | 'strlist' | 'ranges'
  choices: string[]
  choice_labels: [string, string][]
  help: string
  path_mode: string
  suggestions: string[]
}
interface ToolDef {
  id: string
  name: string
  summary: string
  input_kind: string
  output_kind: string
  params: ToolParam[]
}

const tbApi = {
  tools: () => get<ToolDef[]>('/api/toolbox/tools'),
  visible: (tool: string, values: Record<string, unknown>) => post<string[]>('/api/toolbox/visible', { tool, values }),
  envs: () => get<{ name: string; python: string; kind: string; current: boolean }[]>('/api/toolbox/envs'),
  probe: (python: string) => get<{ text: string; ok: boolean }>(`/api/toolbox/probe?python=${encodeURIComponent(python)}`),
  run: (body: Record<string, unknown>) => post<{ id: string }>('/api/toolbox/run', body),
  state: (id: string, since: number) => get<{ lines: { t: string; tone: string }[]; next: number; progress: number[]; status: string; done: boolean; code: number | null }>(`/api/toolbox/runs/${id}?since=${since}`),
  cancel: (id: string) => post<{ ok: boolean }>(`/api/toolbox/runs/${id}/cancel`),
  rasters: () => get<{ name: string; path: string }[]>('/api/toolbox/rasters'),
}

/** 区间参数（如去水汽的波长段）写成 "1324.5-1459, 1795.5-1963.7" 这样的文本 */
const rangesToText = (v: unknown) => Array.isArray(v) ? (v as number[][]).map((p) => p.join('-')).join(', ') : String(v ?? '')
const textToRanges = (t: string) => t.split(/[,;，；]/).map((s) => s.trim()).filter(Boolean).map((s) => s.split(/\s*[-~～]\s*/).map(Number))

function ParamField({ p, value, onChange }: { p: ToolParam; value: unknown; onChange: (v: unknown) => void }) {
  if (p.kind === 'bool') return <Checkbox checked={!!value} onChange={(e) => onChange(e.target.checked)}>{p.label}</Checkbox>
  let control: React.ReactNode
  if (p.kind === 'choice') {
    const labels = Object.fromEntries(p.choice_labels)
    control = <Select value={String(value)} onChange={onChange} options={p.choices.map((c) => ({ value: c, label: labels[c] ?? c }))} />
  } else if (p.kind === 'int' || p.kind === 'float') {
    control = <InputNumber style={{ width: '100%' }} value={Number(value)} step={p.kind === 'int' ? 1 : 0.1} onChange={(v) => onChange(v)} />
  } else if (p.kind === 'ranges') {
    control = <Input value={rangesToText(value)} onChange={(e) => onChange(e.target.value)} placeholder="如 1324.5-1459, 1795.5-1963.7" />
  } else if (p.kind === 'strlist') {
    control = <Input value={Array.isArray(value) ? (value as string[]).join(', ') : String(value ?? '')} onChange={(e) => onChange(e.target.value)} />
  } else if (p.kind === 'path') {
    control = (
      <div className="inline">
        <Input value={String(value ?? '')} onChange={(e) => onChange(e.target.value)} />
        <Button onClick={async () => {
          const dt = desktop()
          const r = dt ? (p.path_mode === 'file' ? await dt.pick_file('any') : await dt.pick_folder()) : null
          if (r) onChange(r)
        }}>浏览…</Button>
      </div>
    )
  } else if (p.suggestions.length) {
    control = <Select showSearch mode={undefined} value={String(value ?? '')} onChange={onChange}
      options={p.suggestions.map((s) => ({ value: s, label: s }))} onSearch={(t) => t && onChange(t)} />
  } else {
    control = <Input value={String(value ?? '')} onChange={(e) => onChange(e.target.value)} />
  }
  return (
    <div className="form-row tb-param" title={p.help}>
      <label>{p.label}</label>
      <div className="form-ctl">{control}</div>
    </div>
  )
}

export function ToolboxWindow(props: { input?: string; onClose: () => void; onStatus: (s: string, path?: string) => void }) {
  const { message } = AntApp.useApp()
  const [tools, setTools] = useState<ToolDef[]>([])
  const [toolId, setToolId] = useState('')
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [visible, setVisible] = useState<string[]>([])
  const [envs, setEnvs] = useState<{ name: string; python: string; current: boolean }[]>([])
  const [python, setPython] = useState('')
  const [probe, setProbe] = useState('')
  const [inputs, setInputs] = useState(props.input ?? '')
  const [output, setOutput] = useState('')
  const [overwrite, setOverwrite] = useState(false)
  const [stop, setStop] = useState(false)
  const [dry, setDry] = useState(false)
  const [run, setRun] = useState<{ id: string; status: string; progress: number[]; done: boolean } | null>(null)
  const [log, setLog] = useState<{ t: string; tone: string }[]>([])
  const [rasters, setRasters] = useState<{ name: string; path: string }[]>([])
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    tbApi.tools().then((t) => { setTools(t); if (t[0]) setToolId(t[0].id) }).catch((e) => message.error((e as Error).message))
    tbApi.envs().then((e) => { setEnvs(e); if (e[0]) setPython(e[0].python) })
    tbApi.rasters().then(setRasters)
  }, [message])
  useEffect(() => { if (props.input) setInputs(props.input) }, [props.input])
  const tool = tools.find((t) => t.id === toolId)
  useEffect(() => {
    if (!tool) return
    setValues(Object.fromEntries(tool.params.map((p) => [p.name, p.default])))
  }, [tool])
  useEffect(() => {
    if (!tool) return
    tbApi.visible(tool.id, values).then(setVisible).catch(() => setVisible(tool.params.map((p) => p.name)))
  }, [tool, values])
  useEffect(() => {
    if (!python) return
    setProbe('正在检查环境…')
    tbApi.probe(python).then((r) => setProbe(r.text)).catch(() => setProbe('检查失败'))
  }, [python])
  useEffect(() => { logRef.current?.scrollTo({ top: 1e9 }) }, [log])

  const poll = useCallback(async (id: string, since: number) => {
    const r = await tbApi.state(id, since)
    setLog((l) => [...l, ...r.lines])
    setRun({ id, status: r.status, progress: r.progress, done: r.done })
    if (!r.done) setTimeout(() => poll(id, r.next), 300)
    else props.onStatus(`工具箱：${r.status}`)
  }, [props])

  const start = async () => {
    if (!tool) return
    const params: Record<string, unknown> = {}
    for (const p of tool.params) {
      const v = values[p.name]
      params[p.name] = p.kind === 'ranges' && typeof v === 'string' ? textToRanges(v) : v
    }
    try {
      const r = await tbApi.run({
        tool: tool.id, python, inputs: inputs.split(' ; ').map((s) => s.trim()).filter(Boolean),
        output_dir: output, params, overwrite, stop_on_error: stop, dry_run: dry,
      })
      setLog([])
      setRun({ id: r.id, status: '运行中…', progress: [0, 0], done: false })
      poll(r.id, 0)
    } catch (e) {
      setLog((l) => [...l, { t: `[错误] ${(e as Error).message}`, tone: 'danger' }])
    }
  }

  const running = !!run && !run.done
  const pickInput = async (mode: 'file' | 'dir') => {
    const dt = desktop()
    const p = dt ? (mode === 'file' ? await dt.pick_file('image') : await dt.pick_folder()) : null
    if (p) setInputs(inputs ? `${inputs} ; ${p}` : p)
  }
  return (
    <Float title="工具箱" onClose={props.onClose} initial={{ x: 160, y: 80, w: 980, h: 600 }}>
      <div className="tb">
        <div className="tb-side">
          <div className="caption">工具</div>
          <div className="pick-list tb-tools">
            {tools.map((t) => (
              <div key={t.id} className={'pick-item tb-tool' + (t.id === toolId ? ' on' : '')} onClick={() => !running && setToolId(t.id)}>
                <div className="tb-tool-name">{t.name}</div>
                <div className="caption">{t.summary}</div>
              </div>
            ))}
          </div>
          <div className="caption">运行环境</div>
          <Select size="small" value={python || undefined} onChange={setPython} disabled={running}
            options={envs.map((e) => ({ value: e.python, label: e.name }))} />
          <div className="caption tb-probe">{probe}</div>
        </div>
        <div className="tb-params">
          <div className="dlg-subtitle small">{tool?.name ?? ''}</div>
          {tool?.params.filter((p) => visible.includes(p.name)).map((p) => (
            <ParamField key={p.name} p={p} value={values[p.name]} onChange={(v) => setValues({ ...values, [p.name]: v })} />
          ))}
          <Button size="small" className="btn-quiet" onClick={() => tool && setValues(Object.fromEntries(tool.params.map((p) => [p.name, p.default])))}>恢复默认参数</Button>
        </div>
        <div className="tb-main">
          <div className="form-row"><label>输入</label><div className="form-ctl inline wrap">
            <Input style={{ flex: '1 1 100%' }} value={inputs} onChange={(e) => setInputs(e.target.value)} placeholder="文件或目录，多个用分号 ; 分隔" />
            <Button onClick={() => pickInput('file')}>文件…</Button>
            <Button onClick={() => pickInput('dir')}>目录…</Button>
            <Select size="small" style={{ width: 90 }} placeholder="图层…" value={null} onChange={(p: string) => setInputs(p)}
              options={rasters.map((r) => ({ value: r.path, label: r.name }))} />
          </div></div>
          <div className="form-row"><label>输出目录</label><div className="form-ctl inline">
            <Input value={output} onChange={(e) => setOutput(e.target.value)} />
            <Button onClick={async () => { const p = desktop() ? await desktop()!.pick_folder(output) : null; if (p) setOutput(p) }}>浏览…</Button>
          </div></div>
          <div className="inline">
            <Checkbox checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)}>覆盖已有结果</Checkbox>
            <Checkbox checked={stop} onChange={(e) => setStop(e.target.checked)}>出错时停止</Checkbox>
            <Checkbox checked={dry} onChange={(e) => setDry(e.target.checked)}>仅预览，不写入文件</Checkbox>
          </div>
          <div className="tb-log" ref={logRef}>
            {log.map((l, i) => <div key={i} className={'tb-line ' + l.tone}>{l.t || ' '}</div>)}
          </div>
          <div className="tb-foot">
            {running && run!.progress[1] ? (
              <div className="tb-bar"><span style={{ width: `${(run!.progress[0] / run!.progress[1]) * 100}%` }} /></div>
            ) : null}
            <span className="caption">{run?.status ?? ''}</span>
            <div className="spacer" />
            <Button disabled={!running} onClick={() => run && tbApi.cancel(run.id)}>取消</Button>
            <Button className="cta" disabled={running || !tool} onClick={start}>开始</Button>
          </div>
        </div>
      </div>
    </Float>
  )
}
