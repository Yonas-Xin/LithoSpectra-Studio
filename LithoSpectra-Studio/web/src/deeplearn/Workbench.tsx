// 深度学习工作台：运行环境、样本集、训练、任务、模型库、预测、特征七页。浮在画布之上，不阻塞主界面。

import { useCallback, useEffect, useMemo, useState } from 'react'
import { App as AntApp, Button, Input, Popconfirm, Segmented, Select, Tooltip } from 'antd'
import type { LayerInfo } from '../api'
import { desktop } from '../desktop'
import Float from '../Float'
import FeaturesPage from './Features'
import JobMonitor from './JobMonitor'
import type { MonitorHooks } from './JobMonitor'
import ParamForm, { PathInput, defaults } from './ParamForm'
import type { Values } from './ParamForm'
import { KIND_LABEL, STATE_LABEL, TASK_LABEL, baseName, dl, fmtDuration, fmtTime, pct } from './dlapi'
import type { BackboneDecl, Capabilities, Dataset, DlStatus, DoctorReport, EnvInfo, JobDetail, JobSummary, ModelInfo } from './dlapi'
import { Card, Chip, ClassMix, ClassStrip, EmptyCard, PageHead, SpeedMeter, Stats } from './ui'

export type Page = 'env' | 'data' | 'train' | 'jobs' | 'models' | 'predict' | 'features'

export interface Preset {
  dataset?: string
  model?: string
  pretrained?: string
  mode?: 'classifier' | 'contrastive'
}

const PAGES: { key: Page; label: string; group: number }[] = [
  { key: 'env', label: '运行环境', group: 0 },
  { key: 'data', label: '样本集', group: 1 },
  { key: 'train', label: '训练', group: 1 },
  { key: 'jobs', label: '任务', group: 1 },
  { key: 'models', label: '模型库', group: 2 },
  { key: 'predict', label: '预测', group: 2 },
  { key: 'features', label: '特征', group: 2 },
]

export interface WorkbenchProps {
  page: Page
  setPage: (p: Page) => void
  status: DlStatus | null
  caps: Capabilities | null
  jobs: JobSummary[]
  focusJob: string | null
  setFocusJob: (id: string | null) => void
  preset: Preset
  setPreset: (p: Preset) => void
  picking: boolean
  setPicking: (v: boolean) => void
  points: [number, number][]
  setPoints: (p: [number, number][]) => void
  libraryOpen: boolean
  layers: LayerInfo[]
  hooks: MonitorHooks
  onClose: () => void
  onClip: () => void
  onEnvChanged: () => void
  onJobCreated: (job: JobSummary, autoShow: boolean) => void
  onSaveDrawn: (layerId: number) => Promise<string>
  dataVersion: number
}

const envName = (python: string) => baseName(python.replace(/[\\/](bin[\\/])?python[\d.]*(\.exe)?$/i, ''))

// 填表的两页（训练、预测）切走时只隐藏、不拆掉：去别的页看一眼再回来，填好的参数还在

export default function Workbench(props: WorkbenchProps) {
  const ready = !!props.status?.python && !!props.caps
  const running = props.jobs.filter((j) => j.state === 'running' || j.state === 'queued').length
  const [visited, setVisited] = useState<Set<Page>>(() => new Set([props.page]))
  useEffect(() => {
    setVisited((v) => (v.has(props.page) ? v : new Set([...v, props.page])))
  }, [props.page])
  const kept = (p: Page, node: React.ReactNode) =>
    ready && (props.page === p || visited.has(p))
      ? <div style={{ display: props.page === p ? 'contents' : 'none' }}>{node}</div> : null
  return (
    <Float id="深度学习" title={<>深度学习<span className="float-sub">{props.status?.python ? envName(props.status.python) : '未选择运行环境'}</span></>}
      onClose={props.onClose} initial={{ x: 120, y: 64, w: 1080, h: 700 }}>
      <div className="dl-wb">
        <nav className="dl-nav">
          {PAGES.map((p, i) => (
            <div key={p.key}>
              {i && PAGES[i - 1].group !== p.group ? <div className="dl-nav-sep" /> : null}
              <button className={'dl-nav-item' + (props.page === p.key ? ' on' : '')}
                disabled={!ready && p.key !== 'env' && p.key !== 'data'}
                title={!ready && p.key !== 'env' && p.key !== 'data' ? '请先在「运行环境」中选择环境' : undefined}
                onClick={() => props.setPage(p.key)}>
                {p.label}
                {p.key === 'jobs' && running ? <span className="dl-badge">{running}</span> : null}
                {p.key === 'env' && !props.status?.python ? <span className="dl-dot" /> : null}
              </button>
            </div>
          ))}
          <div className="spacer" />
          {props.status?.device ? <div className="dl-nav-device">{{ cuda: 'CUDA', mps: 'Apple GPU', cpu: 'CPU' }[props.status.device] ?? props.status.device}</div> : null}
        </nav>
        <section className="dl-page">
          {props.page === 'env' ? <EnvPage {...props} /> : null}
          {props.page === 'data' ? <DataPage {...props} /> : null}
          {kept('train', <TrainPage {...props} />)}
          {props.page === 'jobs' ? <JobsPage {...props} /> : null}
          {props.page === 'models' && ready ? <ModelsPage {...props} /> : null}
          {kept('predict', <PredictPage {...props} />)}
          {props.page === 'features' && ready ? (
            <FeaturesPage jobs={props.jobs} layers={props.layers} hooks={props.hooks} picking={props.picking}
              setPicking={props.setPicking} points={props.points} setPoints={props.setPoints}
              onJobCreated={props.onJobCreated} preset={props.preset} dataVersion={props.dataVersion} />
          ) : null}
        </section>
      </div>
    </Float>
  )
}

// =============================================================================
// 运行环境
// =============================================================================

function EnvPage(props: WorkbenchProps) {
  const { message } = AntApp.useApp()
  const [envs, setEnvs] = useState<EnvInfo[] | null>(null)
  const [checking, setChecking] = useState('')
  const [reports, setReports] = useState<Record<string, DoctorReport>>({})
  const [custom, setCustom] = useState('')
  useEffect(() => { dl.envs().then(setEnvs).catch((e) => message.error((e as Error).message)) }, [message])
  const check = async (python: string, refresh = true) => {
    setChecking(python)
    try {
      const r = await dl.doctor(python, refresh)
      setReports((m) => ({ ...m, [python]: r }))
      setEnvs(await dl.envs())
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setChecking('')
    }
  }
  const use = async (python: string) => {
    setChecking(python)
    try {
      await dl.select(python)
      message.success('已切换运行环境')
      props.onEnvChanged()
    } catch (e) {
      message.error((e as Error).message)
      check(python, false)
    } finally {
      setChecking('')
    }
  }
  return (
    <div className="dl-scroll">
      <PageHead title="运行环境"
        desc="训练与预测在所选的 Python 环境中运行，该环境需安装 PyTorch、h5py 与 GDAL。体检会在该环境中导入 PyTorch 并检测可用的计算设备，耗时数秒。" />
      {envs === null ? <div className="caption">正在查找本机的 Python 环境…</div> : null}
      <div className="dl-grid">
        {(envs ?? []).map((e) => {
          const r = reports[e.python]
          return (
            <Card key={e.python} selected={e.selected} className="dl-env"
              title={e.name}
              chips={e.selected ? <Chip tone="accent">使用中</Chip>
                : e.checked ? (e.ok ? <Chip tone="ok">可用</Chip> : <Chip tone="error">不可用</Chip>) : <Chip>未体检</Chip>}
              sub={<span className="mono">{e.python}</span>}
              foot={<>
                {e.device ? <span className="dl-device">{e.device}</span> : <span className="caption">{e.checked ? '' : '尚未检测计算设备'}</span>}
                <div className="spacer" />
                <Button size="small" loading={checking === e.python && !r} onClick={() => check(e.python)}>体检</Button>
                <Button size="small" className={!e.selected && e.checked && e.ok ? 'cta' : ''} disabled={e.selected || !!checking}
                  loading={checking === e.python && !!r} onClick={() => use(e.python)}>{e.selected ? '已选用' : '选用'}</Button>
              </>}>
              {r ? <DoctorView report={r} /> : null}
            </Card>
          )
        })}
      </div>
      <Card title="指定其他解释器" sub="未列出的环境，可直接指定其中的 python 可执行文件。">
        <div className="dl-inline">
          <div style={{ flex: 1 }}><PathInput value={custom} onChange={setCustom} mode="file" placeholder="python 可执行文件的完整路径" /></div>
          <Button disabled={!custom} onClick={() => use(custom)}>体检并选用</Button>
        </div>
      </Card>
      {props.status ? (
        <Card title="位置与版本">
          <div className="dl-kv">
            <div><span>任务目录</span><span className="mono">{props.status.jobs_root}</span>
              {desktop() ? <button className="dl-link" onClick={() => desktop()!.reveal_in_folder(props.status!.jobs_root)}>打开</button> : null}
              {desktop() ? <button className="dl-link" onClick={async () => {
                const p = await desktop()!.pick_folder(props.status!.jobs_root)
                if (p) { await dl.setJobsRoot(p); props.onEnvChanged() }
              }}>更改…</button> : null}</div>
            <div><span>骨干插件目录</span><span className="mono">{props.status.plugin_dir}</span></div>
            {props.caps ? <div><span>Worker</span><span>版本 {props.caps.worker_version}，{props.caps.backbones.length} 个骨干网络，{props.caps.tasks.length} 类任务</span></div> : null}
            {props.caps?.plugin_errors.length ? <div className="warn"><span>插件加载失败</span><span>{props.caps.plugin_errors.join('；')}</span></div> : null}
          </div>
        </Card>
      ) : null}
    </div>
  )
}

function DoctorView({ report }: { report: DoctorReport }) {
  return (
    <div className="dl-doctor">
      {report.checks.map((c) => (
        <div key={c.name} className={'dl-check' + (c.ok ? '' : ' bad')}>
          <span className="dl-check-mark">{c.ok ? '✓' : '✗'}</span>
          <span className="dl-check-name">{c.name}</span>
          <span className="dl-check-detail">{c.detail}</span>
          {!c.ok && c.hint ? <div className="dl-hint pre">{c.hint}</div> : null}
        </div>
      ))}
    </div>
  )
}

// =============================================================================
// 样本集
// =============================================================================

function DataPage(props: WorkbenchProps) {
  const { message, modal } = AntApp.useApp()
  const [list, setList] = useState<Dataset[] | null>(null)
  const reload = useCallback(() => dl.datasets().then(setList).catch((e) => message.error((e as Error).message)), [message])
  useEffect(() => { reload() }, [reload, props.dataVersion])
  const add = async () => {
    const dt = desktop()
    const p = dt ? await dt.pick_folder() : window.prompt('样本集目录（裁剪输出目录）或 h5 文件路径')
    if (!p) return
    try {
      await dl.addDataset(p)
      reload()
    } catch (e) {
      message.error((e as Error).message)
    }
  }
  const rename = (d: Dataset) => {
    let name = d.name
    const apply = async () => {
      try {
        await dl.renameDataset(d.id, name.trim() || d.name)
        reload()
      } catch (e) {
        message.error((e as Error).message)
      }
    }
    const m = modal.confirm({
      title: '重命名样本集', icon: null,
      content: <Input autoFocus defaultValue={d.name} onChange={(e) => { name = e.target.value }}
        onPressEnter={(e) => { if (e.keyCode === 229) return; m.destroy(); apply() }} />,
      okText: '确定', cancelText: '取消',
      onOk: apply,
    })
  }
  const clipButton = (
    <Tooltip title={props.libraryOpen ? '' : '请先打开样本库'}>
      <Button className="cta" disabled={!props.libraryOpen} onClick={props.onClip}>从样本库裁剪…</Button>
    </Tooltip>
  )
  return (
    <div className="dl-scroll">
      <PageHead title="样本集"
        desc="由样本库裁剪得到的 h5 训练数据。从样本库裁剪时会记录所涉类别的样本状态，之后样本增删或类别归属变化，样本集将标记为「来源已变化」。"
        actions={<>{clipButton}<Button onClick={add}>添加已有样本集…</Button></>} />
      {list && !list.length ? (
        <EmptyCard title="暂无样本集" desc="在样本树中勾选需要的类别，然后从样本库裁剪；也可以添加已有的裁剪输出目录。" action={clipButton} />
      ) : null}
      {(list ?? []).map((d) => (
        <Card key={d.id} className="dl-dataset" title={d.name}
          chips={<>
            {d.missing ? <Chip tone="error">文件缺失</Chip> : null}
            {d.stale ? <Chip tone="warn" title="样本库中相关类别的样本有增删或归属变化，建议重新裁剪">来源已变化</Chip> : null}
            {!d.missing && !d.complete ? <Chip tone="warn" title="裁剪中途取消，可能缺少部分类别">未完成</Chip> : null}
          </>}
          sub={d.missing ? undefined : <>来源影像 <span className="mono">{baseName(d.source_image)}</span> · 登记于 {fmtTime(d.created)}</>}
          foot={<>
            <Button size="small" onClick={() => rename(d)}>重命名</Button>
            {desktop() ? <Button size="small" onClick={() => desktop()!.reveal_in_folder(d.path)}>打开目录</Button> : null}
            <Popconfirm title="仅移除登记，不删除文件。" okText="移除" cancelText="取消" onConfirm={async () => { await dl.removeDataset(d.id); reload() }}>
              <Button size="small">移除</Button>
            </Popconfirm>
            <div className="spacer" />
            <Button size="small" className="cta" disabled={d.missing || !props.status?.python}
              onClick={() => { props.setPreset({ ...props.preset, dataset: d.path, mode: 'classifier' }); props.setPage('train') }}>用于训练</Button>
          </>}>
          {d.missing ? <div className="caption warn">{d.error}</div> : (
            <>
              <Stats items={[
                { label: '类别', value: d.label_counts.filter((n) => n > 0).length },
                { label: '训练块', value: d.count },
                { label: '测试块', value: d.test_count || '—', tone: d.test_count ? undefined : 'warn' },
                { label: '波段 × 块大小', value: `${d.bands} × ${d.patch}` },
                { label: '大小', value: `${d.size_mb} MB` },
              ]} />
              <ClassMix counts={d.label_counts} classes={d.class_table} />
              {d.warnings.length ? <ul className="dl-warnings">{d.warnings.map((w) => <li key={w}>{w}</li>)}</ul> : null}
            </>
          )}
        </Card>
      ))}
    </div>
  )
}

// =============================================================================
// 训练
// =============================================================================

const PRESETS: Record<string, Values> = {
  快速: { epochs: 20, batch_size: 256, lr: 1e-3 },
  标准: { epochs: 50, batch_size: 256, lr: 1e-3 },
  精细: { epochs: 120, batch_size: 128, lr: 5e-4, early_stop: 20 },
}
const PRESET_DESC: Record<string, string> = {
  快速: '20 轮，用于快速检验样本集。',
  标准: '50 轮。',
  精细: '120 轮，学习率较小，连续 20 轮无提升时提前停止。',
}

function BackbonePicker(props: { list: BackboneDecl[]; value: string; onChange: (v: string) => void; patch?: number }) {
  return (
    <div className="dl-backbones">
      {props.list.map((b) => {
        const tooSmall = props.patch !== undefined && props.patch < b.min_patch
        return (
          <button key={b.name} type="button" disabled={tooSmall}
            className={'dl-backbone' + (props.value === b.name ? ' on' : '')}
            onClick={() => props.onChange(b.name)}
            title={tooSmall ? `块大小至少为 ${b.min_patch}，当前样本集为 ${props.patch}` : b.summary}>
            <div className="dl-backbone-head">
              <span className="dl-backbone-name">{b.name}</span>
              <SpeedMeter speed={b.speed} />
            </div>
            <div className="dl-backbone-meta">{KIND_LABEL[b.input_kind] ?? b.input_kind} · {b.speed_label}{b.source !== 'builtin' ? ' · 插件' : ''}</div>
            <div className="dl-backbone-sum">{tooSmall ? `块大小至少为 ${b.min_patch}` : b.summary}</div>
          </button>
        )
      })}
    </div>
  )
}

function TrainPage(props: WorkbenchProps) {
  const [mode, setMode] = useState<'classifier' | 'contrastive'>(props.preset.mode === 'contrastive' ? 'contrastive' : 'classifier')
  useEffect(() => { if (props.preset.mode) setMode(props.preset.mode) }, [props.preset.mode])
  return (
    <div className="dl-scroll">
      <PageHead title="训练"
        extra={<Segmented value={mode} onChange={(v) => setMode(v as 'classifier' | 'contrastive')}
          options={[{ value: 'classifier', label: '监督分类' }, { value: 'contrastive', label: '对比学习预训练' }]} />}
        desc={mode === 'classifier'
          ? '使用带标签的样本集训练分类模型，可选用对比学习得到的骨干网络初始化。'
          : '不使用标签，从整景影像中随机采样训练骨干网络。训练分类模型时可将其选作预训练骨干，适用于标注样本较少的情况。'} />
      {mode === 'classifier' ? <ClassifierForm {...props} /> : <ContrastiveForm {...props} />}
    </div>
  )
}

function useDatasets(version: number) {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  useEffect(() => { dl.datasets().then((l) => setDatasets(l.filter((d) => !d.missing))).catch(() => undefined) }, [version])
  return datasets
}

function DatasetSelect(props: { datasets: Dataset[]; value: unknown; onChange: (v: string) => void; allowClear?: boolean; placeholder?: string }) {
  return (
    <Select value={(props.value as string) || undefined} placeholder={props.placeholder ?? '选择已登记的样本集'} allowClear={props.allowClear}
      onChange={(v) => props.onChange(v ?? '')}
      options={props.datasets.map((d) => ({ value: d.path, label: `${d.name}　${d.label_counts.length} 类 · ${d.count} 块 · ${d.bands} 波段 · 块大小 ${d.patch}${d.stale ? ' · 来源已变化' : ''}` }))} />
  )
}

function slowNote(b: BackboneDecl | undefined, device: string | undefined): string {
  return b && (b.speed === 'slow' || b.speed === 'very_slow') && device !== 'cuda'
    ? `${b.name} 计算量较大，在没有 CUDA 显卡的设备上训练与整景预测都较慢。任务开始后可在「任务」页查看预计耗时。` : ''
}

function Section(props: { title: string; children: React.ReactNode }) {
  return (
    <Card className="dl-form-card" title={props.title}>
      {props.children}
    </Card>
  )
}

function Notes({ items }: { items: string[] }) {
  return items.length ? <ul className="dl-notes">{items.map((n) => <li key={n}>{n}</li>)}</ul> : null
}

function ClassifierForm(props: WorkbenchProps) {
  const { message } = AntApp.useApp()
  const task = props.caps!.tasks.find((t) => t.name === 'train_classifier')!
  const [values, setValues] = useState<Values>(() => ({ ...defaults(task.params), ...(props.preset.dataset ? { dataset: props.preset.dataset } : {}),
    ...(props.preset.pretrained ? { pretrained: props.preset.pretrained } : {}) }))
  const datasets = useDatasets(props.dataVersion)
  const [models, setModels] = useState<ModelInfo[]>([])
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [preset, setPresetName] = useState('标准')
  useEffect(() => { dl.models().then(setModels).catch(() => undefined) }, [props.dataVersion])
  useEffect(() => { setValues((v) => (v.dataset || !datasets.length ? v : { ...v, dataset: datasets[0].path })) }, [datasets])
  useEffect(() => { if (props.preset.dataset) setValues((v) => ({ ...v, dataset: props.preset.dataset })) }, [props.preset.dataset])
  const ds = datasets.find((d) => d.path === values.dataset)
  const pre = models.find((m) => m.path === values.pretrained)
  useEffect(() => { if (pre) setValues((v) => ({ ...v, backbone: pre.backbone })) }, [pre])
  useEffect(() => {
    const p = props.preset.pretrained
    if (p) setValues((v) => ({ ...v, pretrained: p }))
  }, [props.preset.pretrained])
  const backbone = props.caps!.backbones.find((b) => b.name === values.backbone)
  const notes: string[] = []
  if (ds?.stale) notes.push('该样本集的来源已变化，模型将基于裁剪时的样本训练。')
  if (ds) notes.push(...ds.warnings.filter((w) => w.includes('倍') || w.includes('少于')))
  if (pre && ds && pre.bands !== ds.bands) notes.push(`预训练骨干的输入为 ${pre.bands} 个波段，样本集为 ${ds.bands} 个，无法使用。`)
  if (pre && ds && pre.input_kind !== 'spectrum' && pre.patch_size !== ds.patch) notes.push(`预训练骨干的块大小为 ${pre.patch_size}，样本集为 ${ds.patch}。`)
  const slow = slowNote(backbone, props.status?.device)
  if (slow) notes.push(slow)
  const candidates = models.filter((m) => !ds || m.bands === ds.bands)
  const submit = async () => {
    setBusy(true)
    try {
      const job = await dl.newJob('train_classifier', values, title.trim() || `${values.backbone}${pre ? ' · 预训练' : ''} · ${ds?.name ?? baseName(String(values.dataset))}`)
      props.onJobCreated(job, false)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <Section title="数据">
        <div className="dl-form-row"><label>样本集</label><div className="dl-form-ctl">
          <DatasetSelect datasets={datasets} value={values.dataset} onChange={(v) => setValues({ ...values, dataset: v })} />
          {ds ? <ClassMix counts={ds.label_counts} classes={ds.class_table} compact /> : null}
          {!datasets.length ? <div className="caption">暂无样本集，请先在「样本集」页裁剪或添加。</div> : null}
        </div></div>
      </Section>
      <Section title="模型">
        <div className="dl-form-row"><label>预训练骨干</label><div className="dl-form-ctl">
          <Select allowClear value={(values.pretrained as string) || undefined} placeholder="不使用（随机初始化）"
            onChange={(v) => setValues({ ...values, pretrained: v ?? '' })}
            options={candidates.map((m) => ({ value: m.path, label: `${m.task === 'backbone' ? '对比学习骨干' : '分类模型的骨干'} · ${m.name}（${m.backbone}${m.metrics.knn_acc ? ` · kNN ${pct(m.metrics.knn_acc)}` : ''}）` }))} />
          {pre ? <div className="caption">骨干网络已设为 {pre.backbone}，归一化方式沿用预训练时的设置。</div> : null}
        </div></div>
        <div className="dl-form-row"><label>骨干网络</label><div className="dl-form-ctl">
          <BackbonePicker list={pre ? props.caps!.backbones.filter((b) => b.name === pre.backbone) : props.caps!.backbones}
            value={String(values.backbone)} patch={ds?.patch} onChange={(v) => setValues({ ...values, backbone: v })} />
        </div></div>
      </Section>
      <Section title="参数">
        <div className="dl-form-row"><label>预设</label><div className="dl-form-ctl">
          <Segmented value={preset} options={Object.keys(PRESETS)} onChange={(v) => { setPresetName(String(v)); setValues({ ...values, ...PRESETS[String(v)] }) }} />
          <div className="caption">{PRESET_DESC[preset]}</div>
        </div></div>
        <ParamForm params={task.params} values={values} onChange={setValues}
          hide={['dataset', 'backbone', 'pretrained', ...(pre ? [] : ['transfer', 'backbone_lr_ratio'])]} />
        <div className="dl-form-row"><label>任务名称</label><div className="dl-form-ctl">
          <Input value={title} placeholder="留空则自动命名" onChange={(e) => setTitle(e.target.value)} /></div></div>
      </Section>
      <Notes items={notes} />
      <div className="dl-actions">
        <span className="caption">训练在后台运行，关闭本窗口不影响训练，进度可在「任务」页查看。</span>
        <Button className="cta" loading={busy} disabled={!values.dataset || !values.backbone} onClick={submit}>开始训练</Button>
      </div>
    </>
  )
}

function ContrastiveForm(props: WorkbenchProps) {
  const { message } = AntApp.useApp()
  const task = props.caps!.tasks.find((t) => t.name === 'train_contrastive')
  const [values, setValues] = useState<Values>(() => (task ? defaults(task.params) : {}))
  const datasets = useDatasets(props.dataVersion)
  const [rasters, setRasters] = useState<{ id: number; name: string; path: string; bands: number }[]>([])
  const [range, setRange] = useState('all')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { dl.rasters().then((r) => { setRasters(r); setValues((v) => (v.image ? v : { ...v, image: r[0]?.path ?? '' })) }).catch(() => undefined) }, [props.layers])
  if (!task) return <div className="caption">当前 Worker 不支持对比学习，请更新岩谱。</div>
  const ranges = props.layers.filter((l) => l.kind === 'drawn' || (l.kind === 'vector' && l.path))
  const fromImage = values.source === 'image'
  const ds = datasets.find((d) => d.path === values.dataset)
  const evalDs = datasets.find((d) => d.path === values.eval_dataset)
  const patch = fromImage ? Number(values.patch_size) : ds?.patch
  const backbone = props.caps!.backbones.find((b) => b.name === values.backbone)
  const raster = rasters.find((r) => r.path === values.image)
  const notes: string[] = []
  const bands = fromImage ? raster?.bands : ds?.bands
  if (evalDs && bands && (evalDs.bands !== bands || evalDs.patch !== patch)) notes.push(`监控样本集为 ${evalDs.bands} 个波段、块大小 ${evalDs.patch}，与无标签数据不一致，将不进行 kNN 评估。`)
  if (!values.eval_dataset) notes.push('未指定监控样本集，无法比较各轮骨干的效果，将保存最后一轮。')
  const slow = slowNote(backbone, props.status?.device)
  if (slow) notes.push(slow)
  const dsRender = (v: unknown, on: (x: unknown) => void, p: { optional: boolean }) =>
    <DatasetSelect datasets={datasets} value={v} onChange={on} allowClear={p.optional} placeholder={p.optional ? '不使用' : undefined} />
  const submit = async () => {
    setBusy(true)
    try {
      let mask = ''
      if (fromImage && range !== 'all') {
        const l = props.layers.find((x) => String(x.id) === range)
        mask = l?.kind === 'drawn' ? await props.onSaveDrawn(l.id) : l?.path ?? ''
      }
      const job = await dl.newJob('train_contrastive', { ...values, mask },
        title.trim() || `对比学习 · ${values.backbone} · ${fromImage ? baseName(String(values.image)) : ds?.name ?? ''}`)
      props.onJobCreated(job, false)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <Section title="数据">
        <ParamForm params={task.params.filter((p) => ['source', 'image', 'samples', 'patch_size', 'dataset', 'eval_dataset'].includes(p.name))}
          values={values} onChange={setValues}
          renderers={{
            image: (v, on) => (
              <Select value={(v as string) || undefined} placeholder="选择画布中的影像" onChange={on}
                options={rasters.map((r) => ({ value: r.path, label: `${r.name}（${r.bands} 波段）` }))} />
            ),
            dataset: dsRender, eval_dataset: dsRender,
          }} />
        {fromImage ? (
          <div className="dl-form-row"><label>采样范围</label><div className="dl-form-ctl">
            <Select value={range} onChange={setRange} options={[{ value: 'all', label: '全图' },
              ...ranges.map((l) => ({ value: String(l.id), label: `${l.name}（${l.kind === 'drawn' ? '手绘范围' : '矢量'}）` }))]} />
          </div></div>
        ) : null}
      </Section>
      <Section title="模型">
        <div className="dl-form-row"><label>骨干网络</label><div className="dl-form-ctl">
          <BackbonePicker list={props.caps!.backbones} value={String(values.backbone)} patch={patch}
            onChange={(v) => setValues({ ...values, backbone: v })} />
        </div></div>
      </Section>
      <Section title="参数">
        <ParamForm params={task.params} values={values} onChange={setValues}
          hide={['source', 'image', 'mask', 'samples', 'patch_size', 'dataset', 'eval_dataset', 'backbone']} />
        <div className="dl-form-row"><label>任务名称</label><div className="dl-form-ctl">
          <Input value={title} placeholder="留空则自动命名" onChange={(e) => setTitle(e.target.value)} /></div></div>
      </Section>
      <Notes items={notes} />
      <div className="dl-actions">
        <span className="caption">结果为骨干网络模型包，完成后列在「模型库」的骨干网络中。</span>
        <Button className="cta" loading={busy} disabled={!values.backbone || (fromImage ? !values.image : !values.dataset)} onClick={submit}>开始预训练</Button>
      </div>
    </>
  )
}

// =============================================================================
// 任务
// =============================================================================

function JobsPage(props: WorkbenchProps) {
  const { message } = AntApp.useApp()
  const [detail, setDetail] = useState<JobDetail | null>(null)
  const focus = props.focusJob ?? props.jobs[0]?.id ?? null
  const summary = props.jobs.find((j) => j.id === focus)
  const live = summary && (summary.state === 'running' || summary.state === 'queued')
  useEffect(() => {
    if (!focus) { setDetail(null); return }
    let stop = false
    let timer = 0
    const tick = async () => {
      try {
        const d = await dl.job(focus)
        if (!stop) setDetail(d)
      } catch (e) {
        if (!stop) { setDetail(null); message.error((e as Error).message) }
        return
      }
      if (!stop && live) timer = window.setTimeout(tick, 1200)
    }
    tick()
    return () => { stop = true; window.clearTimeout(timer) }
  }, [focus, live, summary?.state, message])
  return (
    <div className="dl-jobs">
      <div className="dl-job-list">
        {!props.jobs.length ? <div className="caption dl-job-empty">暂无任务。</div> : null}
        {props.jobs.map((j) => (
          <button key={j.id} type="button" className={'dl-job' + (j.id === focus ? ' on' : '')} onClick={() => props.setFocusJob(j.id)}>
            <div className="dl-job-head">
              <span className={'dl-dot-state ' + j.state} title={STATE_LABEL[j.state]} />
              <span className="dl-job-title">{j.title}</span>
            </div>
            <div className="dl-job-meta">
              {TASK_LABEL[j.task] ?? j.task} · {fmtTime(j.created)}
              {j.test ? <span className="dl-job-score mono">OA {pct(j.test.oa)}</span> : null}
            </div>
            {j.state === 'running' || j.state === 'queued' ? (
              <>
                <div className="dl-job-bar"><span style={{ width: `${j.progress.fraction * 100}%` }} /></div>
                <div className="dl-job-meta">{j.state === 'queued' ? '排队中' : j.progress.text}{j.state === 'running' && j.progress.eta ? ` · 剩余约 ${fmtDuration(j.progress.eta)}` : ''}</div>
              </>
            ) : <div className={'dl-job-state ' + j.state}>{STATE_LABEL[j.state]}</div>}
          </button>
        ))}
      </div>
      <div className="dl-job-detail dl-scroll">
        {detail && detail.id === focus ? <JobMonitor job={detail} hooks={props.hooks} />
          : <EmptyCard title="未选择任务" desc="在左侧列表中选择任务查看进度与结果。" />}
      </div>
    </div>
  )
}

// =============================================================================
// 模型库
// =============================================================================

function ModelsPage(props: WorkbenchProps) {
  const { message } = AntApp.useApp()
  const [models, setModels] = useState<ModelInfo[] | null>(null)
  const reload = useCallback(() => dl.models().then(setModels).catch((e) => message.error((e as Error).message)), [message])
  useEffect(() => { reload() }, [reload, props.jobs.length, props.dataVersion])
  const add = async () => {
    const p = desktop() ? await desktop()!.pick_folder() : window.prompt('模型包路径（.lithomodel 目录）')
    if (!p) return
    try { await dl.addModel(p); reload() } catch (e) { message.error((e as Error).message) }
  }
  const list = (models ?? []).filter((m) => m.task === 'classifier')
  const backbones = (models ?? []).filter((m) => m.task === 'backbone')
  const toJob = (m: ModelInfo) => { props.setFocusJob(m.job); props.setPage('jobs') }
  const toFeatures = (m: ModelInfo) => { props.setPreset({ ...props.preset, model: m.path }); props.setPage('features') }
  const common = (m: ModelInfo) => <>
    {m.job && !m.external ? <Button size="small" onClick={() => toJob(m)}>训练记录</Button> : null}
    {desktop() ? <Button size="small" onClick={() => desktop()!.reveal_in_folder(m.path)}>打开目录</Button> : null}
    {m.external ? <Button size="small" onClick={async () => { await dl.forgetModel(m.path); reload() }}>移除</Button> : null}
  </>
  return (
    <div className="dl-scroll">
      <PageHead title="模型库" desc="训练任务生成的模型包，以及手动添加的外部模型包（.lithomodel）。"
        actions={<Button onClick={add}>添加模型包…</Button>} />
      {models && !list.length && !backbones.length ? (
        <EmptyCard title="暂无模型" desc="在「训练」页训练分类模型或对比学习骨干，完成后列在这里。"
          action={<Button className="cta" onClick={() => props.setPage('train')}>前往训练</Button>} />
      ) : null}
      {list.length ? <div className="dl-group-title">分类模型<span>{list.length}</span></div> : null}
      <div className="dl-grid wide">
        {list.map((m) => {
          const t = m.metrics.test
          const score = t?.oa ?? m.metrics.best_score
          return (
            <Card key={m.path} className="dl-model" title={m.name}
              chips={<>
                {m.partial ? <Chip tone="warn" title="训练中途取消，使用的是取消前最好的一轮">未训练完</Chip> : null}
                {m.stale ? <Chip tone="warn" title="训练所用样本集的来源已变化">来源已变化</Chip> : null}
                {m.external ? <Chip>外部</Chip> : null}
              </>}
              sub={<>{m.backbone} · {m.bands} 波段 · 块大小 {m.patch_size} · {fmtTime(m.created)}</>}
              aside={score !== undefined && score !== null ? (
                <div className="dl-score"><div className="mono">{pct(score)}</div><span>{t ? '测试 OA' : `验证 ${m.metrics.select_metric === 'macro_f1' ? '宏 F1' : m.metrics.select_metric ?? ''}`}</span></div>
              ) : undefined}
              foot={<>
                {common(m)}
                <Button size="small" onClick={() => toFeatures(m)}>特征</Button>
                <div className="spacer" />
                <Button size="small" className="cta" onClick={() => props.hooks.onPredictWith(m.path)}>预测…</Button>
              </>}>
              <ClassStrip classes={m.classes} />
              <div className="dl-model-classes">{m.classes.map((c) => c.name).join('、')}</div>
              {t ? (
                <div className="dl-metric-row mono">
                  <span>AA {pct(t.aa)}</span><span>宏 F1 {pct(t.macro_f1)}</span><span>Kappa {t.kappa.toFixed(3)}</span>
                </div>
              ) : null}
              {m.pretrained ? <div className="caption">由预训练骨干 {m.pretrained.name} 初始化</div> : null}
            </Card>
          )
        })}
      </div>
      {backbones.length ? <div className="dl-group-title">骨干网络（对比学习）<span>{backbones.length}</span></div> : null}
      <div className="dl-grid wide">
        {backbones.map((m) => (
          <Card key={m.path} className="dl-model" title={m.name}
            chips={<>
              <Chip>{m.method === 'moco' ? 'MoCo' : 'SimCLR'}</Chip>
              {m.partial ? <Chip tone="warn">未训练完</Chip> : null}
              {m.external ? <Chip>外部</Chip> : null}
            </>}
            sub={<>{m.backbone} · {m.bands} 波段 · 块大小 {m.patch_size} · 特征 {m.feature_dim} 维 · {fmtTime(m.created)}</>}
            aside={m.metrics.knn_acc ? <div className="dl-score"><div className="mono">{pct(m.metrics.knn_acc)}</div><span>kNN 精度</span></div> : undefined}
            foot={<>
              {common(m)}
              <Button size="small" onClick={() => toFeatures(m)}>特征</Button>
              <div className="spacer" />
              <Button size="small" className="cta" onClick={() => { props.setPreset({ ...props.preset, pretrained: m.path, mode: 'classifier' }); props.setPage('train') }}>用于分类训练…</Button>
            </>}>
            <div className="caption">
              {m.metrics.knn_acc ? `第 ${m.metrics.best_epoch} 轮的骨干效果最好。` : '训练时未指定监控样本集，保存的是最后一轮。'}
              {m.dataset.count ? ` 训练使用 ${m.dataset.count} 个无标签块。` : ''}
            </div>
          </Card>
        ))}
      </div>
    </div>
  )
}

// =============================================================================
// 预测
// =============================================================================

function PredictPage(props: WorkbenchProps) {
  const { message } = AntApp.useApp()
  const task = props.caps!.tasks.find((t) => t.name === 'predict')!
  const [values, setValues] = useState<Values>(() => ({ ...defaults(task.params), ...(props.preset.model ? { model: props.preset.model } : {}) }))
  const [models, setModels] = useState<ModelInfo[]>([])
  const [rasters, setRasters] = useState<{ id: number; name: string; path: string; bands: number }[]>([])
  const [compat, setCompat] = useState<{ level: string; text: string } | null>(null)
  const [range, setRange] = useState<string>('all')
  const [busy, setBusy] = useState(false)
  // 训练完成后新模型要出现在下拉里
  useEffect(() => { dl.models().then((m) => setModels(m.filter((x) => x.task === 'classifier'))).catch(() => undefined) }, [props.dataVersion])
  useEffect(() => {
    dl.rasters().then((r) => {
      setRasters(r)
      setValues((v) => (v.image ? v : { ...v, image: r[0]?.path ?? '' }))
    }).catch(() => undefined)
  }, [props.layers])
  useEffect(() => { if (props.preset.model) setValues((v) => ({ ...v, model: props.preset.model })) }, [props.preset.model])
  const model = models.find((m) => m.path === values.model)
  const layer = rasters.find((r) => r.path === values.image)
  useEffect(() => {
    setCompat(null)
    if (!model) return
    if (layer) dl.compat(model.path, layer.id).then(setCompat).catch((e) => setCompat({ level: 'error', text: (e as Error).message }))
    else if (values.image) setCompat({ level: 'ok', text: '影像不在画布中，将在任务开始时检查波段与波长。' })
  }, [model, layer, values.image])
  const ranges = useMemo(() => props.layers.filter((l) => l.kind === 'drawn' || (l.kind === 'vector' && l.path)), [props.layers])
  const submit = async () => {
    setBusy(true)
    try {
      let mask = ''
      if (range !== 'all') {
        const l = props.layers.find((x) => String(x.id) === range)
        if (l?.kind === 'drawn') mask = await props.onSaveDrawn(l.id)
        else mask = l?.path ?? ''
      }
      const params = { ...values, mask }
      const job = await dl.newJob('predict', params, `预测 · ${baseName(String(values.image))} · ${model?.name ?? ''}`)
      props.onJobCreated(job, true)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="dl-scroll">
      <PageHead title="整景预测"
        desc="预测开始后，画布中会出现实时预测图层，已完成的分块随即显示；全部完成后替换为带颜色表的分类图。" />
      <Section title="模型与影像">
        <div className="dl-form-row"><label>模型</label><div className="dl-form-ctl">
          <Select value={(values.model as string) || undefined} placeholder="选择分类模型" onChange={(v) => setValues({ ...values, model: v })}
            options={models.map((m) => ({ value: m.path, label: `${m.name}　${m.classes.length} 类 · ${m.bands} 波段${m.metrics.test ? ` · 测试 OA ${pct(m.metrics.test.oa)}` : ''}` }))} />
          {model ? <ClassStrip classes={model.classes} /> : null}
        </div></div>
        <div className="dl-form-row"><label>影像</label><div className="dl-form-ctl">
          <Select value={(values.image as string) || undefined} placeholder="选择画布中的影像" onChange={(v) => setValues({ ...values, image: v })}
            options={[...rasters.map((r) => ({ value: r.path, label: `${r.name}（${r.bands} 波段）` })),
              ...(values.image && !layer ? [{ value: String(values.image), label: String(values.image) }] : [])]} />
          {desktop() ? <PathInput value="" mode="file" kind="image" placeholder="或浏览其他影像文件" onChange={(p) => p && setValues({ ...values, image: p })} /> : null}
          {compat ? <div className={'dl-compat ' + compat.level}>{compat.text}</div> : null}
        </div></div>
        <div className="dl-form-row"><label>预测范围</label><div className="dl-form-ctl">
          <Select value={range} onChange={setRange} options={[{ value: 'all', label: '全图' },
            ...ranges.map((l) => ({ value: String(l.id), label: `${l.name}（${l.kind === 'drawn' ? '手绘范围' : '矢量'}）` }))]} />
        </div></div>
      </Section>
      <Section title="输出">
        <ParamForm params={task.params} values={values} onChange={setValues} hide={['image', 'model', 'mask']} />
      </Section>
      <div className="dl-actions">
        <span className="caption">{model?.input_kind === 'spectrum' ? '一维光谱模型逐像元推理，不使用邻域。' : ''}</span>
        <Button className="cta" loading={busy} disabled={!values.model || !values.image || compat?.level === 'error'} onClick={submit}>开始预测</Button>
      </div>
    </div>
  )
}
