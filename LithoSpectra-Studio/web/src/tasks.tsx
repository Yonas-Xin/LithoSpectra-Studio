// 长任务的进度框。任务短于 400 ms 不弹框，
// 免得小库导入时窗口闪一下；框是模态的，任务期间别的操作点不动；能取消的给取消。

import { createContext, useCallback, useContext, useRef, useState } from 'react'
import { Button, Modal, Progress } from 'antd'
import { api } from './api'
import type { TaskInfo } from './api'

const SHOW_DELAY_MS = 400
const POLL_MS = 150

export class TaskError extends Error {}

/** 任务被取消时 run() 返回的结果里 cancelled 为真，调用方据此收尾。 */
export interface TaskOutcome<T> {
  result: T
  cancelled: boolean
}

type Runner = <T>(start: () => Promise<TaskInfo>) => Promise<TaskOutcome<T>>
const Ctx = createContext<Runner | null>(null)

export function useTaskRunner(): Runner {
  const run = useContext(Ctx)
  if (!run) throw new Error('TaskProvider missing')
  return run
}

export function TaskProvider({ children }: { children: React.ReactNode }) {
  const [task, setTask] = useState<TaskInfo | null>(null)
  const [visible, setVisible] = useState(false)
  const busy = useRef(false)

  const run: Runner = useCallback(async <T,>(start: () => Promise<TaskInfo>) => {
    if (busy.current) throw new TaskError('上一个任务尚未完成。')
    busy.current = true
    const shown = setTimeout(() => setVisible(true), SHOW_DELAY_MS)
    try {
      let t = await start()
      setTask(t)
      while (t.status === 'running') {
        await new Promise((r) => setTimeout(r, POLL_MS))
        t = await api.task(t.id)
        setTask(t)
      }
      if (t.status === 'error') throw new TaskError(t.error || `${t.title}失败`)
      return { result: t.result as T, cancelled: t.status === 'cancelled' }
    } finally {
      clearTimeout(shown)
      busy.current = false
      setVisible(false)
      setTask(null)
    }
  }, [])

  const pct = task && task.total > 0 ? Math.min(100, Math.round((task.done / task.total) * 100)) : null
  return (
    <Ctx.Provider value={run}>
      {children}
      <Modal
        open={visible && !!task}
        title={task?.title}
        closable={false}
        maskClosable={false}
        keyboard={false}
        width={440}
        footer={
          task?.cancellable ? (
            <Button disabled={task.cancel_requested} onClick={() => task && api.cancelTask(task.id)}>
              {task.cancel_requested ? '正在停止…' : '取消'}
            </Button>
          ) : null
        }
      >
        <div className="task-text">{task?.text || '请稍候…'}</div>
        {pct === null ? (
          <div className="task-indeterminate"><span /></div>
        ) : (
          <Progress percent={pct} showInfo={false} strokeLinecap="butt" className="task-bar" />
        )}
        <div className="task-count mono">
          {task && task.total > 0 ? `${task.done} / ${task.total}` : ''}
        </div>
      </Modal>
    </Ctx.Provider>
  )
}
