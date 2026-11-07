// 任务详情：进度、指标曲线、混淆矩阵与样本列表、预测结果、日志。

import { useEffect, useMemo, useState } from 'react'
import { App as AntApp, Button, Popconfirm, Segmented, Slider, Tooltip } from 'antd'
import { desktop } from '../desktop'
import { ConfusionMatrix, LineChart } from './Charts'
import type { Series } from './Charts'
import { STATE_LABEL, TASK_LABEL, baseName, dl, fmtDuration, fmtTime, pct } from './dlapi'
import type { ClassRow, EvalSample, JobDetail } from './dlapi'
import { Card, Chip, Stats } from './ui'
import type { Stat, Tone } from './ui'

const C = {
  train: 'var(--text-muted)',
  val: 'var(--accent)',
  f1: 'var(--success)',
  lr: 'var(--warning)',
}

const STATE_TONE: Record<string, Tone> = {
  running: 'accent', queued: 'accent', succeeded: 'ok', failed: 'error', cancelled: 'warn', interrupted: 'warn',
}

export interface MonitorHooks {
  onShowLayer: (jobId: string) => void
  onLowConfidence: (jobId: string, threshold: number) => void
  onLocate: (lonlat: [number, number]) => void
  onSelectSamples: (keys: string[]) => void
  onCheckSamples: (keys: string[]) => void
  onPredictWith: (modelPath: string) => void
  onUseBackbone: (modelPath: string) => void
  onOpenFeatures: (modelPath?: string) => void
  onResume: (jobId: string) => void
  onCancel: (jobId: string) => void
  onDelete: (jobId: string) => void
}

export default function JobMonitor({ job, hooks }: { job: JobDetail; hooks: MonitorHooks }) {
  const running = job.state === 'running' || job.state === 'queued'
  const isTrain = job.task === 'train_classifier'
  const isPredict = job.task === 'predict' || job.task === 'similarity'
  const isContrastive = job.task === 'train_contrastive'
  const pctDone = Math.round(job.progress.fraction * 1000) / 10
  const progressText = job.state === 'queued' ? '排队中' : job.progress.text || job.phase
  // 用时：运行中按现在算（详情约每秒刷新一次，跟着走），结束了按结束时间
  const end = running ? Date.now() / 1000 : job.finished
  const elapsed = job.started && end && job.state !== 'queued' ? end - job.started : null
  return (
    <div className="dl-monitor">
      <Card className="dl-monitor-head"
        title={job.title}
        chips={<Chip tone={STATE_TONE[job.state]}>{STATE_LABEL[job.state]}</Chip>}
        sub={<>{TASK_LABEL[job.task] ?? job.task} · 创建于 {fmtTime(job.created)}</>}
        aside={<div className="dl-head-actions">
          {running ? (
            <Button size="small" disabled={job.cancel_requested} onClick={() => hooks.onCancel(job.id)}>
              {job.cancel_requested ? '正在取消…' : '取消'}
            </Button>
          ) : null}
          {!running && job.state !== 'succeeded' ? (
            <Tooltip title={isTrain || isContrastive ? '从最近保存的轮次继续训练' : isPredict ? '仅计算未完成的分块' : '重新运行'}>
              <Button size="small" onClick={() => hooks.onResume(job.id)}>继续运行</Button>
            </Tooltip>
          ) : null}
          {desktop() ? <Button size="small" onClick={() => desktop()!.reveal_in_folder(job.dir)}>打开目录</Button> : null}
          {!running ? (
            <Popconfirm title="删除该任务？" description="任务目录中的模型包与预测结果将一并删除。"
              okText="删除" cancelText="取消" onConfirm={() => hooks.onDelete(job.id)}>
              <Button size="small" danger>删除</Button>
            </Popconfirm>
          ) : null}
        </div>}>
        <div className="dl-progress">
          <div className={'dl-progress-bar ' + job.state}><span style={{ width: `${pctDone}%` }} /></div>
          <div className="dl-progress-text">
            <span>{progressText}</span>
            {running && job.phase && job.phase !== progressText ? <span className="caption">{job.phase}</span> : null}
            <div className="spacer" />
            {elapsed !== null && elapsed >= 0 ? <span className="caption">{running ? '已用时' : '用时'} {fmtDuration(elapsed)}</span> : null}
            {running && job.progress.eta ? <span className="caption">剩余约 {fmtDuration(job.progress.eta)}</span> : null}
            <span className="mono">{pctDone}%</span>
          </div>
        </div>
        {job.estimate && isTrain ? (
          <div className="caption">
            测速结果：每轮约 {fmtDuration(job.estimate.epoch_seconds)}；整景预测每百万像元约 {fmtDuration(1e6 / (job.estimate.predict_pixels_per_s || 1))}。
          </div>
        ) : null}
        {job.state === 'failed' ? <div className="dl-error pre">{job.message}</div> : null}
      </Card>
      {isTrain ? <TrainView job={job} hooks={hooks} /> : null}
      {isContrastive ? <ContrastiveView job={job} hooks={hooks} /> : null}
      {job.task === 'embed' && job.state === 'succeeded' ? (
        <Card title="特征分析已完成" sub="散点图与疑似错标样本在「特征」页查看。"
          aside={<Button className="cta" size="small" onClick={() => hooks.onOpenFeatures()}>查看结果</Button>} />
      ) : null}
      {isPredict ? <PredictView job={job} hooks={hooks} /> : null}
      <Logs job={job} />
    </div>
  )
}

const fmtLoss = (v: number | undefined) =>
  v === undefined || !isFinite(v) ? '—' : v !== 0 && Math.abs(v) < 1e-3 ? v.toExponential(2) : v.toFixed(4)

/** 训练进行中的实时状态：这一轮跑到第几批、到目前为止的平均损失，以及刚结束那一轮的各项指标。
 *  曲线每轮结束才追加一个点，一轮很长时（大样本集、CPU）靠这里看出训练在动。 */
function LiveStatus({ job, contrastive }: { job: JobDetail; contrastive?: boolean }) {
  const b = job.batch
  const last = job.epochs[job.epochs.length - 1]
  if (job.state !== 'running' || (!b && !last)) return null
  const items: Stat[] = []
  if (b) {
    items.push({ label: `第 ${b.epoch} 轮已完成的批`, value: `${b.done} / ${b.total}`, tone: 'accent' })
    items.push({ label: '本轮平均损失', value: fmtLoss(b.loss), hint: '这一轮到目前为止各批损失的平均' })
  }
  if (last) {
    const tag = `（第 ${last.epoch} 轮）`
    items.push({ label: '训练损失' + tag, value: fmtLoss(last.train_loss) })
    items.push({ label: (contrastive ? '对比准确率' : '训练精度') + tag, value: pct(last.train_acc) })
    if (last.val_loss !== undefined) items.push({ label: '验证损失' + tag, value: fmtLoss(last.val_loss) })
    if (last.val_oa !== undefined) items.push({ label: '验证 OA' + tag, value: pct(last.val_oa) })
    if (last.knn_acc !== undefined) items.push({ label: 'kNN 精度' + tag, value: pct(last.knn_acc) })
    items.push({ label: '学习率' + tag, value: last.lr === 0 ? '0' : last.lr.toExponential(1) })
    items.push({ label: '每轮用时' + tag, value: fmtDuration(last.seconds) })
  }
  return (
    <Card title="实时状态" sub="每 2 秒更新；曲线在每轮结束时追加一个点。">
      {b ? <div className="dl-progress-bar running dl-batch-bar"><span style={{ width: `${(b.done / Math.max(1, b.total)) * 100}%` }} /></div> : null}
      <Stats items={items} />
    </Card>
  )
}

function TrainView({ job, hooks }: { job: JobDetail; hooks: MonitorHooks }) {
  const ep = job.epochs
  const hasVal = ep.some((e) => e.val_oa !== undefined)
  const best = [...ep].reverse().find((e) => e.best)
  const loss: Series[] = [
    { name: '训练损失', color: C.train, points: ep.map((e) => [e.epoch, e.train_loss]) },
    ...(hasVal ? [{ name: '验证损失', color: C.val, points: ep.map((e) => [e.epoch, e.val_loss ?? NaN] as [number, number]) }] : []),
  ]
  const acc: Series[] = [
    { name: '训练精度', color: C.train, points: ep.map((e) => [e.epoch, e.train_acc]), dashed: true },
    ...(hasVal ? [
      { name: '验证 OA', color: C.val, points: ep.map((e) => [e.epoch, e.val_oa ?? NaN] as [number, number]) },
      { name: '验证宏 F1', color: C.f1, points: ep.map((e) => [e.epoch, e.val_macro_f1 ?? NaN] as [number, number]) },
    ] : []),
  ]
  const lr: Series[] = [{ name: '学习率', color: C.lr, points: ep.map((e) => [e.epoch, e.lr]) }]
  const classRows: ClassRow[] = useMemo(() => {
    const ev = job.evaluations.test ?? job.evaluations.val ?? job.evaluations.train
    return ev?.classes ?? (ev?.per_class ?? []).map((c) => ({ label: c.label, name: String(c.label) }))
  }, [job.evaluations])
  const model = job.artifacts.find((a) => a.kind === 'model')
  const test = job.evaluations.test
  const stats: Stat[] = []
  if (best) {
    stats.push({ label: `验证 OA（第 ${best.epoch} 轮）`, value: pct(best.val_oa ?? best.train_acc) })
    stats.push({ label: '验证宏 F1', value: pct(best.val_macro_f1) })
  }
  if (test) {
    stats.push({ label: '测试 OA', value: pct(test.oa), hint: '训练结束后，用最佳一轮的模型在测试集上评估', tone: 'accent' })
    stats.push({ label: '测试 AA', value: pct(test.aa) })
    stats.push({ label: '测试宏 F1', value: pct(test.macro_f1) })
    stats.push({ label: '测试 Kappa', value: test.kappa.toFixed(3) })
  }
  return (
    <>
      <LiveStatus job={job} />
      {stats.length ? (
        <Card title="精度"
          aside={model && job.state === 'succeeded' ? <Button className="cta" size="small" onClick={() => hooks.onPredictWith(model.path)}>用此模型预测…</Button> : undefined}>
          <Stats items={stats} />
        </Card>
      ) : null}
      <Card title="训练曲线" sub={best ? `虚线标出第 ${best.epoch} 轮，即当前最佳模型` : undefined}>
        {ep.length ? (
          <div className="dl-charts">
            <LineChart title="损失" series={loss} mark={best?.epoch} />
            <LineChart title="精度" series={acc} yRange={[0, 1]} yFmt={(v) => `${Math.round(v * 100)}%`} mark={best?.epoch} />
            <LineChart title="学习率" series={lr} yFmt={(v) => (v === 0 ? '0' : v.toExponential(1))} height={120} />
          </div>
        ) : <div className="caption">第一轮结束后显示曲线。</div>}
      </Card>
      <Evaluations job={job} classes={classRows} hooks={hooks} />
    </>
  )
}

function ContrastiveView({ job, hooks }: { job: JobDetail; hooks: MonitorHooks }) {
  const ep = job.epochs
  const knn = ep.filter((e) => e.knn_acc !== undefined)
  const best = [...ep].reverse().find((e) => e.best)
  const model = job.artifacts.find((a) => a.kind === 'model')
  const stats: Stat[] = []
  if (knn.length) stats.push({ label: `kNN 精度（第 ${best?.epoch ?? '—'} 轮）`, value: pct(best?.knn_acc ?? Math.max(...knn.map((e) => e.knn_acc ?? 0))),
    hint: '用骨干网络提取的特征在监控样本集上做 kNN 分类的精度', tone: 'accent' })
  if (ep.length) stats.push({ label: '对比准确率（最近一轮）', value: pct(ep[ep.length - 1].train_acc), hint: '同一样本的两个增强视图被正确配对的比例' })
  return (
    <>
      <LiveStatus job={job} contrastive />
      {stats.length ? (
        <Card title="效果"
          aside={model && job.state === 'succeeded' ? <div className="dl-head-actions">
            <Button size="small" onClick={() => hooks.onOpenFeatures(model.path)}>特征分析</Button>
            <Button className="cta" size="small" onClick={() => hooks.onUseBackbone(model.path)}>用于分类训练…</Button>
          </div> : undefined}>
          <Stats items={stats} />
          {!knn.length ? <div className="caption">未指定监控样本集，未进行 kNN 评估。</div> : null}
        </Card>
      ) : null}
      <Card title="训练曲线">
        {ep.length ? (
          <div className="dl-charts">
            <LineChart title="对比损失" series={[{ name: '损失', color: C.train, points: ep.map((e) => [e.epoch, e.train_loss]) }]} mark={best?.epoch} />
            <LineChart title="精度" yRange={[0, 1]} yFmt={(v) => `${Math.round(v * 100)}%`} mark={best?.epoch}
              series={[{ name: '对比准确率', color: C.train, dashed: true, points: ep.map((e) => [e.epoch, e.train_acc]) },
                ...(knn.length ? [{ name: 'kNN 精度', color: C.val, points: knn.map((e) => [e.epoch, e.knn_acc!] as [number, number]) }] : [])]} />
          </div>
        ) : <div className="caption">{job.phase || '正在准备…'}</div>}
      </Card>
    </>
  )
}

function Evaluations({ job, classes, hooks }: { job: JobDetail; classes: ClassRow[]; hooks: MonitorHooks }) {
  const { message } = AntApp.useApp()
  const splits = (['test', 'val', 'train'] as const).filter((s) => job.evaluations[s])
  const [split, setSplit] = useState<string>(splits[0] ?? 'val')
  useEffect(() => { if (!job.evaluations[split] && splits[0]) setSplit(splits[0]) }, [job.evaluations]) // eslint-disable-line react-hooks/exhaustive-deps
  const [cellPick, setCellPick] = useState<[number, number] | null>(null)
  const [rows, setRows] = useState<{ total: number; rows: EvalSample[] } | null>(null)
  // 刚在画布上定位过的那一行，标出来，逐条核对时不丢位置
  const [located, setLocated] = useState<number | null>(null)
  useEffect(() => setLocated(null), [rows])
  const ev = job.evaluations[split]
  if (!ev) return null
  const name = (l: number) => classes.find((c) => c.label === l)?.name ?? String(l)
  const canList = split !== 'train'
  const load = async (truth?: number, pred?: number, errorsOnly = true) => {
    try {
      setRows(await dl.samples(job.id, { split, true_label: truth, pred_label: pred, errors_only: errorsOnly }))
    } catch (e) {
      message.error((e as Error).message)
    }
  }
  const pick = (i: number, j: number) => {
    setCellPick([i, j])
    if (canList) load(i, j, i !== j)
  }
  const keys = (rows?.rows ?? []).filter((r) => r.key && r.in_library).map((r) => r.key!)
  const diagonal = cellPick && cellPick[0] === cellPick[1]
  return (
    <Card title="混淆矩阵"
      sub={canList ? '行为真值，列为预测。单击单元格列出对应的样本块。' : '行为真值，列为预测。'}
      aside={<div className="dl-head-actions">
        {splits.length > 1 ? (
          <Segmented size="small" value={split} onChange={(v) => { setSplit(String(v)); setRows(null); setCellPick(null) }}
            options={splits.map((s) => ({ value: s, label: { test: '测试集', val: '验证集', train: '训练集' }[s] }))} />
        ) : null}
        {canList ? <Button size="small" onClick={() => { setCellPick(null); load() }}>列出全部错分</Button> : null}
      </div>}>
      <div className="dl-eval-body">
        <ConfusionMatrix matrix={ev.confusion} classes={classes} selected={cellPick} onPick={pick} />
        <table className="dl-table">
          <thead><tr><th>类别</th><th>精确率</th><th>召回率</th><th>F1</th><th>样本数</th></tr></thead>
          <tbody>
            {ev.per_class.map((c) => {
              const color = classes.find((x) => x.label === c.label)?.color
              return (
                <tr key={c.label} className={c.f1 < 0.7 && c.support ? 'weak' : ''}>
                  <td><i className="dl-swatch" style={{ background: color }} />{name(c.label)}</td>
                  <td className="mono">{pct(c.precision)}</td><td className="mono">{pct(c.recall)}</td>
                  <td className="mono">{pct(c.f1)}</td><td className="mono">{c.support}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {rows ? (
        <div className="dl-samples">
          <div className="dl-section-head">
            <b>{cellPick ? `${name(cellPick[0])} → ${name(cellPick[1])}` : '全部错分'}</b>
            <span className="caption">
              共 {rows.total} 块{rows.total > rows.rows.length ? `，显示前 ${rows.rows.length} 块` : ''}。
              {diagonal ? '按置信度从低到高排列。' : '按置信度从高到低排列，置信度高的错分样本更可能是标注错误。'}
            </span>
            <div className="spacer" />
            <Button size="small" disabled={!keys.length} onClick={() => hooks.onSelectSamples([...new Set(keys)])}>在画布上选中</Button>
            <Button size="small" disabled={!keys.length} onClick={() => hooks.onCheckSamples([...new Set(keys)])}>加入勾选</Button>
          </div>
          <div className="dl-sample-list">
            {rows.rows.map((r, i) => (
              <div key={i} className={'dl-sample' + (located === i ? ' on' : '')}
                onClick={() => { if (r.lonlat) { hooks.onLocate(r.lonlat); setLocated(i) } }}
                title={r.key ? `样本 ${r.key}` : '该样本集未记录样本来源'}>
                <span className="mono">#{r.index}</span>
                <span>{name(r.label)} → <b>{name(r.pred)}</b></span>
                <span className="mono">{pct(r.confidence, 0)}</span>
                <span className="caption">{r.key ? (r.in_library ? r.key : `${r.key}（已不在样本库中）`) : ''}</span>
                {r.lonlat ? <span className="dl-link">定位</span> : null}
              </div>
            ))}
            {!rows.rows.length ? <div className="caption dl-pad">没有符合条件的样本块。</div> : null}
          </div>
        </div>
      ) : null}
    </Card>
  )
}

function PredictView({ job, hooks }: { job: JobDetail; hooks: MonitorHooks }) {
  const [threshold, setThreshold] = useState(0.6)
  const init = job.predict_init
  const done = job.state === 'succeeded'
  const similar = job.task === 'similarity'
  const outs = job.artifacts
  const hasConf = outs.some((a) => a.kind === 'confidence') || (init?.outputs && 'confidence' in init.outputs)
  const labels: Record<string, string> = similar ? { class: '相似度图', confidence: '相似度数值' }
    : { class: '分类栅格', confidence: '置信度栅格', probabilities: '概率栅格', model: '模型包' }
  return (
    <>
      <Card title={similar ? '相似度图' : '分类结果'}
        sub={init ? `${init.cols} × ${init.rows} 像元，共 ${init.blocks} 块${job.blocks ? `；已完成 ${job.blocks.done} 块，用时 ${fmtDuration(job.blocks.elapsed)}` : ''}` : '正在准备…'}
        aside={<Button className="cta" size="small" disabled={!init} onClick={() => hooks.onShowLayer(job.id)}>
          {done ? '添加到画布' : '在画布中查看进度'}
        </Button>}>
        {init?.classes?.length ? (
          <div className="dl-legend-grid">
            {init.classes.map((c) => <span key={c.label}><i style={{ background: c.color }} />{c.name}</span>)}
          </div>
        ) : null}
        {outs.length ? (
          <div className="dl-files">
            {outs.map((a) => (
              <div key={a.path} className="dl-file">
                <span className="dl-file-kind">{labels[a.kind] ?? a.kind}</span>
                <span className="mono dl-file-path" title={a.path}>{baseName(a.path)}</span>
                {desktop() ? <button className="dl-link" onClick={() => desktop()!.reveal_in_folder(a.path)}>显示</button> : null}
              </div>
            ))}
          </div>
        ) : null}
      </Card>
      {done && hasConf && !similar ? (
        <Card title="疑难区" sub="将置信度低于阈值的像元合并为面，添加到画布，作为补充样本的参考范围。">
          <div className="dl-inline">
            <span className="caption">置信度低于</span>
            <Slider style={{ width: 180 }} min={0.3} max={0.99} step={0.01} value={threshold} onChange={setThreshold} />
            <span className="mono">{threshold.toFixed(2)}</span>
            <div className="spacer" />
            <Button size="small" onClick={() => hooks.onLowConfidence(job.id, threshold)}>生成疑难区</Button>
          </div>
        </Card>
      ) : null}
    </>
  )
}

function Logs({ job }: { job: JobDetail }) {
  const [open, setOpen] = useState(job.state === 'failed')
  const warnings = job.logs.filter((l) => l.level === 'warn').length
  return (
    <div className="dl-logs">
      <button className="dl-link" onClick={() => setOpen((v) => !v)}>
        {open ? '收起日志' : `运行日志（${job.logs.length} 条${warnings ? `，其中 ${warnings} 条提示` : ''}）`}
      </button>
      {open ? (
        <div className="dl-log-body">
          {job.logs.map((l, i) => (
            <div key={i} className={'dl-log ' + l.level}><span className="mono">{fmtTime(l.t).slice(-5)}</span> {l.text}</div>
          ))}
        </div>
      ) : null}
    </div>
  )
}
