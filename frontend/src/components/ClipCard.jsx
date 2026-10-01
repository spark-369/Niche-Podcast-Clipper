/**
 * ClipCard - vertical preview tile for one generated clip.
 */

import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { useClips } from '../context/ClipContext'
import {
  formatDuration,
  formatScore,
  formatTimecode,
  statusBadgeClass,
  statusLabel,
} from '../utils/format'

export default function ClipCard({ clip }) {
  const { renderClip, deleteClip, resumeClipRender, renderProgress } = useClips()
  const rendering = clip.status === 'rendering'
  const rendered = Boolean(clip.output_path)
  // Prefer the live Celery figure published by the pollers: `clip.progress` is
  // only as current as the last poll that survived, so a card opened while a
  // render runs elsewhere would otherwise sit frozen.
  const live = renderProgress?.[clip.id] || null
  const progress = Math.max(
    0,
    Math.min(100, Number(live?.progress ?? clip.progress ?? 0) || 0),
  )

  // Re-attach to a render this card did not start (another tab, the editor's
  // save, or a page reload) so the tile keeps ticking. Keyed on the clip id so
  // the write-back of progress onto the shared clip cannot restart the loop.
  const resumedRef = useRef(null)
  useEffect(() => {
    if (!rendering) return undefined
    if (resumedRef.current === clip.id) return undefined
    resumedRef.current = clip.id
    let cancelled = false
    resumeClipRender(clip.id)
      .catch(() => {})
      .finally(() => {
        if (!cancelled) resumedRef.current = null
      })
    return () => {
      cancelled = true
    }
  }, [clip.id, rendering, resumeClipRender])

  return (
    <article className="card card--interactive clip-card">
      <Link to={`/clips/${clip.id}`} className="clip-card__preview" aria-label={`Open ${clip.title}`}>
        {clip.thumbnail_path ? (
          <img
            src={`/media/${clip.thumbnail_path.replace(/^\.?\//, '')}`}
            alt=""
            loading="lazy"
          />
        ) : (
          <div className="clip-card__placeholder">9:16</div>
        )}
        <span className="clip-card__score">🔥 {formatScore(clip.virality_score)}</span>
        <span className="clip-card__duration">{formatDuration(clip.duration_seconds)}</span>
      </Link>

      <div className="stack stack--sm">
        <Link to={`/clips/${clip.id}`} className="clip-card__title truncate">
          {clip.title}
        </Link>
        <p className="dim clamp-2" style={{ margin: 0 }}>{clip.hook}</p>
        <div className="row" style={{ gap: 8 }}>
          <span className={statusBadgeClass(clip.status)}>{statusLabel(clip.status)}</span>
          <span className="dim mono" style={{ fontSize: '0.74rem' }}>
            {formatTimecode(clip.start_time)} → {formatTimecode(clip.end_time)}
          </span>
        </div>
        {rendering && (
          <div className="stack" style={{ gap: 4 }}>
            <div className="progress">
              <div className="progress__bar" style={{ width: `${progress || 4}%` }} />
            </div>
            <span className="dim" style={{ fontSize: '0.72rem' }}>
              {live?.message ? `${progress}% · ${live.message}` : `${progress}% rendered`}
            </span>
          </div>
        )}
      </div>

      <div className="card__footer">
        {!rendered && !rendering && (
          <button
            type="button"
            className="btn btn--primary btn--sm"
            onClick={() => renderClip(clip.id)}
          >
            Render
          </button>
        )}
        {rendered && (
          <a
            className="btn btn--ghost btn--sm"
            href={`/media/${String(clip.output_path).replace(/^\.?\//, '')}`}
            download
          >
            Download
          </a>
        )}
        <span className="spacer" />
        <Link to={`/clips/${clip.id}/edit`} className="btn btn--ghost btn--sm">
          Edit
        </Link>
        <button
          type="button"
          className="btn btn--danger btn--sm"
          onClick={() => deleteClip(clip.id)}
        >
          ✕
        </button>
      </div>
    </article>
  )
}