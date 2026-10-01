/** Shared client-side formatting helpers. */

export function formatDuration(seconds) {
  const value = Number(seconds) || 0
  if (value < 60) return `${Math.round(value)}s`
  const hours = Math.floor(value / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

export function formatTimecode(seconds) {
  const value = Math.max(0, Number(seconds) || 0)
  const mins = Math.floor(value / 60)
  const secs = Math.floor(value % 60)
  const tenths = Math.floor((value - Math.floor(value)) * 10)
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${tenths}`
}

export function formatFileSize(bytes) {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  const kb = value / 1024
  if (kb < 1024) return `${kb.toFixed(1)} KB`
  const mb = kb / 1024
  if (mb < 1024) return `${mb.toFixed(1)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}

export function formatDate(isoString) {
  if (!isoString) return ''
  try {
    return new Date(isoString).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
  } catch {
    return isoString
  }
}

export function formatScore(score) {
  const value = Number(score) || 0
  return `${Math.round(value * 100)}`
}

const STATUS_LABELS = {
  uploaded: 'Uploaded',
  queued: 'Queued',
  processing: 'Processing',
  transcribing: 'Transcribing',
  transcribed: 'Transcribed',
  analyzing: 'Analyzing',
  analyzed: 'Analyzed',
  rendering: 'Rendering',
  ready: 'Ready',
  rendered: 'Rendered',
  completed: 'Completed',
  success: 'Success',
  failed: 'Failed',
  pending: 'Pending',
  running: 'Running',
  started: 'Started',
}

export function statusLabel(status) {
  return STATUS_LABELS[String(status || '').toLowerCase()] || String(status || 'Unknown')
}

const STATUS_BADGES = {
  uploaded: 'badge',
  queued: 'badge badge--warning',
  pending: 'badge badge--warning',
  processing: 'badge badge--warning',
  transcribing: 'badge badge--warning',
  analyzing: 'badge badge--warning',
  rendering: 'badge badge--warning',
  running: 'badge badge--warning',
  started: 'badge badge--warning',
  transcribed: 'badge badge--success',
  analyzed: 'badge badge--success',
  ready: 'badge badge--success',
  rendered: 'badge badge--success',
  completed: 'badge badge--success',
  success: 'badge badge--success',
  failed: 'badge badge--danger',
  failure: 'badge badge--danger',
}

export function statusBadgeClass(status) {
  return STATUS_BADGES[String(status || '').toLowerCase()] || 'badge'
}
