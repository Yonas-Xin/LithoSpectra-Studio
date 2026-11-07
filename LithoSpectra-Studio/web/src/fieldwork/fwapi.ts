// 野外记录模块的后端接口（/api/fw/*）。与样本的 api.ts 分开：模块不在发行版里时，
// 这个文件连同整个 fieldwork 目录一起删掉，外壳照常编译。

import { call, get, post } from '../api'
import type { TaskInfo } from '../api'

export interface FwPoint {
  uid: string
  name: string | null
  note: string | null
  category: string | null
  survey: string | null
  elevation: number | null
  color: string | null
  icon: number | null
  attrs: Record<string, string | number>
  source: string | null
  source_uid: string | null
  created: string | null
  modified: string | null
  sample_key: string | null
  lon: number
  lat: number
  n_photos: number
  n_spectra: number
  /** 照片以外的附件（录音、视频、文档）个数 */
  n_attachments?: number
}

export interface FwEdit {
  op: string
  time: string
  file: string
  [k: string]: unknown
}

export interface FwPhoto {
  uid: string
  point_uid: string | null
  sort: number | null
  file: string
  original: string
  sha256: string
  orig_name: string | null
  width: number | null
  height: number | null
  taken: string | null
  altitude: number | null
  azimuth: number | null
  azimuth_ref: string | null
  azimuth_src: string | null
  caption: string | null
  ovital_mark: number | null
  /** 像素修补的步骤（去奥维水印、框选修补、编辑器），按先后排 */
  edits: FwEdit[]
  /** 地质水印参数；只记参数，看图与导出时现画 */
  overlay: GeomarkSpec | null
  /** 有没有任何调整（像素修补或地质水印） */
  adjusted: boolean
  /** 原片 EXIF 里本来就有的照片信息 */
  orig_meta: Record<string, unknown>
  /** 锁定（来自原片、不可修改）的信息项：position / altitude / azimuth / taken */
  locked: string[]
  lon: number | null
  lat: number | null
}

export interface FwEntry {
  id: string
  name: string
  /** 中文名；没有时为空 */
  cn: string
  class: string
  /** 分类路径的中文，如「矿物 / 硅酸盐 / 层状硅酸盐」 */
  class_cn: string
  library: string
  source: string
  sample: string
  instrument: string
  wl_lo: number
  wl_hi: number
}

export interface FwSpectrum {
  uid: string
  point_uid: string
  sort: number | null
  entry_id: string
  label: string | null
  note: string | null
  created: string | null
  entry?: FwEntry | null
}

/** 照片以外的附件：录音、视频、文档。原文件只存不改 */
export interface FwAttachment {
  uid: string
  point_uid: string | null
  track_uid: string | null
  sort: number | null
  kind: 'audio' | 'video' | 'file' | 'photo'
  file: string
  sha256: string
  orig_name: string | null
  ext: string | null
  size: number | null
  taken: string | null
  caption: string | null
  /** 录音转写出的文字 */
  transcript: string | null
  source: string | null
  created: string | null
  /** 媒体文件夹里的完整路径 */
  path: string
  missing: boolean
  /** 浏览器放不了、后端能转成别的格式再放（奥维录音转 WAV） */
  transcode: 'wav' | null
}

export interface FwPointDetail extends FwPoint {
  photos: FwPhoto[]
  spectra: FwSpectrum[]
  attachments: FwAttachment[]
}

export interface FwTrack {
  uid: string
  name: string | null
  note: string | null
  survey: string | null
  color: string | null
  start_time: string | null
  end_time: string | null
  n_points: number
  length_m: number
  elev_min: number | null
  elev_max: number | null
  source: string | null
  /** [经度, 纬度, 高程, Unix 秒] */
  coords?: [number, number, number, number][]
}

export interface FwSurvey {
  survey: string
  points: number
  tracks: number
  start: string
  end: string
}

export interface FwOverview {
  stats: { fw_point: number; fw_track: number; fw_photo: number; fw_spectrum: number; fw_attachment?: number }
  surveys: FwSurvey[]
  extent: number[] | null
  media_dir: string
  /** 野外记录所在的样本库（当前打开的那个） */
  library: string
}

export interface FwStatus {
  ocr: boolean
  inpaint: { onnxruntime: boolean; opencv: boolean; model: boolean; model_path: string; model_size: number }
  spectra: boolean
  /** 能不能解码奥维录音（Speex） */
  speex?: boolean
  /** 录音转文字：引擎装没装、模型下没下 */
  asr?: { engine: boolean; model: boolean; model_path: string; model_size: number }
}

export interface TranscribeResult {
  done: { uid: string; name: string; text: string; point_uid: string | null; appended: boolean }[]
  failed: { uid: string; name: string; reason: string }[]
  appended: number
  cancelled: boolean
}

/** 追加到备注里的一行，与后端 asr.note_line 同一格式 */
export function noteLine(text: string, taken: string | null | undefined): string {
  const hhmm = (taken ?? '').slice(11, 16)
  return `［录音${hhmm ? ' ' + hhmm : ''}］${text}`
}

export interface OvitalMark {
  found: boolean
  items: { text: string; box: number[] }[]
  box: number[] | null
  text: string
  lon: number | null
  lat: number | null
  altitude: number | null
  azimuth: number | null
  direction: string
  azimuth_ok: boolean
  notes: string[]
}

export interface PhotoExif {
  width: number
  height: number
  taken: string
  lon: number | null
  lat: number | null
  altitude: number | null
  azimuth: number | null
  azimuth_ref: string
  make: string
  model: string
}

export interface OvobjPreview {
  path: string
  name: string
  survey: string
  placemarks: number
  tracks: number
  photos: number
  /** 录音、视频、其他文件的个数 */
  audio: number
  video: number
  files: number
  /** 没有挂在任何标签、轨迹上的附件 */
  orphans: number
  folders: string[]
  track_points: number
  warnings: string[]
  imported_before: { time: string; summary: string } | null
  existing: number
  start?: string
  end?: string
}

/** 批量预览里读不了的那份文件 */
export interface OvobjPreviewError {
  path: string
  name: string
  error: string
}

export interface OvobjImportResult {
  points_new: number
  points_updated: number
  tracks_new: number
  photos_new: number
  attachments_new: number
  audio_new: number
  video_new: number
  files_new: number
  loose: number
  warnings: string[]
  survey: string
  source: string
}

export interface OvobjBatchResult extends Omit<OvobjImportResult, 'warnings' | 'survey' | 'source'> {
  files: OvobjImportResult[]
  failed: { path: string; name: string; reason: string }[]
  surveys: string[]
  cancelled: boolean
}

export interface Curve extends FwEntry {
  x: number[]
  y: (number | null)[]
}

export type PhotoImportMode = 'point' | 'nearest' | 'new' | 'none'

export type GeomarkType = 'north' | 'scale' | 'view' | 'text'

/** 地质水印的一个组件。x / y 是包围框左上角相对照片宽高的比例，size 是相对短边的比例。 */
export interface GeomarkItem {
  id: string
  type: GeomarkType
  x: number
  y: number
  size: number
  /** 指北针：arrow 箭头 / rose 罗盘；比例尺：bar 分段条 / line 线段 */
  design?: string
  /** 指北针顺时针旋转的角度（auto 为假时用） */
  rotation?: number
  /** 指北针按照片方位角自动旋转 */
  auto?: boolean
  /** 指北针下方标一行镜向，格式同镜向组件 */
  label?: boolean
  label_format?: 'bearing' | 'azimuth'
  /** 按离最近两条边的距离摆放（以照片短边为单位）；有它时优先于 x / y */
  anchor?: GeomarkAnchor
  /** 比例尺：原图每毫米多少像素（标定得到） */
  px_per_mm?: number
  length?: number
  unit?: string
  segments?: number
  /** 镜向 */
  azimuth?: number | null
  format?: 'bearing' | 'azimuth'
  /** 文字 */
  lines?: string[]
}

export interface GeomarkAnchor {
  h: 'left' | 'right'
  dx: number
  v: 'top' | 'bottom'
  dy: number
}

/** 各类组件上次的样式、大小与摆放，新加的组件照此摆 */
export interface GeomarkDefaults {
  lang?: 'en' | 'zh'
  theme?: 'light' | 'dark'
  panel?: boolean
  items?: Partial<Record<GeomarkType, Partial<GeomarkItem>>>
}

export interface GeomarkSpec {
  version: 2
  lang: 'en' | 'zh'
  theme: 'light' | 'dark'
  panel: boolean
  items: GeomarkItem[]
}

export interface GeomarkPreview {
  image: string
  boxes: Record<string, [number, number, number, number]>
  width: number
  height: number
}

const enc = encodeURIComponent

/** 取哪一版：调整后（底图 + 地质水印）、底图（像素修补的结果）、原片 */
export type PhotoVariant = 'adjusted' | 'base' | 'original'

/** 照片地址。``v`` 跟着底图与地质水印变，调整过之后浏览器不会拿旧缓存。 */
export function photoUrl(p: Pick<FwPhoto, 'uid' | 'file'> & { overlay?: GeomarkSpec | null; original?: string; azimuth?: number | null },
  size = 0, variant: PhotoVariant = 'adjusted'): string {
  const file = variant === 'original' && p.original ? p.original : p.file
  let v = file.split('/').pop()?.slice(0, 12) ?? ''
  if (variant === 'adjusted' && p.overlay) v += '-' + hash(JSON.stringify([p.overlay, p.azimuth ?? null]))
  return `/api/fw/photos/${p.uid}/image?size=${size}&variant=${variant}&v=${v}`
}

function hash(s: string): string {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0
  return (h >>> 0).toString(36)
}

export interface ExportResult {
  folder: string
  written: number
  files: string[]
  failed: { uid: string; reason: string }[]
}

export const fw = {
  status: () => get<FwStatus>('/api/fw/status'),
  overview: () => get<FwOverview>('/api/fw/overview'),
  renameSurvey: (old: string, name: string) => post<{ changed: number }>('/api/fw/surveys/rename', { old, new: name }),
  deleteSurvey: (survey: string, keepPhotos = false) =>
    post<{ points: number; tracks: number; photos: number }>('/api/fw/surveys/delete', { survey, keep_photos: keepPhotos }),

  points: (survey = '') => get<FwPoint[]>(`/api/fw/points?survey=${enc(survey)}`),
  point: (uid: string) => get<FwPointDetail>(`/api/fw/points/${uid}`),
  addPoint: (body: { lon: number; lat: number; name?: string; survey?: string }) => post<FwPointDetail>('/api/fw/points', body),
  patchPoint: (uid: string, changes: Record<string, unknown>) => call<FwPointDetail>('PATCH', `/api/fw/points/${uid}`, { changes }),
  deletePoint: (uid: string, keepPhotos = false) => call<{ deleted: boolean }>('DELETE', `/api/fw/points/${uid}?keep_photos=${keepPhotos}`),
  toSample: (uid: string, classId: number) => post<{ key: string }>(`/api/fw/points/${uid}/to-sample`, { class_id: classId }),

  tracks: (coords = true) => get<FwTrack[]>(`/api/fw/tracks?coords=${coords}`),
  patchTrack: (uid: string, changes: Record<string, unknown>) => call<FwTrack>('PATCH', `/api/fw/tracks/${uid}`, { changes }),
  deleteTrack: (uid: string) => call<{ deleted: boolean }>('DELETE', `/api/fw/tracks/${uid}`),

  photos: (pointUid = '', unattached = false) => get<FwPhoto[]>(`/api/fw/photos?point_uid=${enc(pointUid)}&unattached=${unattached}`),
  photo: (uid: string) => get<FwPhoto>(`/api/fw/photos/${uid}`),
  photoInfo: (uid: string) => get<PhotoExif>(`/api/fw/photos/${uid}/info`),
  patchPhoto: (uid: string, changes: Record<string, unknown>) => call<FwPhoto>('PATCH', `/api/fw/photos/${uid}`, { changes }),
  movePhoto: (uid: string, pointUid: string | null, index?: number) => post<FwPhoto>(`/api/fw/photos/${uid}/move`, { point_uid: pointUid, index }),
  deletePhoto: (uid: string) => call<{ deleted: boolean }>('DELETE', `/api/fw/photos/${uid}`),
  /** 清除全部调整，回到原片 */
  revertPhoto: (uid: string) => post<FwPhoto>(`/api/fw/photos/${uid}/revert`),
  /** 撤销最后一步像素修补 */
  undoStep: (uid: string) => post<FwPhoto>(`/api/fw/photos/${uid}/undo`),
  removeGeomark: (uid: string) => call<FwPhoto>('DELETE', `/api/fw/photos/${uid}/geomark`),
  exportPhotos: (body: { uids?: string[]; survey?: string; folder: string; content: 'adjusted' | 'original'; template: string; info: boolean }) =>
    post<TaskInfo>('/api/fw/photos/export', body),
  exportTemplate: () => get<{ default: string; fields: Record<string, string> }>('/api/fw/photos/export/template'),

  analyseMark: (uid: string) => post<{ mark: OvitalMark; exif: PhotoExif }>(`/api/fw/photos/${uid}/ovmark/analyse`),
  removeMark: (uid: string, azimuth?: number | null) => post<TaskInfo>(`/api/fw/photos/${uid}/ovmark/remove`, { azimuth: azimuth ?? null }),
  removeMarkBatch: (uids: string[]) => post<TaskInfo>('/api/fw/photos/ovmark/batch', { uids }),
  inpaintRects: (uid: string, rects: number[][]) => post<TaskInfo>(`/api/fw/photos/${uid}/inpaint`, { rects }),
  downloadModel: () => post<TaskInfo>('/api/fw/model/download'),
  geomark: (uid: string, spec: GeomarkSpec) => post<FwPhoto>(`/api/fw/photos/${uid}/geomark`, { spec }),
  geomarkDefaults: () => get<GeomarkDefaults>('/api/fw/geomark/defaults'),
  setGeomarkDefaults: (d: GeomarkDefaults) => call<GeomarkDefaults>('PUT', '/api/fw/geomark/defaults', d),
  geomarkPreview: (uid: string, spec: GeomarkSpec, width: number, azimuth: number | null) =>
    post<GeomarkPreview>(`/api/fw/photos/${uid}/geomark/preview`, { spec, width, azimuth }),
  editPhoto: (uid: string, spec: Record<string, unknown>) => post<TaskInfo>(`/api/fw/photos/${uid}/edit`, { spec }),
  replace: (uid: string, dataUrl: string, op = 'edit') => post<FwPhoto>(`/api/fw/photos/${uid}/replace`, { data_url: dataUrl, op }),

  searchSpectra: (q: string, library = '') => get<FwEntry[]>(`/api/fw/spectra/search?q=${enc(q)}&library=${enc(library)}&limit=60`),
  linkSpectrum: (pointUid: string, entryId: string) => post<FwSpectrum>(`/api/fw/points/${pointUid}/spectra`, { entry_id: entryId }),
  patchSpectrum: (uid: string, changes: Record<string, unknown>) => call<FwSpectrum>('PATCH', `/api/fw/spectra/${uid}`, { changes }),
  unlinkSpectrum: (uid: string) => call<{ deleted: boolean }>('DELETE', `/api/fw/spectra/${uid}`),
  curve: (entryId: string) => get<Curve>(`/api/fw/spectra/curve?entry_id=${enc(entryId)}`),

  claim: (paths: string[]) => post<{ ovobj: string[]; photo: string[]; vector: string[]; rest: string[] }>('/api/fw/claim', { paths }),
  previewOvobj: (path: string) => post<OvobjPreview>('/api/fw/import/ovobj/preview', { path }),
  importOvobj: (path: string, survey: string) => post<TaskInfo>('/api/fw/import/ovobj', { path, survey }),
  previewOvobjBatch: (paths: string[]) =>
    post<{ items: (OvobjPreview | OvobjPreviewError)[] }>('/api/fw/import/ovobj/preview-batch', { paths }),
  importOvobjBatch: (items: { path: string; survey: string }[]) => post<TaskInfo>('/api/fw/import/ovobj-batch', { items }),
  listOvobjFolder: (path: string) => post<{ paths: string[] }>('/api/fw/import/ovobj-folder', { path }),

  attachments: (q: { point?: string; track?: string; unattached?: boolean }) =>
    get<FwAttachment[]>(`/api/fw/attachments?point_uid=${enc(q.point ?? '')}&track_uid=${enc(q.track ?? '')}&unattached=${!!q.unattached}`),
  openAttachment: (uid: string) => post<{ path: string }>(`/api/fw/attachments/${uid}/open`),
  patchAttachment: (uid: string, changes: Record<string, unknown>) => call<FwAttachment>('PATCH', `/api/fw/attachments/${uid}`, { changes }),
  moveAttachment: (uid: string, pointUid: string | null) => post<FwAttachment>(`/api/fw/attachments/${uid}/move`, { point_uid: pointUid }),
  deleteAttachment: (uid: string) => call<{ deleted: boolean }>('DELETE', `/api/fw/attachments/${uid}`),
  importAttachments: (paths: string[], pointUid: string | null) => post<TaskInfo>('/api/fw/import/attachments', { paths, point_uid: pointUid }),
  asrDownload: () => post<TaskInfo>('/api/fw/asr/download'),
  transcribe: (body: { uids?: string[]; point_uid?: string; surveys?: string[]; append?: boolean; redo?: boolean }) =>
    post<TaskInfo>('/api/fw/attachments/transcribe', body),
  importPhotos: (paths: string[], mode: PhotoImportMode, pointUid: string | null, maxDist: number, survey = '') =>
    post<TaskInfo>('/api/fw/import/photos', { paths, mode, point_uid: pointUid, max_dist: maxDist, survey }),
  importVector: (path: string, survey = '') => post<TaskInfo>('/api/fw/import/vector', { path, survey }),
  listFolder: (path: string) => post<{ paths: string[] }>('/api/fw/import/folder', { path }),
}

/** 罗盘八方位。与后端 geomark.direction_name 同一规则。 */
export function directionName(az: number): string {
  return ['北', '东北', '东', '东南', '南', '西南', '西', '西北'][Math.floor((((az % 360) + 360) % 360 + 22.5) / 45) % 8]
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return ''
  return iso.replace('T', ' ').replace(/([+-]\d\d:\d\d|Z)$/, '').slice(0, 16)
}

/** 附件原文件的地址（页面里直接播放录音用） */
export function attachmentUrl(a: Pick<FwAttachment, 'uid' | 'transcode'>): string {
  return `/api/fw/attachments/${a.uid}/file${a.transcode ? `?as=${a.transcode}` : ''}`
}

export function fmtSize(bytes: number | null | undefined): string {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function fmtLen(m: number): string {
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`
}
