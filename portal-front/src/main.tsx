import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/reset.css'
import './styles/base.css'
import './styles/common.css'
import App from './App.tsx'
import { loadRuntimeConfig } from './config/runtimeConfig.ts'

async function start(): Promise<void> {
  await loadRuntimeConfig()
  const rootElement = document.getElementById('root')
  if (!rootElement) throw new Error('Portal root element is missing')
  createRoot(rootElement).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

void start().catch(error => {
  console.error('[runtime-config] Portal startup blocked:', error)
  const rootElement = document.getElementById('root')
  if (rootElement) {
    rootElement.setAttribute('role', 'alert')
    rootElement.textContent = 'Portal configuration could not be loaded.'
  }
})
