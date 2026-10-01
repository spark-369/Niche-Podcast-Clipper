/**
 * ClipContext - global state for episodes, clips, render jobs and the
 * caption/framing editors. Uses only React Context + hooks (no Redux).
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import clipsApi from '../api/clips'
import episodesApi from '../api/episodes'
import jobsApi from '../api/jobs'
import { describeError } from '../api/client'
import { useToast } from './ToastContext'

export const DEFAULT_CAPTION_STYLE = {
  preset: 'tiktok',
  font_size: 72,
  font_family: 'DejaVuSans-Bold',
  text_color: '#FFFFFF',
  highlight_color: '#FF2D55',
  outline_color: '#000000',
  background_color: null,
  background_opacity: 0,
  position: 'center',
  uppercase: true,
  words_per_line: 3,
  max_lines: 2,
  outline_width: 4,
  shadow: true,
  pop_scale: 1.2,
  bottom_margin: 320,
}

export const DEFAULT_FRAMING = {
  mode: 'auto_face',
  focus_x: 0.5,
  focus_y: 0.4,
  zoom: 1,
  smooth_tracking: true,
  blur_background: true,
  background_blur_strength: 41,
  output_width: 1080,
  output_height: 1920,
  fps: 30,
  burn_captions: true,
  crf: 23,
}

// Render polling. Rendering a clip on CPU is slow (tens of seconds for a short
// clip), so an awaited render is polled rather than held open in one long
// request. A job that merely takes longer than the budget is reported as
// `timeout` and the caller keeps the previous render on screen.
const RENDER_POLL_MS = 3000
const RENDER_TIMEOUT_MS = 10 * 60 * 1000

// Transcription polling. Whisper on CPU is slow, so the dashboard polls the
// job mirror (percent + stage message) instead of holding a request open.
const TRANSCRIBE_POLL_MS = 2000
// Matches the backend's 6-hour "active job" staleness window in the
// /episodes/{id}/transcribe guard, so both sides agree on how long a
// transcription may legitimately run before it is considered abandoned.
const TRANSCRIBE_TIMEOUT_MS = 6 * 60 * 60 * 1000
// Same window, used client-side when deciding whether a job found after a
// page refresh is still worth resuming.
const TRANSCRIBE_STALE_MS = 6 * 60 * 60 * 1000

const ClipContext = createContext(null)

export function ClipProvider({ children }) {
  const toast = useToast()

  const [episodes, setEpisodes] = useState([])
  const [clips, setClips] = useState([])
  const [presets, setPresets] = useState([])

  const [activeEpisodeId, setActiveEpisodeId] = useState(null)
  const [activeClipId, setActiveClipId] = useState(null)

  const [transcripts, setTranscripts] = useState({})
  const [jobs, setJobs] = useState({})

  const [loading, setLoading] = useState({ episodes: false, clips: false })
  const [uploadProgress, setUploadProgress] = useState(0)
  const [generating, setGenerating] = useState(false)

  // Live Celery progress per clip, keyed by clip id:
  // `{ progress: 0-100, status, message }`.
  //
  // Every poll tick of a render writes here so components can show the worker's
  // own percentage and stage message ("Burning captions…", "Encoding H.264")
  // while the job runs in the background. `clip.progress` alone is not enough:
  // it is only refreshed when the shared `clips` array is updated, and it
  // carries no message.
  const [renderProgress, setRenderProgress] = useState({})

  // Live transcription progress per episode, keyed by episode id:
  // `{ progress: 0-100, status, message }` for the dashboard progress bars.
  const [transcribeProgress, setTranscribeProgress] = useState({})

  // Live highlight-extraction progress per episode, keyed by episode id:
  // `{ progress: 0-100, status, message }` for the episode page progress bar.
  const [highlightProgress, setHighlightProgress] = useState({})

  const pollTimers = useRef({})
  // Guards against two poll loops racing for the same transcription.
  const transcribePolls = useRef(new Set())
  // Same guard for highlight-extraction poll loops.
  const highlightPolls = useRef(new Set())

  /** Refresh the episode list. */
  const loadEpisodes = useCallback(async () => {
    setLoading((current) => ({ ...current, episodes: true }))
    try {
      const data = await episodesApi.list({ limit: 100 })
      setEpisodes(data)
      return data
    } catch (error) {
      toast.error(describeError(error, 'Could not load episodes'))
      return []
    } finally {
      setLoading((current) => ({ ...current, episodes: false }))
    }
  }, [toast])

  /** Refresh clips, optionally scoped to one episode. */
  const loadClips = useCallback(
    async (episodeId) => {
      setLoading((current) => ({ ...current, clips: true }))
      try {
        const data = await clipsApi.list({ episodeId, limit: 200 })
        setClips(data)
        return data
      } catch (error) {
        toast.error(describeError(error, 'Could not load clips'))
        return []
      } finally {
        setLoading((current) => ({ ...current, clips: false }))
      }
    },
    [toast],
  )

  const loadPresets = useCallback(async () => {
    try {
      const data = await clipsApi.presets()
      setPresets(data)
    } catch {
      setPresets([])
    }
  }, [])

  const uploadEpisode = useCallback(
    async (file, options = {}) => {
      setUploadProgress(0)
      try {
        const result = await episodesApi.upload(file, options, setUploadProgress)
        setEpisodes((current) => [result.episode, ...current])
        toast.success(`"${result.episode.title}" uploaded`)
        return result.episode
      } catch (error) {
        toast.error(describeError(error, 'Upload failed'))
        return null
      } finally {
        setUploadProgress(0)
      }
    },
    [toast],
  )

  const deleteEpisode = useCallback(
    async (episodeId) => {
      try {
        await episodesApi.remove(episodeId)
        setEpisodes((current) => current.filter((item) => item.id !== episodeId))
        setClips((current) => current.filter((item) => item.episode_id !== episodeId))
        toast.success('Episode deleted')
        return true
      } catch (error) {
        toast.error(describeError(error, 'Could not delete the episode'))
        return false
      }
    },
    [toast],
  )

  const refreshEpisode = useCallback(async (episodeId) => {
    try {
      const fresh = await episodesApi.get(episodeId)
      setEpisodes((current) =>
        current.map((item) => (item.id === episodeId ? fresh : item)),
      )
      return fresh
    } catch {
      return null
    }
  }, [])

  /**
   * Poll a transcription job mirror until it settles, mirroring the worker's
   * progress percent and stage message into `transcribeProgress[episodeId]`.
   * On success the episode list is refreshed so the card flips to
   * "transcribed"; on failure the worker's error message surfaces in a toast.
   */
  const pollTranscription = useCallback(
    async (episodeId, jobId) => {
      if (transcribePolls.current.has(episodeId)) return
      transcribePolls.current.add(episodeId)

      const deadline = Date.now() + TRANSCRIBE_TIMEOUT_MS
      let settled = null
      try {
        while (Date.now() < deadline) {
          let job = null
          try {
            job = await jobsApi.get(jobId)
          } catch (error) {
            // The job row is cascade-deleted with its episode; a 404 simply
            // ends the polling loop. Any other error is transient — wait a
            // tick and keep polling so one failed request doesn't freeze the
            // progress bar.
            if (error?.response?.status === 404) break
            await new Promise((resolve) => window.setTimeout(resolve, TRANSCRIBE_POLL_MS))
            continue
          }

          const status = {
            progress: job.progress ?? 0,
            status: job.status,
            message: job.message || job.error_message || 'Transcribing…',
          }
          setTranscribeProgress((current) => ({
            ...current,
            [episodeId]: status,
          }))

          if (['success', 'failure'].includes(job.status)) {
            settled = status
            break
          }
          await new Promise((resolve) => window.setTimeout(resolve, TRANSCRIBE_POLL_MS))
        }
      } finally {
        transcribePolls.current.delete(episodeId)
        setTranscribeProgress((current) => {
          const next = { ...current }
          delete next[episodeId]
          return next
        })
      }

      if (settled?.status === 'success') {
        await refreshEpisode(episodeId)
        toast.success('Transcription complete')
      } else if (settled?.status === 'failure') {
        await refreshEpisode(episodeId)
        toast.error(settled.message || 'Transcription failed')
      }
      // A `null` settled value means the poll hit its timeout; the entry is
      // already cleared, so the user can simply click Transcribe again.
    },
    [refreshEpisode, toast],
  )

  /**
   * Re-attach transcription progress polling after a page refresh: find
   * transcription jobs that are still pending/running (and recent enough not
   * to be leftovers from a crashed worker) and resume their poll loops, so
   * the progress bars are consistent with what the worker is really doing
   * without requiring a fresh click.
   */
  const resumeTranscriptionPolling = useCallback(async () => {
    let activeJobs = []
    try {
      activeJobs = await jobsApi.list({ jobType: 'transcription', limit: 100 })
    } catch {
      // Jobs list unavailable; progress simply won't resume this session.
      return
    }
    const staleBefore = Date.now() - TRANSCRIBE_STALE_MS
    const seen = new Set()
    activeJobs.forEach((job) => {
      if (!job.episode_id || seen.has(job.episode_id)) return
      if (!['pending', 'running'].includes(job.status)) return
      if (new Date(job.created_at).getTime() < staleBefore) return
      seen.add(job.episode_id)
      pollTranscription(job.episode_id, job.id)
    })
  }, [pollTranscription])

  /**
   * Poll a highlight-extraction job mirror until it settles, publishing the
   * worker's percent/stage into `highlightProgress[episodeId]`. On success the
   * episode's clips are reloaded (the worker replaces them) and the episode
   * refreshed; on failure the error message surfaces in a toast.
   */
  const pollHighlightProgress = useCallback(
    async (episodeId, jobId) => {
      if (highlightPolls.current.has(episodeId)) return
      highlightPolls.current.add(episodeId)

      const deadline = Date.now() + TRANSCRIBE_TIMEOUT_MS
      let settled = null
      try {
        while (Date.now() < deadline) {
          let job = null
          try {
            job = await jobsApi.get(jobId)
          } catch (error) {
            // A deleted episode cascades to its job rows; anything else is
            // transient and the loop keeps going.
            if (error?.response?.status === 404) break
            await new Promise((resolve) => window.setTimeout(resolve, TRANSCRIBE_POLL_MS))
            continue
          }

          const status = {
            progress: job.progress ?? 0,
            status: job.status,
            message: job.message || job.error_message || 'Scoring transcript…',
          }
          setHighlightProgress((current) => ({
            ...current,
            [episodeId]: status,
          }))

          if (['success', 'failure'].includes(job.status)) {
            settled = status
            break
          }
          await new Promise((resolve) => window.setTimeout(resolve, TRANSCRIBE_POLL_MS))
        }
      } finally {
        highlightPolls.current.delete(episodeId)
        setHighlightProgress((current) => {
          const next = { ...current }
          delete next[episodeId]
          return next
        })
      }

      if (settled?.status === 'success') {
        await loadClips(episodeId)
        await refreshEpisode(episodeId)
        toast.success('Clips created from the transcript')
      } else if (settled?.status === 'failure') {
        await refreshEpisode(episodeId)
        toast.error(settled.message || 'Highlight extraction failed')
      }
    },
    [loadClips, refreshEpisode, toast],
  )

  /**
   * Re-attach highlight polling after a page refresh: find extraction jobs
   * that are still pending/running (and recent enough not to be leftovers of
   * a crashed worker) and resume their poll loops.
   */
  const resumeHighlightPolling = useCallback(async () => {
    let activeJobs = []
    try {
      activeJobs = await jobsApi.list({ jobType: 'highlight_extraction', limit: 100 })
    } catch {
      return
    }
    const staleBefore = Date.now() - TRANSCRIBE_STALE_MS
    const seen = new Set()
    activeJobs.forEach((job) => {
      if (!job.episode_id || seen.has(job.episode_id)) return
      if (!['pending', 'running'].includes(job.status)) return
      if (new Date(job.created_at).getTime() < staleBefore) return
      seen.add(job.episode_id)
      pollHighlightProgress(job.episode_id, job.id)
    })
  }, [pollHighlightProgress])

  const transcribeEpisode = useCallback(
    async (episodeId, options = {}) => {
      try {
        const result = await episodesApi.transcribe(episodeId, options)
        setEpisodes((current) =>
          current.map((item) =>
            item.id === episodeId ? { ...item, status: result.status } : item,
          ),
        )
        toast.info(result.message)
        // Track worker progress in the background; never blocks the caller.
        if (result.job_id) pollTranscription(episodeId, result.job_id)
        return result
      } catch (error) {
        toast.error(describeError(error, 'Could not start transcription'))
        return null
      }
    },
    [pollTranscription, toast],
  )

  const loadTranscript = useCallback(
    async (episodeId, force = false) => {
      if (!force && transcripts[episodeId]) return transcripts[episodeId]
      try {
        const data = await episodesApi.transcript(episodeId)
        setTranscripts((current) => ({ ...current, [episodeId]: data }))
        return data
      } catch (error) {
        if (error?.response?.status !== 404) {
          toast.error(describeError(error, 'Could not load the transcript'))
        }
        return null
      }
    },
    [transcripts, toast],
  )

  const generateClips = useCallback(
    async (episodeId, options = {}) => {
      try {
        const result = await clipsApi.generate(episodeId, options)
        // The worker REPLACES this episode's clips when it finishes; drop the
        // stale ones from the grid right away so the list reflects what is
        // coming instead of mixing old and new.
        setClips((current) =>
          current.filter((clip) => clip.episode_id !== episodeId),
        )
        toast.info(result.message || 'Highlight extraction queued')
        // Track the Celery job in the background; never blocks the caller.
        if (result.job_id) pollHighlightProgress(episodeId, result.job_id)
        return result
      } catch (error) {
        toast.error(describeError(error, 'Could not start highlight extraction'))
        return null
      }
    },
    [pollHighlightProgress, toast],
  )

  const updateClip = useCallback(
    async (clipId, payload) => {
      try {
        const updated = await clipsApi.update(clipId, payload)
        setClips((current) =>
          current.map((clip) => (clip.id === clipId ? updated : clip)),
        )
        return updated
      } catch (error) {
        // Re-throw so the caller can show the API's own `detail` (for example
        // "No transcript is stored for this clip yet.") rather than a generic
        // "Could not save the clip" toast.
        toast.error(describeError(error, 'Could not save the clip'))
        throw error
      }
    },
    [toast],
  )

  /**
   * Dispatch a plain render and poll it until it settles.
   *
   * `renderClip` above is fire-and-forget: it schedules its own polling timers
   * and returns only the dispatch acknowledgement, so a caller cannot tell when
   * the new MP4 actually exists. The editor's "Save changes" needs to know that,
   * because the PATCH it performs deletes the previous render — until the worker
   * writes the new one there is no playable file at all.
   *
   * It awaits the dispatched job to completion and returns the settled status
   * (`{ status: 'success' | ... }`), or `{ status: 'timeout' }` when the deadline
   * passes, or null when the dispatch request itself failed.
   */
  const waitForClipRender = useCallback(
    async (clipId, { captionStyle, framing, transcript, priority = 5 } = {}) => {
      let dispatch
      try {
        dispatch = await clipsApi.render(clipId, {
          captionStyle,
          framing,
          transcript,
          priority,
        })
      } catch (error) {
        toast.error(describeError(error, 'Could not start the render'))
        return null
      }

      setClips((current) =>
        current.map((clip) =>
          clip.id === clipId ? { ...clip, status: 'rendering', progress: 0 } : clip,
        ),
      )
      setJobs((current) => ({ ...current, [clipId]: dispatch }))
      setRenderProgress((current) => ({
        ...current,
        [clipId]: {
          progress: 0,
          status: 'pending',
          message: dispatch?.message || 'Queued for rendering',
        },
      }))

      const deadline = Date.now() + RENDER_TIMEOUT_MS
      let settled = null
      while (Date.now() < deadline) {
        await new Promise((resolve) => window.setTimeout(resolve, RENDER_POLL_MS))
        let status
        try {
          status = await clipsApi.status(clipId)
        } catch {
          // A transient status failure is not fatal; keep polling until the
          // deadline rather than reporting a broken render.
          continue
        }
        setJobs((current) => ({ ...current, [clipId]: status }))
        // Surface the worker's own progress + stage message to the UI.
        setRenderProgress((current) => ({
          ...current,
          [clipId]: {
            progress: Number(status?.progress ?? 0),
            status: String(status?.status || 'running').toLowerCase(),
            message: status?.message || '',
          },
        }))

        const state = String(status?.status || '').toLowerCase()
        if (['success', 'succeeded'].includes(state)) {
          // Pull the finished clip so `output_path`/`preview_url` land in the
          // shared state, which remounts the preview <video> onto the new file.
          try {
            const fresh = await clipsApi.get(clipId)
            setClips((current) =>
              current.map((clip) => (clip.id === clipId ? fresh : clip)),
            )
          } catch (error) {
            toast.error(describeError(error, 'Render finished but the clip could not be reloaded'))
          }
          settled = { ...status, status: state }
          break
        }

        if (['failure', 'failed', 'error', 'revoked'].includes(state)) {
          setClips((current) =>
            current.map((clip) =>
              clip.id === clipId
                ? { ...clip, status: 'failed', error_message: status.error_message }
                : clip,
            ),
          )
          settled = { ...status, status: state }
          break
        }

        // Mirror the live progress onto the clip so the editor's progress bar
        // tracks the worker instead of sitting at 0 until it finishes.
        setClips((current) =>
          current.map((clip) =>
            clip.id === clipId ? { ...clip, progress: status.progress ?? clip.progress } : clip,
          ),
        )
      }

      // Clear the live entry once the job settles; the clip's own `progress`
      // field carries the final state from here on.
      setRenderProgress((current) => {
        const next = { ...current }
        delete next[clipId]
        return next
      })

      if (settled === null) {
        return { status: 'timeout' }
      }
      return settled
    },
    [toast],
  )

  const deleteClip = useCallback(
    async (clipId) => {
      try {
        await clipsApi.remove(clipId)
        setClips((current) => current.filter((clip) => clip.id !== clipId))
        toast.success('Clip deleted')
        return true
      } catch (error) {
        toast.error(describeError(error, 'Could not delete the clip'))
        return false
      }
    },
    [toast],
  )

  /** Dispatch a render job and poll its progress until it settles. */
  const renderClip = useCallback(
    async (clipId, options = {}) => {
      try {
        const dispatch = await clipsApi.render(clipId, options)
        setClips((current) =>
          current.map((clip) =>
            clip.id === clipId ? { ...clip, status: 'rendering', progress: 0 } : clip,
          ),
        )
        setJobs((current) => ({ ...current, [clipId]: dispatch }))
        setRenderProgress((current) => ({
          ...current,
          [clipId]: {
            progress: 0,
            status: 'pending',
            message: dispatch?.message || 'Queued for OpenCV rendering',
          },
        }))
        toast.info('Render job dispatched to the Celery worker')

        const poll = async () => {
          try {
            const status = await clipsApi.status(clipId)
            setJobs((current) => ({ ...current, [clipId]: status }))
            setClips((current) =>
              current.map((clip) =>
                clip.id === clipId ? { ...clip, progress: status.progress } : clip,
              ),
            )
            // Publish the worker's own percentage + stage message so every view of
            // this clip (detail page, cards, editor) shows live progress rather
            // than only the last-polled `clip.progress`.
            setRenderProgress((current) => ({
              ...current,
              [clipId]: {
                progress: Number(status?.progress ?? 0),
                status: String(status?.status || 'running').toLowerCase(),
                message: status?.message || '',
              },
            }))

            if (status.status === 'success') {
              const fresh = await clipsApi.get(clipId)
              setClips((current) =>
                current.map((clip) => (clip.id === clipId ? fresh : clip)),
              )
              setRenderProgress((current) => {
                const next = { ...current }
                delete next[clipId]
                return next
              })
              toast.success('Clip rendered successfully')
              return
            }
            if (status.status === 'failure') {
              setClips((current) =>
                current.map((clip) =>
                  clip.id === clipId
                    ? { ...clip, status: 'failed', error_message: status.error_message }
                    : clip,
                ),
              )
              setRenderProgress((current) => {
                const next = { ...current }
                delete next[clipId]
                return next
              })
              toast.error(status.error_message || 'Rendering failed')
              return
            }
            pollTimers.current[clipId] = window.setTimeout(poll, 2500)
          } catch {
            pollTimers.current[clipId] = window.setTimeout(poll, 4000)
          }
        }

        pollTimers.current[clipId] = window.setTimeout(poll, 1800)
        return dispatch
      } catch (error) {
        toast.error(describeError(error, 'Could not dispatch the render job'))
        return null
      }
    },
    [toast],
  )

  /**
   * Re-attach to a render that is already running for this clip.
   *
   * The in-memory await loops inside `waitForClipRender` die when the
   * tab is refreshed or the component unmounts, but the Celery job keeps going
   * server-side. On a fresh page load the clip comes back with
   * `status === 'rendering'`, and without this the editor would show no progress
   * at all — the job would finish unseen and the user would have to reload again.
   *
   * Unlike `waitForClipRender` this does **not** dispatch anything: it only polls
   * the existing job to completion, publishing progress into `renderProgress`.
   * Returns the settled status, or null when the clip is not actually rendering.
   */
  const resumeClipRender = useCallback(
    async (clipId) => {
      const deadline = Date.now() + RENDER_TIMEOUT_MS
      let settled = null

      setRenderProgress((current) => ({
        ...current,
        [clipId]: { progress: 0, status: 'running', message: '' },
      }))

      while (Date.now() < deadline) {
        let status
        try {
          status = await clipsApi.status(clipId)
        } catch {
          // No job row yet, or a transient failure: keep trying until the
          // deadline instead of reporting a broken render.
          await new Promise((resolve) => window.setTimeout(resolve, RENDER_POLL_MS))
          continue
        }
        setJobs((current) => ({ ...current, [clipId]: status }))
        setRenderProgress((current) => ({
          ...current,
          [clipId]: {
            progress: Number(status?.progress ?? 0),
            status: String(status?.status || 'running').toLowerCase(),
            message: status?.message || '',
          },
        }))

        const state = String(status?.status || '').toLowerCase()
        if (['success', 'succeeded'].includes(state)) {
          try {
            const fresh = await clipsApi.get(clipId)
            setClips((current) =>
              current.map((clip) => (clip.id === clipId ? fresh : clip)),
            )
          } catch (error) {
            toast.error(describeError(error, 'Render finished but the clip could not be reloaded'))
          }
          settled = { ...status, status: state }
          break
        }

        if (['failure', 'failed', 'error', 'revoked'].includes(state)) {
          setClips((current) =>
            current.map((clip) =>
              clip.id === clipId
                ? { ...clip, status: 'failed', error_message: status.error_message }
                : clip,
            ),
          )
          settled = { ...status, status: state }
          break
        }

        setClips((current) =>
          current.map((clip) =>
            clip.id === clipId ? { ...clip, progress: status.progress ?? clip.progress } : clip,
          ),
        )
        await new Promise((resolve) => window.setTimeout(resolve, RENDER_POLL_MS))
      }

      setRenderProgress((current) => {
        const next = { ...current }
        delete next[clipId]
        return next
      })

      if (settled === null) return { status: 'timeout' }
      return settled
    },
    [toast],
  )

  // Load caption presets once on mount.
  useEffect(() => {
    loadPresets()
  }, [loadPresets])
  // Resume transcription AND highlight progress polling once per page load so
  // the bars stay consistent with in-flight jobs after a refresh, on any route.
  useEffect(() => {
    resumeTranscriptionPolling()
    resumeHighlightPolling()
  }, [resumeTranscriptionPolling, resumeHighlightPolling])
  // Clean up any polling timers on unmount.
  useEffect(
    () => () => {
      Object.values(pollTimers.current).forEach((timer) => window.clearTimeout(timer))
      pollTimers.current = {}
    },
    [],
  )

  const value = useMemo(
    () => ({
      episodes,
      clips,
      presets,
      transcripts,
      jobs,
      renderProgress,
      transcribeProgress,
      highlightProgress,
      loading,
      uploadProgress,
      generating,
      activeEpisodeId,
      activeClipId,
      setActiveEpisodeId,
      setActiveClipId,
      loadEpisodes,
      loadClips,
      loadPresets,
      loadTranscript,
      uploadEpisode,
      deleteEpisode,
      refreshEpisode,
      transcribeEpisode,
      resumeTranscriptionPolling,
      resumeHighlightPolling,
      generateClips,
      updateClip,
      deleteClip,
      renderClip,
      waitForClipRender,
      resumeClipRender,
      status: clipsApi.status,
      fetchClip: clipsApi.get,
      downloadClip: clipsApi.download,
      previewFrame: clipsApi.previewFrame,
      captionTimeline: clipsApi.captionTimeline,
      clipJobs: clipsApi.jobs,
      clipDownloadUrl: clipsApi.downloadUrl,
      clipThumbnailUrl: clipsApi.thumbnailUrl,
      clipPreviewUrl: clipsApi.previewVideoUrl,
      episodeMediaUrl: episodesApi.mediaUrl,
      jobsApi,
    }),
    [
      episodes,
      clips,
      presets,
      transcripts,
      jobs,
      renderProgress,
      transcribeProgress,
      highlightProgress,
      loading,
      uploadProgress,
      generating,
      activeEpisodeId,
      activeClipId,
      loadEpisodes,
      loadClips,
      loadPresets,
      loadTranscript,
      uploadEpisode,
      deleteEpisode,
      refreshEpisode,
      transcribeEpisode,
      resumeTranscriptionPolling,
      resumeHighlightPolling,
      generateClips,
      updateClip,
      deleteClip,
      renderClip,
      waitForClipRender,
      resumeClipRender,
    ],
  )

  return <ClipContext.Provider value={value}>{children}</ClipContext.Provider>
}

export function useClips() {
  const context = useContext(ClipContext)
  if (!context) throw new Error('useClips must be used inside a <ClipProvider>')
  return context
}

export default ClipContext

