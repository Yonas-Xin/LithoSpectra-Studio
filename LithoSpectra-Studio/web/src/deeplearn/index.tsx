// 深度学习模块的前端入口。
//
// 外壳只需要做三件事：
//   1. 菜单动作里 "dl-" 开头的交给 handle.action(id)；
//   2. 把图层列表、OpenLayers 地图对象交进来；
//   3. 提供几个回调：同步图层、刷新某个图层、缩放、选中 / 勾选样本、状态栏。
// 工作台、任务轮询、实时预测图层的刷新、分类图点查都在本目录里；模块不在发行版里时
// 整个目录删掉、外壳那几行去掉即可。

import { useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import type { Ref } from 'react'
import { App as AntApp, ConfigProvider } from 'antd'
import type OlMap from 'ol/Map'
import { transform, transformExtent } from 'ol/proj'
import VectorLayer from 'ol/layer/Vector'
import VectorSource from 'ol/source/Vector'
import OlFeature from 'ol/Feature'
import type { FeatureLike } from 'ol/Feature'
import Point from 'ol/geom/Point'
import { Circle, Fill, Stroke, Style, Text } from 'ol/style'

import { api } from '../api'
import type { LayerInfo, TaskInfo } from '../api'
import { ClipDialog, clipApi } from '../ModuleDialogs'
import { CAPTURE, bringInto, fitInto, freeRegion, modulesMayPick } from '../canvasLink'
import type { ClipRequest } from '../ModuleDialogs'
import { useTaskRunner } from '../tasks'
import { STATE_LABEL, dl } from './dlapi'
import type { Capabilities, DlStatus, JobSummary } from './dlapi'
import type { MonitorHooks } from './JobMonitor'
import Workbench from './Workbench'
import type { Page, Preset } from './Workbench'
import './deeplearn.css'

export interface DeepLearnHandle {
  /** 菜单动作；不是本模块的返回 false */
  action: (id: string) => boolean
}

interface Props {
  handle: Ref<DeepLearnHandle>
  map: OlMap | null
  projection: string
  libraryOpen: boolean
  layers: LayerInfo[]
  onStatus: (text: string, path?: string) => void
  onFit: (extent: number[]) => void
  onSyncLayers: (place?: { id: number; kind: string }[]) => Promise<unknown>
  onBumpLayer: (id: number) => void
  onSelectSamples: (keys: string[]) => void
  onCheckSamples: (keys: string[]) => void
}

const POLL_BUSY_MS = 1500
const POLL_IDLE_MS = 8000

export default function DeepLearn(props: Props) {
  const { message, modal } = AntApp.useApp()
  const runTask = useTaskRunner()
  const [open, setOpen] = useState(false)
  const [page, setPage] = useState<Page>('env')
  const [status, setStatus] = useState<DlStatus | null>(null)
  const [caps, setCaps] = useState<Capabilities | null>(null)
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [focusJob, setFocusJob] = useState<string | null>(null)
  const [preset, setPreset] = useState<Preset>({})
  // 相似检索的参考点：点选模式下画布单击记一个点（经纬度）
  const [picking, setPicking] = useState(false)
  const [points, setPoints] = useState<[number, number][]>([])
  // 最近一次「定位」的位置：画一个圈，看得出定位到的是哪一处
  const [located, setLocated] = useState<[number, number] | null>(null)
  const pickingRef = useRef(false)
  pickingRef.current = picking
  const [clipOpen, setClipOpen] = useState(false)
  const [dataVersion, setDataVersion] = useState(0)
  // 要自动放上画布的预测任务；放上之后记下图层号，块进度一变就刷新它
  const autoShow = useRef<Set<string>>(new Set())
  const liveLayers = useRef<Map<string, { layer: number; progress: string; live: boolean }>>(new Map())
  const lastStates = useRef<Map<string, string>>(new Map())
  const propsRef = useRef(props)
  propsRef.current = props

  // ------------------------------------------------------------- 状态与声明
  const loadStatus = useCallback(async () => {
    const s = await dl.status()
    setStatus(s)
    if (s.python) {
      try {
        setCaps(await dl.caps())
      } catch (e) {
        setCaps(null)
        message.error((e as Error).message)
      }
    } else setCaps(null)
    return s
  }, [message])

  useEffect(() => { loadStatus().catch(() => undefined) }, [loadStatus])

  // ------------------------------------------------------------- 任务轮询
  /** 缩放到经纬度范围；工作台开着时缩放到它旁边没被挡住的那一块 */
  const fitBeside = useCallback((extent: number[]) => {
    const map = propsRef.current.map
    const wb = document.querySelector('.dl-wb')?.closest('.float')
    const region = map && wb ? freeRegion(map, wb) : null
    if (map && region) fitInto(map, extent, region)
    else propsRef.current.onFit(extent)
  }, [])

  /** 经纬度范围与当前视野有没有交集 */
  const inView = useCallback((extent: number[]) => {
    const map = propsRef.current.map
    const size = map?.getSize()
    if (!map || !size) return true
    const v = transformExtent(map.getView().calculateExtent(size), map.getView().getProjection(), 'EPSG:4326')
    return !(extent[2] < v[0] || extent[0] > v[2] || extent[3] < v[1] || extent[1] > v[3])
  }, [])

  const showLayer = useCallback(async (jobId: string, quiet = false) => {
    try {
      const r = await dl.showLayer(jobId)
      await propsRef.current.onSyncLayers([{ id: r.layer.id, kind: r.layer.kind }])
      propsRef.current.onBumpLayer(r.layer.id)
      liveLayers.current.set(jobId, { layer: r.layer.id, progress: '', live: r.live })
      autoShow.current.delete(jobId)
      // 自动放上画布时不挪视野，除非视野里根本看不到它
      if (r.layer.extent && (!quiet || !inView(r.layer.extent))) fitBeside(r.layer.extent)
      return true
    } catch (e) {
      if (!quiet) message.error((e as Error).message)
      return false
    }
  }, [message, fitBeside, inView])

  const poll = useCallback(async () => {
    let list: JobSummary[]
    try {
      list = await dl.jobs()
    } catch {
      return false
    }
    setJobs(list)
    for (const j of list) {
      const before = lastStates.current.get(j.id)
      lastStates.current.set(j.id, j.state)
      if (before && before !== j.state && (before === 'running' || before === 'queued')) {
        if (j.state === 'succeeded') message.success(`「${j.title}」${STATE_LABEL[j.state]}`)
        else if (j.state === 'failed') message.error(`「${j.title}」失败：${j.message.split('\n')[0]}`)
        // 新模型、新骨干：各页的模型下拉要重取
        if (j.task === 'train_classifier' || j.task === 'train_contrastive') setDataVersion((v) => v + 1)
      }
      if ((j.task === 'predict' || j.task === 'similarity') && autoShow.current.has(j.id) && j.state !== 'queued') {
        await showLayer(j.id, true)
      }
      const live = liveLayers.current.get(j.id)
      if (live) {
        if (live.progress !== j.progress.text) {
          live.progress = j.progress.text
          propsRef.current.onBumpLayer(live.layer)
        }
        if (live.live && j.state === 'succeeded') {
          // 后端在任务完成时已把实时图层原地换成正式分类图；这里取一次图层信息并重画
          live.live = false
          await dl.showLayer(j.id).catch(() => undefined)
          await propsRef.current.onSyncLayers()
          propsRef.current.onBumpLayer(live.layer)
          propsRef.current.onStatus(`预测完成，结果已添加到画布：${j.title}`)
        }
      }
    }
    return list.some((j) => j.state === 'running' || j.state === 'queued')
  }, [message, showLayer])

  useEffect(() => {
    let stop = false
    let timer = 0
    const loop = async () => {
      const busy = await poll()
      if (!stop) timer = window.setTimeout(loop, busy || open ? POLL_BUSY_MS : POLL_IDLE_MS)
    }
    loop()
    return () => { stop = true; window.clearTimeout(timer) }
  }, [poll, open])

  // ------------------------------------------------------------- 动作
  const openAt = (p: Page) => {
    setOpen(true)
    setPage(status?.python || p === 'data' ? p : 'env')
  }
  useImperativeHandle(props.handle, () => ({
    action: (id: string) => {
      const pages: Record<string, Page> = {
        'dl-open': status?.python ? 'jobs' : 'env', 'dl-env': 'env', 'dl-data': 'data', 'dl-train': 'train',
        'dl-jobs': 'jobs', 'dl-models': 'models', 'dl-predict': 'predict', 'dl-features': 'features',
      }
      if (id in pages) { openAt(pages[id]); return true }
      if (id === 'dl-clip') { if (props.libraryOpen) setClipOpen(true); return true }
      return false
    },
  }))

  const onJobCreated = (job: JobSummary, show: boolean) => {
    if (show) autoShow.current.add(job.id)
    setFocusJob(job.id)
    // 特征分析与相似检索在特征页里看结果，不跳走
    if (job.task !== 'embed' && job.task !== 'similarity') setPage('jobs')
    poll()
    propsRef.current.onStatus(`已提交：${job.title}`)
  }

  const doClip = async (req: ClipRequest) => {
    setClipOpen(false)
    let r: { task: TaskInfo }
    try {
      r = await clipApi.run({ ...req, output_format: 'h5' })
    } catch (e) {
      return modal.error({ title: '裁剪失败', content: (e as Error).message })
    }
    let outcome
    try {
      outcome = await runTask<{ summary: string; out_dir: string; cancelled: boolean; warnings: string[] }>(async () => r.task)
    } catch (e) {
      return modal.error({ title: '裁剪失败', content: (e as Error).message })
    }
    const res = outcome.result
    if (!res || res.cancelled) return
    try {
      await dl.addDataset(res.out_dir)
      setDataVersion((v) => v + 1)
      setOpen(true)
      setPage('data')
      props.onStatus(`裁剪完成并已登记为样本集：${res.summary}`, res.out_dir)
      if (res.warnings.length) {
        modal.info({ title: '裁剪完成', width: 560, content: <div className="pre report">{[res.summary, '', ...res.warnings.slice(0, 8).map((w) => `· ${w}`)].join('\n')}</div> })
      }
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const saveDrawn = async (layerId: number) => {
    const root = status?.jobs_root ?? ''
    const sep = root.includes('\\') ? '\\' : '/'
    const path = `${root}${sep}range_${Date.now()}.gpkg`
    await api.saveDrawn(layerId, path)
    return path
  }

  const hooks: MonitorHooks = {
    onShowLayer: (id) => { showLayer(id) },
    onLowConfidence: async (id, threshold) => {
      try {
        const r = await dl.lowConfidence(id, threshold)
        await props.onSyncLayers([{ id: r.layer.id, kind: r.layer.kind }])
        props.onStatus(`已生成疑难区：置信度低于 ${threshold.toFixed(2)} 的区域共 ${r.features} 个面，已添加到画布`, r.path)
      } catch (e) {
        message.error((e as Error).message)
      }
    },
    onLocate: ([lon, lat]) => {
      const d = 0.0015
      setLocated([lon, lat])
      const map = props.map
      const region = map ? freeRegion(map, document.querySelector('.dl-wb')?.closest('.float')) : null
      if (!map || !region) return props.onFit([lon - d, lat - d, lon + d, lat + d])
      // 工作台挡着大半个画布：定位到的地方放在空出来那块的正中，比例尺按那一块的大小定
      const view = map.getView()
      const proj = view.getProjection()
      const ext = transformExtent([lon - d * 1.05, lat - d * 1.05, lon + d * 1.05, lat + d * 1.05], 'EPSG:4326', proj)
      const res = Math.max((ext[2] - ext[0]) / (region[2] - region[0]), (ext[3] - ext[1]) / (region[3] - region[1]),
        view.getResolutionForZoom(18))
      bringInto(map, transform([lon, lat], 'EPSG:4326', proj), region, res)
    },
    onSelectSamples: props.onSelectSamples,
    onCheckSamples: props.onCheckSamples,
    onPredictWith: (path) => { setPreset({ ...preset, model: path }); setPage('predict') },
    onUseBackbone: (path) => { setPreset({ ...preset, pretrained: path, mode: 'classifier' }); setPage('train') },
    onOpenFeatures: (path) => { if (path) setPreset({ ...preset, model: path }); setPage('features') },
    onResume: async (id) => {
      try {
        await dl.resume(id)
        const j = jobs.find((x) => x.id === id)
        if ((j?.task === 'predict' || j?.task === 'similarity') && !liveLayers.current.has(id)) autoShow.current.add(id)
        poll()
      } catch (e) {
        message.error((e as Error).message)
      }
    },
    onCancel: async (id) => { try { await dl.cancel(id); poll() } catch (e) { message.error((e as Error).message) } },
    onDelete: async (id) => {
      try {
        await dl.remove(id)
        liveLayers.current.delete(id)
        setFocusJob(null)
        await props.onSyncLayers()
        poll()
      } catch (e) {
        message.error((e as Error).message)
      }
    },
  }

  // ------------------------------------------------------------- 相似检索的参考点
  // 选点期间接管画布单击：画布不再选样本、野外点位不响应，画样本的工具先停用
  useEffect(() => {
    const map = props.map
    if (!map || !picking) return
    map.set(CAPTURE, 'dl-reference')
    return () => { if (map.get(CAPTURE) === 'dl-reference') map.set(CAPTURE, false) }
  }, [props.map, picking])

  // 选点期间：Esc 完成，退格撤销上一个点。抢在外壳之前处理，免得 Delete 删到选中的样本
  useEffect(() => {
    if (!picking) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest?.('input, textarea')) return
      if (e.key === 'Escape' || e.key === 'Enter') setPicking(false)
      else if (e.key === 'Backspace' || e.key === 'Delete') setPoints((list) => list.slice(0, -1))
      else return
      e.preventDefault()
      e.stopImmediatePropagation()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [picking])

  // 参考点画在画布上，带序号，与工作台里的列表一一对应；定位的位置画一个圈。工作台关着时都不显示
  const refSrc = useRef(new VectorSource())
  useEffect(() => {
    const map = props.map
    if (!map) return
    const layer = new VectorLayer({ source: refSrc.current, zIndex: 1001, style: refStyle })
    map.addLayer(layer)
    return () => { map.removeLayer(layer) }
  }, [props.map])
  useEffect(() => {
    const src = refSrc.current
    src.clear(true)
    if (!open) return
    src.addFeatures(points.map(([lon, lat], i) => new OlFeature({
      geometry: new Point(transform([lon, lat], 'EPSG:4326', props.projection)), n: i + 1,
    })))
    if (located) src.addFeature(new OlFeature({ geometry: new Point(transform(located, 'EPSG:4326', props.projection)), located: true }))
  }, [points, located, open, props.projection])
  useEffect(() => { if (!open) setLocated(null) }, [open])

  // ------------------------------------------------------------- 分类图点查
  // 画布上点到深度学习的分类图时，在状态栏报出类别与置信度
  useEffect(() => {
    const map = props.map
    if (!map) return
    const handler = async (evt: { coordinate: number[] }) => {
      const p = propsRef.current
      if (pickingRef.current) {
        const [lon, lat] = transform(evt.coordinate, p.projection, 'EPSG:4326')
        setPoints((list) => [...list, [lon, lat]])
        p.onStatus(`已添加参考点 ${lon.toFixed(5)}, ${lat.toFixed(5)}。可继续选取；退格撤销上一个，Esc 或「完成」返回工作台。`)
        return
      }
      // 画点、画面、取谱时单击另有用处，不去查分类图
      if (!modulesMayPick(map)) return
      const results = p.layers.filter((l) => l.visible && l.kind === 'raster' && l.has_color_table && /__.+\.tif$/i.test(l.path))
      if (!results.length) return
      const [lon, lat] = transform(evt.coordinate, p.projection, 'EPSG:4326')
      for (const l of results) {
        try {
          const r = await dl.pixel(l.id, lon, lat)
          if (r.inside && !r.background && r.class) {
            p.onStatus(`${l.name}：${r.class}${r.confidence !== undefined ? `，置信度 ${(r.confidence * 100).toFixed(1)}%` : ''}`)
            return
          }
          if (r.inside && r.similarity !== undefined) {
            p.onStatus(`${l.name}：与参考点的相似度 ${r.similarity.toFixed(3)}`)
            return
          }
        } catch { /* 不是深度学习成果 */ }
      }
    }
    map.on('singleclick', handler)
    return () => { map.un('singleclick', handler) }
  }, [props.map])

  return (
    <>
      {open && picking ? (
        <div className="dl-pickbar">
          <span>选取参考点：在画布上单击添加，已选 <b>{points.length}</b> 个<span className="dl-pickbar-keys">退格撤销 · Esc 完成</span></span>
          <button className="dl-link" disabled={!points.length} onClick={() => setPoints([])}>清空</button>
          <button className="dl-pickbar-done" onClick={() => setPicking(false)}>完成</button>
        </div>
      ) : null}
      {/* 点选期间收起工作台，把画布让出来；只是隐藏，表单里填的东西都还在 */}
      {/* 两字按钮不插空格，「特征」不显示成「特 征」 */}
      {open ? <ConfigProvider button={{ autoInsertSpace: false }}><div style={{ display: picking ? 'none' : undefined }}>
        <Workbench page={page} setPage={setPage} status={status} caps={caps} jobs={jobs}
          focusJob={focusJob} setFocusJob={setFocusJob} preset={preset} setPreset={setPreset}
          libraryOpen={props.libraryOpen} layers={props.layers} hooks={hooks}
          picking={picking} setPicking={(v) => { setPicking(v); if (v) props.onStatus('在画布上单击添加参考点，可添加多个。') }}
          points={points} setPoints={setPoints}
          onClose={() => setOpen(false)} onClip={() => setClipOpen(true)}
          onEnvChanged={() => { loadStatus().then((s) => { if (s.python) setPage('data') }) }}
          onJobCreated={onJobCreated} onSaveDrawn={saveDrawn} dataVersion={dataVersion} />
      </div></ConfigProvider> : null}
      <ClipDialog open={clipOpen} lockFormat="h5" title="从样本库裁剪训练样本集" onCancel={() => setClipOpen(false)} onOk={doClip} />
    </>
  )
}

/** 参考点：蓝色圆点加序号，与野外点位的菱形、取谱的叉区分开。定位处：一暗一亮两道圈 */
function refStyle(f: FeatureLike): Style | Style[] {
  if (f.get('located')) {
    return [
      new Style({ image: new Circle({ radius: 14, stroke: new Stroke({ color: 'rgba(20,20,19,0.75)', width: 5 }) }) }),
      new Style({ image: new Circle({ radius: 14, stroke: new Stroke({ color: '#F5D547', width: 2 }) }) }),
    ]
  }
  return new Style({
    image: new Circle({ radius: 9, fill: new Fill({ color: '#2F7BEA' }), stroke: new Stroke({ color: '#FFFFFF', width: 2 }) }),
    text: new Text({ text: String(f.get('n')), font: '600 11px "PingFang SC", sans-serif', fill: new Fill({ color: '#FFFFFF' }) }),
  })
}
