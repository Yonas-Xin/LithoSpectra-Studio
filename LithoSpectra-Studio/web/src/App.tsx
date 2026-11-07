import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { App as AntApp, Button, Dropdown, Input, Tooltip } from 'antd'
import type { MenuProps } from 'antd'
import proj4 from 'proj4'
import { register as registerProj4 } from 'ol/proj/proj4'
import { get as getProjection, transformExtent } from 'ol/proj'

import { api } from './api'
import type {
  AppInfo, AttrProfile, BackupEntry, Basemap, ExportOptions, ExportResult, Feature, FeatureCollection, ImportResult,
  LayerInfo, LayerPatch, LibState, SampleClass, TaskInfo,
} from './api'
import { desktop, isTypingTarget, whenDesktopReady } from './desktop'
import ClassTree from './ClassTree'
import type { TreeAction, TreeSel } from './ClassTree'
import Inspector, { IdentifyPanel } from './Inspector'
import MapView from './MapView'
import type { RoiShape, Tool } from './MapView'
import { AttrImportDialog, ExportDialog, RestoreDialog } from './IoDialogs'
import type { AttrImportOptions, ExportRequest } from './IoDialogs'
import { LayerPanel, RasterPropsDialog, RoiSelectDialog, UniqueColorsDialog } from './Layers'
import type { LayerId, SamplesLayerState } from './Layers'
import {
  AboutModal, ColorDialog, DescriptionDialog, HelpModal, KeysDialog, MoveFeaturesDialog, MoveToDialog, subtree,
} from './MiscDialogs'
import { useNameDialog } from './NameDialog'
import { GroupMeansDialog, LibraryWindow, SpectrumWindow, sapi } from './Spectra'
import type { BoardState } from './Spectra'
import { ClipDialog, ToolboxWindow, clipApi } from './ModuleDialogs'
import Fieldwork from './fieldwork'
import DeepLearn from './deeplearn'
import type { DeepLearnHandle } from './deeplearn'
import type { FieldworkHandle } from './fieldwork'
import type OlMap from 'ol/Map'
import type { ClipRequest } from './ModuleDialogs'
import { useTaskRunner } from './tasks'
import { Chevron, Icon, MODE_LABELS } from './theme'
import type { ThemeMode } from './theme'
import type { Palette } from './generated/tokens'
import type { IconName } from './generated/icons'
import { REVEAL, kbd } from './platform'

type Mode = 'browse' | 'annotate'

// 数字键按此顺序直达工具；改样本的那四件只在标注模式可用
const TOOLS: { id: Tool; label: string; icon: IconName; hint: string; edits: boolean; key: string; module?: string }[] = [
  { id: 'pan', label: '平移', icon: 'hand', hint: '拖动平移，滚轮缩放，双击显示全图；单击查看要素信息。', edits: false, key: '1' },
  { id: 'select', label: '选择', icon: 'pointer', hint: '单击选中要素，Shift 加选，空白处拖框多选。', edits: false, key: '2' },
  { id: 'point', label: '画点', icon: 'map-pin', hint: '单击添加一个点样本。', edits: true, key: '3' },
  { id: 'polygon', label: '画面', icon: 'pentagon', hint: '依次单击添加顶点，双击、回车或右键闭合；退格删除上一个顶点，Esc 取消。', edits: true, key: '4' },
  { id: 'vertex', label: '改顶点', icon: 'spline', hint: '单击选中面，拖动顶点移动；Alt 单击删除顶点，拖动边的中点插入顶点。', edits: true, key: '5' },
  { id: 'erase', label: '删要素', icon: 'eraser', hint: '单击要素将其删除。', edits: true, key: '6' },
  { id: 'roi', label: '画范围', icon: 'crop', hint: '绘制范围，保存到「感兴趣区」图层；可在下拉菜单中选择多边形、矩形或圆。', edits: false, key: '7' },
  { id: 'spectrum', label: '取谱', icon: 'chart-line', hint: '单击读取该位置的光谱曲线，Shift 单击保留曲线；拖动为平移。', edits: false, key: '8', module: 'spectra' },
]
const ROI_SHAPES: [RoiShape, string][] = [['polygon', '多边形'], ['rect', '矩形'], ['circle', '圆（从圆心拖出）']]

interface Props {
  palette: Palette
  themeMode: ThemeMode
  onThemeMode: (m: ThemeMode) => void
}

/** 菜单项前的勾：可勾选的项看一眼就知道开没开。 */
const checkLabel = (on: boolean, text: string) => (
  <span className="menu-check">
    <span className="menu-check-mark">{on ? <Icon name="check" size={14} /> : null}</span>
    {text}
  </span>
)

declare global {
  interface Window {
    lithoAction?: (id: string) => void
    lithoDrop?: (paths: string[]) => void
  }
}

export default function App({ palette, themeMode, onThemeMode }: Props) {
  const { message, modal } = AntApp.useApp()
  const askName = useNameDialog()
  const runTask = useTaskRunner()

  // ------------------------------------------------------------- 状态
  const [state, setState] = useState<LibState | null>(null)
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null)
  const [classes, setClasses] = useState<SampleClass[]>([])
  const [features, setFeatures] = useState<FeatureCollection | null>(null)
  // 勾选：逐条样本，记在后端会话里（导出、裁剪的工作集），这里是一份镜像
  const [checkedKeys, setCheckedKeys] = useState<Set<string>>(new Set())
  const [onlyChecked, setOnlyChecked] = useState(true)
  const [showGrid, setShowGrid] = useState(true)
  const [snap, setSnap] = useState(true)
  const [showTree, setShowTree] = useState(true)
  const [showInspector, setShowInspector] = useState(true)
  const [treeSel, setTreeSel] = useState<TreeSel>(null)
  // 样本树的展开节点与滚动位置，跟着样本库走：换了库就回到默认
  const [treeView, setTreeView] = useState<{ library: string; expanded: React.Key[] | null }>({ library: '', expanded: null })
  const [treeScrollTo, setTreeScrollTo] = useState<{ top: number } | null>(null)
  const treeScroll = useRef(0)
  const [treeScrollTick, setTreeScrollTick] = useState(0)
  const [treeReveal, setTreeReveal] = useState<{ id: number } | null>(null)
  const treeScrollTimer = useRef(0)
  const resetTreeView = () => {
    setTreeView({ library: '', expanded: null })
    treeScroll.current = 0
  }
  const [treeFilter, setTreeFilter] = useState<number[] | null>(null)
  const [clipboard, setClipboard] = useState<{ ids: number[]; label: string } | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [feature, setFeature] = useState<Feature | null>(null)
  const [identify, setIdentify] = useState<string[]>([])
  const [mode, setModeState] = useState<Mode>('browse')
  const [tool, setTool] = useState<Tool>('pan')
  const [roiShape, setRoiShape] = useState<RoiShape>('polygon')
  const [basemaps, setBasemaps] = useState<Basemap[]>([])
  const [basemapIds, setBasemapIds] = useState<number[]>([])
  const [layers, setLayers] = useState<LayerInfo[]>([])
  const [layerOrder, setLayerOrder] = useState<LayerId[]>(['samples'])
  const [layerRevs, setLayerRevs] = useState<Record<number, number>>({})
  const [samplesLayer, setSamplesLayer] = useState<SamplesLayerState>({ visible: true, opacity: 1 })
  const [selLayer, setSelLayer] = useState<LayerId | null>(null)
  // Tab 键切换显示的图层：最近在图层面板里点过的那个。选样本树里的类别去标注时不清掉
  const [tabLayer, setTabLayer] = useState<LayerId | null>(null)
  const [projection, setProjection] = useState({ code: 'EPSG:4326', label: 'WGS 84', projected: false })
  const [fitExtent, setFitExtent] = useState<{ extent: number[]; n: number } | null>(null)
  const [cursor, setCursor] = useState<{ lonlat: [number, number]; xy: [number, number] } | null>(null)
  const [status, setStatusText] = useState('')
  const [statusPath, setStatusPath] = useState('')
  const [recent, setRecent] = useState<{ path: string; name: string; folder: string; exists: boolean }[]>([])
  const [crsMenu, setCrsMenu] = useState<{ epsg: number; label: string; recommended: boolean }[]>([])
  const viewRef = useRef<{ center: [number, number]; resolution: number } | null>(null)
  const [initialView, setInitialView] = useState<{ center: [number, number]; resolution: number } | null | undefined>(undefined)

  // 对话框
  const [attrProfile, setAttrProfile] = useState<AttrProfile | null>(null)
  const [exportOpts, setExportOpts] = useState<ExportOptions | null>(null)
  const [backupList, setBackupList] = useState<BackupEntry[] | null>(null)
  const [rasterProps, setRasterProps] = useState<LayerInfo | null>(null)
  const [uniqueLayer, setUniqueLayer] = useState<LayerInfo | null>(null)
  const [roiSelect, setRoiSelect] = useState<{ layerId: number | null; name: string } | null>(null)
  const [colorFor, setColorFor] = useState<SampleClass | null>(null)
  const [descFor, setDescFor] = useState<SampleClass | null>(null)
  const [moveFor, setMoveFor] = useState<SampleClass | null>(null)
  const [moveFeatures, setMoveFeatures] = useState<{ keys: string[]; kind: string; from: Set<number> } | null>(null)
  const [keysOpen, setKeysOpen] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)
  // 功能模块的窗口
  const [spectrumOpen, setSpectrumOpen] = useState(false)
  const [board, setBoard] = useState<BoardState | null>(null)
  const [libOpen, setLibOpen] = useState(false)
  const [groupOpen, setGroupOpen] = useState(false)
  const [clipOpen, setClipOpen] = useState(false)
  const [toolbox, setToolbox] = useState<{ input?: string } | null>(null)
  // 野外记录：往地图上挂自己的图层，菜单动作与拖进来的文件先问它
  const [olMap, setOlMap] = useState<OlMap | null>(null)
  const fieldRef = useRef<FieldworkHandle>(null)
  const dlRef = useRef<DeepLearnHandle>(null)
  // 画布上最近一次右键的位置（经纬度），右键菜单「在此处新建野外点位」用
  const ctxPoint = useRef<[number, number] | null>(null)
  const [fieldShown, setFieldShown] = useState(false)

  const modules = appInfo?.modules ?? []
  const has = (m: string) => modules.includes(m)
  const classMap = useMemo(() => new Map(classes.map((c) => [c.id, c])), [classes])
  const selectedClassId = treeSel?.kind === 'class' ? treeSel.id : null
  const selectedClass = selectedClassId != null ? classMap.get(selectedClassId) ?? null : null
  const activeClass = selectedClass && !selectedClass.is_group ? selectedClass : null
  const selectedList = useMemo(() => [...selected], [selected])
  const totalSamples = features?.features.length ?? 0
  const layerById = useMemo(() => new Map(layers.map((l) => [l.id, l])), [layers])

  // 「只显示勾选的样本」：浏览与标注两种模式一致，画布只认勾选
  const filterOn = onlyChecked
  const shownKeys = filterOn ? checkedKeys : null

  /** 状态栏一句话；给了路径就在后面挂一个「在访达中显示」。 */
  const setStatus = (text: string, path = '') => {
    setStatusText(text)
    setStatusPath(path)
  }

  // ------------------------------------------------------------- 数据刷新
  // 样本要素按快照号增量同步：手里那份对应后端哪个快照。0 表示手里没有，要整批取
  const featToken = useRef(0)
  const featCurrent = useRef<FeatureCollection | null>(null)
  const loadFeatures = useCallback(async (): Promise<FeatureCollection | null> => {
    const base = featCurrent.current ? featToken.current : 0
    const r = await api.features(base || undefined)
    if (r.token <= featToken.current) return featCurrent.current // 晚到的旧响应
    let next: FeatureCollection
    if (r.type === 'FeatureDelta') {
      if (r.since !== featToken.current || !featCurrent.current) {
        // 增量的底子不是手里这份（两次刷新交错了）：作废，整批重取
        featToken.current = 0
        return loadFeatures()
      }
      // 没变的要素沿用原来的对象，画布据此只替换变了的那几条
      const byId = new Map(featCurrent.current.features.map((f) => [f.id, f]))
      for (const id of r.remove) byId.delete(id)
      for (const f of r.upsert) byId.set(f.id, f)
      next = r.upsert.length || r.remove.length
        ? { type: 'FeatureCollection', version: r.version, features: [...byId.values()] }
        : featCurrent.current
    } else {
      next = { type: 'FeatureCollection', version: r.version, features: r.features }
    }
    featToken.current = r.token
    featCurrent.current = next
    return next
  }, [])

  const refresh = useCallback(async () => {
    const s = await api.state()
    setState(s)
    if (!s.open) {
      setClasses([])
      featToken.current = 0
      featCurrent.current = null
      setFeatures(null)
      setCheckedKeys(new Set())
      return
    }
    const [cls, fc, sel] = await Promise.all([api.classes(), loadFeatures(), api.selection()])
    setClasses(cls)
    setFeatures(fc)
    setCheckedKeys(new Set(sel.keys))
  }, [loadFeatures])

  const run = useCallback(
    async (fn: () => Promise<unknown>) => {
      try {
        await fn()
      } catch (e) {
        message.error((e as Error).message)
      }
      await refresh()
    },
    [message, refresh],
  )

  const fitTo = (extent: number[] | null | undefined) => {
    if (extent) setFitExtent({ extent, n: Date.now() })
  }
  const zoomAll = useCallback(async () => {
    const r = await api.extent()
    // 样本库空着时退到图层的范围
    if (r.extent) fitTo(r.extent)
    else {
      const ls = await api.layers()
      const ext = unionExtent(ls.map((l) => l.extent))
      if (ext) fitTo(ext)
    }
  }, [])

  const extentOfKeys = (keys: Iterable<string>): number[] | null => {
    const want = new Set(keys)
    let box: number[] | null = null
    const grow = (x: number, y: number) => {
      box = box ? [Math.min(box[0], x), Math.min(box[1], y), Math.max(box[2], x), Math.max(box[3], y)] : [x, y, x, y]
    }
    for (const f of features?.features ?? []) {
      if (!want.has(f.id)) continue
      const g = f.geometry
      if (g.type === 'Point') {
        const [x, y] = g.coordinates as number[]
        grow(x, y)
      } else {
        for (const poly of g.coordinates as number[][][][]) for (const ring of poly) for (const [x, y] of ring) grow(x, y)
      }
    }
    return box
  }
  const zoomSelection = () => fitTo(extentOfKeys(selected))
  const zoomClasses = (ids: Set<number>) =>
    fitTo(extentOfKeys((features?.features ?? []).filter((f) => ids.has(f.properties.class_id)).map((f) => f.id)))

  // ------------------------------------------------------------- 启动
  useEffect(() => {
    (async () => {
      try {
        const [info] = await Promise.all([api.appInfo(), refresh()])
        setAppInfo(info)
        if (info.modules.includes('basemap')) api.basemaps().then(setBasemaps)
        await restoreUiState()
      } catch (e) {
        message.error(String(e))
        setInitialView(null)
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 单选一条样本时取它的详情（来源、属性）给检查器
  useEffect(() => {
    const key = selected.size === 1 ? [...selected][0] : ''
    // 刚删掉 / 撤销掉的要素还在选中里（下面那个 effect 才把它移出去），别去取
    if (!key || !features?.features.some((f) => f.id === key)) {
      setFeature(null)
      return
    }
    api.feature(key).then(setFeature).catch(() => setFeature(null))
  }, [selected, features])

  // 删掉、撤销掉的要素从画布选中里移出去
  useEffect(() => {
    if (!features) return
    const live = new Set(features.features.map((f) => f.id))
    const next = new Set([...selected].filter((k) => live.has(k)))
    if (next.size !== selected.size) setSelected(next)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [features])
  // 「要素信息」里列的也一样；在交给面板之前就滤掉，免得它先拿已经不在的键去取一次
  const identifyLive = useMemo(() => {
    if (!features) return identify
    const live = new Set(features.features.map((f) => f.id))
    const keep = identify.filter((k) => k.startsWith('L:') || live.has(k))
    return keep.length === identify.length ? identify : keep
  }, [identify, features])

  // ------------------------------------------------------------- 模式与工具
  const setMode = (m: Mode) => {
    if (m === 'annotate' && !state?.open) return setStatus('先打开或新建一个样本库。')
    setModeState(m)
    if (m === 'browse' && TOOLS.find((t) => t.id === tool)?.edits) setTool('pan')
    if (m === 'browse' && state?.dirty) setStatus(`已切换到浏览模式。${state.pending} 尚未保存，可回到标注模式继续编辑。`)
  }
  const tools = TOOLS.filter((t) => (mode === 'annotate' || !t.edits) && (!t.module || has(t.module)))
  const pickTool = (t: Tool) => {
    const def = TOOLS.find((x) => x.id === t)
    if (!def || (def.edits && mode !== 'annotate') || (def.module && !has(def.module))) return
    if (t === 'spectrum') return openSpectrum(undefined, true)
    setTool(t)
  }

  // ------------------------------------------------------------- 对话框
  const promptText = (title: string, placeholder: string, initial = ''): Promise<string | null> =>
    new Promise((resolve) => {
      let value = initial
      // 回车即确定（输入法上屏的那一下回车不算：WebKit 里它的 keyCode 是 229）
      const m = modal.confirm({
        title,
        icon: null,
        content: <Input autoFocus defaultValue={initial} placeholder={placeholder} onChange={(e) => (value = e.target.value)}
          onPressEnter={(e) => { if (e.keyCode === 229) return; m.destroy(); resolve(value.trim() || null) }} />,
        okText: '确定',
        onOk: () => resolve(value.trim() || null),
        onCancel: () => resolve(null),
      })
    })
  const confirm = (title: string, content: React.ReactNode, okText = '确定', danger = false): Promise<boolean> =>
    new Promise((resolve) => {
      modal.confirm({
        title, content: typeof content === 'string' ? <div className="pre">{content}</div> : content, okText,
        okButtonProps: { danger }, onOk: () => resolve(true), onCancel: () => resolve(false),
      })
    })

  // ------------------------------------------------------------- 库文件
  /** 会换掉当前库的动作（打开、新建、关闭）之前：有未保存的改动先问一句。 */
  const confirmDiscard = async (action: string): Promise<boolean> => {
    if (!state?.dirty) return true
    return await new Promise<boolean>((resolve) => {
      const m = modal.confirm({
        title: action,
        content: `还有未保存的改动：${state.pending}。`,
        okText: '保存后继续',
        footer: (_, { OkBtn, CancelBtn }) => (
          <>
            <CancelBtn />
            <Button danger onClick={() => { m.destroy(); resolve(true) }}>不保存</Button>
            <OkBtn />
          </>
        ),
        onOk: async () => {
          await api.save(onlyChecked)
          resolve(true)
        },
        onCancel: () => resolve(false),
      })
    })
  }

  const afterSwitch = (text: string, path = '') => {
    setSelected(new Set())
    setTreeSel(null)
    resetTreeView()
    setIdentify([])
    setStatus(text, path)
    zoomAll()
  }

  const openPath = async (path: string) => {
    if (state?.open && path === state.path) return setStatus(`该样本库已打开：${path}`)
    if (!(await confirmDiscard('打开另一个样本库'))) return
    await run(() => api.openLibrary(path))
    afterSwitch(`已打开样本库：${path}`)
  }

  const openLibrary = async () => {
    const dt = await whenDesktopReady(0)
    const path = dt
      ? await dt.open_library_dialog()
      : await promptText('打开样本库', '样本库文件路径（.gpkg）', state?.path ?? '')
    if (path) openPath(path)
  }

  const fileStem = (path: string) => (path.split(/[\\/]/).pop() ?? '').replace(/\.gpkg$/i, '')

  const newLibrary = async () => {
    if (!(await confirmDiscard('新建样本库'))) return
    const dt = desktop()
    let path = dt ? await dt.save_file('样本库.gpkg') : await promptText('新建样本库', '新样本库路径（.gpkg）')
    if (!path) return
    if (!path.toLowerCase().endsWith('.gpkg')) path += '.gpkg'
    const name = await askName('样本库名称', '名称：', fileStem(path))
    if (name === null) return
    const target = path
    await run(() => api.createLibrary(target, name))
    afterSwitch(`已新建样本库：${target}`, target)
  }

  const closeLibrary = async () => {
    if (!needLibrary()) return
    if (!(await confirmDiscard('关闭样本库'))) return
    setModeState('browse')
    setTool('pan')
    await run(() => api.closeLibrary())
    setSelected(new Set())
    setTreeSel(null)
    resetTreeView()
    setIdentify([])
    setStatus('已关闭样本库。')
  }

  const reloadLibrary = async () => {
    if (!needLibrary() || !state) return
    if (state.dirty && !(await confirm('重新从样本库载入', `未保存的改动将丢失：${state.pending}。`, '重新载入', true))) return
    await run(async () => {
      await api.reloadLibrary()
      setStatus('已重新从样本库载入。')
    })
  }

  /** 另存与备份复制的是库文件，暂存区里的草稿不在文件里：先问清楚。 */
  const settleUnsaved = async (action: string): Promise<boolean> => {
    if (!state?.dirty) return true
    return await new Promise<boolean>((resolve) => {
      const m = modal.confirm({
        title: action,
        content: `有未保存的改动：${state.pending}。${action}只复制样本库文件，不包含这些改动。`,
        okText: `保存后${action}`,
        footer: (_, { OkBtn, CancelBtn }) => (
          <>
            <CancelBtn />
            <Button onClick={() => { m.destroy(); resolve(true) }}>仅复制已保存内容</Button>
            <OkBtn />
          </>
        ),
        onOk: async () => {
          await api.save(onlyChecked)
          resolve(true)
        },
        onCancel: () => resolve(false),
      })
    })
  }

  /** 跑一个长任务；失败弹框，取消在状态栏说一句。失败或取消返回 null。 */
  const task = async <T,>(start: () => Promise<TaskInfo>, failTitle: string): Promise<T | null> => {
    try {
      const { result, cancelled } = await runTask<T>(start)
      if (cancelled) {
        setStatus('已取消。')
        await refresh()
        return null
      }
      return result
    } catch (e) {
      modal.error({ title: failTitle, content: <div className="pre">{(e as Error).message}</div> })
      await refresh()
      return null
    }
  }

  const copyLibrary = async (asNew: boolean) => {
    if (!needLibrary()) return
    const action = asNew ? '另存为' : '备份'
    if (!(await settleUnsaved(action))) return
    const d = await api.libraryDefaults()
    const dir = asNew ? d.save_as_dir : d.backup_dir
    const name = asNew ? d.save_as_name : d.backup_name
    const dt = desktop()
    let path = dt
      ? await dt.save_file(name, dir)
      : await promptText(asNew ? '样本库另存为…' : '备份样本库到…', '目标文件路径', `${dir}/${name}`)
    if (!path) return
    if (!path.toLowerCase().endsWith('.gpkg')) path += '.gpkg'
    const target = path
    const r = await task<{ path: string; old: string; size_mb: number; classes: number; samples: number }>(
      () => api.copyLibrary(target, asNew), `${action}失败`)
    if (!r) return
    if (asNew) {
      await refresh()
      afterSwitch(`已另存为 ${r.path} 并切换到该文件，原文件 ${fileStem(r.old)} 未改动`, r.path)
    } else {
      setStatus(`已备份到 ${r.path}（${r.size_mb} MB，${r.classes} 类 ${r.samples} 条，已校验）`, r.path)
    }
  }

  const doRestore = async (path: string) => {
    setBackupList(null)
    const r = await task<{ source: string }>(() => api.restore(path), '还原失败')
    await refresh()
    if (r) afterSwitch(`已从 ${r.source} 还原。还原前的状态已自动备份，可在「从备份还原」中找回。`)
  }

  const restoreBackup = async () => {
    if (!needLibrary() || !state) return
    if (state.dirty && !(await confirm('从备份还原', `还原将丢弃未保存的改动：${state.pending}。`))) return
    setBackupList(await api.backups())
  }

  const restoreFromFile = async () => {
    const d = await api.libraryDefaults()
    const dt = desktop()
    const path = dt ? await dt.pick_file('library', d.backup_dir) : await promptText('选择要还原的备份', '备份文件路径')
    if (!path) return
    try {
      const p = await api.probe(path)
      const ok = await confirm('从备份还原', (
        <div className="pre">
          {`用此备份替换当前样本库？\n\n备份：${p.path}\n（${p.classes} 类、${p.samples} 条样本）\n\n当前：${p.current.path}\n（${p.current.classes} 类、${p.current.samples} 条样本）\n\n替换前会自动备份当前样本库。`}
        </div>
      ), '还原', true)
      if (ok) doRestore(path)
    } catch (e) {
      modal.warning({ title: '从备份还原', content: (e as Error).message })
    }
  }

  // ------------------------------------------------------------- 导入
  /** 导入结果：一切顺利只在状态栏说一句；有跳过、有警告才弹出来。 */
  const reportImport = (r: ImportResult | null) => {
    if (!r) return
    if (!r.reports) return setStatus('没有找到可导入的工区目录。')
    setStatus(`导入完成：${r.files} 个文件 → 类别 ${r.classes} 个，样本 ${r.samples} 条${r.noisy ? `，有 ${r.warnings} 条提示` : ''}`)
    if (r.noisy) modal.info({ title: '导入完成', width: 560, content: <div className="pre report">{r.summary}</div> })
  }
  const needLibrary = () => {
    if (state?.open) return true
    setStatus('先打开或新建一个样本库。')
    return false
  }

  const importDir = async (root: boolean) => {
    if (!needLibrary()) return
    const dt = desktop()
    const title = root ? '选择样本库根目录（多个工区）' : '选择样本目录（一个工区）'
    const path = dt ? await dt.pick_folder() : await promptText(title, '目录路径')
    if (!path) return
    if (root) {
      reportImport(await task<ImportResult>(() => api.importRoot(path), '导入失败'))
    } else {
      const region = await askName('工区名称', '导入的样本归入工区：', path.split(/[\\/]/).filter(Boolean).pop() ?? '')
      if (region === null) return
      reportImport(await task<ImportResult>(() => api.importDir(path, region), '导入失败'))
    }
    await refresh()
  }

  const importFile = async () => {
    if (!needLibrary()) return
    const dt = desktop()
    const path = dt ? await dt.pick_file('vector') : await promptText('导入单个矢量文件', '矢量文件路径')
    if (!path) return
    const region = await askName('工区名称', '导入的样本归入工区：')
    if (region === null) return
    reportImport(await task<ImportResult>(() => api.importFile(path, region), '导入失败'))
    await refresh()
  }

  const importAttr = async () => {
    if (!needLibrary()) return
    const dt = desktop()
    const path = dt ? await dt.pick_file('vector') : await promptText('按属性导入', '矢量文件路径')
    if (!path) return
    const prof = await task<AttrProfile>(() => api.importProfile(path), '无法读取该文件的属性结构')
    if (prof) setAttrProfile(prof)
  }

  const doImportAttr = async (o: AttrImportOptions) => {
    const path = attrProfile!.path
    setAttrProfile(null)
    reportImport(await task<ImportResult>(() => api.importAttr({ path, ...o }), '导入失败'))
    await refresh()
  }

  // ------------------------------------------------------------- 导出
  const exportDialog = async () => {
    if (!needLibrary()) return
    if (!classes.length) return setStatus('样本库中暂无样本，请先导入或标注。')
    setExportOpts(await api.exportOptions())
  }

  const doExport = async (req: ExportRequest) => {
    setExportOpts(null)
    const r = await task<ExportResult>(() => api.exportRun(req), '导出失败')
    if (!r) return
    setStatus(`导出完成：${r.files} 个文件，${r.samples} 条样本 → ${r.out_dir}${r.checked ? '　' + r.checked : ''}`, r.out_dir)
    // 覆盖校验没过、有类别被拆成两个文件、清理了旧文件……这些要看一眼；一切顺利就不打断
    if (r.warnings.length) modal.info({ title: '导出完成', width: 560, content: <div className="pre report">{r.summary}</div> })
  }

  // ------------------------------------------------------------- 样本树
  const nodeLabel = (sel: TreeSel): { ids: number[]; label: string } | null => {
    if (!sel) return null
    if (sel.kind === 'region') {
      const ids = classes.filter((c) => c.parent_id == null && c.region === sel.region && !c.draft).map((c) => c.id)
      return { ids, label: `工区「${sel.region || '未分组'}」的 ${ids.length} 个类别` }
    }
    const c = classMap.get(sel.id)
    return c ? { ids: [c.id], label: `${c.is_group ? '分组' : '类别'}「${c.name}」` } : null
  }

  const copyNode = (sel: TreeSel) => {
    const n = nodeLabel(sel)
    if (!n) return
    if (!n.ids.length) return setStatus('该工区下没有类别。')
    if (n.ids.some((i) => i < 0)) return setStatus('新建的类别尚未保存，请保存后再复制。')
    setClipboard(n)
    setStatus(kbd(`已复制${n.label}。选中目标工区、分组或类别后按 ⌘V 粘贴。`))
  }
  const pasteNode = (sel: TreeSel) => {
    if (!clipboard) return setStatus(kbd('剪贴板为空。请先在样本树中选中类别、分组或工区，按 ⌘C 复制。'))
    if (!sel) return setStatus('请先选中要粘贴到的工区、分组或类别。')
    run(async () => {
      const r = await api.paste(sel.kind === 'region'
        ? { ids: clipboard.ids, region: sel.region }
        : { ids: clipboard.ids, target_id: sel.id })
      setStatus('已把' + clipboard.label + r.message)
    })
  }

  const onTreeAction = async (a: TreeAction) => {
    if (a.type === 'zoom') zoomClasses(subtree(classes, a.cls.id))
    else if (a.type === 'zoom-region') zoomClasses(new Set(classes.filter((c) => c.region === a.region).map((c) => c.id)))
    else if (a.type === 'rename') {
      const name = await askName(`重命名「${a.cls.name}」`, '类别名称：', a.cls.name)
      if (name && name !== a.cls.name) run(() => api.patchClass(a.cls.id, { name }))
    } else if (a.type === 'description') setDescFor(a.cls)
    else if (a.type === 'color') setColorFor(a.cls)
    else if (a.type === 'add') {
      const where = a.parent ? `「${a.parent.name}」下` : a.region ? `工区「${a.region}」下` : '顶层'
      const name = await askName(`在${where}新建${a.group ? '分组' : '类别'}`, a.group ? '分组名称：' : '类别名称（如 花岗闪长岩）：')
      if (!name) return
      run(async () => {
        const { id } = await api.addClass({ name, region: a.region, parent_id: a.parent?.id ?? null, group: a.group })
        setTreeSel({ kind: 'class', id })
      })
    } else if (a.type === 'delete') {
      if (await confirm(`删除「${a.cls.name}」？`, `将同时删除下级类别与 ${a.cls.total} 条样本。保存前可撤销，保存后无法恢复；如需保留，请先备份样本库。`, '删除', true))
        run(() => api.deleteClasses([a.cls.id]))
    } else if (a.type === 'move') {
      run(() => api.moveClass(a.id, { parent_id: a.parentId, before_id: a.beforeId, region: a.region }))
    } else if (a.type === 'promote') {
      const leaf = classMap.get(a.leafId)
      const other = classMap.get(a.otherId)
      const name = await askName('合成分组', `将「${leaf?.name}」与「${other?.name}」合并为一个分组。分组名称：`, leaf?.name ?? '')
      if (name) run(() => api.promoteClass(a.leafId, a.otherId, name))
    } else if (a.type === 'copy') copyNode(a.sel)
    else if (a.type === 'paste') pasteNode(a.sel)
    else if (a.type === 'merge') {
      // 合并只占一个撤销条目，一步就能退回去，所以不问
      run(async () => setStatus((await api.merge(a.picked, a.target.id)).message))
    } else if (a.type === 'move-to') setMoveFor(a.cls)
    else if (a.type === 'restore') run(async () => setStatus((await api.restoreClass(a.cls.id)).message))
    else if (a.type === 'rename-region') {
      const name = await askName('重命名工区', '工区名称：', a.region)
      if (name && name !== a.region) run(async () => setStatus((await api.renameRegion(a.region, name)).message))
    } else if (a.type === 'delete-region') {
      const n = await api.regionCount(a.region)
      if (await confirm('删除工区', `删除工区「${a.region || '未分组'}」及其下 ${n.classes} 个类别、${n.samples} 条样本？保存前可撤销。`, '删除', true))
        run(async () => setStatus((await api.deleteRegion(a.region)).message))
    } else if (a.type === 'new-region') newRegion()
  }

  const newRegion = async () => {
    if (!needLibrary()) return
    const region = await askName('新建工区', '工区名称（如 青海海德乌拉）：')
    if (!region) return
    if (classes.some((c) => c.region === region)) return setStatus(`已存在同名工区「${region}」。`)
    const first = await askName('新建工区', `工区「${region}」的第一个类别名称：\n（工区至少包含一个类别，之后可在样本树中继续添加。）`)
    if (!first) return
    run(async () => {
      const r = await api.newRegion(region, first)
      setTreeSel({ kind: 'class', id: r.id })
      setStatus(r.message)
    })
  }

  const showEdited = async () => {
    if (!needLibrary()) return
    const list = await api.editedClasses()
    if (!list.length) return setStatus('没有被改动的类别。')
    modal.info({ title: '被改动的类别', width: 520, content: <div className="pre report">{`共 ${list.length} 个：\n\n` + list.slice(0, 60).map((x) => `${x.path}（原：${x.origin}）`).join('\n')}</div> })
  }
  const restoreAll = async () => {
    if (!needLibrary()) return
    if (!(await confirm('恢复全部类别', `将全部类别的名称、类别号、工区与层级恢复到导入时的状态，删除之后新建的空分组，并清空样本的自定义名称。样本的几何与所属类别不变。\n\n此操作会清空撤销记录。如需保留当前状态，请先备份样本库：${state?.path}`, '恢复', true))) return
    run(async () => {
      const r = await api.restoreAll()
      setStatus(`已恢复 ${r.restored} 个类别，删除新建分组 ${r.removed} 个，清空自定义名称 ${r.labels} 条。`)
    })
  }

  const undo = () => !state?.open ? setStatus('没有可撤销的操作') : run(async () => {
    const r = await api.undo()
    setStatus(r.label ? `已撤销：${r.label}` : '没有可撤销的操作')
  })
  const redo = () => !state?.open ? setStatus('没有可重做的操作') : run(async () => {
    const r = await api.redo()
    setStatus(r.label ? `已重做：${r.label}` : '没有可重做的操作')
  })
  const doSave = () =>
    run(async () => {
      // 草稿落库后成了普通样本：过滤开着时让后端把它们勾上，免得刚存完就"标注全没了"
      const r = await api.save(onlyChecked)
      setStatus(`已保存到样本库：写入 ${r.written} 条，删除 ${r.deleted} 条`)
    })
  // 撤得回来的不弹框；含删除的落库撤不回来，拦一道
  const save = () => {
    if (!state?.dirty) return setStatus('没有需要保存的改动。')
    if (!state.has_deletions) return doSave()
    confirm('保存到样本库', `${state.pending}。其中包含删除操作，保存后无法撤销；如需保留，请先备份样本库。`, '保存')
      .then((ok) => { if (ok) doSave() })
  }
  const discard = () =>
    confirm('放弃全部未保存改动？', state?.pending, '放弃', true).then((ok) => { if (ok) run(() => api.discard()) })

  const deleteKeys = (keys: string[]) => {
    if (!keys.length) return setStatus('没有选中的要素。')
    setSelected(new Set())
    run(async () => {
      const r = await api.deleteFeatures(keys)
      setStatus(`已删除 ${r.deleted} 个要素（未保存）`)
    })
  }
  const moveKeys = (keys: string[], classId: number) =>
    run(async () => {
      const r = await api.moveFeatures(keys, classId)
      setStatus(`已把 ${r.moved} 个要素改归到「${classMap.get(classId)?.name}」（未保存）`)
    })
  const checkOp = (op: 'all' | 'none' | 'invert') => needLibrary() && run(() => api.setSelection(op))

  // ------------------------------------------------------------- 画布右键与选中集
  const selectionKind = (keys: string[]) => {
    const kinds = new Set(keys.map((k) => k.split(':')[0]))
    return kinds.size === 1 ? [...kinds][0] : ''
  }
  const canvasMenu = (keys: Set<string>): MenuProps['items'] => {
    if (['point', 'polygon', 'roi', 'spectrum'].includes(tool)) return []
    const n = keys.size
    const editing = mode === 'annotate'
    return [
      ...(editing && n
        ? [
            { key: 'new-class', label: '用选中要素新建类别…' },
            { key: 'move-other', label: '将选中要素改归到其他类别…' },
            { key: 'move-current', label: activeClass ? `将选中要素改归到「${activeClass.name}」` : '将选中要素改归到当前类别', disabled: !activeClass },
            { key: 'label', label: '给选中要素命名…', disabled: n !== 1 },
            { key: 'delete', label: `删除选中的 ${n} 个要素`, danger: true },
            { type: 'divider' as const },
          ]
        : []),
      { key: 'check', label: '将画布选中的样本加入勾选', disabled: !n },
      { key: 'clear', label: '清空画布选中', disabled: !n },
      { type: 'divider' as const },
      { key: 'zoom', label: '缩放到选中要素', disabled: !n },
      ...(has('fieldwork') && state?.open
        ? [{ type: 'divider' as const }, { key: 'fw-new-point', label: '在此处新建野外点位' }]
        : []),
    ]
  }
  const onCanvasAction = async (action: string, keys: Set<string>) => {
    const list = [...keys]
    const recs = (features?.features ?? []).filter((f) => keys.has(f.id))
    const kind = selectionKind(list)
    if (action === 'delete') deleteKeys(list)
    else if (action === 'clear') setSelected(new Set())
    else if (action === 'zoom') fitTo(extentOfKeys(keys))
    else if (action === 'fw-new-point') { if (ctxPoint.current) fieldRef.current?.addPointAt(ctxPoint.current) }
    else if (action === 'check') {
      if (!list.length) return setStatus('没有选中的要素。')
      run(() => api.setSelection('add', undefined, list))
    }
    else if (action === 'move-current') {
      if (!activeClass) return setStatus('请先在样本树中选中目标类别。')
      moveKeys(list, activeClass.id)
    } else if (action === 'move-other') {
      if (!list.length) return setStatus('没有选中的要素。')
      if (!kind) return message.warning('选中的要素同时包含点和面，一个类别只能包含一种几何类型，请分开改归。')
      setMoveFeatures({ keys: list, kind, from: new Set(recs.map((f) => f.properties.class_id)) })
    } else if (action === 'label') {
      if (recs.length !== 1) return setStatus('命名时请只选中一个要素。')
      const name = await askName('给选中要素命名', '名称（可留空）：', recs[0].properties.label, true)
      if (name !== null) run(() => api.setLabel(recs[0].id, name))
    } else if (action === 'new-class') {
      if (!list.length) return setStatus('没有选中的要素。')
      if (!kind) return message.warning('选中的要素同时包含点和面，一个类别只能包含一种几何类型，请分开处理。')
      const name = await askName('用选中要素新建类别', '类别名称：')
      if (!name) return
      const owners = new Set(recs.map((f) => classMap.get(f.properties.class_id)?.parent_id ?? null))
      const first = classMap.get(recs[0].properties.class_id)
      const parent = owners.size === 1 ? [...owners][0] : null
      run(async () => {
        const { id } = await api.addClass({ name, region: first?.region ?? '', parent_id: parent, group: false })
        await api.moveFeatures(list, id)
        setTreeSel({ kind: 'class', id })
      })
    }
  }

  // ------------------------------------------------------------- 图层
  const bumpLayer = (id: number) => setLayerRevs((r) => ({ ...r, [id]: (r[id] ?? 0) + 1 }))
  const syncLayers = useCallback(async (place?: { id: number; kind: string }[]) => {
    const ls = await api.layers()
    setLayers(ls)
    setLayerOrder((order) => {
      const alive = new Set<LayerId>(['samples', ...ls.map((l) => l.id)])
      let next = order.filter((x) => alive.has(x))
      // 新图层：矢量放最上面，影像放在第二格（压在矢量下面、样本上面）
      for (const l of place ?? ls.map((x) => ({ id: x.id, kind: x.kind }))) {
        if (next.includes(l.id)) continue
        if (l.kind === 'vector' || l.kind === 'drawn') next = [l.id, ...next]
        else next.splice(Math.min(1, next.length), 0, l.id)
      }
      return next
    })
    return ls
  }, [])
  // 显示金字塔在后台准备时，隔一会儿取一次图层信息，进度走完就停
  const preparing = layers.some((l) => l.preparing != null)
  useEffect(() => {
    if (!preparing) return
    const t = window.setInterval(() => { api.layers().then(setLayers).catch(() => undefined) }, 1500)
    return () => window.clearInterval(t)
  }, [preparing])

  const addLayerPaths = async (paths: string[]) => {
    if (!paths.length) return
    let r: { library?: string; task?: TaskInfo }
    try {
      r = await api.addLayers(paths)
    } catch (e) {
      return message.error((e as Error).message)
    }
    if (r.library) return openPath(r.library)
    if (!r.task) return
    const res = await task<{ added: LayerInfo[]; failed: string[] }>(async () => r.task!, '加载图层失败')
    if (!res) return
    await syncLayers(res.added.map((l) => ({ id: l.id, kind: l.kind })))
    if (res.added.length) {
      fitTo(unionExtent(res.added.map((l) => l.extent)))
      setStatus(`已加载 ${res.added.length} 个图层：${res.added.map((l) => l.name).join('、')}`)
    }
    if (res.failed.length) modal.warning({ title: '部分文件未能加载', content: <div className="pre">{res.failed.join('\n')}</div> })
  }

  const addLayerDialog = async (kind: 'raster' | 'image' | 'vector') => {
    const dt = desktop()
    const p = dt ? await dt.pick_file(kind === 'vector' ? 'vector' : 'image') : await promptText('添加图层', '文件路径')
    if (p) addLayerPaths([p])
  }

  const patchLayer = async (id: LayerId, patch: LayerPatch) => {
    if (id === 'samples') {
      setSamplesLayer((s) => ({ visible: patch.visible ?? s.visible, opacity: patch.opacity ?? s.opacity }))
      return
    }
    // 不透明度、显隐只是画布上的事，先改本地，拖滑块不卡
    setLayers((ls) => ls.map((l) => (l.id === id ? { ...l, ...patch } as LayerInfo : l)))
    try {
      const info = await api.patchLayer(id, patch)
      setLayers((ls) => ls.map((l) => (l.id === id ? info : l)))
      if (patch.bands || patch.stretch || patch.use_color_table !== undefined || patch.wavelength_file !== undefined) bumpLayer(id)
    } catch (e) {
      message.error((e as Error).message)
      syncLayers()
    }
  }

  const ensureRoiLayer = async (): Promise<number> => {
    const existing = layers.find((l) => l.kind === 'drawn' && l.name === '感兴趣区')
    if (existing) return existing.id
    const l = await api.newDrawnLayer()
    await syncLayers([{ id: l.id, kind: 'drawn' }])
    return l.id
  }
  const onRoiDrawn = async (rings: number[][][]) => {
    try {
      const id = await ensureRoiLayer()
      await api.addRoiPolygon(id, rings)
      await syncLayers()
      bumpLayer(id)
      setStatus('范围已添加到「感兴趣区」图层。右键该图层可按范围选择样本或导出为矢量。')
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const doRoiSelect = async (modeName: string, limit: boolean, add: boolean) => {
    const req = roiSelect
    setRoiSelect(null)
    if (!req) return
    const ids = limit ? treeFilter : null
    try {
      const r = req.layerId === null
        ? await api.roiAgain(modeName, ids, add)
        : await api.selectByLayer(req.layerId, modeName, ids, add)
      setStatus(`按「${req.name}」选中 ${r.hits} 条样本，当前共勾选 ${r.selected} 条。`)
    } catch (e) {
      message.error((e as Error).message)
    }
    refresh()
  }

  const selectByLayerMenu = async () => {
    const candidates = layers.filter((l) => (l.kind === 'vector' || l.kind === 'drawn') && (l.feature_count ?? 0) > 0)
    if (!candidates.length) return setStatus('没有可用的范围。请先绘制范围或添加矢量图层。')
    if (candidates.length === 1) return setRoiSelect({ layerId: candidates[0].id, name: candidates[0].name })
    let pick = candidates[0].id
    const ok = await modal.confirm({
      title: '按图层范围选择样本',
      icon: null,
      content: (
        <select className="native-select" defaultValue={pick} onChange={(e) => (pick = Number(e.target.value))}>
          {candidates.map((l) => <option key={l.id} value={l.id}>{l.name}（{l.feature_count} 个要素）</option>)}
        </select>
      ),
    })
    const l = candidates.find((x) => x.id === pick)
    if (ok && l) setRoiSelect({ layerId: l.id, name: l.name })
  }

  const onLayerAction = async (action: string, l: LayerInfo) => {
    if (action === 'zoom') fitTo(l.extent)
    else if (action === 'props') setRasterProps(l)
    else if (action === 'select') setRoiSelect({ layerId: l.id, name: l.name })
    else if (action === 'unique') setUniqueLayer(l)
    else if (action === 'unique-clear') {
      await api.setUniqueColors(l.id, '')
      await syncLayers()
      bumpLayer(l.id)
    } else if (action === 'fill') patchLayer(l.id, { fill: !l.fill })
    else if (action === 'rename') {
      const name = await askName('重命名图层', '图层名称：', l.name)
      if (name) patchLayer(l.id, { name })
    } else if (action === 'color') {
      let color = l.color ?? '#f97316'
      const ok = await modal.confirm({
        title: `设置颜色 · ${l.name}`, icon: null,
        content: <input type="color" defaultValue={color} onChange={(e) => (color = e.target.value)} className="native-color" />,
      })
      if (ok) patchLayer(l.id, { color })
    } else if (action === 'remove') {
      // 拿着这个图层的模块该松手了：曲线窗口还绑着它，点下去会读一幅看不见的影像
      if (board?.layer_id === l.id) setBoard(await sapi.bind(null))
      await api.removeLayer(l.id)
      if (selLayer === l.id) setSelLayer(null)
      if (tabLayer === l.id) setTabLayer(null)
      setIdentify([])
      await syncLayers()
      setStatus(`已移除图层「${l.name}」`)
    } else if (action === 'extent') {
      let valid = true
      const ok = await modal.confirm({
        title: '生成范围图层', icon: null, width: 520,
        content: (
          <div className="dlg">
            <div>范围类型</div>
            <label className="radio-line"><input type="radio" name="ext" defaultChecked onChange={() => (valid = true)} /> 有效数据轮廓　<span className="muted">按 nodata 掩膜提取实际覆盖区域，适用于有黑边或倾斜的影像</span></label>
            <label className="radio-line"><input type="radio" name="ext" onChange={() => (valid = false)} /> 四至矩形　<span className="muted">取影像四角坐标，适用于规则的正射影像，速度快</span></label>
          </div>
        ),
      })
      if (!ok) return
      let r: { task: TaskInfo }
      try {
        r = await api.extentLayer(l.id, valid)
      } catch (e) {
        return message.error((e as Error).message)
      }
      const res = await task<{ layer: LayerInfo; parts: number }>(async () => r.task, '生成范围失败')
      if (res) {
        await syncLayers([{ id: res.layer.id, kind: 'drawn' }])
        setStatus(`已生成范围图层「${res.layer.name}」，共 ${res.parts} 个面。右键该图层可按范围选择样本。`)
      }
    } else if (action === 'save') {
      const dt = desktop()
      const p = dt ? await dt.save_file(`${l.name}.geojson`) : await promptText('导出范围为矢量', '目标路径（.geojson / .shp / .gpkg）')
      if (!p) return
      try {
        const r = await api.saveDrawn(l.id, p)
        setStatus(`已导出 ${r.count} 个面 → ${r.path}`, r.path)
      } catch (e) {
        message.error((e as Error).message)
      }
    } else if (action === 'clear') {
      await api.clearDrawn(l.id)
      await syncLayers()
      bumpLayer(l.id)
    } else if (action === 'spectrum') openSpectrum(l.id)
    else if (action === 'toolbox') openToolbox(l.id)
  }

  // ------------------------------------------------------------- 功能模块
  const spectralLayers = layers.filter((l) => (l.kind === 'raster' || l.kind === 'image') && (l.band_count ?? 0) > 1)
  /** 打开曲线窗口。从图层右键进来时绑那幅影像并切到取谱工具；从视图菜单进来不绑。 */
  const openSpectrum = async (layerId?: number, fromTool = false) => {
    if (!has('spectra')) return
    try {
      let b = await sapi.state()
      const target = layerId ?? (fromTool && b.layer_id === null ? spectralLayers[0]?.id : undefined)
      if (target !== undefined) b = await sapi.bind(target)
      setBoard(b)
      setSpectrumOpen(true)
      if (b.layer_id !== null && (layerId !== undefined || fromTool)) setTool('spectrum')
      else if (fromTool) setStatus('画布上暂无多波段影像。添加影像后可使用「取谱」；光谱库、CSV 导入与出图不受影响。')
    } catch (e) {
      message.error((e as Error).message)
    }
  }
  const closeSpectrum = () => {
    setSpectrumOpen(false)
    setLibOpen(false)
    // 窗口一关，取谱工具收回去、画布上的标记擦掉
    if (tool === 'spectrum') setTool('pan')
  }
  const openSpeclib = async () => {
    if (!spectrumOpen) await openSpectrum()
    setLibOpen(true)
  }
  const openToolbox = (layerId?: number) => {
    const l = layerId !== undefined ? layerById.get(layerId) : undefined
    setToolbox({ input: l?.path })
  }
  const openClip = () => {
    if (!needLibrary()) return
    setClipOpen(true)
  }
  const doClip = async (req: ClipRequest) => {
    setClipOpen(false)
    let r: { task: TaskInfo }
    try {
      r = await clipApi.run(req)
    } catch (e) {
      return modal.error({ title: '裁剪失败', content: (e as Error).message })
    }
    const res = await task<{ summary: string; out_dir: string; cancelled: boolean; warnings: string[]; units: { label: number; name: string; train: number; test: number }[] }>(async () => r.task, '裁剪失败')
    if (!res) return
    setStatus(`${res.cancelled ? '已取消' : '裁剪完成'}：${res.summary} 输出在 ${res.out_dir}`, res.out_dir)
    // 一切正常就只在状态栏说一句；有需要看一眼的事才弹出来
    if (res.warnings.length) {
      const lines = [res.summary, '', `输出目录：${res.out_dir}`, '',
        ...res.units.slice(0, 20).map((u) => `  标签 ${u.label}  ${u.name}  训练 ${u.train} 块${u.test ? `／测试 ${u.test} 块` : ''}`),
        ...(res.units.length > 20 ? [`  …… 共 ${res.units.length} 类`] : []), '', '提示：', ...res.warnings.slice(0, 8).map((w) => `  · ${w}`),
        '', '标签与类别的对应关系见输出目录中的 labels.json。']
      modal.info({ title: res.cancelled ? '已取消' : '裁剪完成', width: 600, content: <div className="pre report">{lines.join('\n')}</div> })
    }
  }
  const moduleRef = useRef<{ pick?: (lonlat: [number, number], append: boolean) => void }>({})
  moduleRef.current.pick = async (ll, append) => {
    try {
      const b = await sapi.pick(ll[0], ll[1], append)
      setBoard(b)
      if (b.message) setStatus(b.message)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  // ------------------------------------------------------------- 显示坐标系
  const setCrs = async (epsg: number) => {
    try {
      const d = await api.crsDef(epsg)
      const code = `EPSG:${epsg}`
      if (epsg !== 4326 && epsg !== 3857) {
        proj4.defs(code, d.proj4)
        registerProj4(proj4)
        // 投影的边界按适用范围算：经纬网要它，没有就画不出来
        const p = getProjection(code)
        if (p && d.area) {
          let [w, s, e, n] = d.area
          if (e < w) e += 360
          p.setWorldExtent([w, s, e, n])
          p.setExtent(transformExtent([w, s, e, n], 'EPSG:4326', code, 16))
        }
      }
      setProjection({ code, label: d.label, projected: d.projected })
      setStatus(`显示坐标系：${d.label}（仅影响显示，样本库数据不变）`)
    } catch (e) {
      message.error((e as Error).message)
    }
  }
  const crsItems: MenuProps['items'] = [
    ...crsMenu.map((c) => ({ key: String(c.epsg), label: checkLabel(projection.code === `EPSG:${c.epsg}`, c.label + (c.recommended ? '（推荐）' : '')) })),
    { type: 'divider' },
    { key: 'other', label: '其他 EPSG 代码…' },
  ]
  const onCrsMenu = async (key: string) => {
    if (key === 'other') {
      const v = await promptText('显示坐标系', 'EPSG 代码，如 32647')
      const n = Number((v ?? '').replace(/^EPSG:/i, ''))
      if (n) setCrs(n)
    } else setCrs(Number(key))
  }
  const cursorText = cursor
    ? projection.projected
      ? `${cursor.xy[0].toFixed(2)} E  ${cursor.xy[1].toFixed(2)} N`
      : (() => {
          const [lon, lat] = projection.code === 'EPSG:4326' ? cursor.lonlat : cursor.xy
          return `${Math.abs(lon).toFixed(6)}°${lon < 0 ? 'W' : 'E'}  ${Math.abs(lat).toFixed(6)}°${lat < 0 ? 'S' : 'N'}`
        })()
    : ''

  // ------------------------------------------------------------- 界面状态：退出时记下，下次铺回来
  const restoreUiState = async () => {
    const d = (await api.uiState()) as Record<string, unknown>
    const s = await api.state()
    if (typeof d.onlyChecked === 'boolean') setOnlyChecked(d.onlyChecked)
    if (typeof d.grid === 'boolean') setShowGrid(d.grid)
    if (typeof d.snap === 'boolean') setSnap(d.snap)
    if (typeof d.showTree === 'boolean') setShowTree(d.showTree)
    if (typeof d.showInspector === 'boolean') setShowInspector(d.showInspector)
    if (Array.isArray(d.basemapIds)) setBasemapIds(d.basemapIds as number[])
    // 勾选、视野、图层是针对具体数据的：只有打开的确实是上次那个库才恢复
    if (!s.open || d.library !== s.path) {
      setInitialView(null)
      await syncLayers()            // 页面重新载入时后端可能还有图层
      zoomAll()
      return
    }
    const v = d.view as { lon: number; lat: number; res_m: number } | undefined
    if (v && typeof v.lon === 'number') {
      // 视野按经纬度 + 米每像素存，与显示坐标系无关（每次启动都从 WGS 84 起步）
      setInitialView({ center: [v.lon, v.lat], resolution: v.res_m / 111319.49 })
    } else {
      setInitialView(null)
      zoomAll()
    }
    if (Array.isArray(d.selection) && d.selection.length) {
      await api.setSelection('set', undefined, d.selection as string[])
    }
    const tv = d.tree as { expanded?: unknown; scroll?: unknown; sel?: unknown } | undefined
    if (tv && typeof tv === 'object') {
      if (Array.isArray(tv.expanded)) setTreeView({ library: s.path, expanded: tv.expanded.filter((k) => typeof k === 'string') as string[] })
      const sel = tv.sel as TreeSel
      if (sel && ((sel.kind === 'class' && typeof sel.id === 'number') || (sel.kind === 'region' && typeof sel.region === 'string'))) setTreeSel(sel)
      if (typeof tv.scroll === 'number' && tv.scroll > 0) {
        treeScroll.current = tv.scroll
        setTreeScrollTo({ top: tv.scroll })
      }
    }
    if (d.samples) setSamplesLayer(d.samples as SamplesLayerState)
    const existing = await api.layers()
    if (existing.length) {
      // 页面重新载入、后端还开着：图层都还在，接着用，再铺一遍就重复了
      setLayers(existing)
      const alive = new Set<LayerId>(existing.map((l) => l.id))
      const order = (Array.isArray(d.order) ? d.order as (string | number)[] : [])
        .map((x) => (x === 'samples' ? 'samples' : Number(x)))
        .filter((x): x is LayerId => x === 'samples' || alive.has(x))
      for (const l of existing) if (!order.includes(l.id)) order.push(l.id)
      if (!order.includes('samples')) order.push('samples')
      setLayerOrder(order)
    } else if (Array.isArray(d.layers) && d.layers.length) {
      const r = await api.restoreLayers(d.layers as Record<string, unknown>[])
      const ls = await api.layers()
      setLayers(ls)
      const order = (Array.isArray(d.order) ? d.order as (string | number)[] : [])
        .map((x) => (x === 'samples' ? 'samples' : r.mapping[String(x)]?.id))
        .filter((x): x is LayerId => x !== undefined)
      for (const l of ls) if (!order.includes(l.id)) order.push(l.id)
      if (!order.includes('samples')) order.push('samples')
      setLayerOrder(order)
      if (r.failed.length) setStatus(`${r.failed.length} 个图层未能恢复：${r.failed.join('；')}`)
    }
    await refresh()
  }

  const saveTimer = useRef(0)
  const viewTick = useRef(0)
  const [viewVersion, setViewVersion] = useState(0)
  useEffect(() => {
    if (initialView === undefined) return // 还在恢复中，别拿半截状态覆盖
    window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(async () => {
      if (!state) return
      const specs = state.open ? await api.layerSpecs().catch(() => []) : []
      const v = viewRef.current
      let view: { lon: number; lat: number; res_m: number } | undefined
      if (v && projection.code === 'EPSG:4326') view = { lon: v.center[0], lat: v.center[1], res_m: v.resolution * 111319.49 }
      api.saveUiState({
        library: state.open ? state.path : '',
        onlyChecked, grid: showGrid, snap, showTree, showInspector, basemapIds,
        view, selection: [...checkedKeys], samples: samplesLayer,
        layers: specs, order: layerOrder,
        tree: state.open ? {
          expanded: treeView.library === state.path ? treeView.expanded : null,
          scroll: treeScroll.current, sel: treeSel,
        } : undefined,
      }).catch(() => undefined)
    }, 1200)
  }, [state, onlyChecked, showGrid, snap, showTree, showInspector, basemapIds, checkedKeys, samplesLayer, layers, layerOrder, initialView, projection, viewVersion, treeView, treeSel, treeScrollTick]) // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------- 菜单
  const bases = basemaps.filter((b) => !b.overlay)
  const overlays = basemaps.filter((b) => b.overlay)
  const groups = [...new Set(bases.map((b) => b.group))]
  const missingKeys = [...new Set(basemaps.filter((b) => !b.usable).map((b) => b.group))]
  const fieldMenu: MenuProps['items'] = [
    { key: 'fw-panel', label: '野外记录…' },
    { key: 'fw-toggle', label: '在画布上显示 / 隐藏' },
    { key: 'fw-zoom', label: '缩放到野外记录' },
    { type: 'divider' },
    { key: 'fw-import-ovobj', label: '导入奥维收藏夹（.ovobj，可多选）…', disabled: !state?.open },
    { key: 'fw-import-ovobj-folder', label: '导入奥维收藏夹文件夹…', disabled: !state?.open },
    { key: 'fw-import-photos', label: '导入照片…', disabled: !state?.open },
    { key: 'fw-import-folder', label: '导入照片文件夹…', disabled: !state?.open },
    { key: 'fw-import-vector', label: '导入轨迹与点位（GPX / KML）…', disabled: !state?.open },
  ]
  const dlMenu: MenuProps['items'] = [
    { key: 'dl-open', label: '深度学习工作台…' },
    { type: 'divider' },
    { key: 'dl-clip', label: '从样本库裁剪样本集…', disabled: !state?.open },
    { key: 'dl-data', label: '样本集' },
    { key: 'dl-train', label: '训练…' },
    { key: 'dl-predict', label: '整景预测…' },
    { key: 'dl-features', label: '特征分析与相似检索…' },
    { type: 'divider' },
    { key: 'dl-jobs', label: '任务' },
    { key: 'dl-models', label: '模型库' },
    { key: 'dl-env', label: '运行环境…' },
  ]
  const baseMenu: MenuProps['items'] = [
    { key: 'none', label: checkLabel(!basemapIds.some((id) => bases.some((b) => b.id === id)), '无底图') },
    ...groups.map((g) => ({
      type: 'group' as const,
      label: g,
      children: bases
        .filter((b) => b.group === g)
        .map((b) => ({ key: `b${b.id}`, disabled: !b.usable, label: checkLabel(basemapIds.includes(b.id), b.name + (b.usable ? '' : '（缺密钥）')) })),
    })),
    ...(overlays.length
      ? [{ type: 'group' as const, label: '注记', children: overlays.map((b) => ({ key: `o${b.id}`, disabled: !b.usable, label: checkLabel(basemapIds.includes(b.id), b.name) })) }]
      : []),
    { type: 'divider' },
    ...missingKeys.map((g) => ({ key: 'missing-' + g, disabled: true, label: `未配置${g}密钥，相关底图不可用` })),
    { key: 'off', label: '关闭全部底图' },
    { key: 'retry', label: '重试加载失败的瓦片' },
    { key: 'cache', label: '清空瓦片缓存…' },
    { key: 'keys', label: '配置底图密钥…' },
  ]
  const onBaseMenu = async (key: string) => {
    const keepOverlays = basemapIds.filter((id) => overlays.some((b) => b.id === id))
    if (key === 'none') setBasemapIds(keepOverlays)
    else if (key === 'off') setBasemapIds([])
    else if (key === 'retry') { await api.retryTiles(); setBasemapIds([...basemapIds]); setStatus('已重试加载失败的瓦片。') }
    else if (key === 'cache') {
      const c = await api.cacheInfo()
      if (await confirm('清空瓦片缓存', `当前缓存约 ${c.size_mb} MB（上限 ${c.limit_mb} MB），确定清空吗？`, '清空', true)) {
        await api.clearCache()
        setStatus('瓦片缓存已清空。')
      }
    } else if (key === 'keys') setKeysOpen(true)
    else if (key.startsWith('b')) setBasemapIds([Number(key.slice(1)), ...keepOverlays])
    else if (key.startsWith('o')) {
      const id = Number(key.slice(1))
      setBasemapIds(basemapIds.includes(id) ? basemapIds.filter((x) => x !== id) : [...basemapIds, id])
    }
  }

  const viewMenu: MenuProps['items'] = [
    { key: 'all', label: '全图', extra: kbd('⌘0') },
    { key: 'sel', label: '缩放到选中', extra: kbd('⌘J'), disabled: !selected.size },
    { type: 'divider' },
    { key: 'snap', label: checkLabel(snap, '捕捉顶点') },
    { key: 'only', label: checkLabel(onlyChecked, '只显示勾选的样本') },
    { key: 'grid', label: checkLabel(showGrid, '显示经纬网') },
    { type: 'divider' },
    { key: 'tree', label: checkLabel(showTree, '样本与图层栏') },
    { key: 'insp', label: checkLabel(showInspector, '检查器与要素信息栏') },
    ...(has('spectra') ? [{ type: 'divider' as const }, { key: 'spectrum', label: '光谱曲线…' }, { key: 'speclib', label: '光谱库…' }] : []),
    ...(has('toolbox') ? [{ key: 'toolbox', label: '工具箱…' }] : []),
    { type: 'divider' },
    {
      key: 'look',
      label: '外观',
      children: (Object.keys(MODE_LABELS) as ThemeMode[]).map((m) => ({ key: 'theme-' + m, label: checkLabel(themeMode === m, MODE_LABELS[m]) })),
    },
    { type: 'divider' },
    { key: 'help', label: '使用说明' },
    { key: 'about', label: '关于' },
  ]
  const onViewMenu = (key: string) => {
    if (key === 'all') zoomAll()
    else if (key === 'sel') zoomSelection()
    else if (key === 'snap') setSnap(!snap)
    else if (key === 'only') setOnlyChecked(!onlyChecked)
    else if (key === 'grid') setShowGrid(!showGrid)
    else if (key === 'tree') setShowTree(!showTree)
    else if (key === 'insp') setShowInspector(!showInspector)
    else dispatch(key)
  }

  const libMenu: MenuProps['items'] = [
    { key: 'path', label: <span className="menu-path">{state?.path || '未打开样本库'}</span>, disabled: true },
    { type: 'divider' },
    { key: 'reveal', label: REVEAL, disabled: !state?.open || !desktop() },
    { key: 'copy', label: '复制路径', disabled: !state?.open },
    { type: 'divider' },
    { key: 'save-as', label: '另存为…', disabled: !state?.open },
    { key: 'backup', label: '备份到…', disabled: !state?.open },
    { key: 'restore', label: '从备份还原…', disabled: !state?.open },
    { type: 'divider' },
    { key: 'discard', label: '放弃全部未保存改动', disabled: !state?.dirty, danger: true },
  ]

  const openMenu: MenuProps['items'] = [
    { key: 'new', label: '新建样本库…' },
    {
      key: 'recent',
      label: '最近打开',
      children: recent.length
        ? [
            ...recent.map((r) => ({
              key: 'r:' + r.path,
              disabled: !r.exists,
              label: <span title={r.path}>{r.name}　<span className="muted">— {r.folder}</span></span>,
            })),
            { type: 'divider' as const },
            { key: 'clear-recent', label: '清除列表' },
          ]
        : [{ key: 'none', label: '（无）', disabled: true }],
    },
    { type: 'divider' },
    { key: 'close', label: '关闭样本库', disabled: !state?.open },
  ]

  const importMenu: MenuProps['items'] = [
    { key: 'import-dir', label: '导入样本目录…' },
    { key: 'import-root', label: '导入样本库根目录…' },
    { key: 'import-file', label: '导入单个矢量…' },
    { type: 'divider' },
    { key: 'import-attr', label: '按属性导入…' },
  ]
  const exportMenu: MenuProps['items'] = [
    { key: 'export', label: '导出样本…', extra: kbd('⌘E') },
    ...(has('clip') ? [{ key: 'clip', label: '裁剪样本块…' }] : []),
    { type: 'divider' },
    { key: 'clear-levels', label: '清除全部导出层级标记', disabled: !classes.some((c) => c.export_level !== 'default') },
  ]

  /** 所有动作的统一入口：命令条的下拉、原生菜单栏（pywebview）、快捷键都走这里。 */
  const dispatch = async (id: string) => {
    if (id.startsWith('fw-')) {
      fieldRef.current?.action(id)
      return
    }
    if (id.startsWith('dl-')) {
      dlRef.current?.action(id)
      return
    }
    const handlers: Record<string, () => unknown> = {
      new: newLibrary, open: openLibrary, close: closeLibrary, save, 'save-as': () => copyLibrary(true),
      backup: () => copyLibrary(false), restore: restoreBackup, reveal: () => state?.open && desktop()?.reveal_in_folder(state.path),
      copy: async () => {
        const p = state?.path ?? ''
        const ok = desktop() ? await desktop()!.copy_text(p) : await navigator.clipboard.writeText(p).then(() => true, () => false)
        setStatus(ok ? '路径已复制' : '复制失败')
      },
      'import-dir': () => importDir(false), 'import-root': () => importDir(true), 'import-file': importFile, 'import-attr': importAttr,
      export: exportDialog, clip: openClip, 'clear-levels': () => run(() => api.clearExportLevels()),
      'add-raster': () => addLayerDialog('raster'), 'add-image': () => addLayerDialog('image'), 'add-vector': () => addLayerDialog('vector'),
      toolbox: () => openToolbox(),
      undo, redo, 'new-region': newRegion, 'check-all': () => checkOp('all'), 'uncheck-all': () => checkOp('none'),
      'check-invert': () => checkOp('invert'),
      'roi-again': () => setRoiSelect({ layerId: null, name: '当前范围' }),
      'show-edited': showEdited, 'restore-all': restoreAll,
      'delete-sel': () => mode === 'annotate' ? deleteKeys(selectedList) : setStatus(kbd('删除要素需在标注模式下进行（⌘2）。')),
      'move-sel': () => onCanvasAction('move-current', selected),
      'move-sel-to': () => onCanvasAction('move-other', selected),
      'new-class-sel': () => onCanvasAction('new-class', selected),
      'label-sel': () => onCanvasAction('label', selected),
      'clear-sel': () => setSelected(new Set()),
      'check-sel': () => onCanvasAction('check', selected),
      'roi-layer': selectByLayerMenu,
      'roi-clear': async () => {
        const l = layers.find((x) => x.kind === 'drawn' && x.name === '感兴趣区')
        if (l) onLayerAction('clear', l)
      },
      discard, reload: reloadLibrary,
      'zoom-all': zoomAll, 'zoom-sel': zoomSelection, snap: () => setSnap((v) => !v), 'only-checked': () => setOnlyChecked((v) => !v),
      grid: () => setShowGrid((v) => !v), 'toggle-tree': () => setShowTree((v) => !v), 'toggle-inspector': () => setShowInspector((v) => !v),
      spectrum: () => openSpectrum(), speclib: openSpeclib,
      'theme-system': () => onThemeMode('system'), 'theme-light': () => onThemeMode('light'), 'theme-dark': () => onThemeMode('dark'),
      help: () => setHelpOpen(true), about: () => setAboutOpen(true),
      browse: () => setMode('browse'), annotate: () => setMode('annotate'),
    }
    const fn = handlers[id]
    if (fn) await fn()
  }
  const dispatchRef = useRef(dispatch)
  dispatchRef.current = dispatch
  const addRef = useRef(addLayerPaths)
  addRef.current = addLayerPaths
  useEffect(() => {
    window.lithoAction = (id: string) => { dispatchRef.current(id) }
    window.lithoDrop = async (paths: string[]) => {
      const rest = fieldRef.current ? await fieldRef.current.claim(paths) : paths
      addRef.current(rest)
    }
    // 浏览器里拖文件进来默认是"打开这个文件"，会把整个界面换掉——拦住
    const stop = (e: DragEvent) => e.preventDefault()
    // 桌面版：拖进来的文件按名字向外壳换真实路径。只传文件名，界面线程上不做别的事
    const drop = async (e: DragEvent) => {
      e.preventDefault()
      const dt = desktop()
      const files = [...(e.dataTransfer?.files ?? [])]
      if (!dt || !files.length) return
      // Windows（WebView2）要先把文件对象交给外壳，它才记得下路径
      const wv = (window as unknown as { chrome?: { webview?: { postMessageWithAdditionalObjects?: (m: string, o: unknown) => void } } }).chrome?.webview
      wv?.postMessageWithAdditionalObjects?.('FilesDropped', e.dataTransfer!.files)
      const names = files.map((f) => f.name)
      setStatus(`正在加载 ${names.length === 1 ? names[0] : names.length + ' 个文件'}…`)
      let paths = await dt.dropped_paths(names)
      if (!paths.length) {
        await new Promise((r) => setTimeout(r, 150))
        paths = await dt.dropped_paths(names)
      }
      if (paths.length) window.lithoDrop?.(paths)
      else setStatus('未能取得拖入文件的路径，请改用「导入」菜单。')
    }
    window.addEventListener('dragover', stop)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragover', stop)
      window.removeEventListener('drop', drop)
    }
  }, [])

  // ------------------------------------------------------------- 快捷键
  // Tab：切换图层面板里选中的那个图层显示与否（标注时对照底下的影像用）
  const toggleSelLayer = () => {
    // 样本图层不参与 Tab 切换：标注时一按 Tab 把样本藏掉，画布上看着像样本丢了
    const target = tabLayer ?? (selLayer !== 'samples' ? selLayer : null)
    if (target === null) return false
    if (target === 'samples') {
      const v = !samplesLayer.visible
      patchLayer('samples', { visible: v })
      setStatus(`样本图层已${v ? '显示' : '隐藏'}（Tab 切换）`)
      return true
    }
    const l = layerById.get(target)
    if (!l) return false
    patchLayer(l.id, { visible: !l.visible })
    setStatus(`图层「${l.name}」已${l.visible ? '隐藏' : '显示'}（Tab 切换）`)
    return true
  }
  const keyRef = useRef({ dispatch, selectedList, pickTool, treeSel, copyNode, pasteNode, mode, toggleSelLayer })
  keyRef.current = { dispatch, selectedList, pickTool, treeSel, copyNode, pasteNode, mode, toggleSelLayer }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey
      const k = keyRef.current
      const key = e.key.toLowerCase()
      // 保存在输入框里也要能按（先失焦，让输入框把内容提交掉）
      if (mod && key === 's') {
        e.preventDefault()
        ;(document.activeElement as HTMLElement | null)?.blur()
        setTimeout(() => keyRef.current.dispatch(e.shiftKey ? 'save-as' : 'save'), 300)
        return
      }
      // 事件也可能直接发在 document 上（没有元素有焦点时由脚本派发），那时按 body 算
      const t = (e.target instanceof Element ? e.target : document.body) as HTMLElement
      if (isTypingTarget(t) || t.closest('.ant-modal')) return
      // 照片窗口盖住了主界面，它自己的 Delete、方向键等不能再落到样本上
      if (document.querySelector('.pv-backdrop')) return
      // 焦点在浮动窗口里：只对画布起作用的单键（切工具、删样本、切图层）不响应，组合键照常
      if (!mod && t.closest('.float')) return
      if (e.key === 'Tab' && !mod && !e.altKey) {
        if (k.toggleSelLayer()) e.preventDefault()
        return
      }
      const shortcuts: Record<string, string> = {
        z: e.shiftKey ? 'redo' : 'undo', '0': 'zoom-all', j: 'zoom-sel', o: 'open', n: 'new', e: 'export',
        '1': 'browse', '2': 'annotate', a: e.shiftKey ? 'uncheck-all' : 'check-all',
      }
      if (mod && shortcuts[key]) {
        e.preventDefault()
        k.dispatch(shortcuts[key])
      } else if (mod && key === 'c' && k.treeSel) {
        k.copyNode(k.treeSel)
      } else if (mod && key === 'v' && k.treeSel) {
        k.pasteNode(k.treeSel)
      } else if ((e.key === 'Delete' || e.key === 'Backspace') && k.selectedList.length && k.mode === 'annotate') {
        k.dispatch('delete-sel')
      } else if (!mod && /^[1-8]$/.test(key)) {
        const tl = TOOLS.find((x) => x.key === key)
        if (tl) k.pickTool(tl.id)
      }
    }
    // 点回主界面（画布、样本树、图层栏）时把焦点从浮动窗口里收回来，上面那些单键恢复作用。
    // 画布本身拿不到焦点，不收回的话点完浮动窗口再点画布，数字键就一直不灵
    const onDown = (e: PointerEvent) => {
      const t = e.target as HTMLElement | null
      const active = document.activeElement as HTMLElement | null
      if (!t?.closest || !t.closest('.shell') || t.closest('.float')) return
      if (active?.closest?.('.float') && !isTypingTarget(active)) active.blur()
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('pointerdown', onDown, true)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('pointerdown', onDown, true)
    }
  }, [])

  // ------------------------------------------------------------- 渲染
  const emptyText =
    state?.open && !samplesLayer.visible && checkedKeys.size > 0
      ? { title: '样本图层已隐藏', hint: '在左下「图层」中勾选「样本库 · 样本」即可显示' }
      : filterOn && checkedKeys.size === 0 && state?.open && samplesLayer.visible && !fieldShown
        ? { title: '画布上暂无样本', hint: '在左侧样本树中勾选类别，勾选的样本将显示在画布上' }
        : null
  const inspectLayer = selLayer === 'samples' ? 'samples' : selLayer !== null ? layerById.get(selLayer) ?? null : null

  return (
    <div className="shell">
      <header className="toolbar">
        <div className="seg">
          {(['browse', 'annotate'] as Mode[]).map((m) => (
            <Tooltip key={m} title={kbd(m === 'browse' ? '浏览、筛选与导出样本，不修改数据（⌘1）' : '绘制与编辑样本几何（⌘2）')}>
              <button className={'seg-btn' + (mode === m ? ' on' : '')} onClick={() => setMode(m)}>
                {m === 'browse' ? '浏览' : '标注'}
              </button>
            </Tooltip>
          ))}
        </div>
        <span className="tb-sep" />
        {tools.map((t) =>
          t.id === 'roi' ? (
            <div key={t.id} className="split-btn">
              <Tooltip title={`${t.label}（${t.key}）\n${t.hint}`} classNames={{ root: 'pre-tip' }}>
                <button className={'tool-btn' + (tool === t.id ? ' on' : '')} onClick={() => pickTool(t.id)}><Icon name={t.icon} /></button>
              </Tooltip>
              <Dropdown trigger={['click']} menu={{
                items: ROI_SHAPES.map(([k, label]) => ({ key: k, label: checkLabel(roiShape === k, label) })),
                onClick: ({ key }) => { setRoiShape(key as RoiShape); setTool('roi') },
              }}>
                <button className="tool-btn arrow" title="范围形状"><Chevron /></button>
              </Dropdown>
            </div>
          ) : (
            <Tooltip key={t.id} title={`${t.label}（${t.key}）\n${t.hint}`} classNames={{ root: 'pre-tip' }}>
              <button className={'tool-btn' + (tool === t.id ? ' on' : '')} onClick={() => pickTool(t.id)}>
                <Icon name={t.icon} />
              </button>
            </Tooltip>
          ),
        )}
        {mode === 'annotate' ? (
          <>
            <span className="tb-sep thin" />
            <Tooltip title={kbd('撤销（⌘Z）')}>
              <button className="tool-btn" disabled={!state?.can_undo} onClick={undo}><Icon name="undo" /></button>
            </Tooltip>
            <Tooltip title={kbd('重做（⇧⌘Z）')}>
              <button className="tool-btn" disabled={!state?.can_redo} onClick={redo}><Icon name="redo" /></button>
            </Tooltip>
          </>
        ) : null}
        <span className="tb-sep" />
        <div className="split-btn">
          <button className="tb-btn" onClick={openLibrary}>
            <Icon name="folder-open" /> 打开样本库
          </button>
          <Dropdown trigger={['click']} menu={{ items: openMenu, onClick: ({ key }) => {
            if (key === 'clear-recent') api.clearRecent().then(setRecent)
            else if (key.startsWith('r:')) openPath(key.slice(2))
            else dispatch(key)
          } }} onOpenChange={(open) => { if (open) api.recent().then(setRecent) }}>
            <button className="tb-btn arrow"><Chevron /></button>
          </Dropdown>
        </div>
        {mode === 'browse' ? (
          <>
            <Dropdown trigger={['click']} menu={{ items: importMenu, onClick: ({ key }) => dispatch(key) }}>
              <button className="tb-btn"><Icon name="upload" /> 导入 <Chevron /></button>
            </Dropdown>
            <Dropdown trigger={['click']} menu={{ items: exportMenu, onClick: ({ key }) => dispatch(key) }}>
              <button className="tb-btn" title="导出样本，或按样本位置裁剪影像块"><Icon name="download" /> 导出 <Chevron /></button>
            </Dropdown>
          </>
        ) : null}
        {has('fieldwork') ? (
          <Dropdown trigger={['click']} menu={{ items: fieldMenu, onClick: ({ key }) => dispatch(key) }}>
            <button className="tb-btn" title="野外点位、轨迹与照片，支持奥维收藏夹"><Icon name="map-pin" /> 野外 <Chevron /></button>
          </Dropdown>
        ) : null}
        {has('deeplearn') ? (
          <Dropdown trigger={['click']} menu={{ items: dlMenu, onClick: ({ key }) => dispatch(key) }}>
            <button className="tb-btn" title="训练分类模型与对比学习骨干、整景预测、特征分析"><Icon name="target" /> 深度学习 <Chevron /></button>
          </Dropdown>
        ) : null}
        {has('basemap') ? (
          <Dropdown trigger={['click']} menu={{ items: baseMenu, onClick: ({ key }) => onBaseMenu(key) }}>
            <button className="tb-btn"><Icon name="map" /> 底图 <Chevron /></button>
          </Dropdown>
        ) : null}
        <Dropdown trigger={['click']} menu={{ items: viewMenu, onClick: ({ key }) => onViewMenu(key) }}>
          <button className="tb-btn" title="缩放、经纬网与面板显示"><Icon name="maximize" /> 视图 <Chevron /></button>
        </Dropdown>
        <div className="spacer" />
        {mode === 'annotate' && state?.dirty ? <span className="dirty">未保存：{state.pending}</span> : null}
        <Dropdown trigger={['click']} menu={{ items: libMenu, onClick: ({ key }) => dispatch(key) }}>
          <button className="tb-btn quiet" title={state?.path}>
            {state?.open ? state.path.split(/[\\/]/).pop() : '未打开样本库'} <Chevron />
          </button>
        </Dropdown>
        {mode === 'annotate' ? (
          <Tooltip title={kbd('将未保存的标注改动写入样本库（⌘S）')}>
            <Button className="cta" disabled={!state?.dirty} onClick={save}>保存到样本库</Button>
          </Tooltip>
        ) : null}
      </header>

      <main className="body">
        {showTree ? (
          <aside className="side left">
            <div className="split-tree">
              <ClassTree
                classes={classes}
                onCheck={(ids, on) =>
                  run(async () => {
                    const r = await api.setSelection(on ? 'add' : 'remove', ids)
                    // 勾了样本却看不见，多半是样本图层被关掉了：勾选时顺手打开
                    if (on && !samplesLayer.visible) {
                      setSamplesLayer((sl) => ({ ...sl, visible: true }))
                      setStatus('样本图层原先处于隐藏状态，已重新显示。')
                    }
                    // 取消勾选时，看不见的那些也随之移出画布选中
                    if (!on && onlyChecked) {
                      const now = new Set(r.keys)
                      const keep = new Set([...selected].filter((k) => now.has(k) || Number(k.split(':')[1]) < 0))
                      if (keep.size !== selected.size) setSelected(keep)
                    }
                  })
                }
                onCheckOp={checkOp}
                onExportLevel={(id, m) => run(() => api.setExportLevel(id, m))}
                onClearExportLevels={() => run(() => api.clearExportLevels())}
                selection={treeSel}
                expanded={state?.open && treeView.library === state.path ? treeView.expanded : null}
                onExpand={(keys) => { if (state?.open) setTreeView({ library: state.path, expanded: keys }) }}
                scrollTo={treeScrollTo}
                reveal={treeReveal}
                onScroll={(top) => {
                  treeScroll.current = top
                  window.clearTimeout(treeScrollTimer.current)
                  treeScrollTimer.current = window.setTimeout(() => setTreeScrollTick((n) => n + 1), 400)
                }}
                onSelect={(sel) => {
                  setTreeSel(sel)
                  setSelected(new Set())
                  setSelLayer(null)
                }}
                canPaste={!!clipboard}
                onFilter={setTreeFilter}
                onAction={onTreeAction}
              />
            </div>
            <div className="split-layers">
              <LayerPanel
                layers={layers}
                order={layerOrder}
                samples={samplesLayer}
                selected={selLayer}
                tabTarget={tabLayer}
                modules={modules}
                onSelect={(id) => {
                  setSelLayer(id)
                  if (id !== null && id !== 'samples') setTabLayer(id)
                  setTreeSel(null)
                  setSelected(new Set())
                }}
                onZoom={async (id) => {
                  if (id !== 'samples') fitTo(layers.find((l) => l.id === id)?.extent)
                  else {
                    const r = await api.extent()
                    if (r.extent) fitTo(r.extent)
                    else setStatus('样本库里还没有样本。')
                  }
                }}
                onToggle={(id, visible) => patchLayer(id, { visible })}
                onReorder={setLayerOrder}
                onAction={onLayerAction}
                onAdd={(k) => addLayerDialog(k)}
              />
            </div>
          </aside>
        ) : null}
        <section className="center">
          {initialView !== undefined ? (
            <MapView
              features={features}
              classMap={classMap}
              shownKeys={shownKeys}
              selected={selected}
              onSelect={(keys) => {
                setSelected(keys)
                setSelLayer(null)
                const f = keys.size === 1 ? features?.features.find((x) => keys.has(x.id)) : undefined
                if (f) {
                  setTreeSel({ kind: 'class', id: f.properties.class_id })
                  setTreeReveal({ id: f.properties.class_id })
                }
              }}
              onIdentify={setIdentify}
              tool={tool}
              roiShape={roiShape}
              activeClass={activeClass}
              basemaps={basemaps}
              basemapIds={basemapIds}
              layers={layers}
              layerOrder={layerOrder}
              layerRevs={layerRevs}
              samples={samplesLayer}
              projection={projection.code}
              showGrid={showGrid}
              snap={snap}
              fitExtent={fitExtent}
              palette={palette}
              emptyText={emptyText}
              contextMenu={canvasMenu}
              onContextAction={onCanvasAction}
              onContextPoint={(ll) => { ctxPoint.current = ll }}
              onRoiDrawn={onRoiDrawn}
              onSpectrumPick={(ll, append) => moduleRef.current.pick?.(ll, append)}
              marks={spectrumOpen ? board?.marks : undefined}
              onZoomAll={zoomAll}
              onChanged={refresh}
              onCursor={setCursor}
              onViewChange={(v) => {
                viewRef.current = v
                // 视野停下来再记，拖动中不反复写
                window.clearTimeout(viewTick.current)
                viewTick.current = window.setTimeout(() => setViewVersion((n) => n + 1), 800)
              }}
              initialView={initialView}
              onMap={setOlMap}
            />
          ) : null}
        </section>
        {showInspector ? (
          <aside className="side right">
            <div className="split-top">
              <Inspector
                state={state}
                classes={classes}
                totalSamples={totalSamples}
                cls={selectedClass}
                selected={selectedList}
                feature={feature}
                layer={inspectLayer}
                samplesLayer={samplesLayer}
                onPatchLayer={patchLayer}
                onLayerAction={onLayerAction}
                onRename={askName}
                onPatchClass={(id, body) => run(() => api.patchClass(id, body))}
                onLabel={(key, label) => run(() => api.setLabel(key, label))}
                onMove={moveKeys}
                onDelete={deleteKeys}
              />
            </div>
            <div className="split-bottom">
              <IdentifyPanel keys={identifyLive} classes={classes} layers={layers} />
            </div>
          </aside>
        ) : null}
      </main>

      <footer className="status">
        <span className="status-msg">{status || (state?.open ? `已打开样本库：${state.path}` : '')}</span>
        {statusPath && desktop() ? (
          <a className="status-link" onClick={() => desktop()?.reveal_in_folder(statusPath)}>{REVEAL}</a>
        ) : null}
        <div className="spacer" />
        <span>
          {classes.length} 类 · {totalSamples} 条样本 · 已勾选 {state?.selected ?? 0} 条 / {state?.selected_classes ?? 0} 类
          {selected.size ? ` · 画布选中 ${selected.size}` : ''}
        </span>
        <span className="mono">{cursorText}</span>
        <Dropdown trigger={['click']} menu={{ items: crsItems, onClick: ({ key }) => onCrsMenu(key) }}
          onOpenChange={(open) => {
            if (!open) return
            const c = viewRef.current
            const ll = cursor?.lonlat ?? (c && projection.code === 'EPSG:4326' ? c.center : undefined)
            api.crsMenu(ll?.[0], ll?.[1]).then(setCrsMenu)
          }}>
          <span className="mono crs" title="显示坐标系，单击切换；仅影响显示，不改动数据">{projection.label}</span>
        </Dropdown>
      </footer>

      <AttrImportDialog profile={attrProfile} onCancel={() => setAttrProfile(null)} onOk={doImportAttr} />
      <ExportDialog opts={exportOpts} onCancel={() => setExportOpts(null)} onOk={doExport} />
      <RestoreDialog backups={backupList} onCancel={() => setBackupList(null)} onPick={doRestore}
        onFromFile={() => { setBackupList(null); restoreFromFile() }} />
      <RasterPropsDialog layer={rasterProps} onCancel={() => setRasterProps(null)}
        onOk={async (p) => {
          const l = rasterProps!
          setRasterProps(null)
          await patchLayer(l.id, p)
          bumpLayer(l.id)
        }} />
      <UniqueColorsDialog layer={uniqueLayer} onCancel={() => setUniqueLayer(null)}
        onOk={async (field, colors) => {
          const l = uniqueLayer!
          setUniqueLayer(null)
          await api.setUniqueColors(l.id, field, colors)
          await syncLayers()
          bumpLayer(l.id)
        }} />
      <RoiSelectDialog name={roiSelect?.name ?? null} keyword={!!treeFilter} onCancel={() => setRoiSelect(null)} onOk={doRoiSelect} />
      <ColorDialog cls={colorFor} onCancel={() => setColorFor(null)}
        onOk={(color) => { const c = colorFor!; setColorFor(null); run(() => api.patchClass(c.id, { color })) }} />
      <DescriptionDialog cls={descFor} onCancel={() => setDescFor(null)}
        onOk={(description) => { const c = descFor!; setDescFor(null); run(() => api.patchClass(c.id, { description })) }} />
      <MoveToDialog cls={moveFor} classes={classes} onCancel={() => setMoveFor(null)}
        onOk={(parentId) => {
          const c = moveFor!
          setMoveFor(null)
          const target = parentId != null ? classMap.get(parentId) : undefined
          run(() => api.moveClass(c.id, { parent_id: parentId, before_id: null, region: target?.region ?? c.region }))
        }} />
      <MoveFeaturesDialog req={moveFeatures} classes={classes} onCancel={() => setMoveFeatures(null)}
        onOk={(cid) => { const r = moveFeatures!; setMoveFeatures(null); moveKeys(r.keys, cid) }} />
      <KeysDialog open={keysOpen} onClose={(changed) => {
        setKeysOpen(false)
        if (changed) api.basemaps().then(setBasemaps).then(() => setStatus('底图密钥已保存，相关底图已启用。'))
      }} />
      {spectrumOpen && board ? (
        <SpectrumWindow board={board} setBoard={setBoard} rasters={spectralLayers.map((l) => ({ id: l.id, name: l.name }))}
          onClose={closeSpectrum} onOpenLibrary={() => setLibOpen(true)} onGroupMeans={() => setGroupOpen(true)}
          onStatus={setStatus} />
      ) : null}
      {libOpen && board ? (
        <LibraryWindow curves={board.curves} setBoard={setBoard} onClose={() => setLibOpen(false)} onStatus={setStatus} />
      ) : null}
      <GroupMeansDialog open={groupOpen} onClose={() => setGroupOpen(false)} onDone={async (added, problems) => {
        setBoard(await sapi.state())
        setStatus(`已计算 ${added} 条类别平均谱。${problems.length ? '未能计算：' + problems.slice(0, 4).join('；') : ''}`)
      }} />
      {has('fieldwork') ? (
        <Fieldwork handle={fieldRef} map={olMap} projection={projection.code} palette={palette}
          libraryOpen={!!state?.open} libraryPath={state?.path ?? ''} classes={classes}
          onStatus={setStatus} onFit={fitTo} onSamplesChanged={refresh} onShown={setFieldShown} />
      ) : null}
      {has('deeplearn') ? (
        <DeepLearn handle={dlRef} map={olMap} projection={projection.code} libraryOpen={!!state?.open}
          layers={layers} onStatus={setStatus} onFit={fitTo} onSyncLayers={syncLayers} onBumpLayer={bumpLayer}
          onSelectSamples={(keys) => {
            const live = new Set((features?.features ?? []).map((f) => f.id))
            const hit = keys.filter((k) => live.has(k))
            setSelected(new Set(hit))
            fitTo(extentOfKeys(hit))
            setStatus(hit.length ? `已在画布上选中 ${hit.length} 条样本` : '这些样本不在当前显示的样本里（可能被筛选隐藏或已删除）')
          }}
          onCheckSamples={(keys) => run(async () => {
            const r = await api.setSelection('add', undefined, keys)
            setStatus(`已加入勾选，当前勾选 ${r.keys.length} 条样本`)
          })} />
      ) : null}
      <ClipDialog open={clipOpen} onCancel={() => setClipOpen(false)} onOk={doClip} />
      {toolbox ? <ToolboxWindow input={toolbox.input} onClose={() => setToolbox(null)} onStatus={setStatus} /> : null}
      <HelpModal open={helpOpen} onClose={() => setHelpOpen(false)} />
      <AboutModal info={aboutOpen ? appInfo : null} onClose={() => setAboutOpen(false)} />
    </div>
  )
}

function unionExtent(list: (number[] | null | undefined)[]): number[] | null {
  let box: number[] | null = null
  for (const b of list) {
    if (!b) continue
    box = box ? [Math.min(box[0], b[0]), Math.min(box[1], b[1]), Math.max(box[2], b[2]), Math.max(box[3], b[3])] : [...b]
  }
  return box
}
