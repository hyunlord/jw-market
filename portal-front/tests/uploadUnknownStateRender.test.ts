import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import test, { after } from 'node:test'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24690 } },
  appType: 'custom',
})
const { default: UploadProgressList } = await vite.ssrLoadModule('/src/components/main/UploadProgressList.tsx') as {
  default: (props: {
    progress: {
      phase: 'processing'
      uploadId: string
      state: 'unknown'
      files: { fileName: string; state: 'unknown' }[]
      startedAtMs: number
    }
    onRetryStatus: () => void
    onRetryUpload: () => void
  }) => ReturnType<typeof createElement>
}
after(async () => vite.close())

test('F4 renders canonical unknown states without exposing backend values', () => {
  const markup = renderToStaticMarkup(createElement(UploadProgressList, {
    progress: {
      phase: 'processing',
      uploadId: 'upload-unknown',
      state: 'unknown',
      files: [{ fileName: 'future.pdf', state: 'unknown' }],
      startedAtMs: Date.now(),
    },
    onRetryStatus: () => undefined,
    onRetryUpload: () => undefined,
  }))

  assert.match(markup, /상태 미상/)
  assert.match(markup, /future\.pdf/)
  assert.doesNotMatch(markup, /future_internal_state|future_file_state/)
})
