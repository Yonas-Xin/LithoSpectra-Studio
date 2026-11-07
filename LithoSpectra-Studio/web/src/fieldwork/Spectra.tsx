// 点位上链接的光谱：列出、看曲线、从光谱库里挑一条链上、取消链接。
//
// 只链接光谱库里的条目。野外实测的谱先在「光谱库…」里导入（那边管仪器格式与
// 反射率量纲），再回到这里链上——一份谱只存一处。

import { useEffect, useState } from 'react'
import { App as AntApp, Button, Empty, Input, Modal, Segmented, Tooltip } from 'antd'

import { Icon } from '../theme'
import { fw } from './fwapi'
import type { Curve, FwEntry, FwPointDetail } from './fwapi'
import { SpectrumChart } from './Charts'
import type { ChartCurve } from './Charts'

const COLORS = ['#E08A63', '#7FD1C7', '#F0D264', '#C6A0F0', '#9FC3F0', '#F29BB8', '#B5D98A']

export function SpectraSection(props: { point: FwPointDetail; onChanged: () => void; onStatus: (t: string) => void }) {
  const { message } = AntApp.useApp()
  const [curves, setCurves] = useState<Record<string, Curve | string>>({})
  const [picking, setPicking] = useState(false)
  const links = props.point.spectra

  useEffect(() => {
    let alive = true
    links.forEach((l) => {
      if (curves[l.entry_id]) return
      fw.curve(l.entry_id)
        .then((c) => alive && setCurves((m) => ({ ...m, [l.entry_id]: c })))
        .catch((e) => alive && setCurves((m) => ({ ...m, [l.entry_id]: (e as Error).message })))
    })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [links.map((l) => l.entry_id).join()])

  const chart: ChartCurve[] = links
    .map((l, i) => ({ l, c: curves[l.entry_id], color: COLORS[i % COLORS.length] }))
    .filter((x) => x.c && typeof x.c !== 'string')
    .map(({ l, c, color }) => ({ id: l.uid, label: l.entry?.cn || l.entry?.name || l.label || '', x: (c as Curve).x, y: (c as Curve).y, color }))

  const link = async (e: FwEntry) => {
    try {
      await fw.linkSpectrum(props.point.uid, e.id)
      props.onChanged()
      props.onStatus(`已关联光谱「${e.cn || e.name}」`)
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  return (
    <div className="fw-section">
      <div className="fw-section-head">
        <span>光谱 {links.length ? <span className="mono muted">{links.length}</span> : null}</span>
        <Tooltip title="从光谱库选择光谱关联到该点位。实测光谱需先在「视图 → 光谱库」中导入">
          <button className="tb-btn" onClick={() => setPicking(true)}><Icon name="plus" size={14} /> 关联光谱</button>
        </Tooltip>
      </div>
      {links.length ? (
        <>
          {chart.length ? <SpectrumChart curves={chart} /> : null}
          <div className="fw-spec-list">
            {links.map((l, i) => {
              const c = curves[l.entry_id]
              return (
                <div key={l.uid} className="fw-spec-row">
                  <span className="fw-swatch" style={{ background: COLORS[i % COLORS.length] }} />
                  <span className="fw-row-name" title={l.entry?.class_cn || l.entry?.class}>
                    {l.entry ? <EntryLabel e={l.entry} /> : (l.label ?? l.entry_id)}
                    {!l.entry ? <span className="fw-warn">（光谱库中已不存在）</span> : null}
                    {typeof c === 'string' ? <span className="fw-warn">（{c}）</span> : null}
                  </span>
                  <span className="caption">{l.entry ? (l.entry.library === 'user' ? '我的光谱' : l.entry.source) : ''}</span>
                  <button className="icon-btn" title="取消关联（光谱库中的光谱不受影响）" onClick={async () => {
                    await fw.unlinkSpectrum(l.uid)
                    props.onChanged()
                  }}><span className="float-x">×</span></button>
                </div>
              )
            })}
          </div>
        </>
      ) : <div className="caption">可将光谱库中该点位的实测光谱关联到此处。</div>}
      <SpectrumPicker open={picking} linked={new Set(links.map((l) => l.entry_id))}
        onClose={() => setPicking(false)} onPick={link} />
    </div>
  )
}

function SpectrumPicker(props: { open: boolean; linked: Set<string>; onClose: () => void; onPick: (e: FwEntry) => void }) {
  const [q, setQ] = useState('')
  const [lib, setLib] = useState<'user' | ''>('user')
  const [hits, setHits] = useState<FwEntry[] | null>(null)
  const [preview, setPreview] = useState<Curve | null>(null)

  useEffect(() => {
    if (!props.open) return
    const t = setTimeout(() => {
      // 我的光谱库条目不多，空搜索也列出来；标准库得给个词
      if (!q.trim() && lib === '') { setHits([]); return }
      fw.searchSpectra(q.trim() || ' ', lib).then(setHits).catch(() => setHits([]))
    }, 200)
    return () => clearTimeout(t)
  }, [q, lib, props.open])

  return (
    <Modal open={props.open} title="关联光谱库中的光谱" width={720} onCancel={props.onClose} footer={null} destroyOnHidden>
      <div className="fw-inline" style={{ marginBottom: 8 }}>
        <Segmented size="small" value={lib} onChange={(v) => setLib(v as 'user' | '')}
          options={[{ value: 'user', label: '我的光谱' }, { value: '', label: '全部（含标准库）' }]} />
        <Input size="small" allowClear autoFocus placeholder="名称、样品号、矿物或来源" value={q}
          prefix={<Icon name="search" size={14} />} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="fw-picker">
        <div className="fw-picker-list">
          {hits === null ? <div className="caption">检索中…</div> : !hits.length ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={lib === 'user' ? '「我的光谱」中没有匹配项。实测光谱需先在光谱库中导入。' : '输入名称检索标准光谱库。'} />
          ) : hits.map((e) => (
            <div key={e.id} className={'fw-row' + (preview?.id === e.id ? ' sel' : '')}
              onClick={() => fw.curve(e.id).then(setPreview).catch(() => setPreview(null))}
              onDoubleClick={() => { if (!props.linked.has(e.id)) { props.onPick(e); props.onClose() } }}>
              <span className="fw-row-name" title={e.class_cn || e.class}><EntryLabel e={e} />{e.sample ? <span className="caption"> · {e.sample}</span> : null}</span>
              <span className="caption">{e.library === 'user' ? '我的' : e.source}</span>
            </div>
          ))}
        </div>
        <div className="fw-picker-preview">
          {preview ? (
            <>
              <div className="insp-title" style={{ fontSize: 13 }}>{preview.cn && preview.cn !== preview.name ? `${preview.cn}  ${preview.name}` : preview.name}</div>
              <div className="caption">{preview.class_cn || preview.class} · {preview.source}{preview.instrument ? ` · ${preview.instrument}` : ''}</div>
              <SpectrumChart curves={[{ id: preview.id, label: preview.cn || preview.name, x: preview.x, y: preview.y, color: COLORS[0] }]} />
              <div className="actions">
                <Button type="primary" size="small" disabled={props.linked.has(preview.id)}
                  onClick={() => { props.onPick(preview); props.onClose() }}>
                  {props.linked.has(preview.id) ? '已关联' : '关联到该点位'}
                </Button>
              </div>
            </>
          ) : <div className="hint">单击左侧条目查看曲线，双击直接关联。</div>}
        </div>
      </div>
    </Modal>
  )
}

/** 光谱名：中文名在前，英文名灰色跟在后面；没有中文名的只写英文 */
function EntryLabel({ e }: { e: { name: string; cn?: string } }) {
  if (!e.cn || e.cn === e.name) return <>{e.name}</>
  return <>{e.cn}<span className="caption"> {e.name}</span></>
}
