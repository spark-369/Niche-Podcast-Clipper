/**
 * ClipDetailPage - review a generated clip, watch it, render & download it.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useClips } from '../context/ClipContext'
import {
  formatDuration,
  formatScore,
  formatTimecode,
  statusBadgeClass,
  statusLabel,
} from '../utils/format'

export default function ClipDetailPage() {
  const { clipId } = useParams()
  const id = Number(clipId)
  const navigate = useNavigate()

  const {
    clips,
    fetchClip,
    renderClip,
    deleteClip,
    resumeClipRender,
    renderProgress,
    clipThumbnailUrl,
    clipDownloadUrl,
  } = useClips()

  const contextClip = useMemo(() => clips.find((item) => item.id === id) || null, [clips, id])
  const [remoteClip, setRemoteClip] = useState(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const clip = contextClip || remoteClip

  // After a hard refresh the in-memory clip list is empty; fetch the single clip.
  useEffect(() => {
    if (contextClip || !id) return
    let cancelled = false
    fetchClip(id)
      .then((data) => {
        if (!cancelled) setRemoteClip(data)
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [contextClip, id, fetchClip])

  // A render started elsewhere (the editor's "Save changes", or a previous visit)
  // outlives this page. The stored `clip.progress` is only as fresh as the last
  // poll that survived, so an already-running job is re-attached here to keep the
  // overlay counting up instead of freezing on a stale percentage.
  //
  // Compared case-insensitively: the stored value is a lowercase enum, but a
  // server that ever reports `Rendering` would otherwise never match and the page
  // would silently show no progress at all.
  const renderingOnServer = String(clip?.status || '').toLowerCase() === 'rendering'

  // Live Celery progress, published by the context pollers — the same source the
  // editor's preview overlay reads, so both pages report identical numbers.
  const live = renderProgress?.[id] || null

  // Show the progress state whenever *either* the server says a render is running
  // or a job is publishing progress locally. Checking only `clip.status` missed
  // the window between dispatching a job and the first status write landing, and
  // the window right after a re-render succeeds but before the clip is re-fetched.
  const renderInFlight = renderingOnServer || Boolean(live)

  // Prefer the worker's own figure; the clip's stored `progress` is only as
  // current as the last poll that landed.
  const progress = Math.max(
    0,
    Math.min(100, Number(live?.progress ?? clip?.progress ?? 0) || 0),
  )
  const progressMessage = live?.message || ''

  // Fallback media so a save-in-flight does not blank the frame: the PATCH that
  // stores an edit deletes the previous render (and its companion) *before* the
  // replacement is written, so mid-render `output_path` is gone and the <video>
  // would unmount. Remembering the last URL that pointed at a real file keeps the
  // element mounted — and gives the progress overlay something to sit over — for
  // both a render started here and one started in the editor.
  const [lastPlayableUrl, setLastPlayableUrl] = useState(null)
  const mediaUrl = useMemo(() => {
    if (clip?.output_path) {
      return clipDownloadUrl(
        clip.id,
        `${clip.updated_at}-${clip.output_size_bytes}`,
      )
    }
    return lastPlayableUrl
  }, [clip, clipDownloadUrl, lastPlayableUrl])

  useEffect(() => {
    if (clip?.output_path && mediaUrl) setLastPlayableUrl(mediaUrl)
  }, [clip?.output_path, mediaUrl])

  const resumedClipRef = useRef(null)
  useEffect(() => {
    if (!id || !renderingOnServer) return undefined
    // Guarded on the clip id: the poller writes progress back onto the shared
    // clip, so an unguarded effect would restart the loop on every tick.
    if (resumedClipRef.current === id) return undefined
    resumedClipRef.current = id
    let cancelled = false
    resumeClipRender(id)
      .then((settled) => {
        if (cancelled || !settled) return
        fetchClip(id).then(setRemoteClip).catch(() => {})
      })
      .catch(() => {})
      .finally(() => {
        resumedClipRef.current = null
      })
    return () => {
      cancelled = true
    }
  }, [id, renderingOnServer, resumeClipRender, fetchClip])

  if (!clip) {
    if (!loadFailed) {
      return (
        <div className="page">
          <div className="skeleton" style={{ height: 320 }} />
        </div>
      )
    }
    return (
      <div className="page">
        <div className="empty-state">
          <div className="empty-state__icon">🔍</div>
          <h3>Clip not found</h3>
          <p>
            It may have been deleted. <Link to="/dashboard">Back to the dashboard</Link>
          </p>
        </div>
      </div>
    )
  }

  // Whether the clip has a file on disk right now. Used only to choose the
  // action buttons: `renderInFlight` decides the *progress* state, because a
  // render deletes the previous file before the replacement is written.
  const rendered = Boolean(clip.output_path)
  const scores = [
    ['Virality', clip.virality_score],
    ['Hook', clip.hook_score],
    ['Emotion', clip.emotion_score],
    ['Quotability', clip.quotability_score],
  ]

  async function handleDelete() {
    const ok = await deleteClip(clip.id)
    if (ok) navigate('/dashboard')
  }

  return (
    <div className="page page--narrow stack stack--lg">
      <header className="stack stack--sm">
        <Link to={`/episodes/${clip.episode_id}`} className="dim">
          ← Back to episode
        </Link>
        <div className="row row--between">
          <h1 className="truncate">{clip.title}</h1>
          <span className={statusBadgeClass(clip.status)}>
            {renderInFlight ? `Rendering ${progress}%` : statusLabel(clip.status)}
          </span>
        </div>
        <p className="muted" style={{ margin: 0 }}>{clip.hook}</p>
      </header>

      <div className="clip-detail">
        <div className="phone-frame">
          {/* `mediaUrl` falls back to the last file that existed, so deleting the
              render mid-save does not unmount the <video> and blank the frame. */}
          {mediaUrl ? (
            <video
              controls={!renderInFlight}
              preload="metadata"
              playsInline
              key={mediaUrl}
              src={mediaUrl}
              className={`phone-frame__media${renderInFlight ? ' phone-frame__media--busy' : ''}`}
            />
          ) : clip.thumbnail_path ? (
            <img src={clipThumbnailUrl(clip.id)} alt="" className="phone-frame__media" />
          ) : (
            <div className="phone-frame__placeholder">
              <span>9:16</span>
              <p className="dim">Render to preview the vertical cut.</p>
            </div>
          )}
          {/* Blurred live-progress state, mirroring the editor's preview.
              The worker publishes its own percentage and stage message through
              `renderProgress`, so this updates in real time for a job started
              here *or* one started from the editor's "Save changes". */}
          {renderInFlight && (
            <div className="phone-frame__overlay" role="status" aria-live="polite">
              <span
                className="btn__spinner"
                style={{ borderColor: '#fff', borderTopColor: 'transparent' }}
              />
              <span>{progress}%</span>
              {progressMessage && (
                <span className="dim" style={{ fontSize: '0.74rem', maxWidth: '22ch' }}>
                  {progressMessage}
                </span>
              )}
              <div className="progress" style={{ width: 'min(100%, 180px)' }}>
                <div className="progress__bar" style={{ width: `${progress || 4}%` }} />
              </div>
            </div>
          )}
        </div>

        <div className="stack stack--md">
          <div className="card stack stack--sm">
            <h3>Scores</h3>
            {scores.map(([label, value]) => (
              <div key={label} className="stack" style={{ gap: 2 }}>
                <div className="row row--between dim">
                  <span>{label}</span>
                  <span>{formatScore(value)}/100</span>
                </div>
                <div className="progress">
                  <div className="progress__bar" style={{ width: `${Math.min(100, formatScore(value))}%` }} />
                </div>
              </div>
            ))}
          </div>

          <div className="card stack stack--sm">
            <h3>Cut</h3>
            <div className="row row--between dim">
              <span>Start</span>
              <code>{formatTimecode(clip.start_time)}</code>
            </div>
            <div className="row row--between dim">
              <span>End</span>
              <code>{formatTimecode(clip.end_time)}</code>
            </div>
            <div className="row row--between dim">
              <span>Length</span>
              <span>{formatDuration(clip.duration_seconds)}</span>
            </div>
            {clip.keywords?.length > 0 && (
              <div className="tag-list">
                {clip.keywords.map((keyword) => (
                  <span key={keyword} className="tag">#{keyword}</span>
                ))}
              </div>
            )}
          </div>

          <div className="card stack stack--sm">
            <h3>Summary</h3>
            <p className="dim" style={{ margin: 0 }}>{clip.summary}</p>
          </div>
        </div>
      </div>

      {clip.transcript_text && (
        <section className="card stack stack--sm">
          <h3>Clip transcript</h3>
          <p className="dim" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{clip.transcript_text}</p>
        </section>
      )}

      {clip.error_message && <div className="field__error">{clip.error_message}</div>}

      <div className="row">
        {!rendered && !renderInFlight && (
          <button type="button" className="btn btn--primary" onClick={() => renderClip(clip.id)}>
            🎬 Render with OpenCV
          </button>
        )}
        {rendered && (
          <>
            <a className="btn btn--primary" href={clipDownloadUrl(clip.id)} download>
              ⬇ Download MP4
            </a>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => renderClip(clip.id)}
              disabled={renderInFlight}
            >
              ↻ Re-render
            </button>
          </>
        )}
        <Link to={`/clips/${clip.id}/edit`} className="btn btn--ghost">
          ✏️ Edit clip
        </Link>
        <span className="spacer" />
        <button type="button" className="btn btn--danger" onClick={handleDelete}>
          Delete clip
        </button>
      </div>
    </div>
  )
}