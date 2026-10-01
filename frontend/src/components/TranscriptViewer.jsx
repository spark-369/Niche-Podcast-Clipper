/**
 * TranscriptViewer - timestamped segment list with search and click-to-seek.
 */

import { useMemo, useState } from 'react'
import { formatTimecode } from '../utils/format'

export default function TranscriptViewer({ transcript, onSeek }) {
  const [query, setQuery] = useState('')
  const segments = transcript?.segments || []

  const filtered = useMemo(() => {
    if (!query.trim()) return segments
    const needle = query.toLowerCase()
    return segments.filter((segment) => segment.text.toLowerCase().includes(needle))
  }, [segments, query])

  if (!transcript) {
    return (
      <div className="empty-state">
        <div className="empty-state__icon">📝</div>
        <h3>No transcript yet</h3>
        <p>Run the local Whisper pipeline to generate timestamped text.</p>
      </div>
    )
  }

  return (
    <div className="stack stack--sm">
      <div className="row row--between">
        <span className="dim">
          {transcript.language?.toUpperCase()} · {transcript.model_name} ·{' '}
          {transcript.word_count?.toLocaleString()} words
        </span>
        <input
          className="input transcript-viewer__search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search the transcript…"
        />
      </div>

      <div className="transcript-viewer">
        {filtered.length === 0 ? (
          <p className="dim center">No segments match “{query}”.</p>
        ) : (
          filtered.map((segment) => (
            <button
              key={`${segment.index}-${segment.start_time}`}
              type="button"
              className="transcript-segment"
              onClick={() => onSeek?.(segment.start_time)}
              title="Click to seek the player"
            >
              <span className="transcript-segment__time mono">
                {formatTimecode(segment.start_time)}
              </span>
              <span className="transcript-segment__text">{segment.text}</span>
            </button>
          ))
        )}
      </div>
    </div>
  )
}