// 主题：令牌在 generated/tokens.ts。规则同 design-system/lithospectra/CLAUDE-STYLE.md：
// 暖象牙底 + 墨色主色 + 单一陶土强调，硬边、无阴影；画布两套主题下都是深色。

import { useEffect, useState } from 'react'
import { theme as antdTheme } from 'antd'
import type { ThemeConfig } from 'antd'
import { TOKENS } from './generated/tokens'
import type { Palette, PaletteName } from './generated/tokens'
import { ICON_PATHS } from './generated/icons'
import type { IconName } from './generated/icons'

export type ThemeMode = 'system' | 'light' | 'dark'
export const MODE_LABELS: Record<ThemeMode, string> = { system: '跟随系统', light: '浅色', dark: '深色' }

const MODE_KEY = 'lithospectra.themeMode'

function readMode(): ThemeMode {
  try {
    const v = localStorage.getItem(MODE_KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch {
    /* 存储不可用时按跟随系统 */
  }
  return 'system'
}

/** 外观模式与实际生效的调色板。模式是本机偏好，记在 localStorage。 */
export function useThemeMode(): [ThemeMode, (m: ThemeMode) => void, PaletteName] {
  const [mode, setModeState] = useState<ThemeMode>(readMode)
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  const [sysDark, setSysDark] = useState(mq.matches)
  useEffect(() => {
    const on = (e: MediaQueryListEvent) => setSysDark(e.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [mq])
  const setMode = (m: ThemeMode) => {
    setModeState(m)
    try {
      localStorage.setItem(MODE_KEY, m)
    } catch {
      /* 忽略 */
    }
  }
  const name: PaletteName = mode === 'system' ? (sysDark ? 'dark' : 'light') : mode
  return [mode, setMode, name]
}

export function palette(name: PaletteName): Palette {
  return TOKENS.palettes[name] as Palette
}

/** 把调色板写成 CSS 变量（--bg、--surface…），样式表只引用变量。 */
export function applyCssVars(name: PaletteName): void {
  const p = palette(name)
  const root = document.documentElement
  for (const [k, v] of Object.entries(p)) root.style.setProperty('--' + k.replace(/_/g, '-'), v)
  // 系统自带的控件（录音、视频的播放条，滚动条）跟着深浅色走，深色下不再是一块白
  root.style.colorScheme = name
  root.style.setProperty('--font-ui', TOKENS.uiFont.map((f) => `"${f}"`).join(', ') + ', sans-serif')
  root.style.setProperty('--font-mono', TOKENS.monoFont.map((f) => `"${f}"`).join(', ') + ', monospace')
  root.style.setProperty('--font-serif', TOKENS.serifFont.map((f) => `"${f}"`).join(', ') + ', serif')
  root.dataset.theme = name
}

/** Ant Design 的令牌映射到同一套语义色。 */
export function antdThemeConfig(name: PaletteName): ThemeConfig {
  const p = palette(name)
  const dark = name === 'dark'
  return {
    algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      colorPrimary: p.primary,
      colorInfo: p.primary,
      colorLink: p.text,
      colorSuccess: p.success,
      colorWarning: p.warning,
      colorError: p.danger,
      colorBgBase: p.surface,
      colorBgLayout: p.bg,
      colorBgContainer: p.surface,
      colorBgElevated: p.elevated,
      colorBorder: p.border_strong,
      colorBorderSecondary: p.border,
      colorSplit: p.border,
      colorText: p.text,
      colorTextBase: p.text,
      colorTextSecondary: p.text_muted,
      colorTextTertiary: p.text_muted,
      colorTextPlaceholder: p.text_placeholder,
      colorTextDisabled: p.text_disabled,
      colorFillAlter: p.surface_alt,
      controlItemBgHover: p.hover,
      controlItemBgActive: p.selection,
      controlItemBgActiveHover: p.primary_soft,
      controlOutline: 'transparent',
      fontFamily: TOKENS.uiFont.map((f) => `"${f}"`).join(', ') + ', sans-serif',
      fontFamilyCode: TOKENS.monoFont.map((f) => `"${f}"`).join(', ') + ', monospace',
      fontSize: TOKENS.fontSize.body,
      fontSizeSM: TOKENS.fontSize.caption,
      // 柔和的圆角与投影：控件 6px、弹层 10px；弹出的菜单、弹窗、提示浮在界面之上，
      // 靠一层淡投影分出层次，而不是一圈深色描边
      borderRadius: 6,
      borderRadiusSM: 5,
      borderRadiusLG: 10,
      borderRadiusXS: 4,
      controlHeight: TOKENS.controlHeight,
      controlHeightSM: 24,
      boxShadow: dark ? '0 6px 20px -6px rgba(0,0,0,0.6)' : '0 6px 20px -8px rgba(20,20,19,0.22)',
      boxShadowSecondary: dark
        ? '0 12px 32px -8px rgba(0,0,0,0.65), 0 0 0 1px rgba(255,255,255,0.04)'
        : '0 12px 32px -10px rgba(20,20,19,0.26), 0 2px 6px -2px rgba(20,20,19,0.08)',
      boxShadowTertiary: dark ? '0 1px 2px rgba(0,0,0,0.4)' : '0 1px 2px rgba(20,20,19,0.06)',
      motion: false,
      wireframe: false,
    },
    components: {
      Tree: { nodeSelectedBg: p.selection, nodeHoverBg: p.hover, titleHeight: 26 },
      Checkbox: { colorPrimary: p.primary, colorPrimaryHover: p.primary },
      Modal: { contentBg: p.elevated, headerBg: p.elevated, titleFontSize: TOKENS.fontSize.title, borderRadiusLG: 12 },
      Segmented: { itemSelectedBg: p.elevated, trackBg: p.surface_alt },
      Switch: { colorPrimary: p.primary, colorPrimaryHover: p.primary },
      Slider: { trackBg: p.text, trackHoverBg: p.text, handleColor: p.text, handleActiveColor: p.text, dotActiveBorderColor: p.text },
      Dropdown: { controlItemBgHover: p.primary_soft },
      Tooltip: { colorBgSpotlight: p.elevated, colorTextLightSolid: p.text },
      Message: { contentBg: p.elevated },
    },
  }
}

/** 描边图标：24 网格、1.6px、圆头。颜色跟随 currentColor。 */
export function Icon({ name, size = TOKENS.iconSize.md }: { name: IconName; size?: number }) {
  const parts = (ICON_PATHS[name] as string).split(/(?=M)/).filter((s) => s.trim())
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={TOKENS.iconStroke} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      {parts.map((d, i) => <path key={i} d={d} />)}
    </svg>
  )
}

/** 下拉箭头：16 网格、1.6px。 */
export function Chevron({ size = 12 }: { size?: number }) {
  return (
    <svg className="icon chevron" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth={TOKENS.iconStroke} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M4 6.5 8 10.5 12 6.5" />
    </svg>
  )
}
