import {
  type ChangeEvent,
  type FormEvent,
  useEffect,
  useRef,
  useState,
} from 'react'
import { createRoot } from 'react-dom/client'
import {
  api,
  jobUrl,
  json,
  shotUrl,
  upload,
  type Job,
  type Mode,
  type Shot,
} from './api'
import { maskPng, Viewer, type Point } from './viewer'
import './style.css'

const modes: Record<Mode, string> = {
  single_frame_inpaint: 'Inpaint một frame',
  tracked_clean_plate: 'Clean plate theo đoạn',
  sam2_track: 'Track mask · SAM 2',
  temporal_propainter: 'Inpaint theo đoạn · ProPainter',
}
const stageName: Record<string, string> = {
  queued: 'Đang xếp hàng',
  preparing: 'Đang chuẩn bị dữ liệu',
  tracking_sam2: 'SAM 2 đang track mask',
  tracking_background: 'Đang track nền',
  preparing_masks: 'Đang chuẩn bị mask',
  submitting_to_comfyui: 'Đang gửi ComfyUI',
  waiting_for_comfyui: 'ComfyUI đang xử lý',
  inpainting_propainter: 'ProPainter đang xử lý',
  writing_frames: 'Đang ghi kết quả',
  completed: 'Đã hoàn thành',
}
type Run = { job: Job; first: number; last: number; mode: Mode }
const query = new URLSearchParams(location.search)
const inRange = (run: Run | null, frame: number) =>
  run && frame >= run.first && frame <= run.last

function App() {
  const [shots, setShots] = useState<Shot[]>([])
  const [shot, setShot] = useState<Shot | null>(null)
  const [frame, setFrame] = useState(0)
  const [first, setFirst] = useState(0)
  const [last, setLast] = useState(0)
  const [mode, setMode] = useState<Mode>('single_frame_inpaint')
  const [tool, setTool] = useState<'brush' | 'eraser' | 'point'>('brush')
  const [points, setPoints] = useState<Point[]>([])
  const [brush, setBrush] = useState(40)
  const [overlay, setOverlay] = useState(true)
  const [split, setSplit] = useState(50)
  const [playing, setPlaying] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [revision, setRevision] = useState(0)
  const [keys, setKeys] = useState<number[]>([])
  const [tracked, setTracked] = useState<Run | null>(null)
  const [inspectTrack, setInspectTrack] = useState(false)
  const [result, setResult] = useState<Run | null>(null)
  const [run, setRun] = useState<Run | null>(null)
  const [prompt, setPrompt] = useState('')
  const [negative, setNegative] = useState('')
  const [reference, setReference] = useState<{
    path: string
    name: string
    anchor: number
  } | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const canvas = useRef<HTMLCanvasElement>(null)
  const maskInput = useRef<HTMLInputElement>(null)

  const report = (reason: unknown) =>
    setError(reason instanceof Error ? reason.message : String(reason))
  const running = run?.job.status === 'pending' || run?.job.status === 'running'

  function choose(next: Shot) {
    setShot(next)
    setFrame(next.first_frame)
    setFirst(next.first_frame)
    setLast(next.last_frame)
    setMode(
      next.is_demo || next.first_frame !== next.last_frame
        ? 'tracked_clean_plate'
        : 'single_frame_inpaint',
    )
    setPlaying(false)
    setRun(null)
    setResult(null)
    setReference(null)
    setTracked(null)
    setKeys([])
    setDirty(false)
    setInspectTrack(false)
    setError('')
    setMessage('')
  }

  async function refresh() {
    setError('')
    const items = await api<Shot[]>('/api/shots')
    setShots(items)
    if (!shot && items.length)
      choose(
        items.find((item) => item.shot_id === query.get('shot')) ?? items[0],
      )
  }

  useEffect(() => {
    void refresh().catch(report)
  }, [])
  useEffect(() => {
    setPoints([])
  }, [frame, shot?.shot_id])

  const pendingId = running ? run?.job.job_id : null
  useEffect(() => {
    if (!pendingId || !run) return
    let live = true
    const submitted = run
    let timer: number
    async function poll() {
      try {
        const job = await api<Job>(`/api/jobs/${pendingId}`)
        if (!live) return
        const update = { ...submitted, job }
        setRun(update)
        if (job.status === 'completed') {
          if (submitted.mode === 'sam2_track') {
            setTracked(update)
            setInspectTrack(true)
            setOverlay(true)
          } else {
            setResult(update)
            setOverlay(false)
          }
          setMessage(`Đã xử lý frame ${submitted.first}–${submitted.last}.`)
          return
        }
        if (job.status === 'failed') {
          setError(job.error_message)
          return
        }
      } catch (reason) {
        if (live) report(reason)
      }
      if (live) timer = window.setTimeout(poll, 700)
    }
    timer = window.setTimeout(poll, 200)
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [pendingId])

  useEffect(() => {
    if (!playing || !shot) return
    const step = Math.max(1000 / Math.max(shot.fps, 1), 1000 / 60)
    const timer = window.setInterval(
      () =>
        setFrame((value) =>
          value >= shot.last_frame ? shot.first_frame : value + 1,
        ),
      step,
    )
    return () => window.clearInterval(timer)
  }, [playing, shot])

  async function saveMask() {
    if (!shot || !canvas.current || !dirty) return
    setBusy(true)
    try {
      await upload(
        `${shotUrl(shot)}/mask/${frame}`,
        await maskPng(canvas.current),
        `mask.${frame}.png`,
      )
      setDirty(false)
      setInspectTrack(false)
      setRevision((value) => value + 1)
      setMessage(`Đã lưu mask frame ${frame}.`)
    } finally {
      setBusy(false)
    }
  }

  async function go(value: number) {
    if (!shot || busy || !Number.isFinite(value)) return
    const next = Math.min(shot.last_frame, Math.max(shot.first_frame, value))
    if (dirty) await saveMask()
    setFrame(next)
  }

  async function switchShot(id: string) {
    const next = shots.find((item) => item.shot_id === id)
    if (!next || running || busy) return
    await saveMask()
    choose(next)
    history.replaceState(null, '', `?shot=${encodeURIComponent(next.shot_id)}`)
  }

  async function loadMask(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !shot) return
    setPlaying(false)
    setBusy(true)
    setError('')
    try {
      const image = await createImageBitmap(file)
      const valid = image.width === shot.width && image.height === shot.height
      image.close()
      if (!valid)
        throw new Error(
          `Mask cần đúng kích thước ${shot.width} × ${shot.height}.`,
        )
      await upload(`${shotUrl(shot)}/mask/${frame}`, file, `mask.${frame}.png`)
      setDirty(false)
      setInspectTrack(false)
      setOverlay(true)
      setRevision((value) => value + 1)
      setMessage(`Đã nhập mask frame ${frame}.`)
    } finally {
      setBusy(false)
    }
  }

  async function loadReference(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !shot) return
    setBusy(true)
    setError('')
    try {
      const image = await createImageBitmap(file)
      const valid = image.width === shot.width && image.height === shot.height
      image.close()
      if (!valid)
        throw new Error(
          `Clean plate cần đúng kích thước ${shot.width} × ${shot.height}.`,
        )
      const data = await upload<{ reference_image_path: string }>(
        `${shotUrl(shot)}/reference`,
        file,
        file.name,
      )
      setReference({
        path: data.reference_image_path,
        name: file.name,
        anchor: frame,
      })
    } finally {
      setBusy(false)
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!shot || running || busy) return
    setPlaying(false)
    setError('')
    setMessage('')
    const from = mode === 'single_frame_inpaint' ? frame : first
    const to = mode === 'single_frame_inpaint' ? frame : last
    if (
      from < shot.first_frame ||
      to > shot.last_frame ||
      (mode !== 'single_frame_inpaint' && from >= to)
    ) {
      setError('Chọn ít nhất hai frame trong shot cho job theo đoạn.')
      return
    }
    if (
      mode === 'single_frame_inpaint' &&
      shot.first_frame !== shot.last_frame
    ) {
      setError('Hãy chuẩn bị shot một frame trong Nuke để dùng chế độ này.')
      return
    }
    const selectedKeys = keys.filter((key) => key >= from && key <= to)
    if (mode === 'sam2_track' && !selectedKeys.length) {
      setError('Đánh dấu ít nhất một mask keyframe.')
      return
    }
    await saveMask()
    setBusy(true)
    try {
      const job = await api<Job>(
        '/api/jobs',
        json({
          shot_id: shot.shot_id,
          mode,
          first_frame: from,
          last_frame: to,
          prompt,
          negative_prompt: negative,
          reference_image_path:
            mode === 'tracked_clean_plate' ? (reference?.path ?? '') : '',
          anchor_frame:
            mode === 'tracked_clean_plate' ? (reference?.anchor ?? null) : null,
          mask_keyframes:
            mode === 'sam2_track' || mode === 'temporal_propainter'
              ? selectedKeys
              : [],
        }),
      )
      setRun({ job, mode, first: from, last: to })
    } finally {
      setBusy(false)
    }
  }

  async function demo() {
    setBusy(true)
    try {
      const sample = await api<Shot>(
        '/api/shots/demo?shot_id=demo_ui&num_frames=8',
        { method: 'POST' },
      )
      await refresh()
      choose(sample)
    } finally {
      setBusy(false)
    }
  }

  async function segmentPoints() {
    if (!shot || busy || running || !points.some((point) => point.label === 1))
      return
    setPlaying(false)
    setError('')
    setMessage('')
    await saveMask()
    setBusy(true)
    try {
      const job = await api<Job>(
        '/api/jobs',
        json({
          shot_id: shot.shot_id,
          mode: 'sam2_track',
          first_frame: frame,
          last_frame: frame,
          sam2_points: points,
        }),
      )
      setRun({ job, mode: 'sam2_track', first: frame, last: frame })
    } finally {
      setBusy(false)
    }
  }

  async function useTrackedMask() {
    if (!shot || !trackedMask || busy || running) return
    if (dirty) await saveMask()
    else {
      setBusy(true)
      try {
        const response = await fetch(trackedMask)
        if (!response.ok) throw new Error('Không tải được mask SAM 2.')
        await upload(
          `${shotUrl(shot)}/mask/${frame}`,
          await response.blob(),
          `mask.${frame}.png`,
        )
        setRevision((value) => value + 1)
        setInspectTrack(false)
      } finally {
        setBusy(false)
      }
    }
    setKeys((values) =>
      values.includes(frame)
        ? values
        : [...values, frame].sort((a, b) => a - b),
    )
    setPoints([])
    setMessage(`Đã lưu mask SAM 2 làm keyframe ${frame}.`)
  }

  const trackedMask =
    inspectTrack && inRange(tracked, frame)
      ? `${jobUrl(tracked!.job)}/mask/${frame}`
      : ''
  const resultBase = inRange(result, frame) ? jobUrl(result!.job) : ''
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <i />
          ComfyRemove
        </div>
        <span className="product">Nuke ↔ Core</span>
        <label className="shot-picker">
          <span>Shot</span>
          <select
            aria-label="Shot"
            value={shot?.shot_id ?? ''}
            disabled={busy || running}
            onChange={(event) =>
              void switchShot(event.target.value).catch(report)
            }
          >
            <option value="" disabled>
              Chọn shot
            </option>
            {shots.map((item) => (
              <option key={item.shot_id} value={item.shot_id}>
                {item.node_name} · {item.shot_id}
              </option>
            ))}
          </select>
        </label>
        <button className="quiet" onClick={() => void refresh().catch(report)}>
          Làm mới
        </button>
      </header>
      {shot ? (
        <main className="workspace">
          <section className="studio">
            <div className="toolbar">
              <div className="tools">
                <button
                  aria-pressed={tool === 'brush'}
                  onClick={() => {
                    setTool('brush')
                    setOverlay(true)
                  }}
                >
                  Cọ
                </button>
                <button
                  aria-pressed={tool === 'eraser'}
                  onClick={() => {
                    setTool('eraser')
                    setOverlay(true)
                  }}
                >
                  Tẩy
                </button>
                <button
                  aria-pressed={tool === 'point'}
                  onClick={() => {
                    setTool('point')
                    setOverlay(true)
                    setPlaying(false)
                  }}
                >
                  Điểm SAM 2
                </button>
                <label className="brush-size">
                  Cỡ cọ{' '}
                  <input
                    type="range"
                    aria-label="Cỡ cọ"
                    min={4}
                    max={200}
                    value={brush}
                    onChange={(e) => setBrush(+e.target.value)}
                  />
                  <output>{brush} px</output>
                </label>
              </div>
              <label className="check">
                <input
                  type="checkbox"
                  checked={overlay}
                  onChange={(e) => setOverlay(e.target.checked)}
                />{' '}
                Mask
              </label>
            </div>
            <div className="stage">
              <Viewer
                key={shot.shot_id}
                shot={shot}
                plateUrl={`${shotUrl(shot)}/frames/${frame}`}
                maskUrl={
                  trackedMask || `${shotUrl(shot)}/mask/${frame}?v=${revision}`
                }
                resultUrl={resultBase ? `${resultBase}/result/${frame}` : ''}
                resultMaskUrl={resultBase ? `${resultBase}/mask/${frame}` : ''}
                tool={tool}
                brush={brush}
                playing={playing}
                locked={busy || running}
                showMask={overlay}
                split={split}
                points={points}
                onPoint={(point) =>
                  setPoints((values) =>
                    values.length < 64 ? [...values, point] : values,
                  )
                }
                canvasRef={canvas}
                onEdit={() => setDirty(true)}
              />
            </div>
            <div className="timeline">
              <div className="transport">
                <button
                  className="square"
                  aria-label="Frame trước"
                  disabled={busy}
                  onClick={() => void go(frame - 1).catch(report)}
                >
                  ‹
                </button>
                <button
                  className="square"
                  aria-label={playing ? 'Tạm dừng' : 'Phát timeline'}
                  disabled={busy}
                  onClick={() => {
                    void (async () => {
                      await saveMask()
                      setPlaying(!playing)
                    })().catch(report)
                  }}
                >
                  {playing ? 'Ⅱ' : '▶'}
                </button>
                <button
                  className="square"
                  aria-label="Frame sau"
                  disabled={busy}
                  onClick={() => void go(frame + 1).catch(report)}
                >
                  ›
                </button>
                <strong>{frame}</strong>
                <span>{shot.fps} fps</span>
                <span className="geometry">
                  {shot.width} × {shot.height} · {shot.ai_prep_colorspace}
                </span>
              </div>
              <label className="scrub">
                <span>{shot.first_frame}</span>
                <input
                  type="range"
                  aria-label="Timeline"
                  disabled={busy}
                  min={shot.first_frame}
                  max={shot.last_frame}
                  value={frame}
                  onChange={(e) => void go(+e.target.value).catch(report)}
                />
                <span>{shot.last_frame}</span>
              </label>
              {resultBase && (
                <label className="scrub compare-control">
                  <span>Trước</span>
                  <input
                    type="range"
                    aria-label="So sánh trước và sau"
                    value={split}
                    onChange={(e) => setSplit(+e.target.value)}
                  />
                  <span>Sau</span>
                </label>
              )}
            </div>
          </section>
          <aside className="sidebar">
            <section>
              <h2>Mask</h2>
              <div className="button-row">
                <button
                  disabled={busy || running}
                  onClick={() => maskInput.current?.click()}
                >
                  Nhập PNG
                </button>
                <button
                  disabled={!dirty || busy || running}
                  onClick={() => void saveMask().catch(report)}
                >
                  Lưu mask
                </button>
              </div>
              <input
                ref={maskInput}
                type="file"
                accept="image/png"
                hidden
                onChange={(e) => void loadMask(e).catch(report)}
              />
              {tool === 'point' && (
                <>
                  <small>
                    Click thêm điểm. Giữ Shift khi click để loại vùng.
                  </small>
                  <div className="button-row">
                    <button
                      disabled={
                        busy || running || !points.some((p) => p.label === 1)
                      }
                      onClick={() => void segmentPoints().catch(report)}
                    >
                      Tạo mask
                    </button>
                    <button
                      disabled={!points.length}
                      onClick={() => setPoints([])}
                    >
                      Xóa điểm
                    </button>
                  </div>
                </>
              )}
              <button
                className="wide"
                disabled={busy || running}
                onClick={() =>
                  void (async () => {
                    await saveMask()
                    setKeys((values) =>
                      values.includes(frame)
                        ? values.filter((key) => key !== frame)
                        : [...values, frame].sort((a, b) => a - b),
                    )
                  })().catch(report)
                }
              >
                {keys.includes(frame)
                  ? 'Bỏ đánh dấu keyframe'
                  : 'Đánh dấu keyframe'}
              </button>
              {keys.length > 0 && (
                <div className="keyframes" aria-label="Mask keyframes">
                  {keys.map((key) => (
                    <button
                      key={key}
                      onClick={() => void go(key).catch(report)}
                    >
                      {key}
                    </button>
                  ))}
                </div>
              )}
              {tracked && (
                <label className="check">
                  <input
                    type="checkbox"
                    disabled={dirty}
                    checked={inspectTrack}
                    onChange={(e) => {
                      setInspectTrack(e.target.checked)
                      setOverlay(true)
                    }}
                  />{' '}
                  Xem mask SAM 2
                </label>
              )}
              {trackedMask && (
                <button
                  disabled={busy || running}
                  onClick={() => void useTrackedMask().catch(report)}
                >
                  Dùng mask SAM 2 làm keyframe
                </button>
              )}
            </section>
            <form onSubmit={(event) => void submit(event).catch(report)}>
              <h2>Xử lý</h2>
              <label>
                Chế độ
                <select
                  value={mode}
                  onChange={(e) => setMode(e.target.value as Mode)}
                >
                  {Object.entries(modes).map(([value, name]) => (
                    <option key={value} value={value}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
              {mode !== 'single_frame_inpaint' && (
                <div className="range-fields">
                  <label>
                    Frame đầu
                    <input
                      type="number"
                      value={first}
                      min={shot.first_frame}
                      max={shot.last_frame}
                      onChange={(e) => setFirst(+e.target.value)}
                    />
                  </label>
                  <label>
                    Frame cuối
                    <input
                      type="number"
                      value={last}
                      min={shot.first_frame}
                      max={shot.last_frame}
                      onChange={(e) => setLast(+e.target.value)}
                    />
                  </label>
                </div>
              )}
              {(mode === 'single_frame_inpaint' ||
                mode === 'tracked_clean_plate') && (
                <>
                  <label>
                    Prompt
                    <textarea
                      rows={3}
                      value={prompt}
                      onChange={(e) => setPrompt(e.target.value)}
                      placeholder="Mô tả phần nền cần tái tạo"
                    />
                  </label>
                  <label>
                    Negative prompt
                    <input
                      value={negative}
                      onChange={(e) => setNegative(e.target.value)}
                    />
                  </label>
                </>
              )}
              {mode === 'tracked_clean_plate' && (
                <div className="reference">
                  <label className="upload-button">
                    Chọn clean plate{' '}
                    <input
                      type="file"
                      accept="image/*"
                      onChange={(e) => void loadReference(e).catch(report)}
                    />
                  </label>
                  {reference && (
                    <div>
                      <span>
                        {reference.name} · mốc {reference.anchor}
                      </span>
                      <button type="button" onClick={() => setReference(null)}>
                        Bỏ
                      </button>
                    </div>
                  )}
                </div>
              )}
              <button
                className="primary"
                type="submit"
                disabled={busy || running}
              >
                {running ? 'Đang xử lý…' : 'Chạy tác vụ'}
              </button>
            </form>
            <section className="job-status" aria-live="polite">
              <h2>Tiến độ</h2>
              {run && (
                <>
                  <div className="job-line">
                    <span>
                      {run.job.status === 'failed'
                        ? 'Tác vụ lỗi'
                        : (stageName[run.job.stage] ?? run.job.stage)}
                    </span>
                    <strong>{Math.round(run.job.progress * 100)}%</strong>
                  </div>
                  <progress max={1} value={run.job.progress} />
                  {run.job.current_frame !== null && (
                    <small>Frame {run.job.current_frame}</small>
                  )}
                </>
              )}
              {result?.job.apply_manifest_path && (
                <a className="download" href={`${jobUrl(result.job)}/manifest`}>
                  Tải manifest cho Nuke ↓
                </a>
              )}
              {error && (
                <p className="error" role="alert">
                  {error}
                </p>
              )}
              {message && !error && <p className="message">{message}</p>}
            </section>
          </aside>
        </main>
      ) : (
        <main className="empty">
          <h1>Chọn shot để bắt đầu</h1>
          <p>
            Mở shot từ menu ComfyRemove trong Nuke hoặc tạo shot demo để thử
            timeline và mask.
          </p>
          <button
            className="primary"
            disabled={busy}
            onClick={() => void demo().catch(report)}
          >
            Tạo shot demo
          </button>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </main>
      )}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
