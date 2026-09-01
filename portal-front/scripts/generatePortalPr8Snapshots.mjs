import { readFile, writeFile } from 'node:fs/promises'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const [outputDirectory, clinicalSse, patentJson, fileJson] = process.argv.slice(2)
if (!outputDirectory || !clinicalSse || !patentJson || !fileJson) {
  throw new Error('usage: generatePortalPr8Snapshots.mjs OUTPUT_DIR CLINICAL_SSE PATENT_JSON FILE_JSON')
}

function parseClinicalRecords(raw) {
  let selected = []
  for (const line of raw.split(/\r?\n/)) {
    if (!line.startsWith('data:')) continue
    try {
      const event = JSON.parse(line.slice(5).trim())
      const members = event?.evidence_group?.members
      if (Array.isArray(members) && members.length > selected.length) selected = members
    } catch {
      // Non-JSON SSE lines are transport metadata, not record payloads.
    }
  }
  if (selected.length === 0) throw new Error('clinical evidence records were not found in the saved SSE')
  return selected
}

function escapeHtml(value) {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
}

const vite = await createServer({ server: { middlewareMode: true }, appType: 'custom', logLevel: 'error' })
try {
  const { StructuredValueTree } = await vite.ssrLoadModule('/src/components/main/StructuredValueTree.tsx')
  const css = (await readFile(new URL('../src/styles/common.css', import.meta.url), 'utf8'))
    .split(/\r?\n/)
    .filter(line => /^\s*\.(?:trace-|market-detail-field)/.test(line))
    .join('\n')
  const inputs = [
    ['clinical', '임상 도구 상세', parseClinicalRecords(await readFile(clinicalSse, 'utf8'))],
    ['patent', '특허 도구 상세', JSON.parse(await readFile(patentJson, 'utf8'))],
    ['file', '파일 도구 상세', JSON.parse(await readFile(fileJson, 'utf8'))],
  ]
  const script = `<script>(()=>{document.addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;const target=button.dataset.recordTarget;if(target){const record=document.getElementById(target);if(record){record.open=true;record.scrollIntoView({block:'start'});}return;}const action=button.dataset.treeAction;if(!action)return;const tree=button.closest('.trace-output-array');if(!tree)return;tree.querySelectorAll(':scope > .trace-record-list > .trace-record-block').forEach(record=>{record.open=action==='expand';});});})();<\/script>`
  for (const [slug, title, value] of inputs) {
    const body = renderToStaticMarkup(createElement(StructuredValueTree, {
      value,
      labelFor: key => ({ primary: key, source: key }),
      showEveryArrayItem: true,
    }))
    const html = `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escapeHtml(title)}</title><style>body{max-width:1180px;margin:24px auto;padding:0 18px;color:#171A1F;background:#F4F6F8;font:14px/1.5 system-ui,sans-serif}main{padding:20px;background:#fff;border:1px solid #DDE1E7;border-radius:6px}h1{margin:0 0 6px;font-size:22px;letter-spacing:0}p{margin:0 0 18px;color:#555B66}${css}</style></head><body><main><h1>${escapeHtml(title)}</h1><p>실제 StructuredValueTree 정적 렌더 · 저장된 감사 원문 · 외부 의존 0</p><div class="trace-output-view">${body}</div></main>${script}</body></html>`
    await writeFile(`${outputDirectory}/${slug}.html`, html)
    console.log(JSON.stringify({ slug, bytes: Buffer.byteLength(html), records: Array.isArray(value) ? value.length : undefined }))
  }
} finally {
  await vite.close()
}
