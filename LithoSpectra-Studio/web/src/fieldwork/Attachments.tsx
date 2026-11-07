// 照片以外的附件：录音、视频、文档。奥维标签上带的，或手动添加的。
//
// 原文件只存不改。录音、视频在页面里能放就直接放（浏览器认不出的格式，如安卓录音
// 常用的 AMR，交给系统程序打开；奥维自己的 Speex 录音由后端转成 WAV 再放）；文档用
// 系统程序打开。打开的是按原文件名复制出来的临时副本，改了也不会动到库里的原件。

import { useState } from 'react'
import { App as AntApp, Input, Tooltip } from 'antd'

import { desktop } from '../desktop'
import { Icon } from '../theme'
import { attachmentUrl, fmtSize, fmtTime, fw, noteLine } from './fwapi'
import type { FwAttachment, FwStatus } from './fwapi'
import { REVEAL } from '../platform'

/** 拖动附件时 dataTransfer 里的类型：拖到左侧列表的点位上即改挂到那个点位 */
export const ATTACH_MIME = 'application/x-fw-attachment'

const KIND_LABEL: Record<string, string> = { audio: '录音', video: '视频', file: '文件', photo: '图片' }

const MIME: Record<string, string> = {
  mp3: 'audio/mpeg', m4a: 'audio/mp4', aac: 'audio/aac', wav: 'audio/wav', ogg: 'audio/ogg', opus: 'audio/ogg',
  flac: 'audio/flac', amr: 'audio/amr', caf: 'audio/x-caf',
  mp4: 'video/mp4', m4v: 'video/mp4', mov: 'video/quicktime', '3gp': 'video/3gpp', webm: 'video/webm',
}

/** 这个附件能不能在页面里直接放（奥维录音由后端转成 WAV 再放） */
function playable(a: FwAttachment): boolean {
  if (a.kind !== 'audio' && a.kind !== 'video') return false
  const mime = a.transcode === 'wav' ? 'audio/wav' : MIME[a.ext ?? '']
  if (!mime) return false
  const el = document.createElement(a.kind === 'audio' ? 'audio' : 'video')
  return el.canPlayType(mime) !== ''
}

export function AttachmentList(props: {
  items: FwAttachment[]
  onChanged: () => void
  onStatus: (text: string) => void
  /** 可以拖到点位上改挂（列表里的行接得住） */
  draggable?: boolean
  /** 录音转文字：引擎与模型的情况；没有就不显示转写 */
  asr?: FwStatus['asr']
  onTranscribe?: (a: FwAttachment) => void
  /** 把一段转写文字追加到点位备注（挂在点位上的录音才有） */
  onAppendToNote?: (line: string) => void
}) {
  const { message, modal } = AntApp.useApp()
  const [video, setVideo] = useState<string | null>(null)

  const open = async (a: FwAttachment) => {
    try {
      await fw.openAttachment(a.uid)
      props.onStatus(`已用系统程序打开 ${a.orig_name ?? ''}（打开的是副本，库中的原件不变）`)
    } catch (e) {
      message.error((e as Error).message)
    }
  }
  const remove = (a: FwAttachment) => modal.confirm({
    title: `从库中移除附件「${a.orig_name ?? ''}」？`,
    content: '仅从库中移除该记录。媒体文件夹中的原文件保留。',
    okText: '移除', okButtonProps: { danger: true }, cancelText: '取消',
    onOk: async () => {
      await fw.deleteAttachment(a.uid)
      props.onChanged()
    },
  })
  const saveTranscript = async (a: FwAttachment, transcript: string) => {
    if (transcript === (a.transcript ?? '')) return
    try {
      await fw.patchAttachment(a.uid, { transcript })
      props.onChanged()
    } catch (e) {
      message.error((e as Error).message)
    }
  }
  const saveCaption = async (a: FwAttachment, caption: string) => {
    if (caption === (a.caption ?? '')) return
    try {
      await fw.patchAttachment(a.uid, { caption })
      props.onChanged()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  return (
    <div className="fw-atts">
      {props.items.map((a) => (
        <div key={a.uid} className={'fw-att' + (a.missing ? ' missing' : '')}
          draggable={props.draggable}
          onDragStart={(e) => e.dataTransfer.setData(ATTACH_MIME, a.uid)}
          title={[a.orig_name, a.taken ? `时间 ${fmtTime(a.taken)}` : '', a.source ? `来自 ${a.source}` : ''].filter(Boolean).join('\n')}>
          <div className="fw-att-head">
            <span className={'fw-att-kind ' + a.kind}>{KIND_LABEL[a.kind] ?? '文件'}</span>
            <span className="fw-att-name">{a.orig_name || '未命名附件'}</span>
            <span className="caption mono nowrap">{fmtSize(a.size)}</span>
            {a.kind === 'video' && playable(a) ? (
              <Tooltip title={video === a.uid ? '收起' : '在此播放'}>
                <button className="icon-btn" onClick={() => setVideo(video === a.uid ? null : a.uid)}><Icon name="eye" size={14} /></button>
              </Tooltip>
            ) : null}
            <Tooltip title="用系统程序打开">
              <button className="icon-btn" disabled={a.missing} onClick={() => open(a)}><Icon name="folder-open" size={14} /></button>
            </Tooltip>
            {desktop() && !a.missing ? (
              <Tooltip title={`${REVEAL}原文件`}>
                <button className="icon-btn" onClick={() => desktop()!.reveal_in_folder(a.path)}><Icon name="search" size={14} /></button>
              </Tooltip>
            ) : null}
            <Tooltip title="从库中移除">
              <button className="icon-btn" onClick={() => remove(a)}><Icon name="trash" size={14} /></button>
            </Tooltip>
          </div>
          {a.missing ? <div className="fw-warn">媒体文件夹中找不到原文件。</div> : null}
          {!a.missing && a.kind === 'audio' && playable(a) ? (
            <audio className="fw-att-audio" controls preload="none" src={attachmentUrl(a)} />
          ) : null}
          {!a.missing && a.kind === 'audio' && !playable(a) ? (
            <div className="caption">
              {a.ext === 'spx' ? '奥维录音（Speex）需要解码库才能播放，当前环境中没有找到 libspeex。'
                : `该录音格式（${(a.ext ?? '').toUpperCase()}）无法在窗口内播放，可用系统程序打开。`}
            </div>
          ) : null}
          {video === a.uid ? <video className="fw-att-video" controls src={attachmentUrl(a)} /> : null}
          {!a.missing && a.kind === 'audio' && props.asr?.engine && props.onTranscribe ? (
            a.transcript != null ? (
              <div className="fw-att-text">
                <Input.TextArea key={a.uid + ':' + a.transcript} size="small" autoSize={{ minRows: 1, maxRows: 8 }}
                  defaultValue={a.transcript} placeholder="（没有识别出文字）"
                  onBlur={(e) => saveTranscript(a, e.target.value)} />
                <div className="fw-att-text-actions">
                  <span className="caption">转写结果，可直接修改</span>
                  {props.onAppendToNote && a.transcript ? (
                    <button className="tb-btn" onClick={(e) => {
                      // 取文本框里的当前内容（可能刚改过、还没失焦保存）
                      const box = (e.currentTarget.closest('.fw-att-text')?.querySelector('textarea')?.value ?? a.transcript ?? '').trim()
                      if (box) props.onAppendToNote!(noteLine(box, a.taken))
                    }}>追加到备注</button>
                  ) : null}
                  <button className="tb-btn" onClick={() => props.onTranscribe!(a)}>重新转写</button>
                </div>
              </div>
            ) : (
              <div className="fw-att-text-actions">
                <Tooltip title={props.asr.model ? '在本机识别，一般几秒内完成' : '第一次使用需下载约 240 MB 的识别模型'}>
                  <button className="tb-btn" onClick={() => props.onTranscribe!(a)}>转文字</button>
                </Tooltip>
              </div>
            )
          ) : null}
          <Input size="small" variant="borderless" className="fw-att-caption" placeholder="添加说明…" defaultValue={a.caption ?? ''}
            onBlur={(e) => saveCaption(a, e.target.value)} onPressEnter={(e) => (e.target as HTMLInputElement).blur()} />
        </div>
      ))}
    </div>
  )
}
