// 导出照片：库里的照片是原始资料，调整（去水印、修补、地质水印）要经过导出才落到
// 文件上。没调整过的照片导出的就是原片；原片缺的朝向、位置、说明可以选择补进 EXIF，
// 原片已有的信息不动。

import { useEffect, useMemo, useState } from 'react'
import { Button, Checkbox, Input, Modal, Radio, Tag } from 'antd'

import { desktop } from '../desktop'
import { fw } from './fwapi'

export interface ExportRequest {
  /** 对话框标题里说明导出范围，如「HDW-01 的 4 张照片」 */
  title: string
  count: number
  uids?: string[]
  survey?: string
  /** 第一张的点位名与原文件名，用来预览文件名 */
  sample?: { point: string; orig: string; date: string; survey: string }
}

const FOLDER_KEY = 'litho.fw.exportFolder'
const TEMPLATE_KEY = 'litho.fw.exportTemplate'
const INFO_KEY = 'litho.fw.exportInfo'

function remembered(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) || fallback
  } catch {
    return fallback
  }
}

function remember(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // 存不下就算了，下次重选
  }
}

export default function ExportDialog(props: {
  req: ExportRequest | null
  onCancel: () => void
  onOk: (opts: { folder: string; content: 'adjusted' | 'original'; template: string; info: boolean }) => void
}) {
  const r = props.req
  const [fields, setFields] = useState<Record<string, string>>({})
  const [def, setDef] = useState('{点位}_{序号}')
  const [folder, setFolder] = useState('')
  const [content, setContent] = useState<'adjusted' | 'original'>('adjusted')
  const [template, setTemplate] = useState('')
  const [info, setInfo] = useState(true)

  useEffect(() => {
    fw.exportTemplate().then((t) => { setFields(t.fields); setDef(t.default) }).catch(() => undefined)
  }, [])
  useEffect(() => {
    if (!r) return
    setFolder(remembered(FOLDER_KEY, ''))
    setTemplate(remembered(TEMPLATE_KEY, ''))
    setContent('adjusted')
    setInfo(remembered(INFO_KEY, '1') === '1')
  }, [r])

  const tpl = template.trim() || def
  const preview = useMemo(() => {
    const s = r?.sample
    if (!s) return ''
    const values: Record<string, string> = { 点位: s.point, 序号: '01', 原名: s.orig, 日期: s.date, 批次: s.survey }
    const name = tpl.replace(/\{([^{}]+)\}/g, (m, k) => values[k] ?? m).replace(/[\\/:*?"<>|]+/g, '_')
    return name + '.' + (s.orig.includes('.') ? s.orig.split('.').pop() : 'jpg')
  }, [r, tpl, content])

  if (!r) return null

  const pick = async () => {
    const d = await desktop()?.pick_folder()
    if (d) setFolder(d)
  }
  const ok = () => {
    remember(FOLDER_KEY, folder.trim())
    remember(TEMPLATE_KEY, template.trim())
    remember(INFO_KEY, info ? '1' : '0')
    props.onOk({ folder: folder.trim(), content, template: tpl, info })
  }

  return (
    <Modal open title="导出照片" okText={`导出 ${r.count} 张`} cancelText="取消" width={520}
      okButtonProps={{ disabled: !folder.trim() }} onCancel={props.onCancel} onOk={ok}>
      <div className="dlg">
        <div className="caption">{r.title}</div>

        <div className="fw-export-block">
          <div className="fw-export-label">导出内容</div>
          <Radio.Group value={content} onChange={(e) => setContent(e.target.value)} className="radios">
            <Radio value="adjusted">
              调整后的照片
              <div className="caption">调整过的照片（去水印、修补、地质水印）导出效果图，未调整的照片导出原片。</div>
            </Radio>
            <Radio value="original">
              全部导出原片
              <div className="caption">忽略所有调整，导出导入时的原始文件。</div>
            </Radio>
          </Radio.Group>
          <Checkbox className="fw-export-info" checked={info} onChange={(e) => setInfo(e.target.checked)}>
            补写照片信息
            <div className="caption">将库中补记的朝向、位置、说明写入 EXIF，只补原片缺少的项，原有信息不变，像素不重新压缩。不勾选时，未调整的照片与原文件逐字节相同。</div>
          </Checkbox>
        </div>

        <div className="fw-export-block">
          <div className="fw-export-label">文件名</div>
          <Input value={template} placeholder={def} onChange={(e) => setTemplate(e.target.value)} />
          <div className="fw-tokens">
            {Object.entries(fields).map(([k, tip]) => (
              <Tag key={k} className="fw-token" title={tip} onClick={() => setTemplate((t) => (t || '') + `{${k}}`)}>{k}</Tag>
            ))}
          </div>
          {preview ? <div className="caption">示例：<span className="mono">{preview}</span>　文件重名时自动添加 _2、_3 后缀，不会覆盖已有文件。</div> : null}
        </div>

        <div className="fw-export-block">
          <div className="fw-export-label">导出到</div>
          <div className="inline">
            <Input value={folder} placeholder="选择文件夹" onChange={(e) => setFolder(e.target.value)} />
            {desktop() ? <Button onClick={pick}>选择…</Button> : null}
          </div>
        </div>
      </div>
    </Modal>
  )
}
