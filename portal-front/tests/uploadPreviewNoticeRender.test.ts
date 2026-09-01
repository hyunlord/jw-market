import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import UploadPreviewNotice from '../src/components/main/UploadPreviewNotice.ts'
import type { UploadProgressFile } from '../src/utils/uploadProgress.ts'

function render(file: UploadProgressFile): string {
  return renderToStaticMarkup(createElement(UploadPreviewNotice, { file }))
}

test('renders the backend preview notice verbatim while the preview is queryable', () => {
  const message = '앞 20/270페이지는 지금 질문할 수 있습니다.'
  const html = render({
    fileName: 'long.pdf',
    state: 'preprocessing',
    message,
    queryReady: true,
    indexedPages: 20,
    totalPages: 270,
  })

  assert.match(html, /upload-progress-preview-notice/)
  assert.match(html, new RegExp(message))
})

test('does not leave the preview notice behind after the file becomes ready', () => {
  const html = render({
    fileName: 'long.pdf',
    state: 'ready',
    message: '앞 20/270페이지는 지금 질문할 수 있습니다.',
    queryReady: true,
    indexedPages: 20,
    totalPages: 270,
  })

  assert.equal(html, '')
})

test('does not invent a preview notice for files without preview fields', () => {
  const html = render({
    fileName: 'slides.pptx',
    state: 'preprocessing',
    message: null,
    queryReady: false,
    indexedPages: null,
    totalPages: null,
  })

  assert.equal(html, '')
})
