// 导入对话框：奥维收藏夹（一份先预览再导；多份列成一张表批量导）、照片（怎么挂到点位）。

import { useEffect, useMemo, useState } from 'react'
import { Alert, Checkbox, Input, InputNumber, Modal, Radio, Select, Table, Tag } from 'antd'

import { fmtTime } from './fwapi'
import type { FwPoint, FwStatus, OvobjPreview, OvobjPreviewError, PhotoImportMode } from './fwapi'

/** 「导入后把录音转成文字追加到备注」：有录音、装了识别引擎才出现；记住上次的选择 */
const NOTE_KEY = 'fw.ovobj.transcribeToNote'
function useToNote(): [boolean, (v: boolean) => void] {
  const [v, setV] = useState(() => {
    try { return localStorage.getItem(NOTE_KEY) === '1' } catch { return false }
  })
  const set = (x: boolean) => {
    setV(x)
    try { localStorage.setItem(NOTE_KEY, x ? '1' : '0') } catch { /* 存不了就只管这一次 */ }
  }
  return [v, set]
}

function ToNoteOption(props: { asr?: FwStatus['asr']; audio: number; value: boolean; onChange: (v: boolean) => void }) {
  if (!props.audio || !props.asr?.engine) return null
  return (
    <Checkbox checked={props.value} onChange={(e) => props.onChange(e.target.checked)} style={{ marginTop: 12 }}>
      导入后把 {props.audio} 段录音转成文字，追加到点位备注
      {props.asr.model ? '' : <span className="caption">（第一次使用需下载约 {Math.round(props.asr.model_size / 1e6)} MB 的识别模型）</span>}
    </Checkbox>
  )
}

/** 照片以外的附件，写成「录音 2、文件 1」 */
function extraText(p: Pick<OvobjPreview, 'audio' | 'video' | 'files'>): string {
  return [p.audio ? `录音 ${p.audio}` : '', p.video ? `视频 ${p.video}` : '', p.files ? `文件 ${p.files}` : '']
    .filter(Boolean).join('、')
}

export function OvobjImportDialog(props: {
  preview: OvobjPreview | null
  asr?: FwStatus['asr']
  onCancel: () => void
  onOk: (path: string, survey: string, toNote: boolean) => void
}) {
  const p = props.preview
  const [survey, setSurvey] = useState('')
  const [toNote, setToNote] = useToNote()
  useEffect(() => setSurvey(p?.survey ?? ''), [p])
  if (!p) return null
  return (
    <Modal open title="导入奥维收藏夹" okText="导入" cancelText="取消" width={480}
      onCancel={props.onCancel} onOk={() => props.onOk(p.path, survey.trim() || p.survey, toNote && p.audio > 0)}>
      <div className="dlg">
        <div className="dlg-subtitle mono">{p.name}</div>
        <div className="fw-stats">
          <div><span className="caption">点位</span><b className="mono">{p.placemarks}</b></div>
          <div><span className="caption">轨迹</span><b className="mono">{p.tracks}</b></div>
          <div><span className="caption">照片</span><b className="mono">{p.photos}</b></div>
          {p.audio ? <div><span className="caption">录音</span><b className="mono">{p.audio}</b></div> : null}
          {p.video ? <div><span className="caption">视频</span><b className="mono">{p.video}</b></div> : null}
          {p.files ? <div><span className="caption">其他文件</span><b className="mono">{p.files}</b></div> : null}
          <div><span className="caption">轨迹点</span><b className="mono">{p.track_points}</b></div>
        </div>
        {p.orphans ? (
          <div className="caption">其中 {p.orphans} 个附件没有挂在标签上，将放在「未关联点位的照片与附件」中。</div>
        ) : null}
        <ToNoteOption asr={props.asr} audio={p.audio} value={toNote} onChange={setToNote} />
        {p.start ? <div className="caption">时间 {fmtTime(p.start)} — {fmtTime(p.end)}</div> : null}
        <div className="form-row" style={{ marginTop: 12 }}>
          <span>批次名称</span>
          <Input size="small" value={survey} onChange={(e) => setSurvey(e.target.value)} placeholder={p.survey} />
        </div>
        {p.imported_before ? (
          <Alert type="info" showIcon style={{ marginTop: 12 }}
            message={`该文件已于 ${fmtTime(p.imported_before.time)} 导入过（${p.imported_before.summary}）`}
            description="再次导入不会产生重复：已有点位按文件中的名称、备注与位置更新，已有照片与轨迹跳过。" />
        ) : p.existing ? (
          <Alert type="info" showIcon style={{ marginTop: 12 }}
            message={`其中 ${p.existing} 个点位已在库中`} description="这些点位将按文件内容更新，不会重复。" />
        ) : null}
        {p.warnings.length ? <Alert type="warning" style={{ marginTop: 12 }} message={<div className="pre">{p.warnings.join('\n')}</div>} /> : null}
        <div className="hint" style={{ marginTop: 12 }}>
          坐标按 WGS84 读入，不做 GCJ-02 偏移。照片、录音与文档原样保存到样本库旁的 .media 文件夹，作为原始资料不做改动。
        </div>
      </div>
    </Modal>
  )
}

/** 同一天的只写一次日期：2026-08-06 15:06–15:16 */
function fmtRange(a?: string, b?: string): string {
  const x = fmtTime(a)
  const y = fmtTime(b)
  if (!y || x === y) return x
  return x.slice(0, 10) === y.slice(0, 10) ? `${x}–${y.slice(11)}` : `${x} — ${y}`
}

type BatchRow = (OvobjPreview | OvobjPreviewError) & { key: string }

/** 批量导入奥维收藏夹：一份文件一行，各自的批次名；导过的默认不勾，读不了的列出原因。
    按时间先后导，同一个标签出现在几份文件里时后导的更新先导的。 */
export function OvobjBatchDialog(props: {
  items: (OvobjPreview | OvobjPreviewError)[] | null
  asr?: FwStatus['asr']
  onCancel: () => void
  onOk: (items: { path: string; survey: string }[], toNote: boolean) => void
}) {
  const [toNote, setToNote] = useToNote()
  const rows: BatchRow[] = useMemo(() => (props.items ?? []).map((it) => ({ ...it, key: it.path })), [props.items])
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [names, setNames] = useState<Record<string, string>>({})
  const [merge, setMerge] = useState(false)
  const [mergeName, setMergeName] = useState('')
  useEffect(() => {
    const ok = rows.filter((r): r is OvobjPreview & { key: string } => !('error' in r))
    setPicked(new Set(ok.filter((r) => !r.imported_before).map((r) => r.path)))
    setNames(Object.fromEntries(ok.map((r) => [r.path, r.survey])))
    setMerge(false)
    setMergeName('')
  }, [rows])
  if (!props.items) return null
  const good = rows.filter((r): r is OvobjPreview & { key: string } => !('error' in r))
  const chosen = good.filter((r) => picked.has(r.path))
  const sum = (k: 'placemarks' | 'tracks' | 'photos' | 'audio' | 'video' | 'files') => chosen.reduce((n, r) => n + (r[k] ?? 0), 0)
  const before = good.filter((r) => r.imported_before).length
  const toggle = (path: string, on: boolean) => {
    const next = new Set(picked)
    if (on) next.add(path)
    else next.delete(path)
    setPicked(next)
  }
  const submit = () => props.onOk(chosen.map((r) => ({
    path: r.path, survey: (merge ? mergeName : names[r.path] ?? '').trim() || r.survey,
  })), toNote && sum('audio') > 0)
  const extras = { audio: sum('audio'), video: sum('video'), files: sum('files') }
  return (
    <Modal open title={`批量导入奥维收藏夹（${rows.length} 份）`} width={860} cancelText="取消"
      okText={chosen.length ? `导入 ${chosen.length} 份` : '导入'} okButtonProps={{ disabled: !chosen.length || (merge && !mergeName.trim()) }}
      onCancel={props.onCancel} onOk={submit}>
      <div className="dlg">
        <div className="fw-batch-head">
          <Checkbox checked={chosen.length === good.length && good.length > 0}
            indeterminate={chosen.length > 0 && chosen.length < good.length}
            onChange={(e) => setPicked(new Set(e.target.checked ? good.map((r) => r.path) : []))}>全选</Checkbox>
          <Checkbox checked={merge} onChange={(e) => setMerge(e.target.checked)}>全部归入同一批次</Checkbox>
          {merge ? <Input size="small" style={{ width: 200 }} placeholder="批次名称" value={mergeName} autoFocus
            onChange={(e) => setMergeName(e.target.value)} /> : null}
          <span className="spacer" />
          <span className="caption">
            已选 {sum('placemarks')} 个点位、{sum('tracks')} 条轨迹、{sum('photos')} 张照片{extraText(extras) ? `、${extraText(extras)}` : ''}
          </span>
        </div>
        <Table<BatchRow> size="small" pagination={false} scroll={{ y: 380 }} dataSource={rows}
          rowSelection={{
            selectedRowKeys: [...picked],
            getCheckboxProps: (r) => ({ disabled: 'error' in r }),
            onSelect: (r, on) => toggle(r.path, on),
            hideSelectAll: true,
          }}
          columns={[
            {
              title: '文件', key: 'name', render: (_, r) => (
                <div>
                  <div className="mono">{r.name}</div>
                  {'error' in r ? <div className="fw-batch-err">无法读取：{r.error}</div>
                    : r.start ? <div className="caption">{fmtRange(r.start, r.end)}</div> : null}
                </div>
              ),
            },
            {
              title: '批次名称', key: 'survey', width: 180, render: (_, r) => ('error' in r ? null : (
                <Input size="small" disabled={merge || !picked.has(r.path)} value={merge ? mergeName : names[r.path] ?? ''}
                  placeholder={r.survey} onChange={(e) => setNames({ ...names, [r.path]: e.target.value })} />
              )),
            },
            { title: '点位', key: 'pm', width: 56, align: 'right', render: (_, r) => ('error' in r ? '' : <span className="mono">{r.placemarks}</span>) },
            { title: '轨迹', key: 'tr', width: 56, align: 'right', render: (_, r) => ('error' in r ? '' : <span className="mono">{r.tracks}</span>) },
            { title: '照片', key: 'ph', width: 56, align: 'right', render: (_, r) => ('error' in r ? '' : <span className="mono">{r.photos}</span>) },
            { title: '其他附件', key: 'att', width: 110, render: (_, r) => ('error' in r ? '' : <span className="caption">{extraText(r) || '—'}</span>) },
            {
              title: '状态', key: 'st', width: 120, render: (_, r) => ('error' in r ? <Tag color="error">读取失败</Tag>
                : r.imported_before ? <Tag title={`${fmtTime(r.imported_before.time)} 导入：${r.imported_before.summary}`}>导入过</Tag>
                  : r.existing ? <Tag title="这些点位已在库中，将按文件内容更新">{r.existing} 个点位已在库中</Tag> : <Tag color="success">新文件</Tag>),
            },
          ]} />
        <ToNoteOption asr={props.asr} audio={sum('audio')} value={toNote} onChange={setToNote} />
        {before ? (
          <div className="caption" style={{ marginTop: 8 }}>
            {before} 份文件导入过，默认不勾选；勾上再导也不会产生重复，已有点位按文件内容更新。
          </div>
        ) : null}
        <div className="hint" style={{ marginTop: 8 }}>
          按时间先后依次导入，同一个标签出现在几份文件里时，后导入的更新先导入的。某一份导入失败不影响其余文件。
        </div>
      </div>
    </Modal>
  )
}

export function PhotoImportDialog(props: {
  req: { paths: string[]; pointUid: string | null } | null
  points: FwPoint[]
  onCancel: () => void
  onOk: (mode: PhotoImportMode, pointUid: string | null, maxDist: number) => void
}) {
  const r = props.req
  const [mode, setMode] = useState<PhotoImportMode>('nearest')
  const [target, setTarget] = useState<string | null>(null)
  const [dist, setDist] = useState(30)
  useEffect(() => {
    if (!r) return
    setTarget(r.pointUid)
    setMode(r.pointUid ? 'point' : 'nearest')
  }, [r])
  if (!r) return null
  return (
    <Modal open title={`导入 ${r.paths.length} 张照片`} okText="导入" cancelText="取消" width={500}
      okButtonProps={{ disabled: mode === 'point' && !target }}
      onCancel={props.onCancel} onOk={() => props.onOk(mode, mode === 'point' ? target : null, dist)}>
      <div className="dlg">
        <Radio.Group value={mode} onChange={(e) => setMode(e.target.value)} className="radios">
          <Radio value="point">全部关联到指定点位</Radio>
          {mode === 'point' ? (
            <div className="fw-indent">
              <Select size="small" showSearch optionFilterProp="label" style={{ width: '100%' }} value={target ?? undefined}
                placeholder="选择点位" onChange={setTarget}
                options={props.points.map((p) => ({ value: p.uid, label: `${p.name || '未命名'}${p.survey ? '（' + p.survey + '）' : ''}` }))} />
            </div>
          ) : null}
          <Radio value="nearest">按照片 GPS 关联到最近的点位</Radio>
          {mode === 'nearest' ? (
            <div className="fw-indent inline">
              距离不超过 <InputNumber size="small" min={1} max={5000} value={dist} onChange={(v) => setDist(v ?? 30)} addonAfter="m" style={{ width: 120 }} />
              ，超出距离的照片不关联
            </div>
          ) : null}
          <Radio value="new">为每张带 GPS 的照片新建点位</Radio>
          <Radio value="none">暂不关联，稍后在面板中拖到点位上</Radio>
        </Radio.Group>
        <div className="hint" style={{ marginTop: 12 }}>
          同时读取照片中的位置、拍摄时间与朝向。同一张照片已关联到同一点位时跳过。
        </div>
      </div>
    </Modal>
  )
}
