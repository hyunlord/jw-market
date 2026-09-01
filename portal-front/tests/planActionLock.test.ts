import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test, { after } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

interface ChatMessageAIProps {
  id: string
  planContent: string
  isGenerating: boolean
  isPlan?: boolean
  planActionsDisabled?: boolean
}

interface PlanActionLockModule {
  createPlanActionLock: () => {
    isLocked: () => boolean
  }
  runWithPlanActionLock: (
    lock: ReturnType<PlanActionLockModule['createPlanActionLock']>,
    action: () => Promise<void>,
  ) => Promise<boolean>
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24689 } },
  appType: 'custom',
})

const { default: ChatMessageAI } = await vite.ssrLoadModule(
  '/src/components/main/ChatMessageAI.tsx',
) as { default: ComponentType<ChatMessageAIProps> }

async function loadPlanActionLock(): Promise<PlanActionLockModule> {
  try {
    return await vite.ssrLoadModule('/src/utils/planActionLock.ts') as PlanActionLockModule
  } catch (error) {
    assert.fail(`plan action lock module must exist: ${String(error)}`)
  }
}

after(async () => vite.close())

test('locked plan actions render three disabled buttons and a persistent reason', () => {
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'locked-plan',
    planContent: '수집 계획을 확인해 주세요.',
    isGenerating: false,
    isPlan: true,
    planActionsDisabled: true,
  }))

  assert.match(markup, /<button[^>]+class="btn-cancel"[^>]+disabled=""/)
  assert.match(markup, /<button[^>]+class="btn-edit"[^>]+disabled=""/)
  assert.match(markup, /<button[^>]+class="btn-run"[^>]+disabled=""/)
  assert.match(markup, /role="status"/)
  assert.match(markup, /실행 중입니다/)
})

test('an unlocked plan renders enabled actions without the running reason', () => {
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'released-plan',
    planContent: '수집 계획을 확인해 주세요.',
    isGenerating: false,
    isPlan: true,
    planActionsDisabled: false,
  }))

  assert.match(markup, /<button[^>]+class="btn-cancel"/)
  assert.match(markup, /<button[^>]+class="btn-edit"/)
  assert.match(markup, /<button[^>]+class="btn-run"/)
  assert.doesNotMatch(markup, /class="btn-(?:cancel|edit|run)"[^>]+disabled=""/)
  assert.doesNotMatch(markup, /실행 중입니다/)
})

test('ten same-tick attempts execute the action once', async () => {
  const { createPlanActionLock, runWithPlanActionLock } = await loadPlanActionLock()
  const lock = createPlanActionLock()
  let calls = 0
  let releaseAction: (() => void) | undefined
  const pending = new Promise<void>(resolve => { releaseAction = resolve })

  const attempts = Array.from({ length: 10 }, () => runWithPlanActionLock(lock, async () => {
    calls += 1
    await pending
  }))

  assert.equal(calls, 1)
  assert.equal(lock.isLocked(), true)
  releaseAction?.()
  const outcomes = await Promise.all(attempts)
  assert.equal(outcomes.filter(Boolean).length, 1)
  assert.equal(lock.isLocked(), false)
})

test('completion, failure, timeout, and disconnect all release the lock', async () => {
  const { createPlanActionLock, runWithPlanActionLock } = await loadPlanActionLock()
  const terminalCases = [
    { name: 'completion', action: async () => undefined },
    { name: 'failure', action: async () => { throw new Error('request failed') } },
    { name: 'timeout', action: async () => { throw new DOMException('timed out', 'AbortError') } },
    { name: 'disconnect', action: async () => { throw new TypeError('network disconnected') } },
  ]

  for (const terminalCase of terminalCases) {
    const lock = createPlanActionLock()
    await runWithPlanActionLock(lock, terminalCase.action).catch(() => undefined)
    assert.equal(lock.isLocked(), false, `${terminalCase.name} must release the lock`)
  }
})

test('a normal action can run again after the previous action completes', async () => {
  const { createPlanActionLock, runWithPlanActionLock } = await loadPlanActionLock()
  const lock = createPlanActionLock()
  let calls = 0

  assert.equal(await runWithPlanActionLock(lock, async () => { calls += 1 }), true)
  assert.equal(await runWithPlanActionLock(lock, async () => { calls += 1 }), true)
  assert.equal(calls, 2)
})

test('cancel, proceed, and modify enter through the same synchronous lock', async () => {
  const source = await readFile(new URL('../src/pages/StreamPage.tsx', import.meta.url), 'utf8')

  assert.match(source, /handlePlanAbort[\s\S]*runWithPlanActionLock\(planActionLockRef\.current/)
  assert.match(source, /handlePlanAction[\s\S]*runWithPlanActionLock\(planActionLockRef\.current/)
  assert.match(source, /planActionsDisabled=\{isGenerating\}/)
})
