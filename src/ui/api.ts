export type Mode =
  | 'single_frame_inpaint'
  | 'tracked_clean_plate'
  | 'sam2_track'
  | 'temporal_propainter'
export type Shot = {
  shot_id: string
  node_name: string
  first_frame: number
  last_frame: number
  width: number
  height: number
  fps: number
  ai_prep_colorspace: string
  is_demo: boolean
}
export type Job = {
  job_id: string
  status: 'pending' | 'running' | 'failed' | 'completed'
  progress: number
  error_message: string
  stage: string
  current_frame: number | null
  result_sequence_pattern: string
  apply_manifest_path: string
}

export async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  if (!response.ok) {
    const data = await response.json().catch(() => null)
    throw new Error(
      typeof data?.detail === 'string'
        ? data.detail
        : `${response.status} ${response.statusText}`,
    )
  }
  return response.json() as Promise<T>
}

export const shotUrl = (shot: Shot) =>
  `/api/shots/${encodeURIComponent(shot.shot_id)}`
export const jobUrl = (job: Job) => `/api/jobs/${job.job_id}`
export const json = (data: object): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data),
})

export function upload<T>(
  url: string,
  blob: Blob,
  filename: string,
): Promise<T> {
  const data = new FormData()
  data.append('file', blob, filename)
  return api<T>(url, { method: 'POST', body: data })
}

export async function loadImage(url: string): Promise<HTMLImageElement> {
  const img = new Image()
  img.src = url
  await img.decode()
  return img
}
