// 野外记录在画布上的图层：轨迹、点位、选中点位的照片朝向。
//
// 直接挂在外壳的 OpenLayers 地图上，不经过 MapView 的样本图层——野外点位与样本是
// 两回事，样式也刻意分开：样本是类别色的圆点，野外点位是带白边的菱形，一眼分得清。
// 显示坐标系换了（地图换了 View），要素按新坐标系重投影。

import { useEffect, useRef } from 'react'
import type OlMap from 'ol/Map'
import VectorLayer from 'ol/layer/Vector'
import VectorSource from 'ol/source/Vector'
import OlFeature from 'ol/Feature'
import Point from 'ol/geom/Point'
import LineString from 'ol/geom/LineString'
import { Circle, Fill, RegularShape, Stroke, Style, Text } from 'ol/style'
import { toLonLat, transform } from 'ol/proj'
import Collection from 'ol/Collection'
import Translate from 'ol/interaction/Translate'
import type { FeatureLike } from 'ol/Feature'
import type { Geometry } from 'ol/geom'

import type { FwPhoto, FwPoint, FwTrack } from './fwapi'
import type { Palette } from '../generated/tokens'
import { CLAIMS_CLICK, modulesMayPick } from '../canvasLink'

/** 轨迹按批次轮换的颜色：深色画布上都读得清，避开样本常用的纯蓝纯绿。 */
const TRACK_COLORS = ['#E0A458', '#E08A63', '#C6A0F0', '#7FD1C7', '#F0D264', '#F29BB8', '#9FC3F0']
const POINT_FILL = '#F2B134'
/** 缩放到这一级以上才写点位名称，免得全图时糊成一片 */
const LABEL_ZOOM = 12

export type FieldPick = { kind: 'point' | 'track' | 'photo'; uid: string }

interface Options {
  map: OlMap | null
  projection: string
  visible: boolean
  points: FwPoint[]
  tracks: FwTrack[]
  selectedPoint: string | null
  selectedTrack: string | null
  /** 选中点位的照片：有朝向的画一个视线扇形 */
  photos: FwPhoto[]
  /** 轨迹高程剖面上鼠标所在处（经纬度），画一个圆点 */
  cursor?: [number, number] | null
  surveyColors: Record<string, string>
  palette: Palette
  onPick: (pick: FieldPick | null, pixel: number[]) => void
  /** 正在画布上拖动调整位置的点位；松手时报出新位置 */
  moving?: string | null
  onMoved?: (uid: string, lon: number, lat: number) => void
}

export function surveyColor(index: number): string {
  return TRACK_COLORS[index % TRACK_COLORS.length]
}

export function useFieldLayer(o: Options): void {
  const optsRef = useRef(o)
  optsRef.current = o
  const srcRef = useRef({
    tracks: new VectorSource<OlFeature<Geometry>>(),
    points: new VectorSource<OlFeature<Geometry>>(),
    photos: new VectorSource<OlFeature<Geometry>>(),
    cursor: new VectorSource<OlFeature<Geometry>>(),
  })
  const layersRef = useRef<VectorLayer[]>([])

  // ------------------------------------------------------------- 建层、点选
  useEffect(() => {
    const map = o.map
    if (!map) return
    const src = srcRef.current
    const trackLayer = new VectorLayer({ source: src.tracks, zIndex: 150, style: trackStyle })
    const photoLayer = new VectorLayer({ source: src.photos, zIndex: 151, style: photoStyle })
    // 名称互相避让（挨得近的点位不叠字），菱形本身不参与避让、一个不少
    const pointLayer = new VectorLayer({ source: src.points, zIndex: 152, style: pointStyle, declutter: true })
    const layers = [trackLayer, photoLayer, pointLayer]
    // 点到这几层上的要素时，画布不再拿这次单击去选样本（见 MapView）
    layers.forEach((l) => { l.set(CLAIMS_CLICK, true); map.addLayer(l) })
    // 剖面图上的位置只是个指示，不参与点选
    const cursorLayer = new VectorLayer({ source: src.cursor, zIndex: 153, style: cursorStyle })
    map.addLayer(cursorLayer)
    layersRef.current = [...layers, cursorLayer]

    /** 单击处最上面的野外要素；点位优先于轨迹（点位常落在轨迹上） */
    const topAt = (pixel: number[]): OlFeature<Geometry> | null => {
      let best: OlFeature<Geometry> | null = null
      map.forEachFeatureAtPixel(pixel, (f) => {
        const k = f.get('fwkind')
        if (!best || (k !== 'track' && best.get('fwkind') === 'track')) best = f as OlFeature<Geometry>
        return k === 'track' ? undefined : true
      }, { layerFilter: (l) => layers.includes(l as VectorLayer), hitTolerance: 5 })
      return best
    }
    const onClick = (evt: { pixel: number[] }) => {
      // 画点、画面、取谱等工具下单击另有用处，野外要素不抢
      if (!optsRef.current.visible || !modulesMayPick(map)) return
      const f = topAt(evt.pixel)
      optsRef.current.onPick(f ? { kind: f.get('fwkind'), uid: f.get('uid') } : null, evt.pixel)
    }
    const onMove = (evt: { pixel: number[]; dragging: boolean }) => {
      if (evt.dragging) return
      const el = map.getTargetElement() as HTMLElement | null
      if (!el) return
      // 移动点位时：鼠标在那个点位上显示移动光标，其余要素不提示
      const mv = optsRef.current.moving
      el.classList.toggle('fw-moving', !!mv && optsRef.current.visible && topAt(evt.pixel)?.get('uid') === mv)
      if (mv) {
        el.classList.remove('fw-hover')
        if (el.title) el.title = ''
        return
      }
      const f = optsRef.current.visible && modulesMayPick(map) ? topAt(evt.pixel) : null
      el.classList.toggle('fw-hover', !!f)
      // 悬停提示名称：缩小到看不见名称标注时也能认出是哪个点位、哪条轨迹
      const tip = f ? (f.get('fwkind') === 'photo' ? '照片拍摄方向，单击查看照片' : f.get('name') || (f.get('fwkind') === 'track' ? '轨迹' : '未命名点位')) : ''
      if (el.title !== tip) el.title = tip
    }
    // 显示坐标系变了：地图换了 View，要素重投影
    const onView = () => rebuild()
    const onZoom = () => pointLayer.changed()
    map.on('click', onClick)
    map.on('pointermove', onMove)
    map.on('change:view', onView)
    map.on('moveend', onZoom)
    return () => {
      map.un('click', onClick)
      map.un('pointermove', onMove)
      map.un('change:view', onView)
      map.un('moveend', onZoom)
      layers.forEach((l) => map.removeLayer(l))
      map.removeLayer(cursorLayer)
      layersRef.current = []
      const el = map.getTargetElement() as HTMLElement | null
      if (el) { el.classList.remove('fw-hover', 'fw-moving'); el.title = '' }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [o.map])

  // ------------------------------------------------------------- 数据变了
  function rebuild(): void {
    const p = optsRef.current
    const proj = p.map?.getView().getProjection().getCode() ?? p.projection
    const src = srcRef.current
    const tf = (lon: number, lat: number) => transform([lon, lat], 'EPSG:4326', proj)
    src.tracks.clear()
    src.points.clear()
    src.photos.clear()
    src.tracks.addFeatures(p.tracks.filter((t) => t.coords && t.coords.length > 1).map((t) => {
      const f = new OlFeature({ geometry: new LineString(t.coords!.map((c) => tf(c[0], c[1]))) })
      f.setProperties({ fwkind: 'track', uid: t.uid, name: t.name ?? '', color: t.color || p.surveyColors[t.survey ?? ''] || TRACK_COLORS[0] })
      return f
    }))
    src.points.addFeatures(p.points.map((pt) => {
      const f = new OlFeature({ geometry: new Point(tf(pt.lon, pt.lat)) })
      f.setProperties({ fwkind: 'point', uid: pt.uid, name: pt.name ?? '', color: pt.color || POINT_FILL,
        photos: pt.n_photos, spectra: pt.n_spectra })
      return f
    }))
    src.photos.addFeatures(p.photos.filter((ph) => ph.lon != null && ph.lat != null).map((ph) => {
      const f = new OlFeature({ geometry: new Point(tf(ph.lon!, ph.lat!)) })
      f.setProperties({ fwkind: 'photo', uid: ph.uid, azimuth: ph.azimuth })
      return f
    }))
  }

  useEffect(() => {
    rebuild()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [o.points, o.tracks, o.photos, o.projection, o.surveyColors, o.map])

  // 拖动调整点位位置：只让那一个点位能拖。点位数据一刷新要素就换了，跟着重挂
  useEffect(() => {
    const map = o.map
    const uid = o.moving
    if (!map || !uid || !o.visible) return
    const f = srcRef.current.points.getFeatures().find((x) => x.get('uid') === uid)
    if (!f) return
    const tr = new Translate({ features: new Collection([f]), hitTolerance: 5 })
    let from: number[] = []
    tr.on('translatestart', () => { from = (f.getGeometry() as Point).getCoordinates() })
    tr.on('translateend', () => {
      const c = (f.getGeometry() as Point).getCoordinates()
      // 只是点了一下、没拖动：不算移动
      if (c[0] === from[0] && c[1] === from[1]) return
      const [lon, lat] = toLonLat(c, map.getView().getProjection())
      optsRef.current.onMoved?.(uid, lon, lat)
    })
    map.addInteraction(tr)
    return () => { map.removeInteraction(tr) }
  }, [o.map, o.moving, o.points, o.visible, o.projection])

  useEffect(() => {
    const src = srcRef.current.cursor
    src.clear(true)
    const c = o.cursor
    if (!c) return
    const proj = o.map?.getView().getProjection().getCode() ?? o.projection
    src.addFeature(new OlFeature({ geometry: new Point(transform(c, 'EPSG:4326', proj)) }))
  }, [o.cursor, o.projection, o.map])

  useEffect(() => {
    layersRef.current.forEach((l) => l.setVisible(o.visible))
  }, [o.visible, o.map])

  useEffect(() => {
    layersRef.current.forEach((l) => l.changed())
  }, [o.selectedPoint, o.selectedTrack, o.palette])

  // ------------------------------------------------------------- 样式
  function trackStyle(f: FeatureLike): Style[] {
    const sel = optsRef.current.selectedTrack === f.get('uid')
    const pal = optsRef.current.palette
    return [
      new Style({ stroke: new Stroke({ color: pal.canvas_bg, width: sel ? 6 : 4 }), zIndex: 0 }),
      new Style({ stroke: new Stroke({ color: sel ? pal.canvas_select : f.get('color'), width: sel ? 3 : 2 }), zIndex: 1 }),
    ]
  }

  function pointStyle(f: FeatureLike): Style[] {
    const p = optsRef.current
    const sel = p.selectedPoint === f.get('uid')
    const zoom = p.map?.getView().getZoom() ?? 0
    const styles = [
      new Style({
        image: new RegularShape({
          points: 4, radius: sel ? 9 : 7, angle: 0, declutterMode: 'none',
          fill: new Fill({ color: f.get('color') }),
          stroke: new Stroke({ color: sel ? p.palette.canvas_select : p.palette.canvas_bg, width: sel ? 2.5 : 1.5 }),
        }),
        zIndex: sel ? 10 : 1,
      }),
    ]
    if (sel || zoom >= LABEL_ZOOM) {
      styles.push(new Style({
        text: new Text({
          text: f.get('name') || '', offsetY: -16, font: '12px "PingFang SC", sans-serif',
          fill: new Fill({ color: p.palette.canvas_text }),
          stroke: new Stroke({ color: p.palette.canvas_bg, width: 3 }),
        }),
        zIndex: sel ? 11 : 2,
      }))
    }
    return styles
  }

  function cursorStyle(): Style {
    const pal = optsRef.current.palette
    // 空心圈：落在选中轨迹（同为选中色）上也认得出
    return new Style({ image: new Circle({ radius: 7, fill: new Fill({ color: pal.canvas_bg }),
      stroke: new Stroke({ color: pal.canvas_select, width: 3 }) }) })
  }

  function photoStyle(f: FeatureLike): Style | undefined {
    const az = f.get('azimuth') as number | null
    const pal = optsRef.current.palette
    if (az == null) {
      return new Style({ image: new RegularShape({ points: 4, radius: 3, angle: Math.PI / 4,
        fill: new Fill({ color: pal.canvas_select }) }) })
    }
    // 视线方向：一个尖朝外的细三角，底边在拍摄点
    return new Style({
      image: new RegularShape({
        points: 3, radius: 11, rotation: (az * Math.PI) / 180, displacement: [0, 0],
        fill: new Fill({ color: 'rgba(245,241,230,0.35)' }),
        stroke: new Stroke({ color: pal.canvas_select, width: 1.2 }),
        scale: [0.55, 1.3],
      }),
    })
  }
}
