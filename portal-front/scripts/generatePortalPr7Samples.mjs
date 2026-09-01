import { mkdir, writeFile } from 'node:fs/promises'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const output = process.argv[2]
if (!output) throw new Error('output directory is required')
await mkdir(output, { recursive: true })

const rows = Array.from({ length: 20 }, (_, index) => [`2026-${String(index + 1).padStart(2, '0')}`, `${1000 + index * 25}`, `${10 + index / 10}%`])
const mdRows = rows.map(row => `| ${row.join(' | ')} |`).join('\n')
const markdown = `# Portal PR7 export sample\n\n[Source: Internal data mart]\n\n## Sales table\n\nSource: Internal data mart - all 20 rows\n\n| Period | Sales | Share |\n| --- | --- | --- |\n${mdRows}\n\n## Sales chart\n\n> Chart is exported as a data table; no data was silently omitted.\n\n| Period | Series A |\n| --- | --- |\n${rows.map(row => `| ${row[0]} | ${row[1]} |`).join('\n')}\n`
await writeFile(`${output}/portal-pr7-export-sample.md`, markdown)

const htmlRows = rows.slice(0, 3).map((row, index) => `<div class="trace-output-array-item"><span>record ${index + 1}</span><dl class="trace-output-object trace-output-depth-1"><div><dt>Period</dt><dd>${row[0]}</dd></div><div><dt>Metrics</dt><dd><dl class="trace-output-object trace-output-depth-2"><div><dt>Sales</dt><dd>${row[1]}</dd></div><div><dt>Share</dt><dd>${row[2]}</dd></div></dl></dd></div></dl></div>`).join('')
await writeFile(`${output}/portal-pr7-tree-snapshot.html`, `<!doctype html><meta charset="utf-8"><title>PR7 tree snapshot</title><style>body{font:14px system-ui;max-width:860px;margin:32px auto}.trace-output-array{display:grid;gap:14px}.trace-output-array-item{display:grid;grid-template-columns:100px 1fr;border-top:1px solid #ddd;padding-top:10px}.trace-output-object{display:grid;gap:8px;margin:0}.trace-output-object>div{display:grid;grid-template-columns:130px 1fr;border-left:2px solid #dbe4ef;padding-left:12px}dt{font-weight:700}dd{margin:0}</style><h1>Restored cascading detail tree</h1><div class="trace-output-array">${htmlRows}</div>`)

const python = process.env.PORTAL_PR7_PYTHON ?? 'python3'
const pdfScript = fileURLToPath(new URL('./generatePortalPr7Pdf.py', import.meta.url))
const converted = spawnSync(python, [pdfScript, `${output}/portal-pr7-export-sample.pdf`], {
  input: JSON.stringify(rows),
  encoding: 'utf8',
})
if (converted.status !== 0) {
  throw new Error(`PDF sample generation failed: ${converted.stderr}`)
}
console.log(JSON.stringify({ markdownRows: 20, pdfRows: 20, chartFallbackRows: 20, files: 3 }))
