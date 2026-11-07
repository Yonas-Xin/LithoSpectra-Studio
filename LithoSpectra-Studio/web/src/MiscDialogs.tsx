// 零碎的小对话框：类别颜色 / 说明、移动到其它上级、改归到其它类别、底图密钥、
// 使用说明、关于。

import { useEffect, useState } from 'react'
import { Button, ColorPicker, Input, Modal, Select, TreeSelect } from 'antd'
import { api } from './api'
import type { AppInfo, BasemapKey, SampleClass } from './api'
import { REVEAL, kbd } from './platform'

function classPath(classes: SampleClass[], id: number): string {
  const byId = new Map(classes.map((c) => [c.id, c]))
  const out: string[] = []
  let cur = byId.get(id)
  const guard = new Set<number>()
  while (cur && !guard.has(cur.id)) {
    guard.add(cur.id)
    out.unshift(cur.name)
    cur = cur.parent_id != null ? byId.get(cur.parent_id) : undefined
  }
  return out.join(' / ')
}

export function subtree(classes: SampleClass[], id: number): Set<number> {
  const out = new Set<number>([id])
  let grew = true
  while (grew) {
    grew = false
    for (const c of classes) if (c.parent_id != null && out.has(c.parent_id) && !out.has(c.id)) {
      out.add(c.id)
      grew = true
    }
  }
  return out
}

// =============================================================================

export function ColorDialog(props: { cls: SampleClass | null; onCancel: () => void; onOk: (color: string) => void }) {
  const [color, setColor] = useState('#3b82f6')
  useEffect(() => { if (props.cls) setColor(props.cls.color) }, [props.cls])
  return (
    <Modal open={!!props.cls} title={`设置颜色 · ${props.cls?.name ?? ''}`} width={360} onCancel={props.onCancel}
      onOk={() => props.onOk(color)} okText="确定">
      <div className="dlg">
        <ColorPicker value={color} disabledAlpha showText onChangeComplete={(c) => setColor(c.toHexString())} />
        <div className="caption">类别在画布上的显示颜色，修改后立即写入样本库，可撤销。</div>
      </div>
    </Modal>
  )
}

export function DescriptionDialog(props: { cls: SampleClass | null; onCancel: () => void; onOk: (text: string) => void }) {
  const [text, setText] = useState('')
  useEffect(() => { if (props.cls) setText(props.cls.description) }, [props.cls])
  return (
    <Modal open={!!props.cls} title={`编辑说明 · ${props.cls?.name ?? ''}`} width={480} onCancel={props.onCancel}
      onOk={() => props.onOk(text)} okText="确定">
      <Input.TextArea autoSize={{ minRows: 4, maxRows: 12 }} value={text} onChange={(e) => setText(e.target.value)} />
    </Modal>
  )
}

/** 移动类别：只能挂到同工区的分组下面，或移到顶层。基础类别要合并请拖到它上面。 */
export function MoveToDialog(props: { cls: SampleClass | null; classes: SampleClass[]; onCancel: () => void; onOk: (parentId: number | null) => void }) {
  const [target, setTarget] = useState<string>('top')
  useEffect(() => setTarget('top'), [props.cls])
  if (!props.cls) return <Modal open={false} />
  const forbidden = subtree(props.classes, props.cls.id)
  const options = [{ value: 'top', label: '（移到顶层）' }, ...props.classes
    .filter((c) => c.is_group && c.region === props.cls!.region && !forbidden.has(c.id) && !c.draft)
    .map((c) => ({ value: String(c.id), label: classPath(props.classes, c.id) }))]
  return (
    <Modal open title="移动类别" width={460} onCancel={props.onCancel} okText="移动"
      onOk={() => props.onOk(target === 'top' ? null : Number(target))}>
      <div className="dlg">
        <div>将「{props.cls.name}」移动到：</div>
        <Select showSearch optionFilterProp="label" value={target} options={options} onChange={setTarget} />
      </div>
    </Modal>
  )
}

/** 把选中要素改归到其它类别：只列装得下这批要素的基础类别，不列它们眼下所在的那个类。 */
export function MoveFeaturesDialog(props: { req: { keys: string[]; kind: string; from: Set<number> } | null; classes: SampleClass[]; onCancel: () => void; onOk: (classId: number) => void }) {
  const [target, setTarget] = useState<number | null>(null)
  useEffect(() => setTarget(null), [props.req])
  if (!props.req) return <Modal open={false} />
  const { kind, from } = props.req
  interface N { value: number; title: string; selectable: boolean; children: N[] }
  const nodes = new Map<number, N>()
  for (const c of props.classes) {
    const ok = !c.is_group && (c.kind === kind || c.kind === '') && !(from.size === 1 && from.has(c.id))
    nodes.set(c.id, { value: c.id, title: c.name, selectable: ok, children: [] })
  }
  const roots: N[] = []
  for (const c of props.classes) {
    const p = c.parent_id != null ? nodes.get(c.parent_id) : undefined
    if (p) p.children.push(nodes.get(c.id)!)
    else roots.push(nodes.get(c.id)!)
  }
  return (
    <Modal open title={`将选中的 ${props.req.keys.length} 个要素改归到其他类别`} width={480} onCancel={props.onCancel}
      okText="改归" okButtonProps={{ disabled: target === null }} onOk={() => target !== null && props.onOk(target)}>
      <TreeSelect style={{ width: '100%' }} treeData={roots} value={target ?? undefined} placeholder="选择基础类别"
        treeDefaultExpandAll showSearch treeNodeFilterProp="title" onChange={(v: number) => setTarget(v)} />
    </Modal>
  )
}

// =============================================================================

/** 底图密钥：密钥不在代码里，按环境变量 → keys.json 的顺序找，这里写的是 keys.json。 */
export function KeysDialog(props: { open: boolean; onClose: (changed: boolean) => void }) {
  const [keys, setKeys] = useState<BasemapKey[]>([])
  const [file, setFile] = useState('')
  const [edit, setEdit] = useState<Record<string, string>>({})
  const [err, setErr] = useState('')
  useEffect(() => {
    if (!props.open) return
    setEdit({})
    setErr('')
    api.basemapKeys().then((r) => { setKeys(r.keys); setFile(r.file) }).catch((e) => setErr((e as Error).message))
  }, [props.open])
  const save = async () => {
    try {
      for (const [name, value] of Object.entries(edit)) await api.setBasemapKey(name, value)
      props.onClose(Object.keys(edit).length > 0)
    } catch (e) {
      setErr((e as Error).message)
    }
  }
  return (
    <Modal open={props.open} title="配置底图密钥" width={560} onCancel={() => props.onClose(false)} okText="保存" onOk={save}>
      <div className="dlg">
        <div className="caption">密钥仅保存在本机：{file}。如已在环境变量中设置，以环境变量为准。</div>
        {keys.map((k) => (
          <div key={k.name} className="key-block">
            <div className="form-row">
              <label>{k.label}</label>
              <div className="form-ctl">
                <Input.Password placeholder={k.set ? `已配置（${k.source}）${k.masked ? '　' + k.masked : ''}` : '未配置'}
                  value={edit[k.name] ?? ''} onChange={(e) => setEdit({ ...edit, [k.name]: e.target.value })} />
              </div>
            </div>
            <div className="caption key-hint">{k.hint}　申请：{k.apply}</div>
          </div>
        ))}
        {err ? <div className="caption warn">{err}</div> : null}
      </div>
    </Modal>
  )
}

// =============================================================================

const HELP: [string, string[]][] = [
  ['样本树', [
    '勾选框有全选、部分选中、未选三种状态；勾选上级类别会同时勾选其全部下级。导出与裁剪只处理已勾选的样本。',
    '右键菜单可重命名、新建分组、移动、合并、删除类别，以及设置导出层级。',
    '拖动类别可调整层级：拖到分组上成为其下级；拖到基础类别上，两者合并为一个新分组；拖到两行之间仅调整顺序；拖到工区标题上移到顶层。支持跨工区移动。',
    '⌘C 复制类别、分组或工区，选中目标后按 ⌘V 粘贴。',
    '命名对话框提供地质符号面板（年代、岩石代号、成因、希腊字母等）。选中文字后按 ⌘= 转为下标、⇧⌘= 转为上标；未选中时切换下标或上标输入。',
  ]],
  ['画布', [
    '滚轮缩放，双击显示全图。平移：中键拖动，或按住空格后左键拖动；平移工具下左键拖动也可平移。',
    '按下到松开移动不超过 6 像素视为单击。单击样本可选中，Shift 单击加选；选择工具下拖框多选。单击选中一条样本时，样本树展开并定位到其所属类别。',
    '画布默认只显示已勾选的样本，未显示的样本无法选中。',
    '单击位置的要素属性显示在右栏「要素信息」中。',
    '将影像或矢量文件拖入窗口可叠加显示；拖入样本库（.gpkg）则打开该库。',
    '「画范围」工具（可选多边形、矩形、圆）绘制的范围保存在「感兴趣区」图层；右键该图层可按范围选择样本或导出为矢量。',
    '状态栏右端显示当前显示坐标系，单击可切换，不影响样本库中的数据。',
    '浮动窗口（光谱曲线、光谱库、工具箱、野外记录、深度学习）单击即置于最上层，关闭后再打开回到原来的位置与大小。焦点在浮动窗口内时，数字键、Delete、Tab 不作用于画布。',
  ]],
  ['标注', [
    '先在样本树中选中要绘制的基础类别，再切换到标注模式（⌘2）。',
    '工具：1 平移、2 选择、3 画点、4 画面、5 改顶点、6 删要素。画面时双击、回车或右键闭合，退格删除上一个顶点，Esc 取消。',
    '几何的新增、修改与删除先暂存，命令条右侧显示未保存的改动；点击「保存到样本库」（⌘S）后统一写入。',
    '画布右键菜单：用选中要素新建类别、改归到其他类别或当前类别、命名、删除。',
  ]],
  ['光谱', [
    '右键影像图层选择「光谱曲线…」打开曲线窗口，在影像上单击即可取谱；Shift 单击保留当前曲线。',
    '「视图 → 光谱库…」可浏览、检索与匹配光谱，也可将曲线保存到「我的光谱」。',
  ]],
  ['野外记录', [
    '「野外」菜单可导入奥维收藏夹、照片和 GPX 轨迹，点位与照片保存在样本库中。',
    '奥维收藏夹可一次选多份、选一个文件夹（含子文件夹），或同时拖入多份：列表中逐份设置批次名称，导入过的默认不勾选；按时间先后导入，同一个标签出现在几份文件里时，后导入的更新先导入的。',
    '奥维标签上的录音、视频、文档作为附件一并导入，显示在点位详情的「附件」中：录音可直接播放（窗口内无法播放的格式用系统程序打开），文档用系统程序打开。没有挂在标签上的附件放在「未关联点位的照片与附件」中。附件可拖到列表中的其他点位上，也可在点位详情中手动添加。',
    '录音转文字：点位详情的录音下点「转文字」，识别结果可修改，点「追加到备注」加到点位备注末尾；「录音转文字到备注」一次转写点位（或在批次详情中转写整个批次）上的全部录音并追加。导入奥维文件时也可勾选导入后自动转写。识别在本机离线进行，第一次使用需下载约 240 MB 的识别模型。',
    '导入的照片作为原始资料保存，不会被修改。去水印、修补、编辑与地质水印都是可撤销的调整，通过「导出照片」输出调整后的照片。',
    '在平移或选择工具下单击画布上的野外点位或轨迹，打开「野外记录」窗口，左侧列表展开所在批次并定位到该项。野外要素画在样本上面，单击它们不改变样本的选中。',
    '列表中 ↑↓ 切换、回车缩放；选中的项不在视野内或被窗口挡住时，地图平移过去，比例尺不变。',
    '点位详情中的手形按钮：在画布上拖动点位调整位置，松开即保存，可还原到原位置；再次单击或按 Esc 结束。',
    '轨迹的高程剖面图：鼠标移上去读出距离与高程，画布上同步标出对应位置。',
  ]],
  ['深度学习', [
    '「深度学习」菜单打开工作台，包括样本集、训练、任务、模型库、预测与特征。训练与预测在后台运行，关闭工作台不影响任务。',
    '相似检索选取参考点时，画布上的参考点带序号；退格撤销上一个，Esc 完成。选点期间画布不选样本、不画样本。',
    '在任务结果或特征分析中单击「定位」，画布上以圆圈标出该位置，并显示在工作台旁未被遮挡的区域。',
  ]],
  ['导入与导出', [
    '导入方式：样本目录（一个目录对应一个工区，按文件名确定类别）、样本库根目录（批量导入多个工区）、单个矢量文件、按属性字段分级。',
    '导出时每个矢量文件对应一类样本。「导出层级」决定按哪一层划分文件；也可在样本树中为各分支单独设置「整支合并」或「按子类分别输出」。',
  ]],
  ['样本库文件', [
    '点击命令条右上角的库名，可查看完整路径、在访达中显示、复制路径、另存为或备份。',
    '「另存为」将样本库复制到新位置并切换到新文件；「备份到」将样本库复制到指定位置，当前仍使用原文件。',
  ]],
  ['撤销与恢复', [
    '⌘Z 逐步撤销；「编辑 → 恢复全部类别」将类别恢复到导入时的状态；「从备份还原」可选择自动备份或手动备份的文件。',
  ]],
  ['常用快捷键', [
    '⌘O 打开　⌘N 新建　⌘S 保存标注　⇧⌘S 另存为　⌘E 导出',
    '⌘1 / ⌘2 浏览 / 标注　⌘0 全图　⌘J 缩放到选中',
    '⌘Z 撤销　⇧⌘Z 重做　⌘A 全部勾选　⇧⌘A 全部取消勾选',
    '数字键 1~6 切换画布工具，7 画范围，8 取谱；按住空格临时平移',
  ]],
]

export function HelpModal(props: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={props.open} title="使用说明" width={640} onCancel={props.onClose} footer={<Button onClick={props.onClose}>关闭</Button>}>
      <div className="dlg help">
        {HELP.map(([h, lines]) => (
          <section key={h}>
            <div className="dlg-subtitle small">{h}</div>
            {lines.map((l, i) => <p key={i}>{kbd(l).replace('在访达中显示', REVEAL)}</p>)}
          </section>
        ))}
      </div>
    </Modal>
  )
}

export function AboutModal(props: { info: AppInfo | null; onClose: () => void }) {
  const i = props.info
  return (
    <Modal open={!!i} title="关于" width={460} onCancel={props.onClose} footer={<Button onClick={props.onClose}>关闭</Button>}>
      {i ? (
        <div className="about">
          <img src="/logo.png" width={56} height={56} alt="" />
          <div className="about-title">{i.title}</div>
          <div className="muted">版本 {i.version}　{i.profile} 发行版</div>
          <p>样本库存为单个 GeoPackage 文件，提供多级分类、按属性导入、分级导出和叠加图层与底图的样本标注。光谱、野外记录、深度学习等功能以模块形式装入。</p>
          <p className="muted">格式版本 {i.format_version}　存储坐标系 EPSG:{i.storage_epsg}　GDAL {i.gdal}</p>
          <p className="muted">已装模块：{i.module_info.map((m) => m.name).join('、') || '无'}</p>
          <p className="muted">数据目录：{i.data_dir}</p>
        </div>
      ) : null}
    </Modal>
  )
}
