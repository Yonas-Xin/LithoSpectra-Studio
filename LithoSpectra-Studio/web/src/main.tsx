import { StrictMode, useLayoutEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import { NameDialogProvider } from './NameDialog'
import { TaskProvider } from './tasks'
import { antdThemeConfig, applyCssVars, palette, useThemeMode } from './theme'
import './index.css'

function Root() {
  const [mode, setMode, name] = useThemeMode()
  useLayoutEffect(() => applyCssVars(name), [name])
  return (
    <ConfigProvider locale={zhCN} theme={antdThemeConfig(name)} button={{ autoInsertSpace: false }}>
      <AntApp className="app-root">
        <TaskProvider>
          <NameDialogProvider>
            <App palette={palette(name)} themeMode={mode} onThemeMode={setMode} />
          </NameDialogProvider>
        </TaskProvider>
      </AntApp>
    </ConfigProvider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
