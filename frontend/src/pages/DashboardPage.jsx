/**
 * DashboardPage - upload an episode, browse the library, launch the pipeline.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useClips } from '../context/ClipContext'
import { useAuth } from '../context/AuthContext'
import { healthApi } from '../api/jobs'
import EpisodeCard from '../components/EpisodeCard'
import UploadPanel from '../components/UploadPanel'
import {
  formatDuration,
  formatFileSize,
  statusBadgeClass,
  statusLabel,
} from '../utils/format'

function StatusDot({ healthy }) {
  return <span className={`status-dot ${healthy ? 'status-dot--ok' : 'status-dot--down'}`} />
}

export default function DashboardPage() {
  const { user } = useAuth()
  const {
    episodes,
    clips,
    loading,
    loadEpisodes,
    loadClips,
    transcribeEpisode,
    transcribeProgress,
    deleteEpisode,
  } = useClips()

  const [health, setHealth] = useState(null)

  useEffect(() => {
    loadEpisodes()
    loadClips()
    healthApi.status().then(setHealth).catch(() => setHealth(null))
  }, [loadEpisodes, loadClips])

  const totalClips = episodes.reduce((sum, episode) => sum + (episode.clip_count || 0), 0)
  const totalDuration = episodes.reduce((sum, episode) => sum + (episode.duration_seconds || 0), 0)
  const renderedClips = clips.filter((clip) => clip.output_path).length

  return (
    <div className="page stack stack--lg">
      <header className="row row--between">
        <div>
          <h1>Dashboard</h1>
          <p className="muted">
            Welcome back{user?.full_name ? `, ${user.full_name.split(' ')[0]}` : ''} — here is your
            local clipper workspace.
          </p>
        </div>
        <div className="row dim">
          <StatusDot healthy={Boolean(health?.database)} /> API
          <StatusDot healthy={Boolean(health?.redis)} /> Redis
          <StatusDot healthy={Boolean(health?.database)} /> Postgres
        </div>
      </header>

      <section className="grid grid--4">
        <div className="stat">
          <div className="stat__label">Episodes</div>
          <div className="stat__value">{episodes.length}</div>
        </div>
        <div className="stat">
          <div className="stat__label">Clips</div>
          <div className="stat__value">{totalClips}</div>
          <div className="stat__meta">{renderedClips} rendered</div>
        </div>
        <div className="stat">
          <div className="stat__label">Library length</div>
          <div className="stat__value">{formatDuration(totalDuration)}</div>
        </div>
        <div className="stat">
          <div className="stat__label">Storage</div>
          <div className="stat__value">
            {formatFileSize(
              episodes.reduce((sum, episode) => sum + (episode.file_size_bytes || 0), 0),
            )}
          </div>
        </div>
      </section>

      <UploadPanel />

      <section className="stack stack--sm">
        <div className="row row--between">
          <h2>Your episodes</h2>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => loadEpisodes()}
            disabled={loading.episodes}
          >
            ↻ Refresh
          </button>
        </div>

        {loading.episodes && episodes.length === 0 ? (
          <div className="grid grid--2">
            <div className="skeleton" style={{ height: 140 }} />
            <div className="skeleton" style={{ height: 140 }} />
          </div>
        ) : episodes.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state__icon">🎧</div>
            <h3>No episodes yet</h3>
            <p>Upload an MP3, WAV or MP4 above and the local pipeline takes it from there.</p>
          </div>
        ) : (
          <div className="grid grid--2">
            {episodes.map((episode) => (
              <EpisodeCard
                key={episode.id}
                episode={episode}
                transcribeStatus={transcribeProgress[episode.id]}
                onTranscribe={() => transcribeEpisode(episode.id)}
                onDelete={() => deleteEpisode(episode.id)}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}