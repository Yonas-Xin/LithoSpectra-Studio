// 按所在系统换的几处文字：快捷键写法、「在访达中显示」。源码里一律按 Mac 写（⌘ ⇧ ⌥），
// 在 Windows 上换成 Ctrl+ Shift+ Alt+（按键处理本来就把 Ctrl 当 ⌘）。

export const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)

/** 快捷键文字：Mac 上原样，Windows 上 ⇧⌘Z → Ctrl+Shift+Z */
export function kbd(text: string): string {
  if (IS_MAC) return text
  return text.replace(/⇧⌘/g, 'Ctrl+Shift+').replace(/⌘/g, 'Ctrl+').replace(/⇧/g, 'Shift+').replace(/⌥/g, 'Alt+')
}

/** 在系统文件管理器里显示文件的说法 */
export const REVEAL = IS_MAC ? '在访达中显示' : '在资源管理器中显示'
