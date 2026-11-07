// 浮动工具窗：曲线、光谱库、工具箱、野外记录、深度学习用它。
//
// 取谱要一边在影像上点、一边盯着曲线看，停靠在主窗口里两件事抢同一块屏幕。所以这几个
// 窗口浮在画布之上、标题栏拖动、右下角拉大小，不挡住别的操作（不是模态的）。
//
// 几个窗口同时开着时，点哪个哪个到最上面；关掉再打开回到上次的位置与大小（本次运行内）。

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Icon } from './theme'


interface Props {
  title: React.ReactNode
  /** 记位置用的名字；不给时用字符串标题 */
  id?: string
  onClose: () => void
  initial: { x: number; y: number; w: number; h: number }
  extra?: React.ReactNode
  children: React.ReactNode
  onFocus?: () => void
}

// ------------------------------------------------------------- 叠放次序
// 开着的窗口从下到上排成一列；层级 = 底数 + 名次，最多九十来个窗口也压不到照片窗口（1000）上
const stack: number[] = []
const listeners = new Set<() => void>()
let nextId = 0
let version = 0
const notify = () => { version += 1; listeners.forEach((f) => f()) }
const subscribe = (f: () => void) => { listeners.add(f); return () => { listeners.delete(f) } }
const raise = (id: number) => {
  const i = stack.indexOf(id)
  if (i === stack.length - 1) return
  if (i >= 0) stack.splice(i, 1)
  stack.push(id)
  notify()
}

// 关掉时的位置与大小，按窗口名字记
const remembered = new Map<string, { x: number; y: number; w: number; h: number }>()

/** 标题栏至少露出这么多，拖得回来 */
const KEEP_W = 120
const KEEP_H = 40
const clampPos = (x: number, y: number) => ({
  x: Math.max(0, Math.min(window.innerWidth - KEEP_W, x)),
  y: Math.max(0, Math.min(window.innerHeight - KEEP_H, y)),
})

export default function Float(props: Props) {
  const key = props.id ?? (typeof props.title === 'string' ? props.title : '')
  const [start] = useState(() => {
    const last = key ? remembered.get(key) : undefined
    const g = last ?? props.initial
    return { ...g, ...clampPos(g.x, g.y) }
  })
  const [pos, setPos] = useState({ x: start.x, y: start.y })
  const drag = useRef<{ dx: number; dy: number } | null>(null)
  const ref = useRef<HTMLDivElement>(null)
  const [uid] = useState(() => ++nextId)
  useSyncExternalStore(subscribe, () => version)
  const posRef = useRef(pos)
  useEffect(() => { posRef.current = pos }, [pos])

  useEffect(() => {
    stack.push(uid)
    notify()
    return () => {
      const i = stack.indexOf(uid)
      if (i >= 0) stack.splice(i, 1)
      notify()
    }
  }, [uid])

  // 记下位置与大小。大小由 CSS resize 拖出来，写在元素的 style 上，随时量下来；
  // 关窗时元素已经拆掉，量不到了
  const sizeRef = useRef({ w: start.w, h: start.h })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      // 按样式里的宽高记（与 style.width 同一口径），不含边框，反复开关不会越来越大
      const cs = getComputedStyle(el)
      const w = parseFloat(cs.width)
      const h = parseFloat(cs.height)
      if (w > 0 && h > 0) sizeRef.current = { w, h }
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      if (key) remembered.set(key, { ...posRef.current, ...sizeRef.current })
    }
  }, [key])

  useEffect(() => {
    const move = (e: MouseEvent) => {
      if (!drag.current) return
      setPos(clampPos(e.clientX - drag.current.dx, e.clientY - drag.current.dy))
    }
    const up = () => { drag.current = null }
    // 主窗口缩小后标题栏可能跑到屏幕外，拉回来
    const resize = () => setPos((p) => {
      const c = clampPos(p.x, p.y)
      return c.x === p.x && c.y === p.y ? p : c
    })
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    window.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      window.removeEventListener('resize', resize)
    }
  }, [])

  const rank = stack.indexOf(uid)
  return (
    <div ref={ref} className="float" tabIndex={-1}
      style={{ left: pos.x, top: pos.y, width: start.w, height: start.h, zIndex: 900 + Math.max(0, rank) }}
      onMouseDownCapture={() => raise(uid)}
      onMouseDown={props.onFocus}>
      <div className="float-head" onMouseDown={(e) => {
        if ((e.target as HTMLElement).closest('button, .ant-select, input')) return
        drag.current = { dx: e.clientX - pos.x, dy: e.clientY - pos.y }
        e.preventDefault()
      }}>
        <span className="float-title">{props.title}</span>
        <span className="float-extra">{props.extra}</span>
        <button className="float-close" title="关闭" onClick={props.onClose}>
          <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
        </button>
      </div>
      <div className="float-body">{props.children}</div>
    </div>
  )
}

export { Icon }
