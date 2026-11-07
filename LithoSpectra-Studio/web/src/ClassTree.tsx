import { useEffect, useMemo, useRef, useState } from 'react'
import { Dropdown, Empty, Input, Tree } from 'antd'
import type { MenuProps, TreeDataNode, TreeProps } from 'antd'
import type { ExportLevel, SampleClass } from './api'
import { Icon } from './theme'
import { kbd } from './platform'

export type TreeSel = { kind: 'class'; id: number } | { kind: 'region'; region: string } | null

export type TreeAction =
  | { type: 'zoom'; cls: SampleClass }
  | { type: 'zoom-region'; region: string }
  | { type: 'rename'; cls: SampleClass }
  | { type: 'description'; cls: SampleClass }
  | { type: 'color'; cls: SampleClass }
  | { type: 'copy'; sel: TreeSel }
  | { type: 'paste'; sel: TreeSel }
  | { type: 'merge'; target: SampleClass; picked: number[] }
  | { type: 'move-to'; cls: SampleClass }
  | { type: 'restore'; cls: SampleClass }
  | { type: 'rename-region'; region: string }
  | { type: 'delete-region'; region: string }
  | { type: 'new-region' }
  | { type: 'add'; parent: SampleClass | null; region: string; group: boolean }
  | { type: 'delete'; cls: SampleClass }
  | { type: 'move'; id: number; parentId: number | null; beforeId: number | null; region: string }
  | { type: 'promote'; leafId: number; otherId: number }

interface Props {
  classes: SampleClass[]
  /** 勾选 / 取消勾选若干类别（连同子类）。勾选记在后端会话里，逐条样本。 */
  onCheck: (classIds: number[], on: boolean) => void
  onCheckOp: (op: 'all' | 'none' | 'invert') => void
  onExportLevel: (classId: number, mode: ExportLevel) => void
  onClearExportLevels: () => void
  selection: TreeSel
  onSelect: (sel: TreeSel) => void
  /** 展开的节点；null 表示默认，展开各工区。由上层记进界面状态，下次启动恢复 */
  expanded: React.Key[] | null
  onExpand: (keys: React.Key[]) => void
  /** 要恢复到的滚动位置；对象每换一次就滚动一次 */
  scrollTo: { top: number } | null
  onScroll: (top: number) => void
  /** 要露出来的类别（在画布上点中了它的样本）：展开它的上级并滚到它。对象每换一次做一次 */
  reveal?: { id: number } | null
  canPaste: boolean
  /** 检索命中的类别（null 表示没有检索词），「仅在检索结果里筛」要用 */
  onFilter: (ids: number[] | null) => void
  onAction: (a: TreeAction) => void
}

const REGION = 'r:'
const CLASS = 'c:'

function matches(c: SampleClass, kw: string): boolean {
  if (!kw) return true
  const k = kw.toLowerCase()
  const fields = [c.name, c.region, c.description, c.source, ...Object.entries(c.attrs ?? {}).flat().map(String)]
  return fields.some((f) => (f ?? '').toLowerCase().includes(k))
}

export default function ClassTree(props: Props) {
  const lastClick = useRef<{ key: string; t: number } | null>(null)
  const { classes } = props
  const [keyword, setKeyword] = useState('')
  const { expanded } = props
  const scrollRef = useRef<HTMLDivElement>(null)

  const byId = useMemo(() => new Map(classes.map((c) => [c.id, c])), [classes])
  const children = useMemo(() => {
    const m = new Map<number | null, SampleClass[]>()
    for (const c of classes) {
      const list = m.get(c.parent_id) ?? []
      list.push(c)
      m.set(c.parent_id, list)
    }
    return m
  }, [classes])
  const regions = useMemo(() => {
    const out: string[] = []
    for (const c of children.get(null) ?? []) if (!out.includes(c.region)) out.push(c.region)
    return out
  }, [children])

  // 检索命中的类别连同它的祖先都要留着，否则命中的叶子挂不上树
  const visible = useMemo(() => {
    if (!keyword) return null
    const keep = new Set<number>()
    for (const c of classes) {
      if (!matches(c, keyword)) continue
      let cur: SampleClass | undefined = c
      while (cur && !keep.has(cur.id)) {
        keep.add(cur.id)
        cur = cur.parent_id != null ? byId.get(cur.parent_id) : undefined
      }
    }
    return keep
  }, [keyword, classes, byId])

  useEffect(() => {
    props.onFilter(keyword ? classes.filter((x) => matches(x, keyword)).map((x) => x.id) : null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword, classes])

  const matched = useMemo(() => {
    const list = visible ? classes.filter((c) => visible.has(c.id) && matches(c, keyword)) : classes
    const leaves = list.filter((c) => !c.is_group)
    return { n: leaves.length, samples: leaves.reduce((s, c) => s + c.count, 0) }
  }, [visible, classes, keyword])

  const subtree = (id: number): number[] => {
    const out = [id]
    for (const ch of children.get(id) ?? []) out.push(...subtree(ch.id))
    return out
  }

  // 勾到的类别：全勾或部分勾的都算
  const picked = classes.filter((x) => x.checked !== 'none').map((x) => x.id)
  const classMenu = (c: SampleClass): MenuProps['items'] => [
    { key: 'zoom', label: '缩放到此处' },
    { type: 'divider' },
    { key: 'copy', label: kbd('复制（⌘C）') },
    { key: 'paste', label: kbd('粘贴到此处（⌘V）'), disabled: !props.canPaste },
    { type: 'divider' },
    { key: 'rename', label: '重命名…' },
    { key: 'desc', label: '编辑说明…' },
    ...(c.is_group ? [] : [{ key: 'color', label: '设置颜色…' }]),
    { type: 'divider' as const },
    ...(c.is_group
      ? [
          { key: 'sub', label: '在此分组下新建类别…' },
          { key: 'grp', label: '在此分组下新建分组…' },
        ]
      : []),
    ...(picked.length > 1 && picked.includes(c.id) ? [{ key: 'merge', label: `将勾选的 ${picked.length} 个类别合并到此类别…` }] : []),
    { key: 'move-to', label: '移动到其他上级…' },
    {
      key: 'level',
      label: '导出层级',
      children: (
        [
          ['default', '默认（按勾选推断）', '整个分支全部勾选时合并为一个文件，部分勾选时按子类分别输出'],
          ['merge', '整支合并为一个文件', '无论勾选多少子类，该分支只导出一个文件'],
          ['split', '按子类分别输出', '该分支不单独输出，每个子类各导出一个文件'],
        ] as [ExportLevel, string, string][]
      ).map(([mode, label, tip]) => ({
        key: 'level-' + mode,
        label: (
          <span className="menu-check" title={tip}>
            <span className="menu-check-mark">{c.export_level === mode ? <Icon name="check" size={14} /> : null}</span>
            {label}
          </span>
        ),
      })),
    },
    { type: 'divider' },
    ...(c.edited && !c.draft ? [{ key: 'restore', label: '恢复到导入时的状态' }] : []),
    { key: 'del', label: '删除该类别及其样本…', danger: true },
    { type: 'divider' },
    ...checkMenu,
  ]
  const regionMenu: MenuProps['items'] = [
    { key: 'zoom-region', label: '缩放到此处' },
    { type: 'divider' },
    { key: 'copy', label: kbd('复制（⌘C）') },
    { key: 'paste', label: kbd('粘贴到此处（⌘V）'), disabled: !props.canPaste },
    { type: 'divider' },
    { key: 'rename-region', label: '重命名工区…' },
    { key: 'rsub', label: '在此工区下新建类别…' },
    { key: 'rgrp', label: '在此工区下新建分组…' },
    { key: 'new-region', label: '新建工区…' },
    { type: 'divider' },
    { key: 'delete-region', label: '删除该工区及其全部类别与样本…', danger: true },
  ]
  const checkMenu: NonNullable<MenuProps['items']> = [
    { key: 'all', label: '全选' },
    { key: 'none', label: '全不选' },
    { key: 'invert', label: '反选' },
    ...(keyword ? [{ key: 'filtered', label: '选中当前检索结果' }] : []),
    ...(classes.some((x) => x.export_level !== 'default')
      ? [{ type: 'divider' as const }, { key: 'clear-levels', label: '清除全部导出层级标记' }]
      : []),
  ]

  const onMenu = (key: string, c: SampleClass | null, region: string) => {
    const sel: TreeSel = c ? { kind: 'class', id: c.id } : { kind: 'region', region }
    if (key.startsWith('level-') && c) props.onExportLevel(c.id, key.slice(6) as ExportLevel)
    else if (key === 'clear-levels') props.onClearExportLevels()
    else if (key === 'copy') props.onAction({ type: 'copy', sel })
    else if (key === 'paste') props.onAction({ type: 'paste', sel })
    else if (key === 'desc' && c) props.onAction({ type: 'description', cls: c })
    else if (key === 'color' && c) props.onAction({ type: 'color', cls: c })
    else if (key === 'merge' && c) props.onAction({ type: 'merge', target: c, picked })
    else if (key === 'move-to' && c) props.onAction({ type: 'move-to', cls: c })
    else if (key === 'restore' && c) props.onAction({ type: 'restore', cls: c })
    else if (key === 'zoom-region') props.onAction({ type: 'zoom-region', region })
    else if (key === 'rename-region') props.onAction({ type: 'rename-region', region })
    else if (key === 'delete-region') props.onAction({ type: 'delete-region', region })
    else if (key === 'new-region') props.onAction({ type: 'new-region' })
    else if (key === 'zoom' && c) props.onAction({ type: 'zoom', cls: c })
    else if (key === 'rename' && c) props.onAction({ type: 'rename', cls: c })
    else if (key === 'sub' && c) props.onAction({ type: 'add', parent: c, region: c.region, group: false })
    else if (key === 'grp' && c) props.onAction({ type: 'add', parent: c, region: c.region, group: true })
    else if (key === 'rsub') props.onAction({ type: 'add', parent: null, region, group: false })
    else if (key === 'rgrp') props.onAction({ type: 'add', parent: null, region, group: true })
    else if (key === 'del' && c) props.onAction({ type: 'delete', cls: c })
    else if (key === 'all' || key === 'none' || key === 'invert') props.onCheckOp(key)
    else if (key === 'filtered') props.onCheck(classes.filter((x) => matches(x, keyword)).map((x) => x.id), true)
  }

  const treeData: TreeDataNode[] = useMemo(() => {
    const node = (c: SampleClass): TreeDataNode => {
      const kids = (children.get(c.id) ?? []).filter((k) => !visible || visible.has(k.id))
      return {
        key: CLASS + c.id,
        isLeaf: !c.is_group,
        title: (
          <Dropdown trigger={['contextMenu']} menu={{ items: classMenu(c), onClick: ({ key }) => onMenu(key, c, c.region) }}>
            <span className="tree-row" title={Object.entries(c.attrs ?? {}).map(([k, v]) => `${k}：${v}`).join('\n') || undefined}>
              <span className={'tree-name' + (c.is_group ? ' group' : '') + (c.draft ? ' draft' : '')}>
                {c.export_level === 'merge' ? <span className="level-mark">[合] </span> : null}
                {c.export_level === 'split' ? <span className="level-mark">[分] </span> : null}
                {c.edited ? '• ' : ''}
                {c.name}
              </span>
              <span className="tree-count">{c.total}</span>
            </span>
          </Dropdown>
        ),
        children: kids.map(node),
      }
    }
    return regions
      .map((r) => {
        const roots = (children.get(null) ?? []).filter((c) => c.region === r && (!visible || visible.has(c.id)))
        return {
          key: REGION + r,
          title: (
            <Dropdown trigger={['contextMenu']} menu={{ items: [...regionMenu!, { type: 'divider' }, ...checkMenu], onClick: ({ key }) => onMenu(key, null, r) }}>
              <span className="tree-row">
                <span className="tree-name region">{r || '（未分工区）'}</span>
                <span className="tree-count">{roots.reduce((s, c) => s + c.total, 0)}</span>
              </span>
            </Dropdown>
          ),
          children: roots.map(node),
        }
      })
      .filter((n) => n.children.length > 0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [children, visible, regions, classes, keyword])

  const allKeys = useMemo(() => {
    const out: React.Key[] = []
    const walk = (nodes: TreeDataNode[]) =>
      nodes.forEach((n) => {
        out.push(n.key)
        if (n.children) walk(n.children)
      })
    walk(treeData)
    return out
  }, [treeData])

  // 恢复上次的滚动位置：等类别载入、树按展开状态排好之后再滚
  const pendingScroll = useRef<{ top: number } | null>(null)
  useEffect(() => { pendingScroll.current = props.scrollTo }, [props.scrollTo])
  useEffect(() => {
    const want = pendingScroll.current
    if (!want || !treeData.length) return
    const id = requestAnimationFrame(() => {
      if (scrollRef.current && pendingScroll.current === want) {
        scrollRef.current.scrollTop = want.top
        pendingScroll.current = null
      }
    })
    return () => cancelAnimationFrame(id)
  }, [props.scrollTo, treeData])

  // 在画布上点中样本后，树上它的类别被选中：展开上级、滚到它那一行。展开有动画，
  // 动画结束后再对一次位置
  useEffect(() => {
    const c = props.reveal ? byId.get(props.reveal.id) : undefined
    if (!c) return
    const need: React.Key[] = [REGION + c.region]
    for (let p = c.parent_id != null ? byId.get(c.parent_id) : undefined; p; p = p.parent_id != null ? byId.get(p.parent_id) : undefined) {
      need.push(CLASS + p.id)
    }
    const cur = expanded ?? treeData.map((n) => n.key)
    const missing = need.filter((k) => !cur.includes(k))
    if (!keyword && missing.length) props.onExpand([...cur, ...missing])
    const scroll = () => scrollRef.current?.querySelector('.ant-tree-treenode-selected')?.scrollIntoView({ block: 'nearest' })
    const t1 = window.setTimeout(scroll, 30)
    const t2 = window.setTimeout(scroll, 350)
    return () => { window.clearTimeout(t1); window.clearTimeout(t2) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.reveal])

  // 勾选是逐条样本的（记在后端会话里），树上按样本数推出三态：全勾 / 部分 / 未勾。
  // checkStrictly 关掉 Tree 自己的上下联动，三态完全以后端算的为准。
  const regionState = (r: string) => {
    const roots = (children.get(null) ?? []).filter((c) => c.region === r)
    if (roots.every((c) => c.checked === 'all') && roots.length) return 'all'
    return roots.some((c) => c.checked !== 'none') ? 'partial' : 'none'
  }
  const checkedKeys = {
    checked: [
      ...classes.filter((c) => c.checked === 'all').map((c) => CLASS + c.id),
      ...regions.filter((r) => regionState(r) === 'all').map((r) => REGION + r),
    ],
    halfChecked: [
      ...classes.filter((c) => c.checked === 'partial').map((c) => CLASS + c.id),
      ...regions.filter((r) => regionState(r) === 'partial').map((r) => REGION + r),
    ],
  }
  const onCheck: TreeProps['onCheck'] = (_keys, info) => {
    const k = String(info.node.key)
    // 部分勾选的节点不在 checked 里，点一下就是"全勾上"
    const on = info.checked
    if (k.startsWith(REGION)) {
      const r = k.slice(REGION.length)
      props.onCheck((children.get(null) ?? []).filter((c) => c.region === r).map((c) => c.id), on)
    } else {
      props.onCheck([Number(k.slice(CLASS.length))], on)
    }
  }

  const onDrop: TreeProps['onDrop'] = (info) => {
    const dragKey = String(info.dragNode.key)
    const dropKey = String(info.node.key)
    if (!dragKey.startsWith(CLASS)) return
    const dragId = Number(dragKey.slice(CLASS.length))
    const pos = info.node.pos.split('-')
    const rel = info.dropPosition - Number(pos[pos.length - 1])

    if (dropKey.startsWith(REGION)) {
      props.onAction({ type: 'move', id: dragId, parentId: null, beforeId: null, region: dropKey.slice(REGION.length) })
      return
    }
    const target = byId.get(Number(dropKey.slice(CLASS.length)))
    if (!target || target.id === dragId) return
    if (!info.dropToGap && rel === 0) {
      // 拖到分组中间：成为它的子类；拖到基础类别中间：两者升一级，装进一个新分组
      if (target.is_group) props.onAction({ type: 'move', id: dragId, parentId: target.id, beforeId: null, region: target.region })
      else props.onAction({ type: 'promote', leafId: target.id, otherId: dragId })
      return
    }
    const siblings = (children.get(target.parent_id) ?? []).filter((c) => c.region === target.region)
    const idx = siblings.findIndex((c) => c.id === target.id)
    const before = rel < 0 ? target : siblings.slice(idx + 1).find((c) => c.id !== dragId)
    props.onAction({ type: 'move', id: dragId, parentId: target.parent_id, beforeId: before?.id ?? null, region: target.region })
  }

  return (
    <div className="panel">
      <div className="panel-title">样本</div>
      <div className="panel-body tree-panel">
        <div className="caption">样本库 / 工区 / 类别</div>
        <Input
          allowClear
          prefix={<Icon name="search" size={14} />}
          placeholder="检索类别名、工区或属性值"
          onChange={(e) => setKeyword(e.target.value.trim())}
        />
        <div className="tree-frame">
          <div className="tree-header">
            <span>名称</span>
            <span>样本</span>
          </div>
          <div className="tree-scroll" ref={scrollRef} onMouseDownCapture={holdDrag}
            onScroll={(e) => { if (!keyword) props.onScroll(e.currentTarget.scrollTop) }}>
            {treeData.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={keyword ? '没有匹配的类别' : '样本库为空'} />
            ) : (
              <Tree
                blockNode
                checkable
                checkStrictly
                draggable={{ icon: false }}
                treeData={treeData}
                checkedKeys={checkedKeys}
                onCheck={onCheck}
                expandedKeys={keyword ? allKeys : expanded ?? treeData.map((n) => n.key)}
                onExpand={(keys) => { if (!keyword) props.onExpand(keys) }}
                selectedKeys={props.selection ? [props.selection.kind === 'class' ? CLASS + props.selection.id : REGION + props.selection.region] : []}
                onSelect={(_keys, info) => {
                  // 单击已选中的节点仍保持选中（不反选），双击时不会先把它取消掉
                  const k = String(info.node.key)
                  props.onSelect(k.startsWith(CLASS) ? { kind: 'class', id: Number(k.slice(CLASS.length)) }
                    : k.startsWith(REGION) ? { kind: 'region', region: k.slice(REGION.length) } : null)
                }}
                onClick={(_, node) => {
                  // 双击缩放：WebKit（桌面版的窗口）里 draggable 的行收不到 dblclick，
                  // 自己按「同一节点 400 毫秒内两次单击」判断
                  const k = String(node.key)
                  const now = Date.now()
                  const last = lastClick.current
                  lastClick.current = { key: k, t: now }
                  if (!last || last.key !== k || now - last.t > 400) return
                  lastClick.current = null
                  if (k.startsWith(REGION)) return props.onAction({ type: 'zoom-region', region: k.slice(REGION.length) })
                  if (!k.startsWith(CLASS)) return
                  const c = byId.get(Number(k.slice(CLASS.length)))
                  if (c) props.onAction({ type: 'zoom', cls: c })
                }}
                onDrop={onDrop}
                allowDrop={({ dropNode, dropPosition }) => !String(dropNode.key).startsWith(REGION) || dropPosition === 0}
              />
            )}
          </div>
        </div>
        <div className="caption">
          匹配 {matched.n} 类 / {matched.samples} 条样本
        </div>
      </div>
    </div>
  )
}

export function subtreeIds(classes: SampleClass[], id: number): number[] {
  const kids = new Map<number, number[]>()
  for (const c of classes) if (c.parent_id != null) kids.set(c.parent_id, [...(kids.get(c.parent_id) ?? []), c.id])
  const out: number[] = []
  const walk = (i: number) => {
    out.push(i)
    for (const k of kids.get(i) ?? []) walk(k)
  }
  walk(id)
  return out
}

/**
 * 整行都是拖动源：在勾选框、展开箭头上按下时手一抖就成了拖动，勾不上。在这些位置按下时
 * 暂时关掉这一行的拖动，松手后恢复——拖动调整层级只从名称上开始。
 */
function holdDrag(e: React.MouseEvent) {
  const t = e.target as HTMLElement
  if (!t.closest('.ant-tree-checkbox, .ant-tree-switcher, .ant-tree-indent')) return
  const row = t.closest<HTMLElement>('.ant-tree-treenode[draggable="true"]')
  if (!row) return
  row.setAttribute('draggable', 'false')
  const restore = () => {
    row.setAttribute('draggable', 'true')
    window.removeEventListener('mouseup', restore, true)
  }
  window.addEventListener('mouseup', restore, true)
}
