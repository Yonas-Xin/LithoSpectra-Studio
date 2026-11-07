// 起名对话框：类别、分组、工区、样本名都从这里输入，带地质符号面板与上下标。
// 点符号就插在光标处；上下标两种用法与 Word 一致——
// 选中文字按「下标 / 上标」就地转换（再按一次转回），不选则切换下标 / 上标输入
// 状态；⌘= 下标、⇧⌘= 上标；最近用过的符号排在最上面。

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { Button, Input, Modal, Tabs } from 'antd'
import type { InputRef } from 'antd'
import { kbd } from './platform'

type Symbol = [string, string, string] // 插入的文字, 按钮上的字, 提示
interface SymbolData {
  sub: Record<string, string>
  sup: Record<string, string>
  wide: Record<string, string>
  ages: { name: string; symbols: Symbol[] }[]
  age_pieces: Symbol[]
  tabs: { title: string; symbols: Symbol[] }[]
  recent: string[]
}
type Mode = 'normal' | 'sub' | 'sup'
type Ask = (title: string, label: string, initial?: string, allowEmpty?: boolean) => Promise<string | null>

const COLUMNS: Record<string, number> = { 岩石代号: 3, 第四系成因: 3, 希腊字母: 10, '化学 · 符号': 5 }
const RECENT_MAX = 14

const Ctx = createContext<Ask | null>(null)
export function useNameDialog(): Ask {
  const ask = useContext(Ctx)
  if (!ask) throw new Error('NameDialogProvider missing')
  return ask
}

/** 转换规则与 samplelib.geosymbols.convert_char 一致：先折回普通字符再查表。 */
function makeConverter(d: SymbolData) {
  const plain: Record<string, string> = {}
  for (const table of [d.sub, d.sup]) for (const [p, f] of Object.entries(table)) plain[f] = p
  const convertChar = (ch: string, mode: Mode): [string, boolean] => {
    const base = plain[ch] ?? d.wide[ch] ?? ch
    if (mode === 'normal') return [base, true]
    const table = mode === 'sub' ? d.sub : d.sup
    if (table[base]) return [table[base], true]
    if (/\s/.test(base)) return [base, true]
    return [ch, false]
  }
  const convert = (text: string, mode: Mode): [string, string] => {
    let out = ''
    let missed = ''
    for (const ch of text) {
      const [n, ok] = convertChar(ch, mode)
      out += n
      if (!ok && !missed.includes(ch)) missed += ch
    }
    return [out, missed]
  }
  const scriptOf = (text: string): Mode => {
    const kinds = new Set<Mode>()
    const subs = new Set(Object.values(d.sub))
    const sups = new Set(Object.values(d.sup))
    for (const ch of text) {
      if (/\s/.test(ch)) continue
      kinds.add(subs.has(ch) ? 'sub' : sups.has(ch) ? 'sup' : 'normal')
    }
    return kinds.size === 1 ? [...kinds][0] : 'normal'
  }
  return { convertChar, convert, scriptOf }
}

/** 转不成上下标时值不值得提一句：字母、数字、希腊字母才提，汉字不提。 */
const worthWarning = (ch: string) => (/^[\x21-\x7e]$/.test(ch)) || (ch >= 'Ͱ' && ch <= 'Ͽ')

interface Req {
  title: string
  label: string
  initial: string
  allowEmpty: boolean
  resolve: (v: string | null) => void
}

export function NameDialogProvider({ children }: { children: React.ReactNode }) {
  const [req, setReq] = useState<Req | null>(null)
  const [data, setData] = useState<SymbolData | null>(null)
  const [value, setValue] = useState('')
  const [mode, setMode] = useState<Mode>('normal')
  const [note, setNote] = useState('')
  const [recent, setRecent] = useState<string[]>([])
  const [tab, setTab] = useState('ages')
  const inputRef = useRef<InputRef>(null)
  const sel = useRef<[number, number]>([0, 0])

  const ask: Ask = useCallback((title, label, initial = '', allowEmpty = false) =>
    new Promise<string | null>((resolve) => {
      setReq({ title, label, initial, allowEmpty, resolve })
      setValue(initial)
      setMode('normal')
      setNote('')
      sel.current = [initial.length, initial.length]
    }), [])

  useEffect(() => {
    if (!req || data) return
    fetch('/api/geosymbols').then((r) => r.json()).then((d: SymbolData) => {
      setData(d)
      setRecent(d.recent)
    }).catch(() => undefined)
  }, [req, data])

  useEffect(() => {
    if (req) setTimeout(() => inputRef.current?.focus({ cursor: 'end' }), 50)
  }, [req])

  const conv = data ? makeConverter(data) : null
  const el = () => inputRef.current?.input ?? null
  const remember = () => {
    const e = el()
    if (e) sel.current = [e.selectionStart ?? value.length, e.selectionEnd ?? value.length]
  }
  const replaceSelection = (text: string) => {
    const [a, b] = sel.current
    const next = value.slice(0, a) + text + value.slice(b)
    setValue(next)
    const pos = a + text.length
    sel.current = [pos, pos]
    setTimeout(() => {
      const e = el()
      if (e) {
        e.focus()
        e.setSelectionRange(pos, pos)
      }
    }, 0)
  }

  const insertSymbol = (text: string) => {
    replaceSelection(text)
    const next = [text, ...recent.filter((x) => x !== text)].slice(0, RECENT_MAX)
    setRecent(next)
    fetch('/api/geosymbols/recent', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: next }),
    }).catch(() => undefined)
  }

  /** 选中文字：就地转换（已经是这种就转回）；没选：切换输入状态。 */
  const applyScript = (target: Mode) => {
    if (!conv) return
    remember()
    const [a, b] = sel.current
    if (a !== b) {
      const chosen = value.slice(a, b)
      const to = target !== 'normal' && conv.scriptOf(chosen) === target ? 'normal' : target
      const [out, missed] = conv.convert(chosen, to)
      setValue(value.slice(0, a) + out + value.slice(b))
      sel.current = [a, a + out.length]
      setTimeout(() => el()?.setSelectionRange(a, a + out.length), 0)
      const bad = [...missed].filter(worthWarning).join('')
      setNote(bad ? `「${bad}」没有对应的${to === 'sub' ? '下标' : '上标'}字符，已保留原字符。` : '')
      return
    }
    setMode(mode === target ? 'normal' : target)
    setNote('')
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    const mod = e.metaKey || e.ctrlKey
    if (mod && (e.key === '=' || e.key === '+')) {
      e.preventDefault()
      applyScript(e.shiftKey || e.key === '+' ? 'sup' : 'sub')
      return
    }
    if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
      e.preventDefault()
      finish(true)
    }
  }
  // 下标 / 上标输入状态下，打出来的字直接换成下标 / 上标（含输入法提交上来的）
  const onBeforeInput = (e: React.FormEvent<HTMLInputElement>) => {
    const ne = e.nativeEvent as InputEvent
    if (mode === 'normal' || !conv || !ne.data || ne.isComposing) return
    e.preventDefault()
    remember()
    const [out, missed] = conv.convert(ne.data, mode)
    replaceSelection(out)
    const bad = [...missed].filter(worthWarning).join('')
    if (bad) setNote(`「${bad}」没有对应的${mode === 'sub' ? '下标' : '上标'}字符，已按原字符输入。`)
  }
  const onCompositionEnd = (e: React.CompositionEvent<HTMLInputElement>) => {
    if (mode === 'normal' || !conv || !e.data) return
    const end = el()?.selectionStart ?? value.length
    const start = end - e.data.length
    const [out] = conv.convert(e.data, mode)
    setTimeout(() => setValue((v) => v.slice(0, start) + out + v.slice(end)), 0)
  }

  const finish = (ok: boolean) => {
    if (!req) return
    const v = value.trim()
    if (ok && !v && !req.allowEmpty) return
    req.resolve(ok ? v : null)
    setReq(null)
  }

  // 岩石代号、第四系成因这类按钮上写的是中文名，插入的是符号：两样都显示，符号在前
  const btn = ([text, caption, tip]: Symbol) => {
    const named = caption !== text && /[\u4e00-\u9fff]/.test(caption)
    return (
      <button key={text + caption} className={'sym-btn' + (named ? ' named' : '')} title={tip !== caption ? tip : `插入 ${text}`}
        onMouseDown={(e) => { e.preventDefault(); remember() }} onClick={() => insertSymbol(text)}>
        {named ? <><span className="sym-code">{text}</span><span className="sym-name">{caption}</span></> : caption}
      </button>
    )
  }

  const modeHint = mode === 'sub' ? '下标输入：数字、+ − 与部分小写字母自动转为下标'
    : mode === 'sup' ? '上标输入：数字、+ − 与小写字母自动转为上标' : '选中文字后点击下标或上标进行转换'

  return (
    <Ctx.Provider value={ask}>
      {children}
      <Modal open={!!req} title={req?.title} width={640} onCancel={() => finish(false)} destroyOnHidden
        footer={[
          <Button key="c" onClick={() => finish(false)}>取消</Button>,
          <Button key="ok" type="primary" disabled={!req?.allowEmpty && !value.trim()} onClick={() => finish(true)}>确定</Button>,
        ]}>
        <div className="dlg name-dlg">
          {req?.label ? <div className="pre">{req.label}</div> : null}
          <Input ref={inputRef} value={value} onChange={(e) => setValue(e.target.value)}
            onKeyDown={onKeyDown} onBeforeInput={onBeforeInput} onCompositionEnd={onCompositionEnd}
            onSelect={remember} onBlur={remember} />
          <div className="fmt-row">
            {([['sub', 'X₂  下标', kbd('下标（⌘=）\n选中文字时转为下标，再按一次恢复；未选中时切换下标输入')],
              ['sup', 'X²  上标', kbd('上标（⇧⌘=）\n选中文字时转为上标，再按一次恢复；未选中时切换上标输入')],
              ['normal', 'Aa  正常', '恢复正常输入；选中文字时转为普通字符']] as [Mode, string, string][]).map(([m, t, tip]) => (
              <button key={m} className={'fmt-btn' + (mode === m && m !== 'normal' ? ' on' : '')} title={tip}
                onMouseDown={(e) => { e.preventDefault(); remember() }} onClick={() => applyScript(m)}>{t}</button>
            ))}
            <span className="caption">{modeHint}</span>
          </div>
          {recent.length ? (
            <div className="sym-recent">
              <span className="caption">最近使用</span>
              {recent.map((r) => btn([r, r, `插入 ${r}`]))}
            </div>
          ) : null}
          {data ? (
            <Tabs size="small" activeKey={tab} onChange={setTab}
              items={[
                {
                  key: 'ages', label: '地质年代',
                  children: (
                    <div className="sym-scroll">
                      <div className="age-row"><span className="caption age-name">组合范围</span>{data.age_pieces.map(btn)}</div>
                      {data.ages.map((row) => (
                        <div key={row.name} className="age-row"><span className="caption age-name">{row.name}</span>{row.symbols.map(btn)}</div>
                      ))}
                    </div>
                  ),
                },
                ...data.tabs.map((t) => ({
                  key: t.title, label: t.title,
                  children: (
                    <div className="sym-scroll">
                      <div className="sym-grid" style={{ gridTemplateColumns: `repeat(${COLUMNS[t.title] ?? 6}, minmax(0, 1fr))` }}>
                        {t.symbols.map(btn)}
                      </div>
                    </div>
                  ),
                })),
              ]} />
          ) : null}
          <div className="caption">{note}</div>
        </div>
      </Modal>
    </Ctx.Provider>
  )
}
