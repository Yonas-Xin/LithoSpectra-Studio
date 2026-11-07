// 深度学习模块的接口与类型。计算在外部 Python 环境的 Worker 里，这里只管任务目录的读写。

import { call, get, post } from '../api'
import type { LayerInfo } from '../api'

export interface EnvInfo {
  name: string
  python: string
  kind: string
  checked: boolean
  ok: boolean
  device: string
  selected: boolean
}

export interface DoctorCheck {
  name: string
  ok: boolean
  detail: string
  hint: string
  required: boolean
}

export interface DoctorReport {
  ok: boolean
  checks: DoctorCheck[]
  describe?: string
  device?: string
  platform?: string
}

export interface DlStatus {
  available: boolean
  python: string
  device: string
  jobs_root: string
  running: string | null
  plugin_dir: string
}

export interface ParamChoice {
  value: string
  label: string
}

export interface ParamDecl {
  name: string
  label: string
  default: unknown
  kind: 'str' | 'int' | 'float' | 'bool' | 'choice' | 'path'
  help: string
  choices: ParamChoice[]
  minimum: number | null
  maximum: number | null
  path_mode: string
  extensions: string[]
  advanced: boolean
  optional: boolean
  visible_when: { param: string; values: unknown[] }[]
}

export interface TaskDecl {
  name: string
  title: string
  summary: string
  params: ParamDecl[]
}

export interface BackboneDecl {
  name: string
  input_kind: 'spectrum' | 'image' | 'cube'
  min_patch: number
  speed: 'fast' | 'medium' | 'slow' | 'very_slow'
  speed_label: string
  default_head: string
  summary: string
  source: string
}

export interface Capabilities {
  protocol: number
  worker_version: string
  tasks: TaskDecl[]
  backbones: BackboneDecl[]
  plugin_dir: string
  plugin_errors: string[]
}

export interface ClassRow {
  label: number
  name: string
  code?: string
  path?: string
  color?: string
  class_ids?: number[]
}

export interface Dataset {
  id: string
  path: string
  name: string
  created: number
  library: string
  missing: boolean
  error?: string
  count: number
  test_count: number
  bands: number
  patch: number
  dtype: string
  complete: boolean
  class_table: ClassRow[]
  label_counts: number[]
  has_keys: boolean
  source_image: string
  wavelengths: number[]
  size_mb: number
  stale: boolean | null
  warnings: string[]
  train: string
  test: string
}

export interface ModelInfo {
  path: string
  name: string
  task: string
  backbone: string
  input_kind: string
  bands: number
  patch_size: number
  classes: ClassRow[]
  metrics: {
    select_metric?: string
    best_epoch?: number
    best_score?: number
    knn_acc?: number | null
    val?: Record<string, number>
    test?: Record<string, number> | null
  }
  method?: string
  feature_dim?: number
  pretrained?: { name: string; method: string; transfer: string } | null
  created: number
  partial: boolean
  job: string
  dataset: { train?: string; source_image?: string; count?: number }
  wavelengths: boolean
  external: boolean
  stale: boolean
}

export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'

export interface EmbeddingPoint {
  x: number
  y: number
  l: number
  s: number
  m: number
  t: number
  i: number
  k?: string
  ll?: [number, number]
}

export interface EmbeddingData {
  summary: {
    count: number
    explained: number[]
    knn_k: number
    mean_suspicion: number
    per_class: { label: number; name: string; count: number; suspicion: number }[]
    classes: ClassRow[]
    model: string
    dataset: string
  }
  points: EmbeddingPoint[]
  in_library: number
}

export interface EpochRecord {
  epoch: number
  epochs: number
  lr: number
  train_loss: number
  train_acc: number
  val_loss?: number
  val_oa?: number
  val_aa?: number
  val_kappa?: number
  val_macro_f1?: number
  knn_acc?: number
  seconds: number
  best: boolean
}

export interface Evaluation {
  split: 'val' | 'test' | 'train'
  epoch: number
  confusion: number[][]
  oa: number
  aa: number
  kappa: number
  macro_f1: number
  per_class: { label: number; precision: number; recall: number; f1: number; support: number }[]
  classes?: ClassRow[]
}

export interface Artifact {
  kind: 'model' | 'class' | 'confidence' | 'probabilities' | string
  path: string
}

export interface JobSummary {
  id: string
  task: string
  title: string
  created: number
  params: Record<string, unknown>
  state: JobState
  started: number | null
  finished: number | null
  message: string
  phase: string
  progress: { fraction: number; text: string; eta?: number | null }
  artifacts: Artifact[]
  estimate: { epoch_seconds?: number; total_seconds?: number; predict_pixels_per_s?: number; train_samples_per_s?: number } | null
  dir: string
  cancel_requested: boolean
  last_epoch?: EpochRecord
  best_epoch?: EpochRecord | null
  test?: { oa: number; aa: number; kappa: number; macro_f1: number }
}

export interface JobDetail extends JobSummary {
  epochs: EpochRecord[]
  evaluations: Record<string, Evaluation>
  logs: { t: number; level: 'info' | 'warn' | 'error' | 'ok'; text: string }[]
  predict_init: { rows: number; cols: number; blocks: number; classes: ClassRow[]; outputs: Record<string, string> } | null
  blocks: { done: number; total: number; eta: number; elapsed: number } | null
  final: { event: string; message: string } | null
  seq: number
  /** 训练中的这一轮：已完成的批数、总批数、到目前为止的平均损失 */
  batch: { epoch: number; epochs: number; done: number; total: number; loss?: number } | null
}

export interface EvalSample {
  index: number
  label: number
  pred: number
  confidence: number
  pixel?: [number, number]
  lonlat?: [number, number]
  key?: string
  in_library?: boolean
}

export const dl = {
  status: () => get<DlStatus>('/api/dl/status'),
  envs: () => get<EnvInfo[]>('/api/dl/envs'),
  doctor: (python: string, refresh = false) => post<DoctorReport>('/api/dl/envs/doctor', { python, refresh }),
  select: (python: string) => post<{ python: string; report: DoctorReport }>('/api/dl/envs/select', { python }),
  caps: (refresh = false) => get<Capabilities>('/api/dl/capabilities' + (refresh ? '?refresh=true' : '')),
  setJobsRoot: (path: string) => post<{ jobs_root: string }>('/api/dl/jobs-root', { path }),

  datasets: () => get<Dataset[]>('/api/dl/datasets'),
  addDataset: (path: string, name = '') => post<Dataset>('/api/dl/datasets', { path, name }),
  renameDataset: (id: string, name: string) => call<{ ok: boolean }>('PATCH', `/api/dl/datasets/${id}`, { name }),
  removeDataset: (id: string) => call<{ ok: boolean }>('DELETE', `/api/dl/datasets/${id}`),

  models: () => get<ModelInfo[]>('/api/dl/models'),
  addModel: (path: string) => post<{ ok: boolean }>('/api/dl/models', { path }),
  forgetModel: (path: string) => call<{ ok: boolean }>('DELETE', `/api/dl/models?path=${encodeURIComponent(path)}`),
  compat: (path: string, layerId: number) =>
    get<{ ok: boolean; level: 'ok' | 'warn' | 'error'; text: string }>(
      `/api/dl/models/compat?path=${encodeURIComponent(path)}&layer_id=${layerId}`),
  rasters: () => get<{ id: number; name: string; path: string; bands: number }[]>('/api/dl/rasters'),

  jobs: () => get<JobSummary[]>('/api/dl/jobs'),
  job: (id: string) => get<JobDetail>(`/api/dl/jobs/${id}`),
  newJob: (task: string, params: Record<string, unknown>, title = '') =>
    post<JobSummary>('/api/dl/jobs', { task, params, title }),
  cancel: (id: string) => post<JobSummary>(`/api/dl/jobs/${id}/cancel`),
  resume: (id: string) => post<JobSummary>(`/api/dl/jobs/${id}/resume`),
  remove: (id: string) => call<{ ok: boolean }>('DELETE', `/api/dl/jobs/${id}`),
  samples: (id: string, q: { split: string; true_label?: number; pred_label?: number; errors_only?: boolean; limit?: number }) => {
    const p = new URLSearchParams({ split: q.split, errors_only: String(q.errors_only ?? true), limit: String(q.limit ?? 300) })
    if (q.true_label !== undefined) p.set('true_label', String(q.true_label))
    if (q.pred_label !== undefined) p.set('pred_label', String(q.pred_label))
    return get<{ total: number; rows: EvalSample[]; split: string }>(`/api/dl/jobs/${id}/samples?${p}`)
  },
  embedding: (id: string) => get<EmbeddingData>(`/api/dl/jobs/${id}/embedding`),
  showLayer: (id: string) => post<{ layer: LayerInfo; live: boolean }>(`/api/dl/jobs/${id}/layer`),
  lowConfidence: (id: string, threshold: number, min_pixels = 4) =>
    post<{ layer: LayerInfo; features: number; path: string }>(`/api/dl/jobs/${id}/low-confidence`, { threshold, min_pixels }),
  pixel: (layerId: number, lon: number, lat: number) =>
    get<{ inside: boolean; value?: number; class?: string; color?: string; confidence?: number; similarity?: number; background?: boolean; model?: string }>(
      `/api/dl/pixel?layer_id=${layerId}&lon=${lon}&lat=${lat}`),
}

export const STATE_LABEL: Record<JobState, string> = {
  queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断',
}

export const TASK_LABEL: Record<string, string> = {
  train_classifier: '分类训练', predict: '整景预测', train_contrastive: '对比学习',
  embed: '特征分析', similarity: '相似检索',
}

export const KIND_LABEL: Record<string, string> = { spectrum: '中心像元光谱', image: '二维块', cube: '三维块' }

export function fmtDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || !isFinite(sec)) return ''
  if (sec < 1) return sec > 0 ? '不到 1 秒' : '0 秒'
  const s = Math.round(sec)
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`
  return `${Math.floor(s / 3600)} 小时 ${Math.round((s % 3600) / 60)} 分`
}

export function fmtTime(t: number | null | undefined): string {
  if (!t) return ''
  const d = new Date(t * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export const pct = (v: number | undefined | null, digits = 1) =>
  v === undefined || v === null ? '—' : `${(v * 100).toFixed(digits)}%`

export const baseName = (p: string) => p.split(/[\\/]/).filter(Boolean).pop() ?? p
