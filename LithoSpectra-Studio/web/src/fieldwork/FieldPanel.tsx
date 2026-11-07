// 野外记录面板：左边按批次列出点位与轨迹，右边是选中那一项的详情。
//
// 浮动窗口（与曲线窗口同一种），不占样本树与检查器的位置：样本与野外记录是两套
// 东西，各看各的。

import { useEffect, useMemo, useRef, useState } from 'react'
import { App as AntApp, AutoComplete, Button, Dropdown, Empty, Input, InputNumber, Select, Tooltip } from 'antd'
import type { MenuProps } from 'antd'

import type { SampleClass, TaskInfo } from '../api'
import { desktop } from '../desktop'
import Float from '../Float'
import { Icon } from '../theme'
import { fmtLen, fmtTime, fw, photoUrl } from './fwapi'
import type { ExportRequest } from './ExportDialog'
import type { FwOverview, FwPhoto, FwPoint, FwPointDetail, FwStatus, FwTrack } from './fwapi'
import { SpectraSection } from './Spectra'
import { ElevationProfile } from './Charts'
import { ATTACH_MIME, AttachmentList } from './Attachments'
import type { FwAttachment } from './fwapi'
import { REVEAL } from '../platform'

export type Selection = { kind: 'point' | 'track' | 'survey' | 'loose'; uid: string } | null

interface Props {
  overview: FwOverview | null
  status: FwStatus | null
  points: FwPoint[]
  tracks: FwTrack[]
  surveyColors: Record<string, string>
  selection: Selection
  detail: FwPointDetail | null
  visible: boolean
  classes: SampleClass[]
  onSelect: (s: Selection) => void
  onClose: () => void
  onToggleVisible: () => void
  onZoom: (what: 'all' | Selection) => void
  onImport: (what: 'ovobj' | 'ovobj-folder' | 'photos' | 'folder' | 'vector') => void
  /** 给点位添加附件（录音、文档等，从计算机上选） */
  onAddAttachments: (pointUid: string) => void
  /** 录音转文字（后台任务）；append 时追加到所属点位的备注 */
  onTranscribe: (req: { uids?: string[]; point_uid?: string; surveys?: string[]; append?: boolean; redo?: boolean }) => Promise<void>
  onReload: () => Promise<void>
  onRefreshPoint: (uid: string) => Promise<FwPointDetail>
  onOpenPhotos: (photos: FwPhoto[], index: number) => void
  onImportPhotosTo: (pointUid: string) => void
  onDropPhotos: (paths: string[], pointUid: string | null) => void
  onTask: <T>(start: () => Promise<TaskInfo>, failTitle: string) => Promise<T | null>
  onStatus: (text: string) => void
  onSamplesChanged: () => void
  onExport: (req: ExportRequest) => void
  /** 鼠标在轨迹高程剖面上的位置，画布上标出来 */
  onTrackHover?: (lonlat: [number, number] | null) => void
  /** 正在画布上拖动调整位置的点位（及移动前的位置） */
  moving?: { uid: string; lon: number; lat: number } | null
  onMove?: (p: FwPointDetail | null) => void
  onRestoreMove?: () => void
}

const PHOTO_MIME = 'application/x-fw-photo'
/** 拖着的是照片或附件：列表里的点位接得住 */
const dragsMedia = (e: React.DragEvent) => e.dataTransfer.types.includes(PHOTO_MIME) || e.dataTransfer.types.includes(ATTACH_MIME)
const rowKey = (kind: string, uid: string) => kind + ':' + uid

export default function FieldPanel(props: Props) {
  const { message, modal } = AntApp.useApp()
  const [query, setQuery] = useState('')
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase()
    const hit = (s: string | null | undefined) => !q || (s ?? '').toLowerCase().includes(q)
    return (props.overview?.surveys ?? []).map((s) => ({
      survey: s,
      tracks: props.tracks.filter((t) => (t.survey ?? '') === s.survey && hit(t.name)),
      points: props.points
        .filter((p) => (p.survey ?? '') === s.survey && (hit(p.name) || hit(p.note) || hit(p.category)))
        .sort((a, b) => (a.created ?? '').localeCompare(b.created ?? '')),
    })).filter((g) => !q || g.points.length || g.tracks.length)
  }, [props.overview, props.points, props.tracks, query])

  const sel = props.selection
  const isSel = (kind: string, uid: string) => sel?.kind === kind && sel.uid === uid

  // 选中项要在列表里露出来，尤其是在画布上点中的点位：展开所在批次，检索词挡住了它就清掉，
  // 再滚到那一行。等列表按新状态排好后才滚（见下面每次渲染后都跑的那个 effect）
  const scrollRef = useRef<HTMLDivElement>(null)
  const reveal = useRef<string | null>(null)
  const selItem = sel?.kind === 'point' ? props.points.find((p) => p.uid === sel.uid)
    : sel?.kind === 'track' ? props.tracks.find((t) => t.uid === sel.uid) : undefined
  useEffect(() => {
    if (!sel) return
    if (sel.kind === 'point' || sel.kind === 'track') {
      if (!selItem) return
      const survey = selItem.survey ?? ''
      setCollapsed((c) => {
        if (!c.has(survey)) return c
        const next = new Set(c)
        next.delete(survey)
        return next
      })
      const q = query.trim().toLowerCase()
      const hit = (x: string | null | undefined) => (x ?? '').toLowerCase().includes(q)
      const p = selItem as FwPoint
      if (q && !(hit(selItem.name) || (sel.kind === 'point' && (hit(p.note) || hit(p.category))))) setQuery('')
    }
    reveal.current = rowKey(sel.kind, sel.uid)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel?.kind, sel?.uid, !!selItem])
  useEffect(() => {
    const k = reveal.current
    if (!k) return
    const el = [...(scrollRef.current?.querySelectorAll<HTMLElement>('[data-row]') ?? [])].find((e) => e.dataset.row === k)
    if (!el || !scrollRef.current) return
    reveal.current = null
    const box = scrollRef.current.getBoundingClientRect()
    // 已经看得见就不动（在列表里单击、方向键移动）；看不见（画布上点中的）滚到中间
    const r = el.getBoundingClientRect()
    if (r.top < box.top || r.bottom > box.bottom) el.scrollIntoView({ block: 'center' })
  })

  const surveyMenu: MenuProps['items'] = [
    { key: 'zoom', label: '缩放到该批次' },
    { key: 'rename', label: '重命名批次…' },
    { type: 'divider' },
    { key: 'delete', label: '删除批次…', danger: true },
  ]
  const onSurveyMenu = async (key: string, name: string) => {
    if (key === 'zoom') return props.onZoom({ kind: 'survey', uid: name })
    if (key === 'delete') {
      const label = name || '未分批'
      const pts = props.points.filter((p) => (p.survey ?? '') === name)
      const nTracks = props.tracks.filter((t) => (t.survey ?? '') === name).length
      const nPhotos = pts.reduce((n, p) => n + (p.n_photos ?? 0), 0)
      const nAtts = pts.reduce((n, p) => n + (p.n_attachments ?? 0), 0)
      const media = nAtts ? (nPhotos ? `${nPhotos} 张照片与 ${nAtts} 个附件` : `${nAtts} 个附件`) : `${nPhotos} 张照片`
      const run = async (keepPhotos: boolean) => {
        try {
          const r = await fw.deleteSurvey(name, keepPhotos)
          if (isSel('survey', name) || (sel && pts.some((p) => p.uid === sel.uid))) props.onSelect(null)
          await props.onReload()
          props.onStatus(`已删除批次「${label}」：${r.points} 个点位、${r.tracks} 条轨迹` +
            (nPhotos || nAtts ? (keepPhotos ? `，${media}已移至「未关联点位的照片与附件」` : `、${media}`) : ''))
        } catch (e) {
          message.error((e as Error).message)
        }
      }
      const dlg = modal.confirm({
        title: `删除批次「${label}」？`,
        content: `将删除其中的 ${pts.length} 个点位、${nTracks} 条轨迹${nPhotos || nAtts ? `，以及点位上的 ${media}` : ''}。删除后无法撤销；媒体文件夹中的原文件保留。`,
        okText: nPhotos || nAtts ? (nAtts ? '全部删除' : '删除批次和照片') : '删除批次', okButtonProps: { danger: true }, cancelText: '取消',
        footer: (_, { OkBtn, CancelBtn }) => (
          <>
            <CancelBtn />
            {nPhotos || nAtts ? <Button onClick={() => { dlg.destroy(); run(true) }}>删除批次，保留{nAtts ? '照片与附件' : '照片'}</Button> : null}
            <OkBtn />
          </>
        ),
        onOk: () => run(false),
      })
      return
    }
    if (key === 'rename') {
      let value = name
      const apply = async () => {
        if (!value.trim() || value === name) return
        try {
          await fw.renameSurvey(name, value.trim())
          await props.onReload()
          props.onSelect({ kind: 'survey', uid: value.trim() })
        } catch (e) {
          message.error((e as Error).message)
        }
      }
      const m = modal.confirm({
        title: '重命名批次', icon: null,
        content: <Input defaultValue={name} onChange={(e) => { value = e.target.value }} autoFocus
          onPressEnter={(e) => { if (e.keyCode === 229) return; m.destroy(); apply() }} />,
        onOk: apply,
      })
    }
  }

  const dropOnPoint = async (e: React.DragEvent, uid: string | null) => {
    const photo = e.dataTransfer.getData(PHOTO_MIME)
    const att = e.dataTransfer.getData(ATTACH_MIME)
    if (!photo && !att) return
    e.preventDefault()
    try {
      if (photo) await fw.movePhoto(photo, uid)
      else await fw.moveAttachment(att, uid)
      await props.onReload()
      if (props.detail) await props.onRefreshPoint(props.detail.uid)
      const what = photo ? '照片' : '附件'
      props.onStatus(uid ? `${what}已关联到该点位。` : `已取消${what}与点位的关联。`)
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const importMenu: MenuProps['items'] = [
    { key: 'ovobj', label: '奥维收藏夹（.ovobj，可多选）…' },
    { key: 'ovobj-folder', label: '奥维收藏夹文件夹…' },
    { key: 'photos', label: '照片…' },
    { key: 'folder', label: '照片文件夹…' },
    { key: 'vector', label: '轨迹与点位（GPX / KML）…' },
  ]

  // 列表里能选的行，按显示顺序：上下方向键在它们之间移动
  const rows: Selection[] = []
  for (const { survey: s, tracks, points } of groups) {
    rows.push({ kind: 'survey', uid: s.survey })
    if (collapsed.has(s.survey) && !query) continue
    tracks.forEach((t) => rows.push({ kind: 'track', uid: t.uid }))
    points.forEach((p) => rows.push({ kind: 'point', uid: p.uid }))
  }
  if (props.overview?.stats) rows.push({ kind: 'loose', uid: '' })
  const onListKey = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).closest('input, textarea')) return
    const i = sel ? rows.findIndex((r) => r!.kind === sel.kind && r!.uid === sel.uid) : -1
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const next = rows[e.key === 'ArrowDown' ? Math.min(rows.length - 1, i + 1) : Math.max(0, i - 1)]
      if (next && i !== rows.indexOf(next)) props.onSelect(next)
    } else if (e.key === 'Enter' && sel && sel.kind !== 'loose') {
      e.preventDefault()
      props.onZoom(sel)
    } else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && sel?.kind === 'survey') {
      e.preventDefault()
      const next = new Set(collapsed)
      if (e.key === 'ArrowLeft') next.add(sel.uid)
      else next.delete(sel.uid)
      setCollapsed(next)
    }
  }

  const stats = props.overview?.stats
  return (
    <Float
      title="野外记录"
      initial={{ x: Math.max(16, window.innerWidth - 900), y: 90, w: 820, h: Math.min(680, window.innerHeight - 140) }}
      onClose={props.onClose}
      extra={
        <span className="fw-head-extra">
          <Dropdown trigger={['click']} menu={{ items: importMenu, onClick: ({ key }) => props.onImport(key as 'ovobj') }}>
            <button className="tb-btn"><Icon name="upload" size={14} /> 导入</button>
          </Dropdown>
          <Tooltip title={props.visible ? '在画布上隐藏野外记录' : '在画布上显示野外记录'}>
            <button className={'tb-btn' + (props.visible ? ' on' : '')} onClick={props.onToggleVisible}>
              <Icon name="eye" size={14} /> 画布
            </button>
          </Tooltip>
          <Tooltip title="缩放到全部野外记录">
            <button className="tb-btn" onClick={() => props.onZoom('all')}><Icon name="maximize" size={14} /></button>
          </Tooltip>
        </span>
      }
    >
      <div className="fw-panel">
        <div className="fw-list">
          <div className="fw-search">
            <Input size="small" allowClear placeholder="搜索点位名称、备注或类型" value={query}
              prefix={<Icon name="search" size={14} />} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <div className="fw-scroll" ref={scrollRef} tabIndex={0} onKeyDown={onListKey}>
            {!groups.length ? (
              <Empty className="fw-empty" image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={query ? '没有匹配的记录' : '暂无野外记录。可导入奥维收藏夹、照片或 GPX，也可将文件拖入窗口。'} />
            ) : null}
            {groups.map(({ survey: s, tracks, points }) => {
              const shut = collapsed.has(s.survey) && !query
              return (
                <div key={s.survey} className="fw-group">
                  <Dropdown trigger={['contextMenu']} menu={{ items: surveyMenu, onClick: ({ key }) => onSurveyMenu(key, s.survey) }}>
                    <div className={'fw-group-head' + (isSel('survey', s.survey) ? ' sel' : '')}
                      data-row={rowKey('survey', s.survey)}
                      onClick={() => props.onSelect({ kind: 'survey', uid: s.survey })}
                      onDoubleClick={(e) => { if (!(e.target as HTMLElement).closest('.fw-caret')) props.onZoom({ kind: 'survey', uid: s.survey }) }}>
                      <span className="fw-caret" onClick={(e) => {
                        e.stopPropagation()
                        const next = new Set(collapsed)
                        if (next.has(s.survey)) next.delete(s.survey)
                        else next.add(s.survey)
                        setCollapsed(next)
                      }}>{shut ? '▸' : '▾'}</span>
                      <span className="fw-swatch" style={{ background: props.surveyColors[s.survey] }} />
                      <span className="fw-group-name">{s.survey || '（未分批）'}</span>
                      <span className="fw-count mono">{s.points}</span>
                    </div>
                  </Dropdown>
                  {shut ? null : (
                    <>
                      {tracks.map((t) => (
                        <div key={t.uid} className={'fw-row' + (isSel('track', t.uid) ? ' sel' : '')} data-row={rowKey('track', t.uid)}
                          onClick={() => props.onSelect({ kind: 'track', uid: t.uid })}
                          onDoubleClick={() => props.onZoom({ kind: 'track', uid: t.uid })}>
                          <Icon name="spline" size={14} />
                          <span className="fw-row-name">{t.name || '轨迹'}</span>
                          <span className="fw-count mono">{fmtLen(t.length_m)}</span>
                        </div>
                      ))}
                      {points.map((p) => (
                        <div key={p.uid} className={'fw-row' + (isSel('point', p.uid) ? ' sel' : '')} data-row={rowKey('point', p.uid)}
                          onClick={() => props.onSelect({ kind: 'point', uid: p.uid })}
                          onDoubleClick={() => props.onZoom({ kind: 'point', uid: p.uid })}
                          onDragOver={(e) => { if (dragsMedia(e)) e.preventDefault() }}
                          onDrop={(e) => dropOnPoint(e, p.uid)}>
                          <span className="fw-diamond" />
                          <span className="fw-row-name">{p.name || '未命名点位'}</span>
                          {p.n_spectra ? <span className="fw-badge" title="关联的光谱">谱 {p.n_spectra}</span> : null}
                          {p.n_attachments ? <span className="fw-badge" title="录音、文档等附件">附 {p.n_attachments}</span> : null}
                          {p.n_photos ? <span className="fw-count mono" title="照片">{p.n_photos}</span> : null}
                        </div>
                      ))}
                    </>
                  )}
                </div>
              )
            })}
            {stats ? (
              <div className={'fw-row fw-loose' + (isSel('loose', '') ? ' sel' : '')} data-row={rowKey('loose', '')}
                onClick={() => props.onSelect({ kind: 'loose', uid: '' })}
                onDragOver={(e) => { if (dragsMedia(e)) e.preventDefault() }}
                onDrop={(e) => dropOnPoint(e, null)}>
                <Icon name="image" size={14} />
                <span className="fw-row-name">未关联点位的照片与附件</span>
              </div>
            ) : null}
          </div>
          <div className="fw-foot caption">
            {stats ? `${stats.fw_point} 个点位 · ${stats.fw_track} 条轨迹 · ${stats.fw_photo} 张照片${stats.fw_attachment ? ` · ${stats.fw_attachment} 个附件` : ''}` : '未打开样本库'}
            {props.overview?.library ? (
              <Tooltip placement="topLeft" title={<div className="pre">{`野外记录存在当前样本库中：\n${props.overview.library}\n照片：\n${props.overview.media_dir}\n单击${REVEAL}`}</div>}>
                <div className="fw-foot-path" onClick={() => desktop()?.reveal_in_folder(props.overview!.library)}>
                  存储于 {props.overview.library.split(/[\\/]/).pop()}
                </div>
              </Tooltip>
            ) : null}
          </div>
        </div>
        <div className="fw-detail">
          {sel?.kind === 'point' && props.detail && props.detail.uid === sel.uid ? (
            <PointDetail key={props.detail.uid} {...props} point={props.detail} />
          ) : sel?.kind === 'track' ? (
            <TrackDetail key={sel.uid} track={props.tracks.find((t) => t.uid === sel.uid) ?? null} {...props} />
          ) : sel?.kind === 'survey' ? (
            <SurveyDetail survey={sel.uid} {...props} />
          ) : sel?.kind === 'loose' ? (
            <LoosePhotos {...props} />
          ) : (
            <div className="fw-placeholder hint">
              在左侧列表或画布上选择野外点位，查看其照片、属性与关联的光谱。<br />
              将照片或附件拖到左侧列表中的点位上，可更改其所属点位。
            </div>
          )}
        </div>
      </div>
    </Float>
  )
}

// =============================================================================
// 点位
// =============================================================================

/** 失焦才保存的输入框：记下还没提交的改动，组件卸载（换点位、关面板、走菜单）时补交。

    只靠 onBlur 不够：用原生菜单或快捷键关掉面板时输入框不会失焦，刚写的内容就丢了。 */
function usePending(flush: (changes: Record<string, unknown>) => void) {
  const pending = useRef<Record<string, unknown>>({})
  const flushRef = useRef(flush)
  flushRef.current = flush
  useEffect(() => () => {
    const left = pending.current
    if (Object.keys(left).length) flushRef.current(left)
  }, [])
  return {
    mark: (key: string, value: unknown) => { pending.current[key] = value },
    done: (...keys: string[]) => { keys.forEach((k) => delete pending.current[k]) },
  }
}

function attrsObject(rows: [string, string][]): Record<string, string> {
  const obj: Record<string, string> = {}
  rows.forEach(([k, v]) => { if (k.trim()) obj[k.trim()] = v })
  return obj
}

function PointDetail(props: Props & { point: FwPointDetail }) {
  const { message, modal } = AntApp.useApp()
  const p = props.point
  const [name, setName] = useState(p.name ?? '')
  const [note, setNote] = useState(p.note ?? '')
  // 备注在别处被改了（录音转写追加进来）：跟上
  useEffect(() => { setNote(p.note ?? '') }, [p.note])
  const [attrs, setAttrs] = useState<[string, string][]>(Object.entries(p.attrs ?? {}).map(([k, v]) => [k, String(v)]))
  const [dragging, setDragging] = useState<string | null>(null)
  const [sampleClass, setSampleClass] = useState<number | null>(null)

  const categories = useMemo(
    () => [...new Set(props.points.map((x) => x.category).filter(Boolean) as string[])].map((c) => ({ value: c })),
    [props.points])

  const pending = usePending((changes) => {
    fw.patchPoint(p.uid, changes).then(() => props.onRefreshPoint(p.uid)).catch((e) => message.error((e as Error).message))
  })
  const save = async (changes: Record<string, unknown>) => {
    pending.done(...Object.keys(changes))
    try {
      await fw.patchPoint(p.uid, changes)
      await props.onRefreshPoint(p.uid)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const saveAttrs = (rows: [string, string][]) => save({ attrs: attrsObject(rows) })
  const editAttrs = (rows: [string, string][]) => {
    setAttrs(rows)
    pending.mark('attrs', attrsObject(rows))
  }

  const reorder = async (photoUid: string, index: number) => {
    await fw.movePhoto(photoUid, p.uid, index)
    await props.onRefreshPoint(p.uid)
  }

  const batchRemoveMarks = async () => {
    const uids = p.photos.map((x) => x.uid)
    type R = { done: { uid: string; azimuth: number | null; notes: string[] }[]; skipped: { uid: string }[]; failed: { uid: string; reason: string }[] }
    const r = await props.onTask<R>(() => fw.removeMarkBatch(uids), '去除奥维水印失败')
    if (!r) return
    await props.onRefreshPoint(p.uid)
    const notes = r.done.flatMap((d) => d.notes)
    props.onStatus(`去除奥维水印：完成 ${r.done.length} 张，无水印 ${r.skipped.length} 张${r.failed.length ? `，失败 ${r.failed.length} 张` : ''}`)
    if (notes.length || r.failed.length) {
      modal.warning({ title: '以下照片需要核对', content: <div className="pre">{[...notes, ...r.failed.map((f) => f.reason)].join('\n')}</div> })
    }
  }

  const atts = p.attachments ?? []
  const asr = props.status?.asr
  const untranscribed = atts.filter((a) => a.kind === 'audio' && !a.missing && a.transcript == null)
  /** 把一行录音转写追加到备注末尾；同样的文字已经在备注里就不加 */
  const appendToNote = (line: string) => {
    const body = line.replace(/^［录音[^］]*］/, '')
    if (body && note.includes(body)) {
      props.onStatus('备注里已有这段文字。')
      return
    }
    const next = note.trim() ? note.replace(/\s+$/, '') + '\n' + line : line
    setNote(next)
    save({ note: next })
    props.onStatus('已追加到备注。')
  }
  const remove = () => {
    const parts = [p.photos.length ? `${p.photos.length} 张照片` : '', atts.length ? `${atts.length} 个附件` : ''].filter(Boolean)
    const media = atts.length ? (p.photos.length ? '照片与附件' : '附件') : '照片'
    const dlg = modal.confirm({
      title: `删除点位「${p.name || '未命名'}」？`,
      content: parts.length ? `该点位有 ${parts.join('、')}。删除后无法撤销。` : '删除后无法撤销。',
      okText: parts.length ? `删除点位和${media}` : '删除', okButtonProps: { danger: true },
      cancelText: '取消',
      footer: (_, { OkBtn, CancelBtn }) => (
        <>
          <CancelBtn />
          {parts.length ? <Button onClick={() => { dlg.destroy(); doDelete(true) }}>仅删除点位，保留{media}</Button> : null}
          <OkBtn />
        </>
      ),
      onOk: () => doDelete(false),
    })
  }
  const doDelete = async (keepPhotos: boolean) => {
    await fw.deletePoint(p.uid, keepPhotos)
    props.onSelect(null)
    await props.onReload()
    props.onStatus(`已删除点位「${p.name || '未命名'}」${keepPhotos ? '，照片与附件已移至「未关联点位的照片与附件」' : ''}`)
  }

  const toSample = async () => {
    if (sampleClass == null) return
    try {
      const r = await fw.toSample(p.uid, sampleClass)
      await props.onRefreshPoint(p.uid)
      props.onSamplesChanged()
      props.onStatus(`已复制为点样本 ${r.key}，野外点位保持不变`)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const moving = props.moving?.uid === p.uid ? props.moving : null
  const moved = !!moving && (Math.abs(moving.lon - p.lon) > 1e-9 || Math.abs(moving.lat - p.lat) > 1e-9)
  const pointClasses = props.classes.filter((c) => c.kind === 'point' || (c.kind === '' && !c.is_group))
  const hasMarkable = p.photos.some((x) => x.ovital_mark !== 0)

  return (
    <div className="fw-detail-body">
      <div className="fw-title-row">
        <Input className="fw-title-input" value={name} variant="borderless" placeholder="点位名称" autoFocus={!p.name}
          onChange={(e) => { setName(e.target.value); pending.mark('name', e.target.value) }} onBlur={() => name !== (p.name ?? '') && save({ name })}
          onPressEnter={(e) => (e.target as HTMLInputElement).blur()} />
        <Tooltip title="缩放到点位"><button className="icon-btn" onClick={() => props.onZoom({ kind: 'point', uid: p.uid })}><Icon name="target" size={16} /></button></Tooltip>
        {props.onMove && props.visible ? (
          <Tooltip title={moving ? '结束移动（Esc）' : '在画布上拖动，调整点位位置'}>
            <button className={'icon-btn' + (moving ? ' on' : '')} onClick={() => props.onMove!(moving ? null : p)}><Icon name="hand" size={16} /></button>
          </Tooltip>
        ) : null}
        <Tooltip title="删除点位"><button className="icon-btn" onClick={remove}><Icon name="trash" size={16} /></button></Tooltip>
      </div>
      <div className="fw-meta mono">
        {p.lon.toFixed(6)}°E {p.lat.toFixed(6)}°N
        {p.elevation != null ? ` · ${Math.round(p.elevation)} m` : ''}
        {p.created ? ` · ${fmtTime(p.created)}` : ''}
      </div>
      <div className="fw-meta">{p.survey ? `批次 ${p.survey}` : ''}{p.source ? ` · 来自 ${p.source}` : ''}</div>
      {moving ? (
        <div className="fw-movebar">
          <span>拖动画布上的菱形到新位置，松开即保存。</span>
          {moved ? <button className="tb-btn" onClick={props.onRestoreMove}>还原到原位置</button> : null}
          <Button size="small" type="primary" onClick={() => props.onMove?.(null)}>完成</Button>
        </div>
      ) : null}

      <div className="fw-form">
        <label>类型</label>
        <AutoComplete size="small" options={categories} defaultValue={p.category ?? ''} placeholder="观察点、采样点、光谱测点…"
          onChange={(v: string) => pending.mark('category', v)}
          onBlur={(e) => { const v = (e.target as HTMLInputElement).value; if (v !== (p.category ?? '')) save({ category: v }) }}
          onSelect={(v: string) => save({ category: v })} />
        <label>备注</label>
        <Input.TextArea size="small" autoSize={{ minRows: 2, maxRows: 6 }} value={note}
          onChange={(e) => { setNote(e.target.value); pending.mark('note', e.target.value) }} onBlur={() => note !== (p.note ?? '') && save({ note })} />
        <label>高程</label>
        <InputNumber size="small" value={p.elevation ?? undefined} addonAfter="m" style={{ width: 140 }}
          onChange={(v) => { if (typeof v === 'number') pending.mark('elevation', v) }}
          onBlur={(e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v) && v !== p.elevation) save({ elevation: v }) }} />
      </div>

      <div className="fw-section">
        <div className="fw-section-head">
          <span>属性</span>
          <button className="tb-btn" onClick={() => setAttrs([...attrs, ['', '']])}><Icon name="plus" size={14} /> 添加</button>
        </div>
        {attrs.length ? (
          <div className="fw-attrs">
            {attrs.map(([k, v], i) => (
              <div key={i} className="fw-attr">
                <Input size="small" placeholder="名称（如 岩性、产状）" value={k}
                  onChange={(e) => editAttrs(attrs.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))}
                  onBlur={() => saveAttrs(attrs)} />
                <Input size="small" placeholder="值" value={v}
                  onChange={(e) => editAttrs(attrs.map((r, j) => (j === i ? [r[0], e.target.value] : r)))}
                  onBlur={() => saveAttrs(attrs)} />
                <button className="icon-btn" title="删除此属性" onClick={() => {
                  const rows = attrs.filter((_, j) => j !== i)
                  setAttrs(rows)
                  saveAttrs(rows)
                }}><span className="float-x">×</span></button>
              </div>
            ))}
          </div>
        ) : <div className="caption">可添加岩性、产状、样品编号等属性。</div>}
      </div>

      <div className="fw-section">
        <div className="fw-section-head">
          <span>照片 {p.photos.length ? <span className="mono muted">{p.photos.length}</span> : null}</span>
          {hasMarkable && props.status?.ocr ? (
            <Tooltip title="识别并去除各照片左下角的奥维水印，朝向记入照片信息；原片不变">
              <button className="tb-btn" onClick={batchRemoveMarks}>去除奥维水印</button>
            </Tooltip>
          ) : null}
          <button className="tb-btn" onClick={() => props.onImportPhotosTo(p.uid)}><Icon name="plus" size={14} /> 添加照片</button>
          {p.photos.length ? (
            <Tooltip title="导出该点位的照片（调整后或原片）">
              <button className="tb-btn" onClick={() => props.onExport({
                title: `点位「${p.name || '未命名'}」的 ${p.photos.length} 张照片`, count: p.photos.length, uids: p.photos.map((x) => x.uid),
                sample: { point: p.name || '未命名点位', orig: p.photos[0].orig_name ?? 'IMG', date: (p.photos[0].taken ?? '').slice(0, 10).replace(/-/g, ''), survey: p.survey ?? '' },
              })}><Icon name="download" size={14} /> 导出</button>
            </Tooltip>
          ) : null}
        </div>
        {p.photos.length ? (
          <div className="fw-thumbs">
            {p.photos.map((ph, i) => (
              <div key={ph.uid} className={'fw-thumb' + (dragging === ph.uid ? ' dragging' : '')}
                draggable
                onDragStart={(e) => { e.dataTransfer.setData(PHOTO_MIME, ph.uid); setDragging(ph.uid) }}
                onDragEnd={() => setDragging(null)}
                onDragOver={(e) => { if (e.dataTransfer.types.includes(PHOTO_MIME)) e.preventDefault() }}
                onDrop={(e) => {
                  const uid = e.dataTransfer.getData(PHOTO_MIME)
                  if (uid && uid !== ph.uid) { e.preventDefault(); e.stopPropagation(); reorder(uid, i) }
                }}
                onClick={() => props.onOpenPhotos(p.photos, i)}
                title={[ph.orig_name, fmtTime(ph.taken), ph.caption].filter(Boolean).join('\n')}>
                <img src={photoUrl(ph, 160)} alt="" loading="lazy" draggable={false} />
                {ph.azimuth != null ? <span className="fw-thumb-az mono">{Math.round(ph.azimuth)}°</span> : null}
                {ph.adjusted ? <span className="fw-thumb-edit" title="有调整；原片未改动">已调整</span> : null}
              </div>
            ))}
          </div>
        ) : <div className="caption">暂无照片。点击「添加照片」，或选中该点位后将照片拖入窗口。</div>}
      </div>

      <div className="fw-section">
        <div className="fw-section-head">
          <span>附件 {atts.length ? <span className="mono muted">{atts.length}</span> : null}</span>
          {untranscribed.length && asr?.engine ? (
            <Tooltip title={`离线识别该点位上的 ${untranscribed.length} 段录音，文字追加到备注末尾${asr.model ? '' : '（第一次使用需下载约 240 MB 的识别模型）'}`}>
              <button className="tb-btn" onClick={() => props.onTranscribe({ point_uid: p.uid, append: true })}>录音转文字到备注</button>
            </Tooltip>
          ) : null}
          <Tooltip title="把录音、文档等文件挂到该点位上；选中的照片归入上面的「照片」">
            <button className="tb-btn" onClick={() => props.onAddAttachments(p.uid)}><Icon name="plus" size={14} /> 添加附件</button>
          </Tooltip>
        </div>
        {atts.length ? (
          <AttachmentList items={atts} draggable onChanged={() => { props.onRefreshPoint(p.uid); props.onReload() }} onStatus={props.onStatus}
            asr={asr} onTranscribe={(a) => props.onTranscribe({ uids: [a.uid], redo: true })} onAppendToNote={appendToNote} />
        ) : <div className="caption">暂无录音、文档等附件。奥维标签上的附件在导入时一并带入。</div>}
      </div>

      {props.status?.spectra ? (
        <SpectraSection point={p} onChanged={() => props.onRefreshPoint(p.uid)} onStatus={props.onStatus} />
      ) : null}

      <div className="fw-section">
        <div className="fw-section-head"><span>用作样本</span></div>
        {p.sample_key ? (
          <div className="caption">已复制为点样本 <span className="mono">{p.sample_key}</span>。</div>
        ) : (
          <div className="fw-inline">
            <Select size="small" style={{ flex: 1 }} placeholder="选择点类别" value={sampleClass ?? undefined}
              showSearch optionFilterProp="label" onChange={setSampleClass}
              options={pointClasses.map((c) => ({ value: c.id, label: `${c.region ? c.region + ' / ' : ''}${c.name}` }))} />
            <Tooltip title="在样本库中按该点位的位置新建一条点样本；野外点位保持不变，不会出现在样本树中">
              <Button size="small" disabled={sampleClass == null} onClick={toSample}>复制为点样本</Button>
            </Tooltip>
          </div>
        )}
      </div>
    </div>
  )
}

// =============================================================================
// 轨迹
// =============================================================================

function TrackDetail(props: Props & { track: FwTrack | null }) {
  const { message, modal } = AntApp.useApp()
  const t = props.track
  const [name, setName] = useState(t?.name ?? '')
  useEffect(() => setName(t?.name ?? ''), [t?.name])
  const uid = t?.uid
  const pending = usePending((changes) => {
    if (uid) fw.patchTrack(uid, changes).then(() => props.onReload()).catch((e) => message.error((e as Error).message))
  })
  // 取色器拖动时每一步都触发 change：停手 300 毫秒再保存
  const colorTimer = useRef(0)
  // 换了选中项时剖面图被拆掉，收不到 mouseleave：画布上的标记在这里擦掉
  const { onTrackHover } = props
  useEffect(() => () => onTrackHover?.(null), [onTrackHover])
  // 奥维轨迹上带的附件（录音等）
  const [atts, setAtts] = useState<FwAttachment[]>([])
  const loadAtts = () => { if (uid) fw.attachments({ track: uid }).then(setAtts).catch(() => setAtts([])) }
  useEffect(loadAtts, [uid, props.points]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!t) return <div className="fw-placeholder hint">轨迹不存在，可能已被删除。</div>
  const save = async (changes: Record<string, unknown>) => {
    pending.done(...Object.keys(changes))
    try {
      await fw.patchTrack(t.uid, changes)
      await props.onReload()
    } catch (e) {
      message.error((e as Error).message)
    }
  }
  const saveColor = (color: string) => {
    pending.mark('color', color)
    window.clearTimeout(colorTimer.current)
    colorTimer.current = window.setTimeout(() => save({ color }), 300)
  }
  const hours = t.start_time && t.end_time ? (Date.parse(t.end_time) - Date.parse(t.start_time)) / 3.6e6 : null
  return (
    <div className="fw-detail-body">
      <div className="fw-title-row">
        <Input className="fw-title-input" value={name} variant="borderless"
          onChange={(e) => { setName(e.target.value); pending.mark('name', e.target.value) }}
          onBlur={() => name !== (t.name ?? '') && save({ name })} />
        <Tooltip title="缩放到轨迹"><button className="icon-btn" onClick={() => props.onZoom({ kind: 'track', uid: t.uid })}><Icon name="target" size={16} /></button></Tooltip>
        <Tooltip title="删除轨迹"><button className="icon-btn" onClick={() => modal.confirm({
          title: `删除轨迹「${t.name || ''}」？`, okButtonProps: { danger: true }, okText: '删除', cancelText: '取消',
          onOk: async () => { await fw.deleteTrack(t.uid); props.onSelect(null); await props.onReload() },
        })}><Icon name="trash" size={16} /></button></Tooltip>
      </div>
      <div className="fw-meta mono">{fmtTime(t.start_time)} — {fmtTime(t.end_time)}{hours != null ? `（${hours < 1 ? `${Math.max(1, Math.round(hours * 60))} 分钟` : `${hours.toFixed(1)} 小时`}）` : ''}</div>
      <div className="fw-stats">
        <div><span className="caption">长度</span><b className="mono">{fmtLen(t.length_m)}</b></div>
        <div><span className="caption">点数</span><b className="mono">{t.n_points}</b></div>
        <div><span className="caption">高程</span><b className="mono">{t.elev_min != null ? `${Math.round(t.elev_min)}–${Math.round(t.elev_max!)} m` : '—'}</b></div>
      </div>
      {t.coords && t.coords.some((c) => c[2]) ? (
        <div className="fw-section">
          <div className="fw-section-head"><span>高程剖面</span></div>
          <ElevationProfile coords={t.coords} onHover={props.onTrackHover} />
        </div>
      ) : null}
      {atts.length ? (
        <div className="fw-section">
          <div className="fw-section-head"><span>附件 <span className="mono muted">{atts.length}</span></span></div>
          <AttachmentList items={atts} draggable onChanged={() => { loadAtts(); props.onReload() }} onStatus={props.onStatus}
            asr={props.status?.asr} onTranscribe={async (a) => { await props.onTranscribe({ uids: [a.uid], redo: true }); loadAtts() }} />
          <div className="caption" style={{ marginTop: 4 }}>拖到左侧列表中的点位上，可改挂到该点位。</div>
        </div>
      ) : null}
      <div className="fw-form">
        <label>批次</label>
        <Input size="small" defaultValue={t.survey ?? ''} onChange={(e) => pending.mark('survey', e.target.value)}
          onBlur={(e) => e.target.value !== (t.survey ?? '') && save({ survey: e.target.value })} />
        <label>颜色</label>
        <input type="color" className="native-color" defaultValue={t.color || props.surveyColors[t.survey ?? ''] || '#E0A458'}
          onChange={(e) => saveColor(e.target.value)} />
        <label>备注</label>
        <Input.TextArea size="small" autoSize={{ minRows: 2, maxRows: 5 }} defaultValue={t.note ?? ''}
          onChange={(e) => pending.mark('note', e.target.value)}
          onBlur={(e) => e.target.value !== (t.note ?? '') && save({ note: e.target.value })} />
      </div>
    </div>
  )
}

// =============================================================================
// 批次与独立照片
// =============================================================================

function SurveyDetail(props: Props & { survey: string }) {
  const s = props.overview?.surveys.find((x) => x.survey === props.survey)
  const pts = props.points.filter((p) => (p.survey ?? '') === props.survey)
  const trs = props.tracks.filter((t) => (t.survey ?? '') === props.survey)
  if (!s) return <div className="fw-placeholder hint">批次不存在。</div>
  const photos = pts.reduce((n, p) => n + p.n_photos, 0)
  const spectra = pts.reduce((n, p) => n + p.n_spectra, 0)
  return (
    <div className="fw-detail-body">
      <div className="fw-title-row"><span className="insp-title">{s.survey || '（未分批）'}</span></div>
      <div className="fw-meta mono">{fmtTime(s.start)} — {fmtTime(s.end)}</div>
      <div className="fw-stats">
        <div><span className="caption">点位</span><b className="mono">{pts.length}</b></div>
        <div><span className="caption">轨迹</span><b className="mono">{trs.length}</b></div>
        <div><span className="caption">照片</span><b className="mono">{photos}</b></div>
        <div><span className="caption">光谱</span><b className="mono">{spectra}</b></div>
        <div><span className="caption">轨迹总长</span><b className="mono">{fmtLen(trs.reduce((n, t) => n + t.length_m, 0))}</b></div>
      </div>
      <div className="actions">
        <Button size="small" onClick={() => props.onZoom({ kind: 'survey', uid: props.survey })}>缩放到该批次</Button>
        <Button size="small" disabled={!photos} onClick={() => props.onExport({
          title: `批次「${s.survey || '未分批'}」的 ${photos} 张照片`, count: photos, survey: props.survey,
          sample: { point: pts[0]?.name || '未命名点位', orig: 'IMG', date: (s.start ?? '').slice(0, 10).replace(/-/g, ''), survey: s.survey },
        })}>导出本批次照片…</Button>
        {props.status?.asr?.engine && pts.some((p) => p.n_attachments) ? (
          <Tooltip title="离线识别本批次各点位上还没转写的录音，文字追加到各自点位的备注末尾">
            <Button size="small" onClick={() => props.onTranscribe({ surveys: [props.survey], append: true })}>录音转文字到备注</Button>
          </Tooltip>
        ) : null}
      </div>
      <div className="hint" style={{ marginTop: 12 }}>右键左侧批次名可重命名。批次名默认为导入的文件名。</div>
    </div>
  )
}

function LoosePhotos(props: Props) {
  const [photos, setPhotos] = useState<FwPhoto[] | null>(null)
  const [atts, setAtts] = useState<FwAttachment[]>([])
  const load = () => {
    fw.photos('', true).then(setPhotos).catch(() => setPhotos([]))
    fw.attachments({ unattached: true }).then(setAtts).catch(() => setAtts([]))
  }
  useEffect(load, [props.points]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="fw-detail-body">
      <div className="fw-title-row"><span className="insp-title">未关联点位的照片与附件</span></div>
      <div className="hint">没有 GPS 或附近没有点位的照片，以及奥维文件里没有挂在标签上的附件。拖到左侧点位上即可关联。</div>
      {atts.length ? (
        <div className="fw-section">
          <div className="fw-section-head"><span>附件 <span className="mono muted">{atts.length}</span></span></div>
          <AttachmentList items={atts} draggable onChanged={() => { load(); props.onReload() }} onStatus={props.onStatus}
            asr={props.status?.asr} onTranscribe={async (a) => { await props.onTranscribe({ uids: [a.uid], redo: true }); load() }} />
        </div>
      ) : null}
      {atts.length ? <div className="fw-section-head" style={{ marginTop: 14 }}><span>照片</span></div> : null}
      {photos && photos.length ? (
        <div className="fw-thumbs" style={{ marginTop: 12 }}>
          {photos.map((ph, i) => (
            <div key={ph.uid} className="fw-thumb" draggable
              onDragStart={(e) => e.dataTransfer.setData(PHOTO_MIME, ph.uid)}
              onClick={() => props.onOpenPhotos(photos, i)} title={ph.orig_name ?? ''}>
              <img src={photoUrl(ph, 160)} alt="" loading="lazy" draggable={false} />
            </div>
          ))}
        </div>
      ) : <div className="caption" style={{ marginTop: 12 }}>{photos ? '无' : '读取中…'}</div>}
    </div>
  )
}
