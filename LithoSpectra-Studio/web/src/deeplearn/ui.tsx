// 工作台里共用的几种版面元素：页头、卡片、状态标签、统计块、类别组成条。
// 样式在 deeplearn.css，形态与主界面右栏的卡片一致（12px 圆角、细边框加柔和投影）。

import type { ReactNode } from 'react'
import type { ClassRow } from './dlapi'

export function PageHead(props: { title: string; desc?: ReactNode; actions?: ReactNode; extra?: ReactNode }) {
  return (
    <header className="dl-page-head">
      <div className="dl-page-title">
        <h3>{props.title}</h3>
        {props.extra}
        {props.actions ? <div className="dl-head-actions">{props.actions}</div> : null}
      </div>
      {props.desc ? <p className="dl-page-desc">{props.desc}</p> : null}
    </header>
  )
}

export type Tone = 'neutral' | 'ok' | 'warn' | 'error' | 'accent'

export function Chip(props: { tone?: Tone; title?: string; children: ReactNode }) {
  return <span className={'dl-chip ' + (props.tone ?? 'neutral')} title={props.title}>{props.children}</span>
}

export function Card(props: {
  title?: ReactNode; sub?: ReactNode; chips?: ReactNode; aside?: ReactNode
  foot?: ReactNode; className?: string; selected?: boolean; children?: ReactNode
}) {
  return (
    <section className={'dl-card' + (props.selected ? ' on' : '') + (props.className ? ' ' + props.className : '')}>
      {props.title || props.chips || props.aside ? (
        <div className="dl-card-head">
          <div className="dl-card-heading">
            <div className="dl-card-title">{props.title}{props.chips}</div>
            {props.sub ? <div className="dl-card-sub">{props.sub}</div> : null}
          </div>
          {props.aside ? <div className="dl-card-aside">{props.aside}</div> : null}
        </div>
      ) : null}
      {props.children ? <div className="dl-card-body">{props.children}</div> : null}
      {props.foot ? <div className="dl-card-foot">{props.foot}</div> : null}
    </section>
  )
}

export interface Stat {
  label: string
  value: ReactNode
  hint?: string
  tone?: Tone
}

export function Stats({ items }: { items: Stat[] }) {
  return (
    <div className="dl-stats">
      {items.map((s) => (
        <div key={s.label} className={'dl-stat ' + (s.tone ?? '')} title={s.hint}>
          <div className="dl-stat-value mono">{s.value}</div>
          <div className="dl-stat-label">{s.label}</div>
        </div>
      ))}
    </div>
  )
}

/** 类别组成：一根按样本数分段的色条，下面是各类的名称与数量。 */
export function ClassMix(props: { counts: number[]; classes: ClassRow[]; compact?: boolean }) {
  const total = props.counts.reduce((a, b) => a + b, 0) || 1
  const rows = props.counts.map((n, i) => {
    const c = props.classes.find((r) => r.label === i)
    return { label: i, n, name: c?.name ?? `类别 ${i}`, color: c?.color || 'var(--border-strong)', path: c?.path }
  })
  return (
    <div className="dl-mix">
      <div className="dl-mix-bar">
        {rows.filter((r) => r.n > 0).map((r) => (
          <span key={r.label} style={{ flexGrow: r.n, background: r.color }} title={`${r.name}：${r.n}（${((r.n / total) * 100).toFixed(1)}%）`} />
        ))}
      </div>
      {props.compact ? null : (
        <div className="dl-mix-legend">
          {rows.map((r) => (
            <span key={r.label} className={r.n ? '' : 'empty'} title={r.path || r.name}>
              <i style={{ background: r.color }} />
              <span className="dl-mix-name">{r.name}</span>
              <span className="mono">{r.n}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/** 一排类别色块，模型卡片用。 */
export function ClassStrip({ classes }: { classes: ClassRow[] }) {
  return (
    <div className="dl-strip">
      {classes.map((c) => <span key={c.label} style={{ background: c.color }} title={c.name} />)}
    </div>
  )
}

export function EmptyCard(props: { title: string; desc?: ReactNode; action?: ReactNode }) {
  return (
    <div className="dl-empty">
      <div className="dl-empty-title">{props.title}</div>
      {props.desc ? <div className="dl-empty-desc">{props.desc}</div> : null}
      {props.action ? <div className="dl-empty-action">{props.action}</div> : null}
    </div>
  )
}

/** 速度档：四格，亮几格表示多快。 */
export function SpeedMeter({ speed }: { speed: string }) {
  const level = { fast: 4, medium: 3, slow: 2, very_slow: 1 }[speed] ?? 2
  return (
    <span className={'dl-speed s' + level}>
      {[1, 2, 3, 4].map((i) => <i key={i} className={i <= level ? 'on' : ''} />)}
    </span>
  )
}
