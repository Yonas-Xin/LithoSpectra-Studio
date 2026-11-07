// 野外记录模块的前端入口。
//
// 外壳只需要做三件事：
//   1. 把 OpenLayers 地图对象交进来（MapView 的 onMap）；
//   2. 菜单动作里 "fw-" 开头的交给 handle.action(id)；
//   3. 拖进窗口的文件先过 handle.claim(paths)，剩下的再当图层加载。
// 其余（画布图层、面板、照片浏览、编辑、导入对话框）都在本目录里，模块不在发行版里
// 时整个目录删掉、外壳那几行去掉即可。

import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react'
import type { Ref } from 'react'
import { App as AntApp } from 'antd'
import type OlMap from 'ol/Map'
import { transform } from 'ol/proj'

import type { SampleClass, TaskInfo } from '../api'
import { desktop } from '../desktop'
import { CAPTURE, bringInto, fitInto, freeRegion, inRegion } from '../canvasLink'
import { useTaskRunner } from '../tasks'
import type { Palette } from '../generated/tokens'
import { fw } from './fwapi'
import type {
  FwOverview, FwPhoto, FwPoint, FwPointDetail, FwStatus, FwTrack, OvobjBatchResult, OvobjImportResult, OvobjPreview,
  OvobjPreviewError, PhotoImportMode, TranscribeResult,
} from './fwapi'
import { surveyColor, useFieldLayer } from './FieldLayer'
import type { FieldPick } from './FieldLayer'
import FieldPanel from './FieldPanel'
import type { Selection } from './FieldPanel'
import PhotoViewer from './PhotoViewer'
import { OvobjBatchDialog, OvobjImportDialog, PhotoImportDialog } from './ImportDialogs'
import ExportDialog from './ExportDialog'
import type { ExportRequest } from './ExportDialog'
import type { ExportResult } from './fwapi'
import './fieldwork.css'

export interface FieldworkHandle {
  /** 菜单动作；不是本模块的返回 false */
  action: (id: string) => boolean
  /** 认领拖进来的文件，返回不归本模块的那些 */
  claim: (paths: string[]) => Promise<string[]>
  /** 在给定位置新建一个野外点位，打开它的详情 */
  addPointAt: (lonlat: [number, number]) => Promise<void>
}

interface Props {
  map: OlMap | null
  projection: string
  palette: Palette
  libraryOpen: boolean
  libraryPath: string
  classes: SampleClass[]
  onStatus: (text: string) => void
  onFit: (extent: number[]) => void
  onSamplesChanged: () => void
  /** 画布上有没有显示着野外记录（外壳据此决定要不要提示「画布上还没有样本」） */
  onShown?: (shown: boolean) => void
  handle: Ref<FieldworkHandle>
}

export default function Fieldwork(props: Props) {
  const { message, modal } = AntApp.useApp()
  const runTask = useTaskRunner()
  const [open, setOpen] = useState(false)
  const [visible, setVisible] = useState(true)
  const [status, setStatus] = useState<FwStatus | null>(null)
  const [overview, setOverview] = useState<FwOverview | null>(null)
  const [points, setPoints] = useState<FwPoint[]>([])
  const [tracks, setTracks] = useState<FwTrack[]>([])
  const [sel, setSel] = useState<Selection>(null)
  const [detail, setDetail] = useState<FwPointDetail | null>(null)
  const [viewer, setViewer] = useState<{ photos: FwPhoto[]; index: number } | null>(null)
  const [ovobjPreview, setOvobjPreview] = useState<OvobjPreview | null>(null)
  const [ovobjBatch, setOvobjBatch] = useState<(OvobjPreview | OvobjPreviewError)[] | null>(null)
  const [photoImport, setPhotoImport] = useState<{ paths: string[]; pointUid: string | null } | null>(null)
  const [exportReq, setExportReq] = useState<ExportRequest | null>(null)
  const [trackCursor, setTrackCursor] = useState<[number, number] | null>(null)
  // 正在画布上拖动调整位置的点位，记着它开始移动前的位置（还原用）
  const [moving, setMoving] = useState<{ uid: string; lon: number; lat: number; name: string } | null>(null)

  // ------------------------------------------------------------- 读数据
  const reload = useCallback(async () => {
    if (!props.libraryOpen) {
      setOverview(null)
      setPoints([])
      setTracks([])
      setDetail(null)
      return
    }
    try {
      const [ov, pts, trs] = await Promise.all([fw.overview(), fw.points(), fw.tracks(true)])
      setOverview(ov)
      setPoints(pts)
      setTracks(trs)
    } catch (e) {
      message.error((e as Error).message)
    }
  }, [props.libraryOpen, message])

  useEffect(() => {
    fw.status().then(setStatus).catch(() => setStatus(null))
  }, [])

  useEffect(() => {
    setSel(null)
    setDetail(null)
    reload()
  }, [props.libraryPath, props.libraryOpen, reload])

  const loadDetail = useCallback(async (uid: string | null) => {
    if (!uid) return setDetail(null)
    try {
      setDetail(await fw.point(uid))
    } catch (e) {
      setDetail(null)
      message.error((e as Error).message)
    }
  }, [message])

  useEffect(() => {
    loadDetail(sel?.kind === 'point' ? sel.uid : null)
  }, [sel, loadDetail])

  /** 点位改动之后：详情、列表里那一行、画布一起刷新 */
  const refreshPoint = useCallback(async (uid: string) => {
    const d = await fw.point(uid)
    setDetail((cur) => (cur && cur.uid === uid ? d : cur))
    setPoints((list) => list.map((p) => (p.uid === uid ? { ...d } : p)))
    return d
  }, [])

  // ------------------------------------------------------------- 移动点位
  // 选了别的、关了面板、隐藏了图层、换了库：都结束移动
  const movingUid = moving?.uid ?? null
  useEffect(() => {
    if (movingUid && (!open || !visible || !props.libraryOpen || sel?.kind !== 'point' || sel.uid !== movingUid)) setMoving(null)
  }, [movingUid, open, visible, props.libraryOpen, sel])
  // 移动期间接管画布单击：不选样本、不点选别的野外要素，画样本的工具先停用；Esc 结束
  useEffect(() => {
    const map = props.map
    if (!map || !movingUid) return
    map.set(CAPTURE, 'fw-move')
    const onKey = (e: KeyboardEvent) => {
      // 照片窗口开着时 Esc 是关它的
      if (e.key !== 'Escape' || (e.target as HTMLElement).closest?.('input, textarea') || document.querySelector('.pv-backdrop')) return
      e.preventDefault()
      e.stopImmediatePropagation()
      setMoving(null)
    }
    window.addEventListener('keydown', onKey, true)
    return () => {
      window.removeEventListener('keydown', onKey, true)
      if (map.get(CAPTURE) === 'fw-move') map.set(CAPTURE, false)
    }
  }, [props.map, movingUid])

  const startMove = useCallback((p: FwPointDetail | null) => {
    if (!p) {
      setMoving(null)
      return
    }
    setMoving({ uid: p.uid, lon: p.lon, lat: p.lat, name: p.name || '未命名点位' })
    props.onStatus(`拖动画布上的菱形调整「${p.name || '未命名点位'}」的位置，松开即保存；按 Esc 或再次单击移动按钮结束。`)
  }, [props])

  const savePosition = useCallback(async (uid: string, lon: number, lat: number) => {
    try {
      await fw.patchPoint(uid, { lon, lat })
      await reload()
      await refreshPoint(uid)
    } catch (e) {
      message.error((e as Error).message)
      await reload()
    }
  }, [reload, refreshPoint, message])

  const onMoved = useCallback(async (uid: string, lon: number, lat: number) => {
    await savePosition(uid, lon, lat)
    props.onStatus(`已移动到 ${lon.toFixed(6)}°E ${lat.toFixed(6)}°N。可继续拖动，或在详情中还原到原位置。`)
  }, [savePosition, props])

  const restoreMove = useCallback(async () => {
    if (!moving) return
    await savePosition(moving.uid, moving.lon, moving.lat)
    props.onStatus(`已将「${moving.name}」还原到原位置。`)
  }, [moving, savePosition, props])

  // ------------------------------------------------------------- 画布
  const surveyColors = useMemo(() => {
    const out: Record<string, string> = {}
    ;(overview?.surveys ?? []).forEach((s, i) => { out[s.survey] = surveyColor(i) })
    return out
  }, [overview])

  const revealRef = useRef<(s: Selection) => void>(() => undefined)
  const onPick = useCallback((pick: FieldPick | null) => {
    if (!pick) return
    if (pick.kind === 'photo') {
      const photos = detail?.photos ?? []
      const i = photos.findIndex((p) => p.uid === pick.uid)
      if (i >= 0) setViewer({ photos, index: i })
      return
    }
    const s: Selection = { kind: pick.kind, uid: pick.uid }
    setSel(s)
    setOpen(true)
    // 面板刚弹出来可能正好盖住刚点的地方：等它摆好再看要不要挪地图
    window.setTimeout(() => revealRef.current(s), 0)
  }, [detail])

  useFieldLayer({
    map: props.map, projection: props.projection, visible: visible && props.libraryOpen,
    points, tracks, selectedPoint: sel?.kind === 'point' ? sel.uid : null,
    selectedTrack: sel?.kind === 'track' ? sel.uid : null,
    photos: detail?.photos ?? [], surveyColors, palette: props.palette, onPick,
    cursor: open ? trackCursor : null,
    moving: movingUid, onMoved,
  })

  const shown = visible && props.libraryOpen && (points.length > 0 || tracks.length > 0)
  const { onShown } = props
  useEffect(() => { onShown?.(shown) }, [shown, onShown])

  /** 缩放到经纬度范围。野外记录面板开着时缩放到面板旁边空着的那一块，免得一半落在面板底下 */
  const fit = useCallback((extent: number[]) => {
    const map = props.map
    const panel = document.querySelector('.fw-panel')?.closest('.float')
    const region = map && panel ? freeRegion(map, panel) : null
    if (map && region) fitInto(map, extent, region)
    else props.onFit(extent)
  }, [props])

  const zoomTo = useCallback((what: 'all' | Selection) => {
    if (what === 'all') {
      if (overview?.extent) fit(overview.extent)
      return
    }
    if (!what) return
    if (what.kind === 'point') {
      const p = points.find((x) => x.uid === what.uid)
      if (p) fit([p.lon - 0.002, p.lat - 0.002, p.lon + 0.002, p.lat + 0.002])
    } else if (what.kind === 'track') {
      const t = tracks.find((x) => x.uid === what.uid)
      if (t?.coords?.length) {
        const xs = t.coords.map((c) => c[0])
        const ys = t.coords.map((c) => c[1])
        fit([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)])
      }
    } else if (what.kind === 'survey') {
      const ps = points.filter((p) => (p.survey ?? '') === what.uid)
      const ts = tracks.filter((t) => (t.survey ?? '') === what.uid).flatMap((t) => t.coords ?? [])
      const xs = [...ps.map((p) => p.lon), ...ts.map((c) => c[0])]
      const ys = [...ps.map((p) => p.lat), ...ts.map((c) => c[1])]
      if (xs.length) fit([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)])
    }
  }, [overview, points, tracks, fit])

  /** 让选中的点位或轨迹在画布上看得见：完全不在视野里、或被野外记录面板挡住时，把地图平移到
      面板旁边空着的那一块，比例尺不变 */
  const revealOnMap = useCallback((s: Selection) => {
    const map = props.map
    const size = map?.getSize()
    if (!map || !size || !s || !visible) return
    let lonlat: [number, number][] = []
    if (s.kind === 'point') {
      const p = points.find((x) => x.uid === s.uid)
      if (p) lonlat = [[p.lon, p.lat]]
    } else if (s.kind === 'track') {
      lonlat = (tracks.find((x) => x.uid === s.uid)?.coords ?? []).map((c) => [c[0], c[1]])
    }
    if (!lonlat.length) return
    const region = freeRegion(map, document.querySelector('.fw-panel')?.closest('.float'))
    if (!region) return
    const proj = map.getView().getProjection()
    const xy = lonlat.map((c) => transform(c, 'EPSG:4326', proj))
    if (xy.some((c) => inRegion(map, c, region))) return
    const xs = xy.map((c) => c[0])
    const ys = xy.map((c) => c[1])
    bringInto(map, [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2], region)
  }, [props.map, visible, points, tracks])
  useEffect(() => { revealRef.current = revealOnMap }, [revealOnMap])

  /** 在列表里选中 */
  const selectFromList = useCallback((s: Selection) => {
    setSel(s)
    revealOnMap(s)
  }, [revealOnMap])

  // ------------------------------------------------------------- 任务
  const task = useCallback(async <T,>(start: () => Promise<TaskInfo>, failTitle: string): Promise<T | null> => {
    try {
      const { result, cancelled } = await runTask<T>(start)
      if (cancelled) {
        props.onStatus('已取消。')
        return null
      }
      return result
    } catch (e) {
      modal.error({ title: failTitle, content: <div className="pre">{(e as Error).message}</div> })
      return null
    }
  }, [runTask, modal, props])

  // ------------------------------------------------------------- 导入
  const needLibrary = useCallback(() => {
    if (props.libraryOpen) return true
    message.warning('野外记录保存在样本库中，请先打开或新建样本库。')
    return false
  }, [props.libraryOpen, message])

  /** 一份走单个文件的预览对话框，多份列成一张表批量导 */
  const startOvobj = useCallback(async (paths: string[]) => {
    if (!needLibrary() || !paths.length) return
    if (paths.length === 1) {
      try {
        setOvobjPreview(await fw.previewOvobj(paths[0]))
      } catch (e) {
        modal.error({ title: '无法读取该奥维文件', content: (e as Error).message })
      }
      return
    }
    props.onStatus(`正在读取 ${paths.length} 份奥维文件…`)
    try {
      setOvobjBatch((await fw.previewOvobjBatch(paths)).items)
      props.onStatus('')
    } catch (e) {
      modal.error({ title: '无法读取奥维文件', content: (e as Error).message })
    }
  }, [needLibrary, modal, props])

  /** 附件的情况：「录音 2 个、文件 1 个」 */
  const extras = (r: Pick<OvobjImportResult, 'audio_new' | 'video_new' | 'files_new'>) =>
    [r.audio_new ? `${r.audio_new} 段录音` : '', r.video_new ? `${r.video_new} 段视频` : '', r.files_new ? `${r.files_new} 个文件` : '']
      .filter(Boolean).join('、')

  const doOvobj = async (path: string, survey: string, toNote = false) => {
    setOvobjPreview(null)
    const r = await task<OvobjImportResult>(() => fw.importOvobj(path, survey), '导入奥维收藏夹失败')
    if (!r) return
    await reload()
    setOpen(true)
    setSel({ kind: 'survey', uid: r.survey })
    const ex = extras(r)
    props.onStatus(`已导入「${r.survey}」：新增 ${r.points_new} 个点位、${r.tracks_new} 条轨迹、${r.photos_new} 张照片` +
      (ex ? `、${ex}` : '') + (r.points_updated ? `，更新 ${r.points_updated} 个点位` : ''))
    if (r.warnings.length) modal.warning({ title: '导入提示', content: <div className="pre">{r.warnings.join('\n')}</div> })
    await zoomSurveys([r.survey])
    if (toNote && r.audio_new) await transcribe({ surveys: [r.survey], append: true })
  }

  const doOvobjBatch = async (items: { path: string; survey: string }[], toNote = false) => {
    setOvobjBatch(null)
    const r = await task<OvobjBatchResult>(() => fw.importOvobjBatch(items), '批量导入奥维收藏夹失败')
    if (!r) return
    await reload()
    setOpen(true)
    if (r.surveys.length) setSel({ kind: 'survey', uid: r.surveys[r.surveys.length - 1] })
    const ex = extras(r)
    props.onStatus(`${r.cancelled ? '已取消，此前' : ''}已导入 ${r.files.length} 份奥维文件：新增 ${r.points_new} 个点位、${r.tracks_new} 条轨迹、` +
      `${r.photos_new} 张照片${ex ? `、${ex}` : ''}${r.points_updated ? `，更新 ${r.points_updated} 个点位` : ''}` +
      (r.failed.length ? `；${r.failed.length} 份失败` : ''))
    const notes = [
      ...r.failed.map((f) => `${f.name}：导入失败，${f.reason}`),
      ...r.files.flatMap((f) => f.warnings.map((w) => `${f.source}：${w}`)),
    ]
    if (notes.length) {
      modal.warning({ title: r.failed.length ? `${r.failed.length} 份文件导入失败` : '导入提示', width: 560,
        content: <div className="pre">{notes.join('\n')}</div> })
    }
    await zoomSurveys(r.surveys)
    if (toNote && r.audio_new) await transcribe({ surveys: r.surveys, append: true })
  }

  /** 刚导入的批次：列表状态还没更新到，直接问后端要坐标 */
  const zoomSurveys = async (surveys: string[]) => {
    if (!surveys.length) return
    const want = new Set(surveys)
    const [pts, trs] = await Promise.all([fw.points(), fw.tracks(true)])
    const ps = pts.filter((p) => want.has(p.survey ?? ''))
    const cs = trs.filter((t) => want.has(t.survey ?? '')).flatMap((t) => t.coords ?? [])
    const xs = [...ps.map((p) => p.lon), ...cs.map((c) => c[0])]
    const ys = [...ps.map((p) => p.lat), ...cs.map((c) => c[1])]
    if (xs.length) fit([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)])
  }

  // ------------------------------------------------------------- 录音转文字
  /** 识别模型第一次用时要下载：问一句，下完刷新状态。返回能不能接着转写 */
  const ensureAsrModel = useCallback(async (): Promise<boolean> => {
    const st = status ?? await fw.status().catch(() => null)
    if (!st?.asr?.engine) {
      message.warning('没有安装语音识别引擎（sherpa-onnx），无法转写录音。')
      return false
    }
    if (st.asr.model) return true
    const ok = await new Promise<boolean>((resolve) => modal.confirm({
      title: '下载语音识别模型？',
      content: `录音转文字用 SenseVoice 中文识别模型，约 ${Math.round(st.asr!.model_size / 1e6)} MB，优先从国内的魔搭（ModelScope）下载，保存到 ${st.asr!.model_path}。之后识别完全离线，不上传录音。`,
      okText: '下载', cancelText: '暂不下载', onOk: () => resolve(true), onCancel: () => resolve(false),
    }))
    if (!ok) return false
    const r = await task<{ path: string }>(() => fw.asrDownload(), '语音识别模型下载失败')
    const fresh = await fw.status().catch(() => null)
    if (fresh) setStatus(fresh)
    return !!r && !!fresh?.asr?.model
  }, [status, message, modal, task])

  const transcribe = useCallback(async (req: { uids?: string[]; point_uid?: string; surveys?: string[]; append?: boolean; redo?: boolean }) => {
    if (!(await ensureAsrModel())) return
    let r: TranscribeResult | null
    try {
      r = await task<TranscribeResult>(() => fw.transcribe(req), '录音转文字失败')
    } catch (e) {
      message.error((e as Error).message)
      return
    }
    if (!r) return
    await reload()
    if (detail) await refreshPoint(detail.uid)
    const done = r.done.length
    props.onStatus(done
      ? `已转写 ${done} 段录音${req.append ? `，追加到备注 ${r.appended} 段` : ''}${r.failed.length ? `，${r.failed.length} 段失败` : ''}`
      : '没有转写出文字。')
    if (r.failed.length) {
      modal.warning({ title: `${r.failed.length} 段录音未能转写`, content: <div className="pre">{r.failed.map((f) => `${f.name}：${f.reason}`).join('\n')}</div> })
    }
  }, [ensureAsrModel, task, reload, detail, refreshPoint, props, message, modal])

  /** 从计算机上选文件作为点位的附件 */
  const addAttachments = useCallback(async (pointUid: string) => {
    const dt = desktop()
    if (!dt) {
      message.info('浏览器中无法打开文件选择框。')
      return
    }
    const paths = await dt.pick_files('any')
    if (!paths?.length) return
    type R = { photos_new: number; attachments_new: number; skipped: number; warnings: string[] }
    const r = await task<R>(() => fw.importAttachments(paths, pointUid), '添加附件失败')
    if (!r) return
    await reload()
    await refreshPoint(pointUid)
    props.onStatus(`已添加 ${r.attachments_new} 个附件${r.photos_new ? `、${r.photos_new} 张照片` : ''}${r.skipped ? `，跳过已有的 ${r.skipped} 个` : ''}`)
    if (r.warnings.length) modal.warning({ title: '添加附件提示', content: <div className="pre">{r.warnings.join('\n')}</div> })
  }, [message, task, reload, refreshPoint, props, modal])

  const startPhotos = useCallback((paths: string[], pointUid: string | null = null) => {
    if (!needLibrary() || !paths.length) return
    setPhotoImport({ paths, pointUid })
  }, [needLibrary])

  const doPhotos = async (mode: PhotoImportMode, pointUid: string | null, maxDist: number) => {
    const req = photoImport!
    setPhotoImport(null)
    type R = { photos_new: number; photos_skipped: number; attached: number; points_new: number; no_gps: number; warnings: string[] }
    const r = await task<R>(() => fw.importPhotos(req.paths, mode, pointUid, maxDist), '导入照片失败')
    if (!r) return
    await reload()
    if (detail) await refreshPoint(detail.uid)
    props.onStatus(`已导入照片 ${r.photos_new} 张（关联到点位 ${r.attached} 张${r.points_new ? `，新建点位 ${r.points_new} 个` : ''}` +
      `${r.photos_skipped ? `，跳过已有的 ${r.photos_skipped} 张` : ''}${r.no_gps ? `，${r.no_gps} 张没有 GPS` : ''}）`)
  }

  const startVector = useCallback(async (path: string) => {
    if (!needLibrary()) return
    type R = { points_new: number; tracks_new: number; skipped: number; survey: string }
    const r = await task<R>(() => fw.importVector(path), '导入轨迹失败')
    if (!r) return
    await reload()
    setOpen(true)
    setSel({ kind: 'survey', uid: r.survey })
    props.onStatus(`已导入「${r.survey}」：${r.points_new} 个点位、${r.tracks_new} 条轨迹`)
  }, [needLibrary, task, reload, props])

  const pickAndImport = useCallback(async (what: 'ovobj' | 'ovobj-folder' | 'photos' | 'folder' | 'vector', pointUid: string | null = null) => {
    if (!needLibrary()) return
    const dt = desktop()
    if (!dt) {
      message.info('浏览器中无法打开文件选择框，请将文件拖入窗口。')
      return
    }
    if (what === 'ovobj') {
      const ps = await dt.pick_files('ovobj')
      if (ps?.length) startOvobj(ps)
    } else if (what === 'ovobj-folder') {
      const d = await dt.pick_folder()
      if (d) {
        const { paths } = await fw.listOvobjFolder(d)
        if (!paths.length) message.info('该文件夹（含子文件夹）中没有奥维收藏夹文件（.ovobj）。')
        else startOvobj(paths)
      }
    } else if (what === 'photos') {
      const ps = await dt.pick_files('photo')
      if (ps?.length) startPhotos(ps, pointUid)
    } else if (what === 'folder') {
      const d = await dt.pick_folder()
      if (d) {
        const { paths } = await fw.listFolder(d)
        if (!paths.length) message.info('该文件夹中没有照片。')
        else startPhotos(paths, pointUid)
      }
    } else {
      const p = await dt.pick_file('track')
      if (p) startVector(p)
    }
  }, [needLibrary, message, startOvobj, startPhotos, startVector])

  // ------------------------------------------------------------- 导出照片
  const doExport = async (opts: { folder: string; content: 'adjusted' | 'original'; template: string; info: boolean }) => {
    const req = exportReq!
    setExportReq(null)
    const r = await task<ExportResult>(() => fw.exportPhotos({ uids: req.uids, survey: req.survey, ...opts }), '导出照片失败')
    if (!r) return
    props.onStatus(`已导出 ${r.written} 张照片到 ${r.folder}` + (r.failed.length ? `，${r.failed.length} 张失败` : ''))
    if (r.failed.length) {
      modal.warning({ title: `${r.failed.length} 张照片导出失败`, content: <div className="pre">{r.failed.map((f) => f.reason).join('\n')}</div> })
    }
    if (r.written) desktop()?.reveal_in_folder(r.files[0])
  }

  // ------------------------------------------------------------- 对外的口子
  useImperativeHandle(props.handle, () => ({
    action: (id: string) => {
      const map: Record<string, () => void> = {
        'fw-panel': () => setOpen((o) => !o),
        'fw-toggle': () => setVisible((v) => !v),
        'fw-zoom': () => zoomTo('all'),
        'fw-import-ovobj': () => pickAndImport('ovobj'),
        'fw-import-ovobj-folder': () => pickAndImport('ovobj-folder'),
        'fw-import-photos': () => pickAndImport('photos'),
        'fw-import-folder': () => pickAndImport('folder'),
        'fw-import-vector': () => pickAndImport('vector'),
      }
      const fn = map[id]
      if (!fn) return false
      fn()
      return true
    },
    addPointAt: async ([lon, lat]: [number, number]) => {
      if (!needLibrary()) return
      try {
        const p = await fw.addPoint({ lon, lat })
        await reload()
        setOpen(true)
        setSel({ kind: 'point', uid: p.uid })
        props.onStatus('已新建野外点位，请填写名称。')
      } catch (e) {
        message.error((e as Error).message)
      }
    },
    claim: async (paths: string[]) => {
      if (!paths.length) return paths
      let c
      try {
        c = await fw.claim(paths)
      } catch {
        return paths
      }
      const mine = c.ovobj.length + c.photo.length + c.vector.length
      if (!mine) return paths
      // 带地理变换的 tif / png / jpg（配准过的图件）后端不认领，留给图层；
      // 没有地理变换的才按野外照片处理
      if (c.ovobj.length) await startOvobj(c.ovobj)
      else if (c.photo.length) startPhotos(c.photo, sel?.kind === 'point' ? sel.uid : null)
      else if (c.vector.length) await startVector(c.vector[0])
      return c.rest
    },
  }), [zoomTo, pickAndImport, startOvobj, startPhotos, startVector, sel, needLibrary, reload, message, props])

  // ------------------------------------------------------------- 照片浏览器里的改动
  const onPhotoChanged = async (ph: FwPhoto | null, removedUid?: string) => {
    if (!viewer) return
    let photos = viewer.photos
    if (removedUid) photos = photos.filter((p) => p.uid !== removedUid)
    if (ph) photos = photos.map((p) => (p.uid === ph.uid ? ph : p))
    if (!photos.length) setViewer(null)
    else setViewer({ photos, index: Math.min(viewer.index, photos.length - 1) })
    if (detail) await refreshPoint(detail.uid)
    else await reload()
  }

  if (!props.libraryOpen && !open) return null
  return (
    <>
      {open ? (
        <FieldPanel
          overview={overview}
          status={status}
          points={points}
          tracks={tracks}
          surveyColors={surveyColors}
          selection={sel}
          detail={detail}
          visible={visible}
          classes={props.classes}
          onSelect={selectFromList}
          onClose={() => setOpen(false)}
          onToggleVisible={() => setVisible((v) => !v)}
          onZoom={zoomTo}
          onImport={pickAndImport}
          onReload={reload}
          onRefreshPoint={refreshPoint}
          onOpenPhotos={(photos, index) => setViewer({ photos, index })}
          onImportPhotosTo={(uid) => pickAndImport('photos', uid)}
          onAddAttachments={addAttachments}
          onTranscribe={transcribe}
          onDropPhotos={(paths, uid) => startPhotos(paths, uid)}
          onTask={task}
          onStatus={props.onStatus}
          onSamplesChanged={props.onSamplesChanged}
          onExport={setExportReq}
          onTrackHover={setTrackCursor}
          moving={moving}
          onMove={startMove}
          onRestoreMove={restoreMove}
        />
      ) : null}
      {viewer ? (
        <PhotoViewer
          photos={viewer.photos}
          index={viewer.index}
          point={detail}
          points={points}
          status={status}
          onIndex={(i) => setViewer({ ...viewer, index: i })}
          onClose={() => setViewer(null)}
          onChanged={onPhotoChanged}
          onTask={task}
          onStatus={props.onStatus}
          onStatusRefresh={() => fw.status().then(setStatus)}
          onExport={setExportReq}
        />
      ) : null}
      <OvobjImportDialog preview={ovobjPreview} asr={status?.asr} onCancel={() => setOvobjPreview(null)} onOk={doOvobj} />
      <OvobjBatchDialog items={ovobjBatch} asr={status?.asr} onCancel={() => setOvobjBatch(null)} onOk={doOvobjBatch} />
      <PhotoImportDialog req={photoImport} points={points} onCancel={() => setPhotoImport(null)} onOk={doPhotos} />
      <ExportDialog req={exportReq} onCancel={() => setExportReq(null)} onOk={doExport} />
    </>
  )
}
