/**
 * UploadPanel - drag & drop media upload into ./media_storage on the server.
 */

import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useClips } from '../context/ClipContext'

const ACCEPT = '.mp3,.wav,.m4a,.mp4,.mov,.mkv,.webm,audio/*,video/*'
const MAX_MB = 500

export default function UploadPanel() {
  const { uploadEpisode, uploadProgress, transcribeEpisode } = useClips()
  const navigate = useNavigate()
  const inputRef = useRef(null)

  const [file, setFile] = useState(null)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [autoTranscribe, setAutoTranscribe] = useState(true)
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)

  function pickFile(next) {
    const selected = next?.[0]
    if (!selected) return
    if (selected.size > MAX_MB * 1024 * 1024) {
      alert(`File exceeds the ${MAX_MB} MB limit`)
      return
    }
    setFile(selected)
    if (!title) setTitle(selected.name.replace(/\.[^.]+$/, ''))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    if (!file) return
    setUploading(true)
    try {
      const episode = await uploadEpisode(file, { title, description, autoTranscribe })
      setFile(null)
      setTitle('')
      setDescription('')
      if (inputRef.current) inputRef.current.value = ''
      if (episode) {
        if (!autoTranscribe) {
          navigate(`/episodes/${episode.id}`)
        } else {
          await transcribeEpisode(episode.id)
          navigate(`/episodes/${episode.id}`)
        }
      }
    } finally {
      setUploading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="card upload-panel stack">
      <h3>Upload an episode</h3>

      <div
        className={`upload-panel__dropzone ${dragging ? 'upload-panel__dropzone--active' : ''}`}
        role="button"
        tabIndex={0}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') inputRef.current?.click()
        }}
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          pickFile(event.dataTransfer.files)
        }}
      >
        {file ? (
          <div className="stack stack--sm">
            <span className="upload-panel__file-icon">🎬</span>
            <strong className="truncate">{file.name}</strong>
            <span className="dim">{(file.size / (1024 * 1024)).toFixed(1)} MB</span>
          </div>
        ) : (
          <div className="stack stack--sm">
            <span className="upload-panel__file-icon">⬆️</span>
            <strong>Drag & drop a media file</strong>
            <span className="dim">or click to browse · MP3, WAV, M4A, MP4, MOV, MKV, WebM · max {MAX_MB} MB</span>
          </div>
        )}
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="sr-only"
          onChange={(event) => pickFile(event.target.files)}
        />
      </div>

      {uploading && (
        <div className="progress" aria-label="Upload progress">
          <div className="progress__bar" style={{ width: `${uploadProgress}%` }} />
        </div>
      )}
      {uploading && <span className="dim">Uploading… {uploadProgress}%</span>}

      <div className="grid grid--2">
        <div className="field">
          <label className="field__label" htmlFor="upload-title">
            Title
          </label>
          <input
            id="upload-title"
            className="input"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Episode 42 — Building in public"
          />
        </div>
        <div className="field">
          <label className="field__label" htmlFor="upload-description">
            Description <span className="dim">(optional)</span>
          </label>
          <input
            id="upload-description"
            className="input"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="A short note about this episode"
          />
        </div>
      </div>

      <label className="row dim" style={{ cursor: 'pointer' }}>
        <input
          type="checkbox"
          checked={autoTranscribe}
          onChange={(event) => setAutoTranscribe(event.target.checked)}
        />
        Transcribe immediately with the local Whisper pipeline
      </label>

      <div className="row">
        <button type="submit" className="btn btn--primary" disabled={!file || uploading}>
          {uploading && <span className="btn__spinner" />}
          {uploading ? 'Uploading…' : 'Upload episode'}
        </button>
        {file && (
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => {
              setFile(null)
              if (inputRef.current) inputRef.current.value = ''
            }}
            disabled={uploading}
          >
            Clear
          </button>
        )}
      </div>
    </form>
  )
}