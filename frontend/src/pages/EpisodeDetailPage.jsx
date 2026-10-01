/**
 * EpisodeDetailPage - single episode workspace: player, transcript and
 * highlight generation with the local Flan-T5 scorer.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useClips, DEFAULT_CAPTION_STYLE, DEFAULT_FRAMING } from '../context/ClipContext'
import ClipCard from '../components/ClipCard'
import TranscriptViewer from '../components/TranscriptViewer'
import { healthApi } from '../api/jobs'
import {
  formatDuration,
  formatFileSize,
  statusBadgeClass,
  statusLabel,
} from '../utils/format'

const POLL_STATUSES = ['queued', 'transcribing', 'processing']

export default function EpisodeDetailPage() {
  const { episodeId } = useParams()
  const id = Number(episodeId)

  const {
    episodes,
    clips,
    transcripts,
    loading,
    loadEpisodes,
    loadClips,
    loadTranscript,
    transcribeEpisode,
    transcribeProgress,
    highlightProgress,
    generateClips,
    refreshEpisode,
    episodeMediaUrl,
  } = useClips()

  const videoRef = useRef(null)
  const [options, setOptions] = useState({
    clipCount: 5,
    minDuration: 15,
    maxDuration: 60,
    focus: '',
    useCache: true,
  })
  const [showAdvanced, setShowAdvanced] = useState(false)

  const episode = useMemo(
    () => episodes.find((item) => item.id === id) || null,
    [episodes, id],
  )
  const transcript = transcripts[id] || null
  // Live transcription progress (survives page refresh via resumed polling).
  const transcribeStatus = transcribeProgress[id]
  // Live clip-generation progress (also refresh-proof).
  const highlightStatus = highlightProgress[id]
  const episodeClips = useMemo(
    () => clips.filter((clip) => clip.episode_id === id),
    [clips, id],
  )

  useEffect(() => {
    if (!episodes.length) loadEpisodes()
    loadClips(id)
    loadTranscript(id)
  }, [id, episodes.length, loadEpisodes, loadClips, loadTranscript])

  // Poll the episode while a transcription job is in flight.
  useEffect(() => {
    const status = String(episode?.status || '').toLowerCase()
    if (!POLL_STATUSES.includes(status)) return undefined
    const timer = window.setInterval(async () => {
      const fresh = await refreshEpisode(id)
      if (fresh && !POLL_STATUSES.includes(String(fresh.status).toLowerCase())) {
        loadTranscript(id, true)
      }
    }, 3000)
    return () => window.clearInterval(timer)
  }, [episode?.status, id, refreshEpisode, loadTranscript])

  const handleSeek = useCallback((seconds) => {
    const player = videoRef.current
    if (player) {
      player.currentTime = seconds
      player.play().catch(() => {})
    }
  }, [])

  async function handleGenerate(event) {
    event.preventDefault()
    await generateClips(id, {
      clipCount: options.clipCount,
      minDuration: options.minDuration,
      maxDuration: options.maxDuration,
      focus: options.focus || null,
      useCache: options.useCache,
      captionStyle: DEFAULT_CAPTION_STYLE,
      framing: DEFAULT_FRAMING,
    })
  }

  if (!episode) {
    return (
      <div className="page">
        {loading.episodes ? (
          <div className="skeleton" style={{ height: 240 }} />
        ) : (
          <div className="empty-state">
            <div className="empty-state__icon">🔍</div>
            <h3>Episode not found</h3>
            <p>
              It may have been deleted. <Link to="/dashboard">Back to the dashboard</Link>
            </p>
          </div>
        )}
      </div>
    )
  }

  const busy = POLL_STATUSES.includes(String(episode.status).toLowerCase())
  const mediaUrl = episodeMediaUrl(episode.id)

  return (
    <div className="page stack stack--lg">
      <header className="stack stack--sm">
        <div className="row row--between">
          <div>
            <Link to="/dashboard" className="dim">← Dashboard</Link>
            <h1>{episode.title}</h1>
            <div className="row dim">
              <span className={statusBadgeClass(episode.status)}>
                {statusLabel(episode.status)}
              </span>
              <span>{formatDuration(episode.duration_seconds)}</span>
              <span>{formatFileSize(episode.file_size_bytes)}</span>
              <span>{episode.has_video_stream ? 'video' : 'audio'}</span>
              <span>{episodeClips.length} clips</span>
            </div>
          </div>
          <div className="row">
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => transcribeEpisode(id, { force: true })}
              disabled={busy}
            >
              ↻ Re-transcribe
            </button>
          </div>
        </div>
        {busy && (
          <div className="card row" style={{ borderColor: 'var(--accent)' }}>
            {transcribeStatus ? (
              <div className="stack" style={{ gap: 4, width: '100%' }}>
                <div className="progress" aria-label="Transcription progress">
                  <div
                    className="progress__bar"
                    style={{ width: `${transcribeStatus.progress || 4}%` }}
                  />
                </div>
                <span className="dim" style={{ fontSize: '0.72rem' }}>
                  {transcribeStatus.message || 'Transcribing…'} ·{' '}
                  {transcribeStatus.progress || 0}%
                </span>
              </div>
            ) : (
              <span>Local ASR job running — this page updates automatically.</span>
            )}
          </div>
        )}
        {episode.error_message && <div className="field__error">{episode.error_message}</div>}
      </header>

      <section className="episode-grid">
        <div className="stack">
          {episode.has_video_stream ? (
            <video
              ref={videoRef}
              className="episode-player"
              controls
              preload="metadata"
              src={mediaUrl}
            />
          ) : (
            <audio
              ref={videoRef}
              controls
              preload="metadata"
              src={mediaUrl}
              style={{ width: '100%' }}
            />
          )}

          <TranscriptViewer transcript={transcript} onSeek={handleSeek} />
        </div>

        <aside className="stack">
          <form onSubmit={handleGenerate} className="card stack">
            <h3>Generate highlights</h3>
            <p className="dim" style={{ margin: 0 }}>
              Flan-T5 scores every candidate window locally. No transcript → no clips.
            </p>

            <div className="field">
              <label className="field__label" htmlFor="gen-count">
                Number of clips: {options.clipCount}
              </label>
              <input
                id="gen-count"
                type="range"
                min="1"
                max="12"
                value={options.clipCount}
                onChange={(event) =>
                  setOptions((c) => ({ ...c, clipCount: Number(event.target.value) }))
                }
              />
            </div>

            <div className="grid grid--2">
              <div className="field">
                <label className="field__label" htmlFor="gen-min">Min length (s)</label>
                <input
                  id="gen-min"
                  className="input"
                  type="number"
                  min="5"
                  max="120"
                  value={options.minDuration}
                  onChange={(event) =>
                    setOptions((c) => ({ ...c, minDuration: Number(event.target.value) }))
                  }
                />
              </div>
              <div className="field">
                <label className="field__label" htmlFor="gen-max">Max length (s)</label>
                <input
                  id="gen-max"
                  className="input"
                  type="number"
                  min="10"
                  max="180"
                  value={options.maxDuration}
                  onChange={(event) =>
                    setOptions((c) => ({ ...c, maxDuration: Number(event.target.value) }))
                  }
                />
              </div>
            </div>

            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setShowAdvanced((value) => !value)}
            >
              {showAdvanced ? '− Hide' : '+ Show'} creative focus
            </button>

            {showAdvanced && (
              <div className="field">
                <label className="field__label" htmlFor="gen-focus">Focus hint</label>
                <input
                  id="gen-focus"
                  className="input"
                  value={options.focus}
                  onChange={(event) =>
                    setOptions((c) => ({ ...c, focus: event.target.value }))
                  }
                  placeholder="e.g. funniest takes, hot advice"
                />
                <span className="field__hint">
                  Optional steering prompt for the local model.
                </span>
              </div>
            )}

            <label className="row dim" style={{ cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={options.useCache}
                onChange={(event) =>
                  setOptions((c) => ({ ...c, useCache: event.target.checked }))
                }
              />
              Reuse cached highlights when available
            </label>

            <button
              type="submit"
              className="btn btn--primary btn--block"
              disabled={Boolean(highlightStatus) || !transcript}
            >
              {highlightStatus && <span className="btn__spinner" />}
              {highlightStatus ? 'Scoring transcript…' : '✨ Generate clips'}
            </button>
            {highlightStatus && (
              <div className="stack" style={{ gap: 4 }}>
                <div className="progress" aria-label="Clip generation progress">
                  <div
                    className="progress__bar"
                    style={{ width: `${highlightStatus.progress || 4}%` }}
                  />
                </div>
                <span className="dim" style={{ fontSize: '0.72rem' }}>
                  {highlightStatus.message || 'Scoring transcript…'} ·{' '}
                  {highlightStatus.progress || 0}%
                </span>
              </div>
            )}
            {!transcript && !busy && (
              <span className="field__hint">
                Transcribe this episode first to unlock highlight generation.
              </span>
            )}
          </form>

          <ModelInfoCard />
        </aside>
      </section>

      <section className="stack stack--sm">
        <h2>Clips ({episodeClips.length})</h2>
        {episodeClips.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state__icon">✂️</div>
            <h3>No clips yet</h3>
            <p>Generate highlights to populate this shelf.</p>
          </div>
        ) : (
          <div className="grid grid--clips">
            {episodeClips
              .slice()
              .sort((a, b) => b.virality_score - a.virality_score)
              .map((clip) => (
                <ClipCard key={clip.id} clip={clip} />
              ))}
          </div>
        )}
      </section>
    </div>
  )
}

function ModelInfoCard() {
  const [models, setModels] = useState(null)
  useEffect(() => {
    healthApi.models().then(setModels).catch(() => setModels(null))
  }, [])

  return (
    <div className="card stack stack--sm">
      <h3>Local models</h3>
      {!models ? (
        <span className="dim">loading model info…</span>
      ) : (
        <>
          <div className="row row--between dim">
            <span>ASR</span>
            <code>{models.asr_model}</code>
          </div>
          <div className="row row--between dim">
            <span>Highlights</span>
            <code>{models.highlight_model}</code>
          </div>
          <span className="dim" style={{ fontSize: '0.76rem' }}>
            Both checkpoints run on-device via PyTorch pipelines — transcripts never leave the
            machine.
          </span>
        </>
      )}
    </div>
  )
}