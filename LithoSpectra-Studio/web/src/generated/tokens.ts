// 界面的设计令牌（配色、字号、间距、圆角），深浅两套主题共用这一份来源。
/* eslint-disable */

export const TOKENS = {
  "palettes": {
    "light": {
      "bg": "#F0EEE6",
      "surface": "#FAF9F6",
      "surface_alt": "#E8E5DB",
      "elevated": "#FDFCFA",
      "border": "#DDD8CC",
      "border_strong": "#8A8477",
      "text": "#141413",
      "text_muted": "#5C574C",
      "text_placeholder": "#6B6558",
      "text_disabled": "#A9A296",
      "primary": "#141413",
      "on_primary": "#FAF9F6",
      "primary_soft": "#E0DACC",
      "accent": "#9A4A25",
      "on_accent": "#FAF9F6",
      "danger": "#B42318",
      "on_danger": "#FAF9F6",
      "success": "#15803D",
      "warning": "#A85B08",
      "focus": "#141413",
      "hover": "#E6E2D7",
      "selection": "#E0DACC",
      "canvas_bg": "#1A1915",
      "canvas_grid": "#2A2822",
      "canvas_text": "#D6D1C4",
      "canvas_select": "#F5F1E6"
    },
    "dark": {
      "bg": "#141413",
      "surface": "#1C1B19",
      "surface_alt": "#100F0E",
      "elevated": "#26241F",
      "border": "#34322C",
      "border_strong": "#6E695E",
      "text": "#F0EEE6",
      "text_muted": "#B8B2A4",
      "text_placeholder": "#A29B8C",
      "text_disabled": "#5E594F",
      "primary": "#F0EEE6",
      "on_primary": "#141413",
      "primary_soft": "#332F27",
      "accent": "#E08A63",
      "on_accent": "#141413",
      "danger": "#F0806C",
      "on_danger": "#141413",
      "success": "#5BC17F",
      "warning": "#E0A458",
      "focus": "#E08A63",
      "hover": "#26241F",
      "selection": "#332F27",
      "canvas_bg": "#0E0D0C",
      "canvas_grid": "#26241F",
      "canvas_text": "#C9C3B5",
      "canvas_select": "#F5F1E6"
    }
  },
  "fontSize": {
    "caption": 12,
    "body": 13,
    "subtitle": 15,
    "title": 18,
    "display": 22
  },
  "space": {
    "xs": 4,
    "sm": 8,
    "md": 12,
    "lg": 16,
    "xl": 24
  },
  "radius": {
    "sm": 0,
    "md": 0,
    "lg": 0,
    "cta": 8
  },
  "iconSize": {
    "sm": 14,
    "md": 16,
    "lg": 20
  },
  "iconStroke": 1.6,
  "controlHeight": 28,
  "controlHeightLg": 32,
  "uiFont": [
    "PingFang SC",
    "Microsoft YaHei",
    "Hiragino Sans GB",
    "Helvetica Neue",
    "Segoe UI",
    "Arial"
  ],
  "monoFont": [
    "Menlo",
    "SF Mono",
    "Consolas",
    "DejaVu Sans Mono",
    "Courier New"
  ],
  "serifFont": [
    "Songti SC",
    "Source Han Serif SC",
    "Noto Serif CJK SC",
    "SimSun",
    "Georgia",
    "Times New Roman"
  ]
} as const

export type PaletteName = keyof typeof TOKENS.palettes
export type Palette = (typeof TOKENS.palettes)['light']
