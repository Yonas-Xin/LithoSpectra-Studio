// 功能模块（野外记录、深度学习）直接往 OpenLayers 地图上挂图层、收单击，通过地图对象上的
// 这几个属性与画布（MapView）协调：画布当前的工具；模块临时接管单击；某个图层上的要素被
// 点到时，这次单击归模块，画布不拿它去选样本。

import type OlMap from 'ol/Map'
import { transformExtent } from 'ol/proj'

/** 画布当前的工具（MapView 的 Tool） */
export const TOOL = 'lithoTool'
/** 非空时由某个模块接管单击，画布自己不选、不识别，画样本的交互暂停 */
export const CAPTURE = 'lithoCapture'
/** 图层属性：这一层的要素被点到时单击归模块 */
export const CLAIMS_CLICK = 'lithoClaimsClick'

/** 画布当前工具下，功能模块能不能响应单击（画点、画面、取谱等工具下单击另有用处） */
export function modulesMayPick(map: OlMap): boolean {
  const t = map.get(TOOL)
  return !map.get(CAPTURE) && (t === undefined || t === 'pan' || t === 'select')
}

/** 画布上没被某个浮动窗口挡住的最大一块，[x0, y0, x1, y1]（像素，相对画布左上角）。
    窗口不压在画布上时是整个画布；空出来的地方太窄（窗口几乎全挡住）时也退回整个画布 */
export function freeRegion(map: OlMap, cover: Element | null | undefined): [number, number, number, number] | null {
  const size = map.getSize()
  if (!size) return null
  const [w, h] = size
  const all: [number, number, number, number] = [0, 0, w, h]
  const box = map.getViewport()?.getBoundingClientRect()
  const r = cover?.getBoundingClientRect()
  if (!box || !r) return all
  const l = r.left - box.left
  const rt = r.right - box.left
  const t = r.top - box.top
  const b = r.bottom - box.top
  if (rt <= 0 || l >= w || b <= 0 || t >= h) return all
  const cands: [number, number, number, number][] = [[0, 0, Math.max(0, l), h], [Math.min(w, rt), 0, w, h], [0, 0, w, Math.max(0, t)], [0, Math.min(h, b), w, h]]
  const area = (c: number[]) => Math.max(0, c[2] - c[0]) * Math.max(0, c[3] - c[1])
  const best = cands.reduce((a, c) => (area(c) > area(a) ? c : a))
  return best[2] - best[0] < 120 || best[3] - best[1] < 100 ? all : best
}

/** 地图坐标 coord 是否落在画布上的这一块里 */
export function inRegion(map: OlMap, coord: number[], region: number[]): boolean {
  const px = map.getPixelFromCoordinate(coord)
  return !!px && px[0] >= region[0] && px[0] <= region[2] && px[1] >= region[1] && px[1] <= region[3]
}

/** 平移（必要时连同缩放）地图，让 coord 落在 region 的正中。旋转过的地图不处理 */
export function bringInto(map: OlMap, coord: number[], region: number[], resolution?: number): void {
  const view = map.getView()
  const size = map.getSize()
  const res = resolution ?? view.getResolution()
  if (!size || !res || view.getRotation()) return
  const px = (region[0] + region[2]) / 2
  const py = (region[1] + region[3]) / 2
  view.animate({ center: [coord[0] - (px - size[0] / 2) * res, coord[1] + (py - size[1] / 2) * res], resolution: res, duration: 250 })
}

/** 把一个经纬度范围缩放到画布的 region 里（与外壳的「缩放到」同样四周留 5%、最大 18 级） */
export function fitInto(map: OlMap, extent: number[], region: number[]): void {
  const view = map.getView()
  const [x0, y0, x1, y1] = extent
  const pad = Math.max(x1 - x0, y1 - y0) * 0.05 || 0.01
  const e = transformExtent([x0 - pad, y0 - pad, x1 + pad, y1 + pad], 'EPSG:4326', view.getProjection())
  const res = Math.max((e[2] - e[0]) / (region[2] - region[0]), (e[3] - e[1]) / (region[3] - region[1]), view.getResolutionForZoom(18))
  bringInto(map, [(e[0] + e[2]) / 2, (e[1] + e[3]) / 2], region, res)
}
