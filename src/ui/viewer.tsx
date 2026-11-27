import {
  type CSSProperties,
  type PointerEvent,
  useEffect,
  useRef,
  useState,
} from 'react'
import { loadImage, type Shot } from './api'

type Props = {
  shot: Shot
  plateUrl: string
  maskUrl: string
  resultUrl: string
  resultMaskUrl: string
  tool: 'brush' | 'eraser' | 'point'
  brush: number
  showMask: boolean
  playing: boolean
  locked: boolean
  split: number
  points: Point[]
  onPoint: (point: Point) => void
  canvasRef: React.RefObject<HTMLCanvasElement | null>
  onEdit: () => void
}

export type Point = { x: number; y: number; label: 0 | 1 }

function canvas(width: number, height: number) {
  const layer = document.createElement('canvas')
  layer.width = width
  layer.height = height
  return layer
}

function applyMask(layer: HTMLCanvasElement, image: HTMLImageElement) {
  const ctx = layer.getContext('2d')!
  ctx.clearRect(0, 0, layer.width, layer.height)
  ctx.drawImage(image, 0, 0, layer.width, layer.height)
  const pixels = ctx.getImageData(0, 0, layer.width, layer.height)
  for (let i = 0; i < pixels.data.length; i += 4) {
    const value = pixels.data[i]
    pixels.data[i] = 86
    pixels.data[i + 1] = 245
    pixels.data[i + 2] = 183
    pixels.data[i + 3] = value
  }
  ctx.putImageData(pixels, 0, 0)
}

export async function maskPng(layer: HTMLCanvasElement): Promise<Blob> {
  const png = canvas(layer.width, layer.height)
  const ctx = png.getContext('2d')!
  const pixels = layer
    .getContext('2d')!
    .getImageData(0, 0, layer.width, layer.height)
  for (let i = 0; i < pixels.data.length; i += 4) {
    const value = pixels.data[i + 3]
    pixels.data[i] = value
    pixels.data[i + 1] = value
    pixels.data[i + 2] = value
    pixels.data[i + 3] = 255
  }
  ctx.putImageData(pixels, 0, 0)
  return new Promise((resolve, reject) =>
    png.toBlob(
      (blob) =>
        blob ? resolve(blob) : reject(new Error('Không tạo được PNG mask')),
      'image/png',
    ),
  )
}

export function Viewer({
  shot,
  plateUrl,
  maskUrl,
  resultUrl,
  resultMaskUrl,
  tool,
  brush,
  showMask,
  playing,
  locked,
  split,
  points,
  onPoint,
  canvasRef,
  onEdit,
}: Props) {
  const result = useRef<HTMLCanvasElement>(null)
  const last = useRef<{ x: number; y: number } | null>(null)
  const [maskReady, setMaskReady] = useState(false)
  const [readyResult, setReadyResult] = useState('')
  const [failedPlate, setFailedPlate] = useState('')
  const [failedResult, setFailedResult] = useState('')
  useEffect(() => {
    const layer = canvasRef.current
    if (!layer) return
    let live = true
    last.current = null
    layer.getContext('2d')!.clearRect(0, 0, layer.width, layer.height)
    setMaskReady(false)
    if (!maskUrl || playing) {
      setMaskReady(!playing)
      return
    }
    loadImage(maskUrl)
      .then((image) => {
        if (live) applyMask(layer, image)
      })
      .catch(() => {})
      .finally(() => {
        if (live) setMaskReady(true)
      })
    return () => {
      live = false
    }
  }, [canvasRef, maskUrl, playing, shot.width, shot.height])

  useEffect(() => {
    if (!resultUrl || !resultMaskUrl) return
    let live = true
    Promise.all([
      loadImage(plateUrl),
      loadImage(resultUrl),
      loadImage(resultMaskUrl),
    ])
      .then(([plate, patch, mask]) => {
        if (!live || !result.current) return
        const target = result.current,
          ctx = target.getContext('2d')!
        const matte = canvas(target.width, target.height)
        applyMask(matte, mask)
        const layer = canvas(target.width, target.height),
          blend = layer.getContext('2d')!
        blend.drawImage(patch, 0, 0, layer.width, layer.height)
        blend.globalCompositeOperation = 'destination-in'
        blend.drawImage(matte, 0, 0)
        ctx.clearRect(0, 0, target.width, target.height)
        ctx.drawImage(plate, 0, 0, target.width, target.height)
        ctx.drawImage(layer, 0, 0)
        setReadyResult(resultUrl)
        setFailedResult('')
      })
      .catch(() => {
        if (live) setFailedResult(resultUrl)
      })
    return () => {
      live = false
    }
  }, [plateUrl, resultUrl, resultMaskUrl])

  function draw(event: PointerEvent<HTMLCanvasElement>, start = false) {
    if (
      locked ||
      !maskReady ||
      !showMask ||
      (event.pointerType === 'mouse' && event.button !== 0 && start)
    )
      return
    const layer = event.currentTarget,
      box = layer.getBoundingClientRect()
    if (tool === 'point') {
      if (start)
        onPoint({
          x: Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)),
          y: Math.min(1, Math.max(0, (event.clientY - box.top) / box.height)),
          label: event.shiftKey ? 0 : 1,
        })
      return
    }
    if (!start && !last.current) return
    if (start) layer.setPointerCapture(event.pointerId)
    const point = {
      x: ((event.clientX - box.left) * layer.width) / box.width,
      y: ((event.clientY - box.top) * layer.height) / box.height,
    }
    const prior = last.current ?? point,
      ctx = layer.getContext('2d')!
    ctx.globalCompositeOperation =
      tool === 'eraser' ? 'destination-out' : 'source-over'
    ctx.strokeStyle = ctx.fillStyle = '#56f5b7'
    ctx.lineWidth = brush
    ctx.lineCap = 'round'
    ctx.beginPath()
    if (start) {
      ctx.arc(point.x, point.y, brush / 2, 0, 2 * Math.PI)
      ctx.fill()
    } else {
      ctx.moveTo(prior.x, prior.y)
      ctx.lineTo(point.x, point.y)
      ctx.stroke()
    }
    ctx.globalCompositeOperation = 'source-over'
    last.current = point
    onEdit()
  }

  const compare = Boolean(resultUrl && readyResult === resultUrl)
  return (
    <div
      className="viewer-media"
      style={
        {
          '--aspect': shot.width / shot.height,
          '--split': `${split}%`,
        } as CSSProperties
      }
    >
      <img
        src={plateUrl}
        alt="Plate gốc"
        draggable={false}
        onError={() => setFailedPlate(plateUrl)}
      />
      <canvas
        ref={result}
        width={shot.width}
        height={shot.height}
        className={`after-layer ${compare ? '' : 'hidden'}`}
      />
      {compare && (
        <>
          <div className="compare-divider" />
          <span className="frame-label before">Gốc</span>
          <span className="frame-label after">Kết quả</span>
        </>
      )}
      <canvas
        ref={canvasRef}
        width={shot.width}
        height={shot.height}
        aria-label="Canvas vẽ mask"
        className={`mask-layer ${!showMask || playing ? 'hidden' : ''}`}
        onPointerDown={(e) => draw(e, true)}
        onPointerMove={(e) => draw(e)}
        onPointerUp={() => {
          last.current = null
        }}
        onPointerCancel={() => {
          last.current = null
        }}
        onLostPointerCapture={() => {
          last.current = null
        }}
      />
      {!playing &&
        points.map((point, index) => (
          <span
            key={index}
            className={`sam-point ${point.label ? '' : 'negative'}`}
            style={{ left: `${point.x * 100}%`, top: `${point.y * 100}%` }}
          >
            {point.label ? '+' : '−'}
          </span>
        ))}
      {(failedPlate === plateUrl ||
        (resultUrl && failedResult === resultUrl)) && (
        <span className="viewer-error">
          Không tải được{' '}
          {failedPlate === plateUrl ? 'frame gốc' : 'frame kết quả'}
        </span>
      )}
    </div>
  )
}
