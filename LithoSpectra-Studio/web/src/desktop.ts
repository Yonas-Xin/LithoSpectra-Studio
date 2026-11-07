// 桌面壳（pywebview）暴露给页面的原生能力。
// 在普通浏览器里打开时（开发调试）这些能力不存在，调用方退回手填路径。

interface DesktopApi {
  open_library_dialog(): Promise<string | null>
  pick_file(kind: 'library' | 'vector' | 'image' | 'any' | 'ovobj' | 'photo' | 'track', directory?: string): Promise<string | null>
  pick_files(kind: 'photo' | 'ovobj' | 'any', directory?: string): Promise<string[]>
  pick_folder(directory?: string): Promise<string | null>
  /** 拖进窗口的文件：按文件名换真实路径 */
  dropped_paths(names: string[]): Promise<string[]>
  save_file(filename?: string, directory?: string): Promise<string | null>
  reveal_in_folder(path: string): Promise<boolean>
  copy_text(text: string): Promise<boolean>
}

declare global {
  interface Window {
    pywebview?: { api: DesktopApi }
  }
}

export function desktop(): DesktopApi | null {
  return window.pywebview?.api ?? null
}

/** pywebview 的 api 对象在页面加载之后才注入，等它就绪。 */
export function whenDesktopReady(timeoutMs = 1500): Promise<DesktopApi | null> {
  if (window.pywebview?.api) return Promise.resolve(window.pywebview.api)
  return new Promise((resolve) => {
    const done = () => resolve(window.pywebview?.api ?? null)
    window.addEventListener('pywebviewready', done, { once: true })
    setTimeout(done, timeoutMs)
  })
}

/** 焦点是不是在能打字的地方（文本框、下拉、可编辑区）。复选框、单选、滑块不算：
 *  在图层或样本树里勾了一下，数字键切工具、空格平移照样要能用。 */
export function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  if (!el || !el.closest) return false
  if (el.closest('textarea, select, [contenteditable=true], [contenteditable=""]')) return true
  const input = el.closest('input') as HTMLInputElement | null
  return !!input && !['checkbox', 'radio', 'range', 'button', 'submit', 'reset', 'color', 'file'].includes(input.type)
}
