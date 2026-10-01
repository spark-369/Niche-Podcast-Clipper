/**
 * HomePage - public landing page explaining the local-AI pipeline.
 */

import { Link } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

const FEATURES = [
  {
    icon: '🎙️',
    title: 'Local lightweight ASR',
    body: 'openai/whisper-base runs through a Hugging Face pipeline on your CPU — timestamps included, nothing leaves the box.',
  },
  {
    icon: '🤖',
    title: 'On-premise highlights',
    body: 'google/flan-t5-base scores every window of the transcript for hook, emotion and quotability. Zero API fees.',
  },
  {
    icon: '🔐',
    title: 'JWT secured',
    body: 'Access + refresh tokens with rotation and a Redis blacklist. OAuth2 password flow, protected routes throughout.',
  },
  {
    icon: '⚡',
    title: 'Redis queues & cache',
    body: 'Celery workers execute transcription and rendering; Redis caches highlights, users and rate-limit windows.',
  },
  {
    icon: '✍️',
    title: 'Auto-captions',
    body: 'Word-by-word karaoke subtitles in TikTok, Hormozi or minimal styles — rendered by OpenCV draw calls.',
  },
  {
    icon: '📱',
    title: '9:16 auto-framing',
    body: 'Pure Python OpenCV crop-and-zoom keeps the speaker centred. Blur-fill background, no FFmpeg binary.',
  },
]

const PIPELINE = [
  { step: '01', label: 'Upload', detail: 'MP3, WAV, MP4 or MOV into ./media_storage' },
  { step: '02', label: 'Transcribe', detail: 'Whisper timestamps every word locally' },
  { step: '03', label: 'Score', detail: 'Flan-T5 surfaces the most viral windows' },
  { step: '04', label: 'Render', detail: 'OpenCV burns captions into a 1080×1920 MP4' },
]

export default function HomePage() {
  const { isAuthenticated } = useAuth()

  return (
    <div className="page stack stack--lg">
      <section className="hero-block">
        <div className="stack">
          <span className="badge">100% on-premise · no cloud keys</span>
          <h1>Turn long podcasts into viral vertical clips.</h1>
          <p className="hero-block__sub">
            Whisper + Flan-T5 run locally behind a FastAPI backend. Redis queues the jobs,
            OpenCV cuts the frames, and your media never leaves this machine.
          </p>
          <div className="row">
            {isAuthenticated ? (
              <Link to="/dashboard" className="btn btn--primary">
                Open dashboard →
              </Link>
            ) : (
              <>
                <Link to="/register" className="btn btn--primary">
                  Start clipping — free
                </Link>
                <Link to="/login" className="btn btn--ghost">
                  Sign in
                </Link>
              </>
            )}
            <a
              href="http://localhost:8000/docs"
              target="_blank"
              rel="noreferrer"
              className="btn btn--ghost"
            >
              Browse the API
            </a>
          </div>
        </div>
      </section>

      <section className="grid grid--3">
        {FEATURES.map((feature) => (
          <article key={feature.title} className="card card--interactive">
            <div className="card__icon">{feature.icon}</div>
            <h3>{feature.title}</h3>
            <p className="dim">{feature.body}</p>
          </article>
        ))}
      </section>

      <section className="stack">
        <h2>The pipeline</h2>
        <div className="grid grid--4">
          {PIPELINE.map((item) => (
            <article key={item.step} className="stat">
              <div className="stat__label">Step {item.step}</div>
              <div className="stat__value">{item.label}</div>
              <div className="stat__meta">{item.detail}</div>
            </article>
          ))}
        </div>
      </section>

      <section className="card center">
        <h2>Ready to ship your first clip?</h2>
        <p>Everything runs on localhost: Postgres, Redis, Celery, Whisper, Flan-T5, OpenCV.</p>
        <Link to={isAuthenticated ? '/dashboard' : '/register'} className="btn btn--primary">
          {isAuthenticated ? 'Go to dashboard' : 'Create your account'}
        </Link>
      </section>
    </div>
  )
}