// 特征页：样本在特征空间中的分布与疑似错标样本，以及整景相似区域检索。

import { useEffect, useMemo, useRef, useState } from 'react'
import { App as AntApp, Button, Checkbox, Select, Slider } from 'antd'
import type { LayerInfo } from '../api'
import { STATE_LABEL, baseName, dl, fmtTime, pct } from './dlapi'
import type { ClassRow, Dataset, EmbeddingData, EmbeddingPoint, JobSummary, ModelInfo } from './dlapi'
import type { MonitorHooks } from './JobMonitor'
import { Card, Chip, EmptyCard, PageHead } from './ui'

export interface FeatureProps {
  jobs: JobSummary[]
  layers: LayerInfo[]
  hooks: MonitorHooks
  picking: boolean
  setPicking: (v: boolean) => void
  points: [number, number][]
  setPoints: (p: [number, number][]) => void
  onJobCreated: (job: JobSummary, autoShow: boolean) => void
  preset: { model?: string }
  dataVersion: number
}

const modelLabel = (m: ModelInfo) => `${m.task === 'backbone' ? '骨干网络' : '分类模型'} · ${m.name}`

export default function FeaturesPage(props: FeatureProps) {
  const [models, setModels] = useState<ModelInfo[]>([])
  const [datasets, setDatasets] = useState<Dataset[]>([])
  useEffect(() => {
    dl.models().then(setModels).catch(() => undefined)
    dl.datasets().then((d) => setDatasets(d.filter((x) => !x.missing))).catch(() => undefined)
  }, [props.dataVersion, props.jobs.length])
  return (
    <div className="dl-scroll">
      <PageHead title="特征"
        desc="用骨干网络提取样本与像元的特征：查看各类样本在特征空间中是否分得开、找出疑似标注错误的样本，或在整景影像中检索与参考点相似的区域。骨干网络可以是对比学习的结果，也可以取自分类模型。" />
      <EmbedSection {...props} models={models} datasets={datasets} />
      <SimilaritySection {...props} models={models} />
    </div>
  )
}

// =============================================================================
// 样本特征分析
// =============================================================================

function EmbedSection(props: FeatureProps & { models: ModelInfo[]; datasets: Dataset[] }) {
  const { message } = AntApp.useApp()
  const [model, setModel] = useState(props.preset.model ?? '')
  const [dataset, setDataset] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (props.preset.model) setModel(props.preset.model) }, [props.preset.model])
  useEffect(() => { if (!dataset && props.datasets[0]) setDataset(props.datasets[0].path) }, [props.datasets, dataset])
  const runs = props.jobs.filter((j) => j.task === 'embed')
  const [focus, setFocus] = useState<string | null>(null)
  const current = runs.find((j) => j.id === focus) ?? runs[0]
  const m = props.models.find((x) => x.path === model)
  const ds = props.datasets.find((d) => d.path === dataset)
  const mismatch = m && ds && (m.bands !== ds.bands || (m.input_kind !== 'spectrum' && m.patch_size !== ds.patch))
  const start = async () => {
    setBusy(true)
    try {
      const job = await dl.newJob('embed', { model, dataset }, `特征分析 · ${m?.name ?? ''} · ${ds?.name ?? ''}`)
      setFocus(job.id)
      props.onJobCreated(job, false)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card title="样本特征分析" sub="每个样本块显示为一个点，颜色表示其标签。近邻中其他类别占比高的样本块列为疑似错标。">
      <div className="dl-toolbar">
        <Select style={{ flex: 1, minWidth: 220 }} placeholder="选择模型或骨干网络" value={model || undefined} onChange={setModel}
          options={props.models.map((x) => ({ value: x.path, label: modelLabel(x) }))} />
        <Select style={{ flex: 1, minWidth: 200 }} placeholder="选择样本集" value={dataset || undefined} onChange={setDataset}
          options={props.datasets.map((d) => ({ value: d.path, label: `${d.name}（${d.bands} 波段 · 块大小 ${d.patch}）` }))} />
        <Button className="cta" loading={busy} disabled={!model || !dataset || !!mismatch} onClick={start}>开始分析</Button>
      </div>
      {mismatch ? <div className="dl-compat error">模型与样本集的波段数或块大小不一致。</div> : null}
      {runs.length > 1 ? (
        <div className="dl-toolbar">
          <span className="caption">分析记录</span>
          <Select size="small" style={{ flex: 1 }} value={current?.id} onChange={setFocus}
            options={runs.map((j) => ({ value: j.id, label: `${fmtTime(j.created)} · ${j.title} · ${STATE_LABEL[j.state]}` }))} />
        </div>
      ) : null}
      {current ? <EmbeddingView job={current} hooks={props.hooks} />
        : <EmptyCard title="尚未分析" desc="选择模型与样本集后开始分析。" />}
    </Card>
  )
}

function EmbeddingView({ job, hooks }: { job: JobSummary; hooks: MonitorHooks }) {
  const { message } = AntApp.useApp()
  const [data, setData] = useState<EmbeddingData | null>(null)
  const [threshold, setThreshold] = useState(0.5)
  const [onlySuspect, setOnlySuspect] = useState(false)
  const [hidden, setHidden] = useState<Set<number>>(new Set())
  const [hover, setHover] = useState<EmbeddingPoint | null>(null)
  const [located, setLocated] = useState<EmbeddingPoint | null>(null)
  useEffect(() => {
    setData(null)
    if (job.state !== 'succeeded') return
    dl.embedding(job.id).then(setData).catch((e) => message.error((e as Error).message))
  }, [job.id, job.state, message])
  if (job.state !== 'succeeded') {
    return <div className="caption dl-pad">{job.state === 'running' || job.state === 'queued' ? `正在计算${job.phase ? `：${job.phase}` : '…'}` : `${STATE_LABEL[job.state]}：${job.message}`}</div>
  }
  if (!data) return <div className="caption dl-pad">正在读取结果…</div>
  const classes = data.summary.classes
  const name = (l: number) => classes.find((c) => c.label === l)?.name ?? String(l)
  const suspects = data.points.filter((p) => p.s >= threshold).sort((a, b) => b.s - a.s)
  const keys = [...new Set(suspects.filter((p) => p.k).map((p) => p.k!))]
  return (
    <div className="dl-embed">
      <div className="dl-embed-main">
        <Scatter points={data.points} classes={classes} hidden={hidden} threshold={onlySuspect ? threshold : -1}
          onHover={setHover} onPick={(p) => { if (p.ll) { hooks.onLocate(p.ll); setLocated(p) } }} />
        <div className="dl-embed-side">
          <div className="caption">
            {data.summary.count} 个样本块；前两个主成分解释 {pct(data.summary.explained[0] + data.summary.explained[1], 0)} 的方差；近邻数 {data.summary.knn_k}。
          </div>
          <div className="dl-embed-legend">
            <div className="dl-embed-cls head"><span /><span>类别</span><span>样本块</span><span title="近邻中其他类别的平均占比">混杂度</span></div>
            {data.summary.per_class.map((c) => {
              const color = classes.find((x) => x.label === c.label)?.color
              const off = hidden.has(c.label)
              return (
                <div key={c.label} className={'dl-embed-cls' + (off ? ' off' : '')} title={off ? '单击显示' : '单击隐藏'}
                  onClick={() => { const s = new Set(hidden); if (off) s.delete(c.label); else s.add(c.label); setHidden(s) }}>
                  <i style={{ background: color }} /><span>{c.name}</span>
                  <span className="mono caption">{c.count}</span>
                  <span className={'mono ' + (c.suspicion > 0.2 ? 'warn' : 'caption')}>{pct(c.suspicion, 0)}</span>
                </div>
              )
            })}
          </div>
          <div className="dl-hover-info">
            {hover ? <>{name(hover.l)}{hover.m !== hover.l ? <>，近邻多为 <b>{name(hover.m)}</b></> : null}；疑似度 {pct(hover.s, 0)}；{hover.t ? '测试集' : '训练集'} #{hover.i}{hover.k ? `；${hover.k}` : ''}</>
              : <span className="caption">悬停查看样本，单击在画布上定位。单击类别可隐藏或显示。</span>}
          </div>
        </div>
      </div>
      <div className="dl-section-head">
        <b>疑似错标</b>
        <span className="caption">近邻中其他类别占比不低于</span>
        <Slider style={{ width: 120 }} min={0.3} max={1} step={0.05} value={threshold} onChange={setThreshold} />
        <span className="mono">{pct(threshold, 0)}</span>
        <Checkbox checked={onlySuspect} onChange={(e) => setOnlySuspect(e.target.checked)}>散点图仅显示这些样本</Checkbox>
        <div className="spacer" />
        <Chip tone={suspects.length ? 'warn' : 'neutral'}>{suspects.length} 块 · {keys.length} 条样本</Chip>
        <Button size="small" disabled={!keys.length} onClick={() => hooks.onSelectSamples(keys)}>在画布上选中</Button>
        <Button size="small" disabled={!keys.length} onClick={() => hooks.onCheckSamples(keys)}>加入勾选</Button>
      </div>
      <div className="dl-sample-list">
        {suspects.slice(0, 300).map((p, i) => (
          <div key={i} className={'dl-sample' + (located === p ? ' on' : '')} onClick={() => { if (p.ll) { hooks.onLocate(p.ll); setLocated(p) } }}>
            <span className="mono">#{p.i}</span>
            <span>{name(p.l)}，近邻多为 <b>{name(p.m)}</b></span>
            <span className="mono">{pct(p.s, 0)}</span>
            <span className="caption">{p.k ?? ''}</span>
            {p.ll ? <span className="dl-link">定位</span> : null}
          </div>
        ))}
        {!suspects.length ? <div className="caption dl-pad">没有达到阈值的样本块。</div> : null}
      </div>
    </div>
  )
}

function Scatter(props: {
  points: EmbeddingPoint[]; classes: ClassRow[]; hidden: Set<number>; threshold: number
  onHover: (p: EmbeddingPoint | null) => void; onPick: (p: EmbeddingPoint) => void
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const W = 420
  const H = 340
  const pad = 12
  const color = useMemo(() => Object.fromEntries(props.classes.map((c) => [c.label, c.color || '#888'])), [props.classes])
  const shown = useMemo(() => props.points.filter((p) => !props.hidden.has(p.l) && p.s >= props.threshold),
    [props.points, props.hidden, props.threshold])
  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const dpr = window.devicePixelRatio || 1
    cv.width = W * dpr
    cv.height = H * dpr
    const ctx = cv.getContext('2d')!
    ctx.scale(dpr, dpr)
    ctx.clearRect(0, 0, W, H)
    const r = shown.length > 5000 ? 1.4 : 2.4
    // 疑似度低的先画，高的压在上面
    for (const p of [...shown].sort((a, b) => a.s - b.s)) {
      ctx.globalAlpha = 0.35 + 0.65 * p.s
      ctx.fillStyle = color[p.l] ?? '#888'
      ctx.beginPath()
      ctx.arc(pad + p.x * (W - 2 * pad), H - pad - p.y * (H - 2 * pad), p.s >= 0.5 ? r + 1 : r, 0, Math.PI * 2)
      ctx.fill()
    }
  }, [shown, color])
  const nearest = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const x = ((e.clientX - box.left) / box.width) * W
    const y = ((e.clientY - box.top) / box.height) * H
    let best: EmbeddingPoint | null = null
    let bd = 64
    for (const p of shown) {
      const dx = pad + p.x * (W - 2 * pad) - x
      const dy = H - pad - p.y * (H - 2 * pad) - y
      const d = dx * dx + dy * dy
      if (d < bd) { bd = d; best = p }
    }
    return best
  }
  return (
    <canvas ref={ref} className="dl-scatter" style={{ width: W, height: H }}
      onMouseMove={(e) => props.onHover(nearest(e))} onMouseLeave={() => props.onHover(null)}
      onClick={(e) => { const p = nearest(e); if (p) props.onPick(p) }} />
  )
}

// =============================================================================
// 相似区域检索
// =============================================================================

function SimilaritySection(props: FeatureProps & { models: ModelInfo[] }) {
  const { message } = AntApp.useApp()
  const [model, setModel] = useState(props.preset.model ?? '')
  const [image, setImage] = useState('')
  const [rasters, setRasters] = useState<{ id: number; name: string; path: string; bands: number }[]>([])
  const [busy, setBusy] = useState(false)
  useEffect(() => { dl.rasters().then((r) => { setRasters(r); setImage((v) => v || r[0]?.path || '') }).catch(() => undefined) }, [props.layers])
  useEffect(() => { if (props.preset.model) setModel(props.preset.model) }, [props.preset.model])
  useEffect(() => () => props.setPicking(false), []) // eslint-disable-line react-hooks/exhaustive-deps
  const m = props.models.find((x) => x.path === model)
  const r = rasters.find((x) => x.path === image)
  const mismatch = m && r && m.bands !== r.bands
  const runs = props.jobs.filter((j) => j.task === 'similarity').slice(0, 5)
  const start = async () => {
    setBusy(true)
    try {
      const query = props.points.map(([lon, lat]) => `${lon.toFixed(7)},${lat.toFixed(7)}`).join(';')
      const job = await dl.newJob('similarity', { model, image, query }, `相似检索 · ${baseName(image)} · ${props.points.length} 个参考点`)
      props.setPicking(false)
      props.onJobCreated(job, true)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card title="相似区域检索" sub="在画布上选取一个或多个参考点（如已知的蚀变带），计算整景各像元与参考点的特征相似度。">
      <div className="dl-toolbar">
        <Select style={{ flex: 1, minWidth: 220 }} placeholder="选择模型或骨干网络" value={model || undefined} onChange={setModel}
          options={props.models.map((x) => ({ value: x.path, label: modelLabel(x) }))} />
        <Select style={{ flex: 1, minWidth: 200 }} placeholder="选择画布中的影像" value={image || undefined} onChange={setImage}
          options={rasters.map((x) => ({ value: x.path, label: `${x.name}（${x.bands} 波段）` }))} />
      </div>
      {mismatch ? <div className="dl-compat error">模型与影像的波段数不一致。</div> : null}
      <div className="dl-toolbar">
        <Button onClick={() => props.setPicking(true)}>在画布上选取参考点</Button>
        <div className="dl-points">
          {props.points.length ? props.points.map(([lon, lat], i) => (
            <span key={i} className="dl-point">
              {lon.toFixed(5)}, {lat.toFixed(5)}
              <button title="移除" onClick={() => props.setPoints(props.points.filter((_, j) => j !== i))}>×</button>
            </span>
          )) : <span className="caption">尚未选取参考点</span>}
        </div>
        {props.points.length ? <button className="dl-link" onClick={() => props.setPoints([])}>清空</button> : null}
        <Button className="cta" loading={busy} disabled={!model || !image || !props.points.length || !!mismatch} onClick={start}>开始检索</Button>
      </div>
      {runs.length ? (
        <div className="dl-files">
          {runs.map((j) => (
            <div key={j.id} className="dl-file">
              <span className={'dl-dot-state ' + j.state} title={STATE_LABEL[j.state]} />
              <span className="dl-file-path">{j.title}</span>
              <span className="caption">{fmtTime(j.created)}</span>
              {j.state === 'succeeded' || j.state === 'running' ? <button className="dl-link" onClick={() => props.hooks.onShowLayer(j.id)}>添加到画布</button> : null}
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  )
}
