// 后端接口。所有请求走同源 /api，桌面版与开发服务器（vite 代理）一致。

export interface LibState {
  open: boolean
  name: string
  path: string
  version: number
  dirty: boolean
  has_deletions: boolean
  pending: string
  can_undo: boolean
  can_redo: boolean
  selected: number
  selected_classes: number
}

export type CheckState = 'none' | 'partial' | 'all'
export type ExportLevel = 'default' | 'merge' | 'split'

export interface TaskInfo {
  id: string
  title: string
  text: string
  status: 'running' | 'done' | 'error' | 'cancelled'
  done: number
  total: number
  cancellable: boolean
  cancel_requested: boolean
  result: unknown
  error: string
  elapsed: number
}

export interface ImportResult {
  reports: number
  files: number
  classes: number
  samples: number
  warnings: number
  noisy: boolean
  summary: string
}

export interface FieldInfo {
  name: string
  type: string
  total: number
  non_null: number
  unique: number
  samples: string[]
}

export interface AttrProfile {
  path: string
  name: string
  default_region: string
  info: Record<string, string | number | null>
  fields: FieldInfo[]
  suggested: string[]
}

export interface ExportOptions {
  drivers: string[]
  driver: string
  template: string
  buffer_radius: number
  max_depth: number
  units: { id: number; name: string }[]
  selected: number
  selected_classes: number
  point_classes: { id: number; path: string }[]
  last_dir: string
}

export interface ExportResult {
  out_dir: string
  files: number
  samples: number
  checked: string
  warnings: string[]
  summary: string
}

export interface LayerInfo {
  id: number
  /** 大影像的显示金字塔正在后台准备（0..1）；没有这一项表示用不着或已就绪 */
  preparing?: number | null
  kind: 'raster' | 'image' | 'vector' | 'drawn' | string
  kind_cn: string
  name: string
  visible: boolean
  opacity: number
  extent: number[] | null
  info: string[]
  error: string
  path: string
  // 影像
  band_count?: number
  bands?: number[]
  band_labels?: string[]
  stretch?: number[]
  has_color_table?: boolean
  color_table_size?: number
  use_color_table?: boolean
  wavelengths?: boolean
  wavelength_source?: string
  wavelength_presets?: Record<string, string>
  // 矢量
  color?: string
  fill?: boolean
  fill_alpha?: number
  line_width?: number
  feature_count?: number
  fields?: string[]
  color_field?: string
  color_map?: Record<string, string>
}

export interface LayerPatch {
  name?: string
  visible?: boolean
  opacity?: number
  bands?: number[]
  stretch?: number[]
  use_color_table?: boolean
  wavelength_file?: string
  color?: string
  fill?: boolean
  fill_alpha?: number
  line_width?: number
}

export interface AppInfo {
  title: string
  name: string
  name_en: string
  version: string
  profile: string
  modules: string[]
  module_info: { id: string; name: string; summary: string }[]
  format_version: string
  storage_epsg: number
  gdal: string
  data_dir: string
}

export interface BasemapKey {
  name: string
  label: string
  apply: string
  hint: string
  prefix: string
  set: boolean
  source: string
  masked: string
}

export interface BackupEntry {
  path: string
  name: string
  time: string
  size_mb: number
}

export interface SampleClass {
  id: number
  parent_id: number | null
  name: string
  region: string
  color: string
  kind: string // point | polygon | group | mixed | ''
  is_group: boolean
  description: string
  source: string
  attrs: Record<string, unknown>
  count: number
  total: number
  draft: boolean
  edited: boolean
  checked: CheckState
  export_level: ExportLevel
}

export interface FeatureProps {
  key: string
  class_id: number
  label: string
  kind: 'point' | 'polygon'
  draft: boolean
  source?: string
  attrs?: Record<string, unknown>
  bbox?: number[]
}

export interface Geometry {
  type: string
  coordinates: unknown
}

export interface Feature {
  type: 'Feature'
  id: string
  geometry: Geometry
  properties: FeatureProps
}

export interface FeatureCollection {
  type: 'FeatureCollection'
  version: number
  features: Feature[]
}

/** /api/features 的响应：整批，或相对上次快照（since）的增量。token 是这次的快照号。 */
export type FeaturesReply =
  | (FeatureCollection & { token: number })
  | { type: 'FeatureDelta'; version: number; token: number; since: number; upsert: Feature[]; remove: string[] }

export interface Basemap {
  id: number
  name: string
  group: string
  scheme: 'geodetic' | 'mercator'
  overlay: boolean
  min_zoom: number
  max_zoom: number
  usable: boolean
  attribution: string
}

export async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    try {
      const data = await res.json()
      if (typeof data.detail === 'string') msg = data.detail
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

export const get = <T,>(url: string) => call<T>('GET', url)
export const post = <T,>(url: string, body: unknown = {}) => call<T>('POST', url, body)

export const api = {
  state: () => get<LibState>('/api/state'),
  openLibrary: (path: string) => post<LibState>('/api/library/open', { path }),
  createLibrary: (path: string, name: string) =>
    post<LibState>('/api/library/create', { path, name }),
  extent: () => get<{ extent: number[] | null }>('/api/library/extent'),
  undo: () => post<LibState & { label: string }>('/api/undo'),
  redo: () => post<LibState & { label: string }>('/api/redo'),
  save: (autoCheck = false) =>
    post<LibState & { written: number; deleted: number }>('/api/save', { auto_check: autoCheck }),
  closeLibrary: () => post<LibState>('/api/library/close'),
  reloadLibrary: () => post<LibState>('/api/library/reload'),
  paste: (body: { ids: number[]; parent_id?: number | null; region?: string | null; target_id?: number | null }) =>
    post<{ message: string }>('/api/classes/paste', body),
  merge: (ids: number[], target_id: number) => post<{ message: string }>('/api/classes/merge', { ids, target_id }),
  restoreClass: (id: number) => post<{ ok: boolean; message: string }>(`/api/classes/${id}/restore`),
  editedClasses: () => get<{ id: number; path: string; origin: string }[]>('/api/classes/edited'),
  restoreAll: () => post<{ restored: number; removed: number; labels: number }>('/api/classes/restore-all'),
  renameRegion: (region: string, name: string) => post<{ message: string }>('/api/regions/rename', { region, name }),
  deleteRegion: (region: string) => post<{ message: string }>('/api/regions/delete', { region }),
  newRegion: (region: string, name: string) => post<{ id: number; message: string }>('/api/regions/new', { region, name }),
  regionCount: (region: string) => get<{ classes: number; samples: number }>(`/api/regions/${encodeURIComponent(region)}/count`),
  appInfo: () => get<AppInfo>('/api/app'),
  basemapKeys: () => get<{ keys: BasemapKey[]; file: string }>('/api/basemap/keys'),
  setBasemapKey: (name: string, value: string) => post<{ keys: BasemapKey[]; file: string }>('/api/basemap/keys', { name, value }),
  cacheInfo: () => get<{ size_mb: number; limit_mb: number; dir: string }>('/api/basemap/cache'),
  clearCache: () => post<{ size_mb: number }>('/api/basemap/cache/clear'),
  retryTiles: () => post<{ ok: boolean }>('/api/basemap/retry'),
  crsMenu: (lon?: number, lat?: number) =>
    get<{ epsg: number; label: string; recommended: boolean }[]>(`/api/crs/menu${lon !== undefined ? `?lon=${lon}&lat=${lat}` : ''}`),
  crsDef: (epsg: number) => get<{ epsg: number; label: string; projected: boolean; proj4: string; area: number[] | null }>(`/api/crs/${epsg}`),
  uiState: () => get<Record<string, unknown>>('/api/ui-state'),
  saveUiState: (state: Record<string, unknown>) => post<{ saved: boolean }>('/api/ui-state', { state }),
  recent: () => get<{ path: string; name: string; folder: string; exists: boolean }[]>('/api/library/recent'),
  clearRecent: () => post<[]>('/api/library/recent/clear'),
  libraryDefaults: () =>
    get<{ backup_dir: string; backup_name: string; save_as_dir: string; save_as_name: string; library_dir: string }>(
      '/api/library/defaults'),
  copyLibrary: (path: string, open: boolean) => post<TaskInfo>('/api/library/copy', { path, open }),
  backups: () => get<BackupEntry[]>('/api/library/backups'),
  probe: (path: string) =>
    post<{ path: string; classes: number; samples: number; current: { path: string; classes: number; samples: number } }>(
      '/api/library/probe', { path }),
  restore: (path: string) => post<TaskInfo>('/api/library/restore', { path }),

  selection: () => get<{ keys: string[] }>('/api/selection'),
  setSelection: (op: 'set' | 'add' | 'remove' | 'all' | 'none' | 'invert', class_ids?: number[], keys?: string[]) =>
    post<{ keys: string[] }>('/api/selection', { op, class_ids, keys }),
  setExportLevel: (id: number, mode: ExportLevel) => post<{ ok: boolean }>(`/api/classes/${id}/export-level`, { mode }),
  clearExportLevels: () => post<{ ok: boolean }>('/api/export-levels/clear'),

  task: (id: string) => get<TaskInfo>(`/api/tasks/${id}`),
  cancelTask: (id: string) => post<TaskInfo>(`/api/tasks/${id}/cancel`),
  importDir: (path: string, region: string) => post<TaskInfo>('/api/import/dir', { path, region }),
  importRoot: (path: string) => post<TaskInfo>('/api/import/root', { path }),
  importFile: (path: string, region: string) => post<TaskInfo>('/api/import/file', { path, region }),
  importProfile: (path: string) => post<TaskInfo>('/api/import/profile', { path }),
  attrPreview: (path: string, class_fields: string[]) =>
    post<{ leaves: number; groups: number; examples: string[] }>('/api/import/attr/preview', { path, class_fields }),
  importAttr: (body: { path: string; class_fields: string[]; region: string; name_field: string; code_field: string; sample_type: string }) =>
    post<TaskInfo>('/api/import/attr', body),
  exportOptions: () => get<ExportOptions>('/api/export/options'),
  exportPreview: (level: string) =>
    post<{ units: number; files: number; samples: number; notes: string[] }>('/api/export/preview', { level }),
  exportRun: (body: {
    out_dir: string; driver: string; template: string; target_crs: string; level: string
    restore_attrs: boolean; buffer_radius: number; buffer_class_ids: number[] | null
  }) => post<TaskInfo>('/api/export', body),
  discard: () => post<LibState>('/api/discard'),

  classes: () => get<SampleClass[]>('/api/classes'),
  addClass: (body: { name: string; region: string; parent_id: number | null; group: boolean }) =>
    post<{ id: number }>('/api/classes', body),
  patchClass: (id: number, body: { name?: string; color?: string; description?: string }) =>
    call<{ ok: boolean }>('PATCH', `/api/classes/${id}`, body),
  moveClass: (id: number, body: { parent_id: number | null; before_id?: number | null; region?: string }) =>
    post<{ ok: boolean }>(`/api/classes/${id}/move`, body),
  promoteClass: (leafId: number, otherId: number, name = '') =>
    post<{ id: number }>(`/api/classes/${leafId}/promote`, { other_id: otherId, name }),
  deleteClasses: (ids: number[]) =>
    post<{ classes: number; features: number }>('/api/classes/delete', { ids }),

  features: (since?: number) => get<FeaturesReply>('/api/features' + (since ? `?since=${since}` : '')),
  feature: (key: string) => get<Feature>(`/api/features/${encodeURIComponent(key)}`),
  addFeature: (class_id: number, geometry: Geometry, label = '') =>
    post<{ key: string }>('/api/features', { class_id, geometry, label }),
  setGeometry: (key: string, geometry: Geometry) =>
    call<{ ok: boolean }>('PUT', `/api/features/${encodeURIComponent(key)}/geometry`, { geometry }),
  setLabel: (key: string, label: string) =>
    call<{ ok: boolean }>('PATCH', `/api/features/${encodeURIComponent(key)}`, { label }),
  deleteFeatures: (keys: string[]) => post<{ deleted: number }>('/api/features/delete', { keys }),
  moveFeatures: (keys: string[], class_id: number) =>
    post<{ moved: number }>('/api/features/move', { keys, class_id }),

  basemaps: () => get<Basemap[]>('/api/basemaps'),

  layers: () => get<LayerInfo[]>('/api/layers'),
  addLayers: (paths: string[]) => post<{ library?: string; task?: TaskInfo }>('/api/layers/add', { paths }),
  patchLayer: (id: number, body: LayerPatch) => call<LayerInfo>('PATCH', `/api/layers/${id}`, body),
  removeLayer: (id: number) => call<{ ok: boolean }>('DELETE', `/api/layers/${id}`),
  layerGeojson: (id: number) =>
    get<{ type: 'FeatureCollection'; features: unknown[]; simplified: boolean; vertices: number }>(`/api/layers/${id}/geojson`),
  layerFeature: (id: number, fid: number) =>
    get<{ fid: number; kind: string; attrs: Record<string, unknown> }>(`/api/layers/${id}/features/${fid}`),
  layerValues: (id: number, field: string) =>
    get<{ field: string; values: { value: string; color: string }[]; palette_size: number }>(
      `/api/layers/${id}/values?field=${encodeURIComponent(field)}`),
  setUniqueColors: (id: number, field: string, colors?: Record<string, string>) =>
    post<LayerInfo>(`/api/layers/${id}/unique-colors`, { field, colors }),
  legendColors: (id: number, field: string) =>
    post<{ colors: Record<string, string>; unknown: string[]; total: number;
      hits: Record<string, { matched: string; rule: string; path: string }> }>(`/api/layers/${id}/legend-colors`, { field }),
  newDrawnLayer: () => post<LayerInfo>('/api/layers/drawn'),
  addRoiPolygon: (id: number, rings: number[][][]) => post<LayerInfo>(`/api/layers/${id}/polygons`, { rings }),
  clearDrawn: (id: number) => post<LayerInfo>(`/api/layers/${id}/clear`),
  selectByLayer: (id: number, mode: string, classIds: number[] | null, add: boolean) =>
    post<{ hits: number; selected: number }>(`/api/layers/${id}/select-samples`, { mode, class_ids: classIds, add }),
  roiAgain: (mode: string, classIds: number[] | null, add: boolean) =>
    post<{ hits: number; selected: number; name: string }>('/api/roi/apply', { mode, class_ids: classIds, add }),
  extentLayer: (id: number, validOnly: boolean) => post<{ task: TaskInfo }>(`/api/layers/${id}/extent-layer`, { valid_only: validOnly }),
  saveDrawn: (id: number, path: string) => post<{ path: string; count: number }>(`/api/layers/${id}/save`, { path }),
  layerSpecs: () => get<Record<string, unknown>[]>('/api/layers/specs'),
  restoreLayers: (specs: Record<string, unknown>[]) =>
    post<{ mapping: Record<string, LayerInfo>; failed: string[] }>('/api/layers/restore', { specs }),
}
