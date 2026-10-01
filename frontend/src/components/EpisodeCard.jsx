/**
 * EpisodeCard - dashboard tile for one episode with quick actions.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  formatDuration,
  formatFileSize,
  statusBadgeClass,
  statusLabel,
} from '../utils/format'

export default function EpisodeCard({ episode, onTranscribe, onDelete, transcribeStatus }) {
  const [confirming, setConfirming] = useState(false)
  const hasTranscript = Boolean(episode.has_transcript)

  return (
    <article className="card card--interactive episode-card">
      <div className="card__header">
        <div className="stack stack--sm" style={{ minWidth: 0 }}>
          <Link to={`/episodes/${episode.id}`} className="episode-card__title">
            <span className="truncate">{episode.title}</span>
          </Link>
          <span className="dim">
            {formatDuration(episode.duration_seconds)} ·{' '}
            {formatFileSize(episode.file_size_bytes)} · {episode.media_type}
          </span>
        </div>
        <span className={statusBadgeClass(episode.status)}>{statusLabel(episode.status)}</span>
      </div>

      {episode.description && (
        <p className="dim truncate">{episode.description}</p>
      )}

      <div className="tag-list">
        <span className="tag">{hasTranscript ? '✓ transcript' : 'no transcript'}</span>
        <span className="tag">{episode.clip_count || 0} clips</span>
        {episode.language && <span className="tag">{episode.language}</span>}
      </div>

      {transcribeStatus && (
        <div className="stack" style={{ gap: 4 }}>
          <div className="progress" aria-label="Transcription progress">
            <div
              className="progress__bar"
              style={{ width: `${transcribeStatus.progress || 4}%` }}
            />
          </div>
          <span className="dim" style={{ fontSize: '0.72rem' }}>
            {transcribeStatus.message || 'Transcribing…'} · {transcribeStatus.progress || 0}%
          </span>
        </div>
      )}

      <div className="card__footer">
        <Link to={`/episodes/${episode.id}`} className="btn btn--ghost btn--sm">
          Open
        </Link>
        {!hasTranscript && (
          <button
            type="button"
            className="btn btn--primary btn--sm"
            onClick={onTranscribe}
            disabled={Boolean(transcribeStatus)}
          >
            {transcribeStatus ? 'Transcribing…' : 'Transcribe'}
          </button>
        )}
        <span className="spacer" />
        {confirming ? (
          <div className="row">
            <button
              type="button"
              className="btn btn--danger btn--sm"
              onClick={() => {
                setConfirming(false)
                onDelete()
              }}
            >
              Confirm delete
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="btn btn--danger btn--sm"
            onClick={() => setConfirming(true)}
          >
            Delete
          </button>
        )}
      </div>
    </article>
  )
}