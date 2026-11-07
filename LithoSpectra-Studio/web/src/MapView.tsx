import { useCallback, useEffect, useRef, useState } from 'react'
import { App as AntApp, Dropdown } from 'antd'
import type { MenuProps } from 'antd'
import OlMap from 'ol/Map'
import View from 'ol/View'
import TileLayer from 'ol/layer/Tile'
import ImageLayer from 'ol/layer/Image'
import VectorLayer from 'ol/layer/Vector'
import VectorImageLayer from 'ol/layer/VectorImage'
import Graticule from 'ol/layer/Graticule'
import VectorSource from 'ol/source/Vector'
import ImageWMS from 'ol/source/ImageWMS'
import XYZ from 'ol/source/XYZ'
import TileGrid from 'ol/tilegrid/TileGrid'
import GeoJSON from 'ol/format/GeoJSON'
import Collection from 'ol/Collection'
import { DoubleClickZoom, DragBox, DragPan, Draw, Modify, Snap } from 'ol/interaction'
import { createBox } from 'ol/interaction/Draw'
import { platformModifierKeyOnly, primaryAction, shiftKeyOnly } from 'ol/events/condition'
import { ScaleLine } from 'ol/control'
import { Circle, Fill, RegularShape, Stroke, Style, Text } from 'ol/style'
import OlFeature from 'ol/Feature'
import Point from 'ol/geom/Point'
import { fromCircle } from 'ol/geom/Polygon'
import { toLonLat, transform, transformExtent } from 'ol/proj'
import type Feature from 'ol/Feature'
import type { FeatureLike } from 'ol/Feature'
import type { Geometry as OlGeometry, Polygon } from 'ol/geom'
import type BaseLayer from 'ol/layer/Base'
import 'ol/ol.css'

import { api } from './api'
import { CAPTURE, CLAIMS_CLICK, TOOL } from './canvasLink'
import { isTypingTarget } from './desktop'
import type { Basemap, FeatureCollection, LayerInfo, SampleClass } from './api'
import type { LayerId, SamplesLayerState } from './Layers'
import type { Palette } from './generated/tokens'

/** 画布工具。前六件按数字键 1–6 直达，画范围与取谱两种模式都能用。 */
export type Tool = 'pan' | 'select' | 'point' | 'polygon' | 'vertex' | 'erase' | 'roi' | 'spectrum'
export type RoiShape = 'polygon' | 'rect' | 'circle'

interface Props {
  features: FeatureCollection | null
  classMap: Map<number, SampleClass>
  /** 画布上显示哪些样本（勾选的那些）；null 表示不过滤。草稿不受此限。 */
  shownKeys: Set<string> | null
  selected: Set<string>
  onSelect: (keys: Set<string>) => void
  /** 单击处压着的要素：样本是 "point:12"，参考矢量是 "L:图层号:要素号"。 */
  onIdentify: (keys: string[]) => void
  tool: Tool
  roiShape: RoiShape
  activeClass: SampleClass | null
  basemaps: Basemap[]
  basemapIds: number[]
  layers: LayerInfo[]
  layerOrder: LayerId[]
  layerRevs: Record<number, number>
  samples: SamplesLayerState
  projection: string
  showGrid: boolean
  snap: boolean
  fitExtent: { extent: number[]; n: number } | null
  palette: Palette
  emptyText: { title: string; hint: string } | null
  contextMenu: (keys: Set<string>) => MenuProps['items']
  onContextAction: (action: string, keys: Set<string>) => void
  /** 右键位置（经纬度）：右键菜单里「在此处新建…」要用 */
  onContextPoint?: (lonlat: [number, number]) => void
  onRoiDrawn: (rings: number[][][]) => void
  onSpectrumPick: (lonlat: [number, number], append: boolean) => void
  onZoomAll: () => void
  onChanged: () => void
  onCursor: (pos: { lonlat: [number, number]; xy: [number, number] } | null) => void
  onViewChange?: (v: { center: [number, number]; resolution: number }) => void
  /** 取谱位置的标记（曲线窗口开着时） */
  marks?: { lon: number; lat: number; color: string; live: boolean }[]
  initialView?: { center: [number, number]; resolution: number } | null
  /** 地图建好 / 拆掉时通知外壳：功能模块（野外记录）要往上挂自己的图层 */
  onMap?: (map: OlMap | null) => void
}

// 天地图经纬度切片（TILEMATRIXSET=c）：第 z 级 2^z 列，每片跨 360/2^z 度。
const GEODETIC_GRID = new TileGrid({
  extent: [-180, -90, 180, 90],
  origin: [-180, 90],
  resolutions: Array.from({ length: 19 }, (_, z) => 360 / 2 ** z / 256),
  tileSize: 256,
})

// 底图瓦片走另一个主机名。浏览器对同一主机名最多同时开 6 个连接，没缓存的瓦片要等后端
// 从网上下载（Mapbox 一张约 1 秒），会占满这 6 个连接，影像出图请求只能排在后面，拖动画布时
// 「正在加载影像…」要等几秒。页面在 127.0.0.1 上时瓦片改走 localhost，两边各用各的连接。
const TILE_ORIGIN = location.hostname === '127.0.0.1' && location.port
  ? `${location.protocol}//localhost:${location.port}` : ''

function basemapLayer(b: Basemap): TileLayer<XYZ> {
  const url = `${TILE_ORIGIN}/api/tiles/${b.id}/{z}/{x}/{y}`
  const crossOrigin = TILE_ORIGIN ? 'anonymous' : undefined
  const source =
    b.scheme === 'geodetic'
      ? new XYZ({ url, crossOrigin, projection: 'EPSG:4326', tileGrid: GEODETIC_GRID, attributions: b.attribution })
      : new XYZ({ url, crossOrigin, maxZoom: b.max_zoom, attributions: b.attribution })
  return new TileLayer({ source, zIndex: b.overlay ? 1 : 0 })
}

function rgba(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const n = parseInt(h.length === 3 ? h.replace(/./g, (c) => c + c) : h, 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`
}

// first：首次出图的状态，shown 为第一张图已出来，pending 为计入进度条的请求数
type RefLayer = { kind: 'raster'; ol: ImageLayer<ImageWMS>; rev: number; first: { shown: boolean; pending: number } }
  | { kind: 'vector'; ol: VectorImageLayer<VectorSource<Feature<OlGeometry>>>; rev: number; info: LayerInfo }

export default function MapView(props: Props) {
  // 栅格图层首次出图、还没回来的请求数；超过 250 ms 才显示进度条，快的不闪
  const [loadingN, setLoadingN] = useState(0)
  const loadingAdd = useCallback((d: number) => setLoadingN((n) => Math.max(0, n + d)), [])
  const [showLoading, setShowLoading] = useState(false)
  useEffect(() => {
    if (!loadingN) { setShowLoading(false); return }
    const t = window.setTimeout(() => setShowLoading(true), 250)
    return () => window.clearTimeout(t)
  }, [loadingN])
  const { message } = AntApp.useApp()
  const divRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<OlMap | null>(null)
  // 点、面分两个源：捕捉只对面的顶点，点样本既不捕捉、也不被捕捉
  const pointSrc = useRef(new VectorSource<Feature<OlGeometry>>())
  const polySrc = useRef(new VectorSource<Feature<OlGeometry>>())
  const sampleLayers = useRef<VectorLayer[]>([])
  const refLayers = useRef(new Map<number, RefLayer>())
  const baseLayersRef = useRef<TileLayer<XYZ>[]>([])
  const gridRef = useRef<Graticule | null>(null)
  const dragPanRef = useRef<DragPan | null>(null)
  // 当前工具挂上的交互（画、改顶点、框选）与正在画的草图：空格平移时先停掉它们，
  // 右键闭合要知道草图落了几个点
  const toolInteractions = useRef<(Draw | Modify | Snap | DragBox)[]>([])
  const sketch = useRef<{ draw: Draw | null; points: number }>({ draw: null, points: 0 })
  const spaceHeld = useRef(false)
  const [panning, setPanning] = useState<'' | 'ready' | 'drag'>('')
  // 功能模块接管单击期间：画、改顶点、框选这些工具交互先停掉，左键拖动平移，光标是十字
  const [capturing, setCapturing] = useState(false)
  const editCollection = useRef(new Collection<Feature<OlGeometry>>())
  const markSrc = useRef(new VectorSource<Feature<OlGeometry>>())
  const ctxKeysRef = useRef<Set<string>>(new Set())
  const propsRef = useRef(props)
  propsRef.current = props

  const fmt = () => new GeoJSON({ dataProjection: 'EPSG:4326', featureProjection: propsRef.current.projection })

  const featureByKey = (key: string) =>
    (polySrc.current.getFeatureById(key) ?? pointSrc.current.getFeatureById(key)) as Feature<OlGeometry> | null

  /** 点中的样本键（在前）与参考矢量要素（"L:图层:要素"）。只碰画得出来的。 */
  const hitKeys = (pixel: number[]): { samples: string[]; refs: string[] } => {
    const map = mapRef.current!
    const samples: string[] = []
    const refs: string[] = []
    map.forEachFeatureAtPixel(
      pixel,
      (f, layer) => {
        if (sampleLayers.current.includes(layer as VectorLayer)) samples.push(f.get('key') as string)
        else {
          for (const [id, r] of refLayers.current) if (r.ol === layer) refs.push(`L:${id}:${f.get('fid')}`)
        }
      },
      {
        layerFilter: (l) => sampleLayers.current.includes(l as VectorLayer) ||
          [...refLayers.current.values()].some((r) => r.kind === 'vector' && r.ol === l),
        hitTolerance: 4,
      },
    )
    return { samples, refs }
  }

  // ------------------------------------------------------------- 建图
  useEffect(() => {
    const polyLayer = new VectorLayer({ source: polySrc.current, zIndex: 100, style: styleOf })
    const pointLayer = new VectorLayer({ source: pointSrc.current, zIndex: 101, style: styleOf })
    sampleLayers.current = [polyLayer, pointLayer]
    // 左键拖动平移：只在平移、取谱工具下（以及按住空格时）开，见下面「工具」
    const dragPan = new DragPan()
    dragPanRef.current = dragPan
    // 中键拖动平移：哪件工具下都行
    const middlePan = new DragPan({ condition: (e) => (e.originalEvent as PointerEvent).button === 1 })
    const init = props.initialView
    // 取谱位置：一个叉，颜色跟曲线；当前那条大一号
    const markLayer = new VectorLayer({
      source: markSrc.current, zIndex: 1000,
      style: (f) => new Style({ image: new RegularShape({
        points: 4, radius: f.get('live') ? 9 : 7, radius2: 0, angle: Math.PI / 4,
        stroke: new Stroke({ color: f.get('color'), width: f.get('live') ? 3 : 2 }) }) }),
    })
    const map = new OlMap({
      target: divRef.current!,
      // 按下到松开抖 6 像素以内仍算单击；默认 1 像素，
      // 点样本时手一抖就成了拖动
      moveTolerance: 6,
      layers: [polyLayer, pointLayer, markLayer],
      // 没有缩放按钮，滚轮缩放
      controls: [new ScaleLine({ units: 'metric' })],
      view: new View({
        projection: props.projection,
        center: init?.center ?? transform([96, 36.5], 'EPSG:4326', props.projection),
        resolution: init?.resolution,
        zoom: init ? undefined : 7,
        maxZoom: 24,
      }),
    })
    // 双击：平移工具下是全图，不是 OpenLayers 默认的放大一级；
    // 平移交给自己管：选择工具下左键拖动是框选，不是平移
    map.getInteractions().getArray().slice().forEach((i) => {
      if (i instanceof DragPan || i instanceof DoubleClickZoom) map.removeInteraction(i)
    })
    map.addInteraction(dragPan)
    map.addInteraction(middlePan)
    // 中键按下时浏览器默认进入自动滚动（Windows），拦住
    const viewport = map.getViewport() as HTMLElement
    viewport.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault() })
    viewport.addEventListener('auxclick', (e) => { if (e.button === 1) e.preventDefault() })
    map.on('pointerdrag', () => { if (dragPan.getActive() || spaceHeld.current) setPanning('drag') })
    viewport.addEventListener('pointerup', () => setPanning(spaceHeld.current ? 'ready' : ''))
    mapRef.current = map
    map.on('propertychange', (e) => {
      if (e.key !== CAPTURE) return
      const cap = !!map.get(CAPTURE)
      setCapturing(cap)
      toolInteractions.current.forEach((i) => i.setActive(!cap && !spaceHeld.current))
      const t = propsRef.current.tool
      dragPan.setActive(cap || spaceHeld.current || t === 'pan' || t === 'spectrum')
    })
    props.onMap?.(map)

    map.on('dblclick', (evt) => {
      if (propsRef.current.tool !== 'pan' || map.get(CAPTURE)) return
      evt.preventDefault()
      propsRef.current.onZoomAll()
    })
    // 用 click 而不是 singleclick：后者要等 250 毫秒确认不是双击，点选、删要素都慢半拍
    map.on('click', async (evt) => {
      const p = propsRef.current
      // 功能模块临时接管了单击（如深度学习选参考点）：画布自己不选、不识别
      if (map.get(CAPTURE)) return
      const { samples: keys, refs } = hitKeys(evt.pixel)
      const shift = evt.originalEvent instanceof MouseEvent && evt.originalEvent.shiftKey
      if (p.tool === 'spectrum') {
        const ll = toLonLat(evt.coordinate, p.projection) as [number, number]
        p.onSpectrumPick(ll, shift)
        return
      }
      if (p.tool === 'erase') {
        if (keys[0]) {
          try {
            await api.deleteFeatures([keys[0]])
          } catch (e) {
            message.error((e as Error).message)
          }
          p.onSelect(new Set())
          p.onChanged()
        }
        return
      }
      if (p.tool === 'point' || p.tool === 'polygon' || p.tool === 'roi') return
      const top = keys[0]
      if (p.tool === 'vertex') {
        const poly = keys.find((k) => k.startsWith('polygon:'))
        p.onSelect(poly ? new Set([poly]) : new Set())
        return
      }
      // 点到了功能模块自己处理的要素（野外点位、轨迹）：它们画在样本上面，单击归它们，
      // 样本选中不动；底下参考矢量的属性照常列出
      if (map.hasFeatureAtPixel(evt.pixel, { layerFilter: (l) => !!l.get(CLAIMS_CLICK), hitTolerance: 5 })) {
        p.onIdentify(refs)
        return
      }
      p.onIdentify([...keys, ...refs])
      if (shift) {
        if (!top) return
        const next = new Set(p.selected)
        if (next.has(top)) next.delete(top)
        else next.add(top)
        p.onSelect(next)
      } else {
        p.onSelect(top ? new Set([top]) : new Set())
      }
    })
    map.on('pointermove', (evt) => {
      const p = propsRef.current
      const xy = evt.coordinate as [number, number]
      p.onCursor({ xy, lonlat: toLonLat(xy, p.projection) as [number, number] })
    })
    map.on('moveend', () => {
      const v = map.getView()
      const c = v.getCenter()
      const r = v.getResolution()
      if (c && r) propsRef.current.onViewChange?.({ center: c as [number, number], resolution: r })
    })
    divRef.current!.addEventListener('mouseleave', () => propsRef.current.onCursor(null))
    return () => {
      propsRef.current.onMap?.(null)
      map.setTarget(undefined)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 样本的样式读 propsRef：类别颜色、显隐、选中变了只要 changed()
  function styleOf(f: FeatureLike): Style | Style[] | undefined {
    const p = propsRef.current
    const cid = f.get('class_id') as number
    const draft = !!f.get('draft')
    if (!draft && p.shownKeys && !p.shownKeys.has(f.get('key'))) return undefined
    const color = p.classMap.get(cid)?.color ?? '#3b82f6'
    const selected = p.selected.has(f.get('key') as string)
    const pal = p.palette
    if (f.get('kind') === 'point') {
      const base = new Style({
        image: new Circle({
          radius: 4.5,
          fill: new Fill({ color }),
          stroke: new Stroke({ color: rgba(pal.canvas_bg, 0.8), width: 1 }),
        }),
        zIndex: 1,
      })
      if (!selected) return base
      // 选中：一暗一亮双描边，落在任何颜色的图斑上都看得见
      return [
        new Style({ image: new Circle({ radius: 8, stroke: new Stroke({ color: rgba(pal.canvas_bg, 0.78), width: 4 }) }), zIndex: 99 }),
        new Style({ image: new Circle({ radius: 8, stroke: new Stroke({ color: pal.canvas_select, width: 1.5 }) }), zIndex: 100 }),
        base,
      ]
    }
    const base = new Style({
      fill: new Fill({ color: rgba(color, 0.35) }),
      stroke: new Stroke({ color, width: 1.2, lineDash: draft ? [6, 4] : undefined }),
    })
    if (!selected) return base
    return [
      base,
      new Style({ stroke: new Stroke({ color: rgba(pal.canvas_bg, 0.78), width: 5 }), zIndex: 99 }),
      new Style({ stroke: new Stroke({ color: pal.canvas_select, width: 1.5 }), zIndex: 100 }),
    ]
  }

  // ------------------------------------------------------------- 投影
  // 换显示坐标系：新建一个 View，中心换算过去、比例尺大致保持；样本按新投影重读
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const old = map.getView()
    const oldProj = old.getProjection().getCode()
    if (oldProj === props.projection) return
    const center = old.getCenter()
    const lonlat = center ? toLonLat(center, oldProj) : [96, 36.5]
    const metersPerUnitOld = old.getProjection().getMetersPerUnit() ?? 1
    const res = (old.getResolution() ?? 1) * metersPerUnitOld
    const view = new View({ projection: props.projection, center: transform(lonlat, 'EPSG:4326', props.projection), maxZoom: 24 })
    const mpu = view.getProjection().getMetersPerUnit() ?? 1
    view.setResolution(res / mpu)
    map.setView(view)
    shownFc.current = null
    loadSamples()
    refLayers.current.forEach((r, id) => { if (r.kind === 'vector') loadVector(id, r) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.projection])

  // ------------------------------------------------------------- 样本数据
  // 画布上那份对应的要素数据。新数据里没变的要素沿用同一个对象（见 App 的增量
  // 同步），据此只替换变了的那几条：删一个点、撤销一步不必把几千条要素整批重建
  const shownFc = useRef<FeatureCollection | null>(null)
  const loadSamples = () => {
    const fc = propsRef.current.features
    const prev = shownFc.current
    shownFc.current = fc
    if (fc && prev) {
      const before = new Map(prev.features.map((f) => [f.id, f]))
      const changed = fc.features.filter((f) => before.get(f.id) !== f)
      const alive = new Set(fc.features.map((f) => f.id))
      const gone = prev.features.filter((f) => !alive.has(f.id)).map((f) => f.id)
      // 变动占多数（换了库、库操作后底稿重读）时整批重建反而更快
      if (changed.length + gone.length < Math.max(50, fc.features.length / 4)) {
        for (const id of [...gone, ...changed.map((f) => f.id)]) {
          const old = featureByKey(id)
          if (old) (old.get('kind') === 'point' ? pointSrc : polySrc).current.removeFeature(old)
        }
        if (changed.length) {
          const fresh = fmt().readFeatures({ type: 'FeatureCollection', features: changed }) as Feature<OlGeometry>[]
          pointSrc.current.addFeatures(fresh.filter((f) => f.get('kind') === 'point'))
          polySrc.current.addFeatures(fresh.filter((f) => f.get('kind') !== 'point'))
        }
        syncEditTarget()
        return
      }
    }
    pointSrc.current.clear(true)
    polySrc.current.clear(true)
    if (fc) {
      const all = fmt().readFeatures(fc) as Feature<OlGeometry>[]
      pointSrc.current.addFeatures(all.filter((f) => f.get('kind') === 'point'))
      polySrc.current.addFeatures(all.filter((f) => f.get('kind') !== 'point'))
    }
    syncEditTarget()
  }
  useEffect(loadSamples, [props.features]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    sampleLayers.current.forEach((l) => l.changed())
    syncEditTarget()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.classMap, props.shownKeys, props.selected, props.palette])

  function syncEditTarget() {
    const col = editCollection.current
    const want: Feature<OlGeometry>[] = []
    for (const key of propsRef.current.selected) {
      const f = featureByKey(key)
      if (f && f.get('kind') !== 'point') want.push(f)
    }
    // 没变就别动：每换一次集合，编辑交互都要把这些面的顶点重新建一遍索引
    const have = col.getArray()
    if (have.length === want.length && have.every((f, i) => f === want[i])) return
    col.clear()
    col.extend(want)
  }

  // ------------------------------------------------------------- 参考图层
  const vectorStyle = (info: LayerInfo) => (f: FeatureLike) => {
    const color = (f.get('c') as string) || info.color || '#f97316'
    const width = info.line_width ?? 1.2
    if (f.get('kind') === 'point') {
      return new Style({ image: new Circle({ radius: 4, fill: new Fill({ color }), stroke: new Stroke({ color: '#000a', width: 1 }) }) })
    }
    return new Style({
      stroke: new Stroke({ color, width }),
      fill: f.get('kind') === 'polygon' && info.fill !== false ? new Fill({ color: rgba(color, (info.fill_alpha ?? 70) / 255) }) : undefined,
    })
  }

  const loadVector = async (id: number, r: Extract<RefLayer, { kind: 'vector' }>) => {
    try {
      const gj = await api.layerGeojson(id)
      const src = r.ol.getSource()!
      src.clear(true)
      src.addFeatures(fmt().readFeatures(gj) as Feature<OlGeometry>[])
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const p = props
    const alive = new Set(p.layers.map((l) => l.id))
    for (const [id, r] of refLayers.current) {
      if (!alive.has(id)) {
        map.removeLayer(r.ol as BaseLayer)
        refLayers.current.delete(id)
        // 第一张图出来前就被移除：它计入的请求不会再有结束事件，在这里减掉
        if (r.kind === 'raster' && r.first.pending) loadingAdd(-r.first.pending)
      }
    }
    for (const info of p.layers) {
      const rev = p.layerRevs[info.id] ?? 0
      let r = refLayers.current.get(info.id)
      const raster = info.kind === 'raster' || info.kind === 'image'
      if (!r) {
        if (raster) {
          // 按视窗向后端要图（WMS 1.1.1 的 BBOX / WIDTH / HEIGHT / SRS），四周多要一圈当平移余量
          // serverType 声明之后 OpenLayers 才按屏幕像素比（Retina 为 2）要图，否则按 1 倍出图再拉大，发虚
          const src = new ImageWMS({ url: `/api/layers/${info.id}/wms`, params: { LAYERS: 'x', VERSION: '1.1.1', v: rev }, ratio: 1.4, serverType: 'mapserver', hidpi: true })
          // 图层加入后第一张图还没出来时，画布顶上显示「正在加载影像…」；之后拖动、缩放不再提示
          const first = { shown: false, pending: 0 }
          src.on('imageloadstart', () => {
            if (first.shown) return
            first.pending += 1
            loadingAdd(1)
          })
          src.on(['imageloadend', 'imageloaderror'], (e) => {
            if (first.pending > 0) {
              first.pending -= 1
              loadingAdd(-1)
            }
            if (e.type === 'imageloadend' && !first.shown) {
              first.shown = true
              loadingAdd(-first.pending)
              first.pending = 0
            }
          })
          r = { kind: 'raster', ol: new ImageLayer({ source: src }), rev, first }
        } else {
          const ol = new VectorImageLayer({ source: new VectorSource<Feature<OlGeometry>>(), imageRatio: 1.5 })
          r = { kind: 'vector', ol, rev: -1, info }
        }
        refLayers.current.set(info.id, r)
        map.addLayer(r.ol as BaseLayer)
      }
      if (r.kind === 'raster') {
        if (r.rev !== rev) {
          r.ol.getSource()!.updateParams({ v: rev })
          r.rev = rev
        }
      } else {
        r.info = info
        r.ol.setStyle(vectorStyle(info))
        if (r.rev !== rev) {
          r.rev = rev
          loadVector(info.id, r)
        }
      }
      r.ol.setVisible(info.visible)
      r.ol.setOpacity(info.opacity)
    }
    // 叠放顺序：图层清单从上往下 = 画布从上往下；样本图层在清单里也占一格
    const n = p.layerOrder.length
    p.layerOrder.forEach((id, i) => {
      const z = 10 + (n - i) * 3
      if (id === 'samples') {
        sampleLayers.current[0].setZIndex(z)
        sampleLayers.current[1].setZIndex(z + 1)
      } else refLayers.current.get(id)?.ol.setZIndex(z)
    })
    sampleLayers.current.forEach((l) => {
      l.setVisible(p.samples.visible)
      l.setOpacity(p.samples.opacity)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.layers, props.layerOrder, props.layerRevs, props.samples])

  useEffect(() => {
    markSrc.current.clear(true)
    for (const m of props.marks ?? []) {
      const f = new OlFeature({ geometry: new Point(transform([m.lon, m.lat], 'EPSG:4326', props.projection)) })
      f.set('color', m.color)
      f.set('live', m.live)
      markSrc.current.addFeature(f as Feature<OlGeometry>)
    }
  }, [props.marks, props.projection])

  // ------------------------------------------------------------- 视野
  useEffect(() => {
    const fe = props.fitExtent
    const map = mapRef.current
    if (!fe || !map) return
    const [x0, y0, x1, y1] = fe.extent
    const pad = Math.max(x1 - x0, y1 - y0) * 0.05 || 0.01
    const ext = transformExtent([x0 - pad, y0 - pad, x1 + pad, y1 + pad], 'EPSG:4326', props.projection)
    map.getView().fit(ext, { maxZoom: 18 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.fitExtent])

  // ------------------------------------------------------------- 底图与经纬网
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    baseLayersRef.current.forEach((l) => map.removeLayer(l))
    baseLayersRef.current = props.basemapIds
      .map((id) => props.basemaps.find((b) => b.id === id))
      .filter((b): b is Basemap => !!b)
      .map(basemapLayer)
    baseLayersRef.current.forEach((l) => map.addLayer(l))
  }, [props.basemapIds, props.basemaps])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    if (gridRef.current) map.removeLayer(gridRef.current)
    gridRef.current = null
    if (!props.showGrid) return
    const pal = props.palette
    const label = (align: CanvasTextAlign, baseline: CanvasTextBaseline, offsetX: number, offsetY: number) =>
      new Text({
        font: '11px ' + getComputedStyle(document.documentElement).getPropertyValue('--font-ui'),
        fill: new Fill({ color: pal.canvas_text }),
        textAlign: align,
        textBaseline: baseline,
        offsetX,
        offsetY,
      })
    const grid = new Graticule({
      strokeStyle: new Stroke({ color: pal.canvas_grid, width: 1 }),
      showLabels: true,
      wrapX: false,
      zIndex: 5,
      lonLabelPosition: 0.012,
      latLabelPosition: 0.006,
      lonLabelStyle: label('center', 'bottom', 0, 0),
      latLabelStyle: label('left', 'bottom', 2, -2),
      lonLabelFormatter: (lon) => `${lon.toFixed(2)}°E`,
      latLabelFormatter: (lat) => `${lat.toFixed(2)}°N`,
    })
    map.addLayer(grid)
    gridRef.current = grid
  }, [props.showGrid, props.palette, props.projection])

  // ------------------------------------------------------------- 工具
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const added: (Draw | Modify | Snap | DragBox)[] = []
    const { tool } = props
    map.set(TOOL, tool)
    // 左键拖动平移只给平移与取谱；其余工具左键各有用处：
    // 选择是框选，画点画面是落点，改顶点是拖顶点。要平移用中键或按住空格
    dragPanRef.current?.setActive(tool === 'pan' || tool === 'spectrum')

    if (tool === 'select') {
      // 只认左键：中键要留给平移
      const box = new DragBox({ condition: (e) => primaryAction(e) && !platformModifierKeyOnly(e) })
      box.on('boxend', (evt) => {
        const p = propsRef.current
        const extent = box.getGeometry().getExtent()
        const add = !!evt.mapBrowserEvent && shiftKeyOnly(evt.mapBrowserEvent)
        const next = add ? new Set(p.selected) : new Set<string>()
        if (p.samples.visible) {
          for (const src of [polySrc.current, pointSrc.current]) {
            src.forEachFeatureIntersectingExtent(extent, (f) => {
              const visible = !!f.get('draft') || !p.shownKeys || p.shownKeys.has(f.get('key'))
              if (visible) next.add(f.get('key'))
            })
          }
        }
        p.onSelect(next)
      })
      added.push(box)
    }

    let draw: Draw | null = null
    if (tool === 'point' || tool === 'polygon') {
      // 快速连点时手（尤其触控板）还在移动，按下到松开常滑出 6 像素以上，OpenLayers 默认就当
      // 成拖动而不落点——表现为「点快了丢样本」。画点、画面时左键拖动本来就不平移（平移用
      // 中键或空格），把容差放宽到 32 像素
      draw = new Draw({ type: tool === 'point' ? 'Point' : 'Polygon', clickTolerance: 32 })
      draw.on('drawstart', () => { sketch.current.points = 1 })
      draw.on('drawabort', () => { sketch.current.points = 0 })
      draw.on('drawend', async (evt) => {
        sketch.current.points = 0
        const cls = propsRef.current.activeClass
        if (!cls) {
          message.warning('请先在左侧样本树中选中基础类别，再开始绘制。')
          return
        }
        const geometry = fmt().writeGeometryObject(evt.feature.getGeometry()!)
        try {
          const { key } = await api.addFeature(cls.id, geometry as never)
          propsRef.current.onSelect(new Set([key]))
        } catch (e) {
          message.error((e as Error).message)
        }
        propsRef.current.onChanged()
      })
      added.push(draw)
    }

    if (tool === 'roi') {
      // 矩形与圆在屏幕上是正的；圆以按下点为圆心（想圈的是"以这个露头为中心方圆多少"）
      const shape = props.roiShape
      // 矩形、圆按住拖出来，多边形逐点单击
      draw = shape === 'rect'
        ? new Draw({ type: 'Circle', geometryFunction: createBox(), freehand: true })
        : shape === 'circle' ? new Draw({ type: 'Circle', freehand: true }) : new Draw({ type: 'Polygon' })
      draw.on('drawstart', () => { sketch.current.points = 1 })
      draw.on('drawabort', () => { sketch.current.points = 0 })
      draw.on('drawend', (evt) => {
        sketch.current.points = 0
        let geom = evt.feature.getGeometry()!
        if (geom.getType() === 'Circle') geom = fromCircle(geom as never, 72)
        const proj = propsRef.current.projection
        const rings = (geom as Polygon).getCoordinates().map((ring) => ring.map((c) => toLonLat(c, proj)))
        propsRef.current.onRoiDrawn(rings)
      })
      added.push(draw)
    }

    if (tool === 'vertex') {
      const modify = new Modify({ features: editCollection.current })
      modify.on('modifyend', async (evt) => {
        for (const f of evt.features.getArray()) {
          const geometry = fmt().writeGeometryObject(f.getGeometry()!)
          try {
            await api.setGeometry(f.get('key'), geometry as never)
          } catch (e) {
            message.error((e as Error).message)
          }
        }
        propsRef.current.onChanged()
      })
      added.push(modify)
    }

    if (propsRef.current.snap && (tool === 'polygon' || tool === 'vertex')) {
      added.push(new Snap({ source: polySrc.current, pixelTolerance: 8 }))
    }
    added.forEach((i) => map.addInteraction(i))
    if (map.get(CAPTURE)) {
      added.forEach((i) => i.setActive(false))
      dragPanRef.current?.setActive(true)
    }
    toolInteractions.current = added
    sketch.current = { draw, points: 0 }
    // 草图每落一个点（单击）记一笔，右键闭合要够 3 个点
    const onDrawClick = () => { if (sketch.current.points > 0) sketch.current.points += 1 }
    if (draw) map.on('click', onDrawClick)

    // 画面 / 画范围时：退格删上一个点，回车闭合，Esc 放弃
    const onKey = (e: KeyboardEvent) => {
      if (!draw || (tool !== 'polygon' && tool !== 'roi')) return
      // 焦点在输入框、浮动窗口或照片窗口里时，这些键是给它们的
      if ((e.target as HTMLElement).closest?.('input, textarea, .float') || document.querySelector('.pv-backdrop')) return
      if (e.key === 'Backspace' || e.key === 'Delete') {
        draw.removeLastPoint()
        e.stopImmediatePropagation()
        e.preventDefault()
      } else if (e.key === 'Enter') {
        draw.finishDrawing()
      } else if (e.key === 'Escape') {
        draw.abortDrawing()
        e.stopImmediatePropagation()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => {
      window.removeEventListener('keydown', onKey, true)
      if (draw) map.un('click', onDrawClick)
      added.forEach((i) => map.removeInteraction(i))
      toolInteractions.current = []
      sketch.current = { draw: null, points: 0 }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.tool, props.snap, props.roiShape, props.projection])

  // ------------------------------------------------------------- 空格平移
  // 鼠标在画布上时按住空格：左键拖动临时变平移（工具交互先停掉），松开恢复。
  // 没有中键的触控板靠它平移；同 Photoshop / QGIS 的习惯
  const overMap = useRef(false)
  useEffect(() => {
    const el = divRef.current!
    const enter = () => { overMap.current = true }
    const leave = () => { overMap.current = false }
    el.addEventListener('pointerenter', enter)
    el.addEventListener('pointerleave', leave)
    const typing = isTypingTarget
    const down = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || !overMap.current || typing(e.target)) return
      e.preventDefault()                      // 别让空格去按下刚点过的按钮
      if (spaceHeld.current) return
      spaceHeld.current = true
      toolInteractions.current.forEach((i) => i.setActive(false))
      dragPanRef.current?.setActive(true)
      setPanning('ready')
    }
    const up = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || !spaceHeld.current) return
      spaceHeld.current = false
      const cap = !!mapRef.current?.get(CAPTURE)
      toolInteractions.current.forEach((i) => i.setActive(!cap))
      const t = propsRef.current.tool
      dragPanRef.current?.setActive(cap || t === 'pan' || t === 'spectrum')
      setPanning('')
    }
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    return () => {
      el.removeEventListener('pointerenter', enter)
      el.removeEventListener('pointerleave', leave)
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 正在画面 / 画多边形范围时，右键是闭合（够 3 个点）而不是菜单
  const onMapContextMenu = (e: React.MouseEvent) => {
    const sk = sketch.current
    if (!sk.draw || sk.points <= 0) return
    e.preventDefault()
    e.stopPropagation()
    if (sk.points >= 3) sk.draw.finishDrawing()
  }

  // ------------------------------------------------------------- 右键菜单
  // 右键落在没选中的要素上：先把它选成唯一选中，菜单里的动作都对选中集做
  const onContextMenu = (e: React.MouseEvent) => {
    const map = mapRef.current
    if (!map) return
    const p = propsRef.current
    const pixel = map.getEventPixel(e.nativeEvent)
    const coord = map.getCoordinateFromPixel(pixel)
    if (coord) p.onContextPoint?.(toLonLat(coord, p.projection) as [number, number])
    const top = hitKeys(pixel).samples[0]
    let keys = p.selected
    if (top && !p.selected.has(top)) {
      keys = new Set([top])
      p.onSelect(keys)
    }
    ctxKeysRef.current = keys
  }

  const drawing = ['point', 'polygon', 'roi', 'spectrum'].includes(props.tool)
  const cursor = panning === 'drag' ? 'grabbing' : panning === 'ready' ? 'grab'
    : drawing || capturing ? 'crosshair' : props.tool === 'pan' ? 'grab' : props.tool === 'vertex' ? 'pointer'
      : props.tool === 'erase' ? 'not-allowed' : 'default'
  return (
    <Dropdown
      trigger={['contextMenu']}
      menu={{
        items: props.contextMenu(props.selected),
        onClick: ({ key }) => props.onContextAction(key, ctxKeysRef.current),
      }}
    >
      <div className="map-wrap" onContextMenu={onContextMenu}>
        <div ref={divRef} className="map" style={{ cursor }} onContextMenu={onMapContextMenu} />
        {(() => {
          const prep = props.layers.find((l) => l.preparing != null)
          if (!prep && !showLoading) return null
          return (
            <div className="map-loading" role="status">
              <div className={'map-loading-bar' + (prep ? '' : ' indeterminate')}>
                <span style={prep ? { width: `${Math.max(4, Math.round((prep.preparing ?? 0) * 100))}%` } : undefined} />
              </div>
              <div className="map-loading-text">
                {prep ? `正在为「${prep.name}」建立显示金字塔 ${Math.round((prep.preparing ?? 0) * 100)}%，完成后缩放、平移更流畅` : '正在加载影像…'}
              </div>
            </div>
          )
        })()}
        {props.emptyText ? (
          <div className="map-empty">
            <div className="map-empty-title">{props.emptyText.title}</div>
            <div className="map-empty-hint">{props.emptyText.hint}</div>
          </div>
        ) : null}
      </div>
    </Dropdown>
  )
}
