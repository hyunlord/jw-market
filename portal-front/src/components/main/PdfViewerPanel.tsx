import { useEffect, useRef, useState } from 'react'
import * as pdfjsLib from 'pdfjs-dist'
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist'
import PdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?worker'
import { PDFDocument, rgb } from 'pdf-lib'
import type { ChunkBbox } from '../../utils/planSignal'

interface Props {
  url: string
  fileName: string
  bboxes?: ChunkBbox[]
  initialPage?: number
  onClose: () => void
}

const HIGHLIGHT_COLORS: [number, number, number][] = [
  [0, 169, 229],   // 파랑 (기존 기본색)
  [245, 158, 11],  // 주황
  [16, 185, 129],  // 초록
  [139, 92, 246],  // 보라
  [239, 68, 68],   // 빨강
  [236, 72, 153],  // 분홍
  [20, 184, 166],  // 청록
  [249, 115, 22],  // 진주황
  [99, 102, 241],  // 인디고
  [132, 204, 22],  // 라임
  [217, 70, 239],  // 자홍(마젠타)
  [6, 182, 212],   // 시안
  [234, 179, 8],   // 노랑
  [168, 85, 247],  // 자주보라
  [34, 197, 94],   // 밝은초록
  [244, 63, 94],   // 로즈
  [59, 130, 246],  // 하늘파랑
  [190, 24, 93],   // 진분홍
  [13, 148, 136],  // 진청록
  [124, 58, 237],  // 진보라
]

function highlightColor(chunkIndex: number): [number, number, number] {
  return HIGHLIGHT_COLORS[Math.abs(chunkIndex) % HIGHLIGHT_COLORS.length]
}

export default function PdfViewerPanel({ url, fileName, bboxes, initialPage, onClose }: Props) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null)
  const [numPages, setNumPages] = useState(0)
  const [error, setError] = useState(false)
  const [pageWidth, setPageWidth] = useState(0)
  const [pageHeights, setPageHeights] = useState<number[]>([])
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    const worker = pdfjsLib.PDFWorker.create({ port: new PdfWorker() })
    const task = pdfjsLib.getDocument({ url, worker, disableFontFace: true })
    task.promise.then(doc => {
      if (cancelled) return
      setPdf(doc)
      setNumPages(doc.numPages)
    }).catch(() => { if (!cancelled) setError(true) })
    return () => {
      cancelled = true
      task.destroy().catch(() => {}).finally(() => worker.destroy())
    }
  }, [url])

  const handleDownload = async () => {
    try {
      const res = await fetch(url)
      const srcBytes = await res.arrayBuffer()
      const doc = await PDFDocument.load(srcBytes)
      const pages = doc.getPages()
      for (const bb of bboxes ?? []) {
        const page = pages[bb.page - 1]
        if (!page) continue
        const { width: W, height: H } = page.getSize()
        const x = bb.l * W
        const boxW = (bb.r - bb.l) * W
        const isTopLeft = bb.coordOrigin === 'TOPLEFT'
        const y = (isTopLeft ? 1 - bb.b : bb.b) * H
        const boxH = Math.abs((isTopLeft ? bb.b - bb.t : bb.t - bb.b)) * H
        const [cr, cg, cb] = highlightColor(bb.chunkIndex)
        page.drawRectangle({
          x, y, width: boxW, height: boxH,
          color: rgb(cr / 255, cg / 255, cb / 255),
          opacity: 0.12,
          borderColor: rgb(cr / 255, cg / 255, cb / 255),
          borderWidth: 1.6,
          borderOpacity: 1,
        })
      }

      const outBytes = await doc.save()
      const blob = new Blob([outBytes as BlobPart], { type: 'application/pdf' })
      const blobUrl = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = blobUrl
      a.download = fileName
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(blobUrl)
    } catch {
      window.open(url, '_blank')
    }
  }

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const measure = () => setPageWidth(Math.max(0, el.clientWidth - 32))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    if (!pdf || pageWidth <= 0) return
    let cancelled = false
    ;(async () => {
      const heights: number[] = []
      for (let n = 1; n <= pdf.numPages; n++) {
        const page = await pdf.getPage(n)
        if (cancelled) return
        const vp = page.getViewport({ scale: 1 })
        heights[n - 1] = Math.floor(pageWidth * (vp.height / vp.width))
      }
      if (!cancelled) setPageHeights(heights)
    })()
    return () => { cancelled = true }
  }, [pdf, pageWidth])

  return (
    <div className="pdf-viewer-split">
      <div className="pdf-viewer-header">
        <div className="pdf-viewer-title" title={fileName}>{fileName}</div>
        <div className="btn-wrap">
        <button className="btn-down" onClick={handleDownload} title="PDF 다운"></button>
        <div className="pdf-viewer-close" onClick={onClose}>닫기</div>
        </div>
      </div>

      <div className="pdf-viewer-content scroll-container" ref={scrollRef}>
        {error ? (
          <div className="pdf-viewer-msg">문서를 불러올 수 없습니다.</div>
        ) : pdf && pageWidth > 0 && pageHeights.length === numPages ? (
          Array.from({ length: numPages }, (_, i) => i + 1).map(pageNumber => (
            <PdfPage
              key={pageNumber}
              pdf={pdf}
              pageNumber={pageNumber}
              width={pageWidth}
              estHeight={pageHeights[pageNumber - 1]}
              bboxes={(bboxes ?? []).filter(b => b.page === pageNumber)}
              autoScroll={pageNumber === initialPage}
            />
          ))
        ) : (
          <div className="pdf-viewer-msg">불러오는 중…</div>
        )}
      </div>
    </div>
  )
}

function PdfPage({ pdf, pageNumber, width, estHeight, bboxes, autoScroll }: {
  pdf: PDFDocumentProxy
  pageNumber: number
  width: number
  estHeight: number
  bboxes: ChunkBbox[]
  autoScroll: boolean
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState<{ w: number; h: number } | null>(null)

  useEffect(() => {
    let cancelled = false
    let task: RenderTask | null = null
    pdf.getPage(pageNumber).then(page => {
      if (cancelled) return
      const scale = width / page.getViewport({ scale: 1 }).width
      const viewport = page.getViewport({ scale })
      const canvas = canvasRef.current
      const ctx = canvas?.getContext('2d')
      if (!canvas || !ctx) return
      // 고해상도 디스플레이 대응 — 캔버스 픽셀은 devicePixelRatio 배, CSS 크기는 논리 px
      const dpr = window.devicePixelRatio || 1
      const w = Math.floor(viewport.width)
      const h = Math.floor(viewport.height)
      canvas.width = Math.floor(w * dpr)
      canvas.height = Math.floor(h * dpr)
      canvas.style.width = `${w}px`
      canvas.style.height = `${h}px`
      setSize({ w, h })
      task = page.render({
        canvas,
        canvasContext: ctx,
        viewport,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
      })
      task.promise.catch(() => {  })
    }).catch(() => {  })
    return () => { cancelled = true; task?.cancel() }
  }, [pdf, pageNumber, width])

  useEffect(() => {
    if (autoScroll && wrapRef.current) {
      wrapRef.current.scrollIntoView({ block: 'start' })
    }
  }, [autoScroll, size])

  return (
    <div ref={wrapRef} className="pdf-page" style={size ? { width: size.w, height: size.h } : { width, height: estHeight }}>
      <canvas ref={canvasRef} />
      {size && bboxes.map((bb, i) => {
        const left = bb.l * size.w
        const boxW = (bb.r - bb.l) * size.w
        const isTopLeft = bb.coordOrigin === 'TOPLEFT'
        const top = (isTopLeft ? bb.t : 1 - bb.t) * size.h
        const boxH = Math.abs((isTopLeft ? bb.b - bb.t : bb.t - bb.b)) * size.h
        const [cr, cg, cb] = highlightColor(bb.chunkIndex)
        return (
          <div
            key={i}
            className="pdf-bbox"
            style={{
              left, top, width: boxW, height: boxH,
              borderColor: `rgb(${cr}, ${cg}, ${cb})`,
              background: `rgba(${cr}, ${cg}, ${cb}, 0.12)`,
            }}
          />
        )
      })}
    </div>
  )
}
