/**
 * ClipEditorPage - tweak the cut, transcript, caption style and framing of a clip.
 *
 * The preview plays the episode's source media as the moving picture and draws
 * the *draft* captions over it on a canvas (`CaptionOverlay`) — compositing
 * happens entirely in the frontend, so the preview responds instantly while
 * editing. Playing the final MP4 here instead showed two competing caption
 * tracks: the deliverable has the previous captions burned into its pixels, so
 * the moment the draft style differed from the stored render the old captions
 * were still visible underneath the new ones. The source carries no captions,
 * which leaves the canvas as the single caption track — and each clip ships
 * exactly one video (the burned deliverable) plus one thumbnail.
 *
 * **Save changes** is the single write action:
 *
 * - it always persists the whole edit — title, hook, summary, cut boundaries,
 *   caption style, framing *and* the transcript;
 * - it then re-renders the clip with the captions burned from that text, so the
 *   updated video is generated as soon as the button is pressed. The backend
 *   queues a Celery task and the editor polls the job until it settles.
 *
 * Nothing is applied automatically: typing or dragging only updates the local
 * draft (and the local caption overlay), and every change reaches the server —
 * and the rendered file — only through **Save changes**.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useClips, DEFAULT_CAPTION_STYLE, DEFAULT_FRAMING } from '../context/ClipContext'
import { describeError, isUnexpectedError } from '../api/client'
import { formatDuration, formatTimecode } from '../utils/format'
import CaptionOverlay from '../components/CaptionOverlay'

const COLOR_FIELDS = [
  ['text_color', 'Text'],
  ['highlight_color', 'Highlight'],
  ['outline_color', 'Outline'],
]

const OUTPUT_SIZES = [
  { label: '1080×1920 (9:16)', width: 1080, height: 1920 },
  { label: '720×1280 (9:16)', width: 720, height: 1280 },
  { label: '1440×2560 (9:16)', width: 1440, height: 2560 },
]

/**
 * Build the caption timeline entirely on the client, from the draft transcript
 * and the stored word timings.
 *
 * Faithful client port of the backend's `services.video.captions` timeline
 * resolution (`words_from_payload` + `group_caption_lines`), so the preview
 * captions are attached to the clip's audio exactly as the OpenCV render does:
 *
 * - If the stored word timings still describe the (possibly edited) transcript,
 *   they are rebased off the clip's in-point and used verbatim — captions light
 *   up in lockstep with the speaker's audio.
 * - If the transcript was edited (words no longer match) or no timings exist,
 *   the text is distributed evenly across the clip window.
 *
 * Everything is computed client-side, so no backend round-trip happens while
 * editing; the real word timings arrive once via the clip detail (`clip.words`).
 */
function normaliseToken(token) {
  return String(token || '')
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '')
}

function buildClientTimeline({ transcript, start, end, captionStyle, clipWords }) {
  const duration = Math.max(Number(end) - Number(start), 0)
  const wordsPerLine = Math.max(Number(captionStyle?.words_per_line) || 1, 1)
  const maxLines = Math.max(Number(captionStyle?.max_lines) || 1, 1)
  const groupSize = Math.max(wordsPerLine * maxLines, 1)
  const fallbackText = String(transcript || '')

  // 1. Rebase stored word timings onto the clip start (mirrors words_from_payload).
  let captionWords = []
  for (const item of clipWords || []) {
    const text = String(item?.word || item?.text || '').trim()
    if (!text) continue
    let wordStart = Number(item?.start || 0)
    let wordEnd = Number(item?.end || wordStart)
    if (Number.isNaN(wordStart)) wordStart = 0
    if (Number.isNaN(wordEnd)) wordEnd = wordStart
    if (wordEnd <= wordStart) wordEnd = wordStart + 0.28
    captionWords.push({
      text,
      start: Math.max(wordStart - Number(start), 0),
      end: Math.max(wordEnd - Number(start), 0),
    })
  }
  for (const word of captionWords) {
    if (word.end <= word.start) word.end = word.start + 0.28
  }

  // 2. Use stored timings only if they still describe the current transcript.
  const expected = fallbackText
    .split(/\s+/)
    .map(normaliseToken)
    .filter(Boolean)
  const actual = captionWords.map((w) => normaliseToken(w.text)).filter(Boolean)
  // An empty transcript keeps the stored timings (mirrors `_matches_fallback_text`).
  const matchesEdit =
    expected.length === 0 || expected.join(' ') === actual.join(' ')

  if (!matchesEdit) {
    // 3. Transcript edited (or no timings): distribute evenly across the clip.
    const tokens = fallbackText.split(/\s+/).filter(Boolean)
    captionWords = []
    if (tokens.length > 0) {
      const span = Math.max(duration, 0.5)
      const perWord = span / tokens.length
      captionWords = tokens.map((token, index) => ({
        text: token,
        start: Math.round(index * perWord * 1000) / 1000,
        end: Math.round((index + 1) * perWord * 1000) / 1000,
      }))
    }
  }

  // 4. Group the word stream into fixed-size caption blocks.
  const groups = []
  for (let index = 0; index < captionWords.length; index += groupSize) {
    const words = captionWords.slice(index, index + groupSize)
    groups.push({
      words,
      start: words[0].start,
      end: words[words.length - 1].end,
    })
  }

  return {
    clip_id: null,
    duration_seconds: Math.round(duration * 1000) / 1000,
    words_per_line: wordsPerLine,
    max_lines: maxLines,
    groups,
  }
}

export default function ClipEditorPage() {
  const { clipId } = useParams()
  const id = Number(clipId)

  const {
    clips,
    fetchClip,
    updateClip,
    presets,
    loadPresets,
    waitForClipRender,
    resumeClipRender,
    renderProgress,
    clipThumbnailUrl,
    episodeMediaUrl,
  } = useClips()

  const [remote, setRemote] = useState(null)
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [savedNote, setSavedNote] = useState(null)
  const [saveError, setSaveError] = useState(null)
  // The preview `<video>`. `CaptionOverlay` reads its `currentTime` on every
  // animation frame, and the cut-loop effect below keeps playback inside the
  // clip's boundaries.
  const videoRef = useRef(null)
  // The blurred background `<video>` shown behind the foreground crop when
  // `blur_background` is on. Kept in lock-step with the main video below.
  const blurVideoRef = useRef(null)

  const contextClip = useMemo(() => clips.find((item) => item.id === id) || null, [clips, id])
  const clip = contextClip || remote

  // The clip's framing options (draft wins once edited). Declared early so the
  // render-toggles and blur-background effect below can read `framing.blur_background`.
  const framing = draft?.framing || DEFAULT_FRAMING

  // The moving picture behind the draft captions.
  //
  // It is always the episode's *source* media: the editor composites the draft
  // captions client-side (CaptionOverlay), so no per-clip companion render is
  // needed — each clip ships exactly one video (the burned deliverable) and one
  // thumbnail. Playing the source here is also the fastest possible preview: it
  // is available the moment the page loads, with no render required first.
  //
  // The source's clock is absolute (0 = episode start) while the caption
  // timeline is clip-relative; `captionTimeOffset` below rebases it.
  const previewIsClipLocal = false
  const previewMediaUrl = useMemo(
    () => (clip?.episode_id ? episodeMediaUrl(clip.episode_id) : null),
    [clip?.episode_id, episodeMediaUrl],
  )

  // Rebase for the caption overlay: the caption timeline is clip-relative but
  // the preview plays the episode's source media, whose clock is absolute.
  const captionTimeOffset = previewIsClipLocal ? 0 : draft?.start

  // True when the clip itself says a render is running. This is what makes the
  // progress survive a refresh: the `await` loop in `handleSave` dies with the
  // page, but the Celery job does not, so the clip is still `rendering` when the
  // editor reloads and the overlay must come back on its own.
  const renderingOnServer = String(clip?.status || '').toLowerCase() === 'rendering'

  // Whether the preview slot should hold a <video>: true when the clip has a
  // render, and also while a render is in flight — the PATCH deletes the previous
  // file before the replacement is written, and after a refresh the clip has no
  // `output_path` at all, so without this the element would unmount (tearing down
  // the caption streaming/loop effects) and the preview would have nothing to
  // blur behind the progress overlay.
  // The preview always has a moving picture while the episode media resolves —
  // it no longer depends on a rendered file existing for the clip.
  const hasPlayableVideo = Boolean(previewMediaUrl)

  // Live Celery progress for this clip, published by the context pollers while a
  // render runs in the background. Drives the blurred progress overlay.
  const jobProgress = renderProgress?.[id] || null
  const progressPercent = jobProgress
    ? Math.max(0, Math.min(100, Number(jobProgress.progress) || 0))
    : null

  // True while this page owns an in-flight save.
  const savingHere = saving
  const renderInFlight = savingHere || renderingOnServer

  // The overlay is shown for the whole render, even before the first poll tick
  // lands, so the preview blurs immediately rather than after one poll interval.
  const showRenderOverlay = renderInFlight && hasPlayableVideo

  // Re-attach to a render that outlived the page. Runs once per clip: a reload
  // mid-render must keep showing live progress instead of silently finishing the
  // job in the background with a stale preview on screen.
  //
  // Guarded by a ref keyed on the clip id: `renderingOnServer` is derived from the
  // clip, and the poller writes progress back onto that same clip, so a naive
  // effect would restart the loop whenever the status field it watches changes.
  const resumedClipRef = useRef(null)
  useEffect(() => {
    if (!id || !renderingOnServer) return undefined
    if (resumedClipRef.current === id) return undefined
    resumedClipRef.current = id
    let cancelled = false
    resumeClipRender(id)
      .then((settled) => {
        if (cancelled || !settled) return
        // Mirror the outcome into the local copy so the preview swaps to the new
        // MP4 (or surfaces the failure) without another manual reload.
        fetchClip(id).then(setRemote).catch(() => {})
      })
      .catch(() => {})
      .finally(() => {
        // Allow a later save in this session to resume again.
        resumedClipRef.current = null
      })
    return () => {
      cancelled = true
    }
  }, [id, renderingOnServer, resumeClipRender, fetchClip])

  useEffect(() => {
    if (!contextClip && id) {
      fetchClip(id).then(setRemote).catch(() => setRemote(null))
    }
  }, [contextClip, id, fetchClip])

  useEffect(() => {
    if (presets.length === 0) loadPresets()
  }, [presets.length, loadPresets])

  // Initialise the draft once, from whichever clip object arrives first.
  useEffect(() => {
    if (!clip) return
    // Seed from the clip the first time its data lands; later `clip` refreshes
    // (for example after a render completes) must not clobber in-flight edits.
    setDraft((current) => {
      if (current) return current
      return {
        title: clip.title,
        hook: clip.hook,
        summary: clip.summary,
        start: clip.start_time,
        end: clip.end_time,
        transcript: clip.transcript_text || '',
        captionStyle: { ...DEFAULT_CAPTION_STYLE, ...clip.caption_style },
        framing: { ...DEFAULT_FRAMING, ...clip.framing },
      }
    })
  }, [clip])

  // Draft transcript sent to the server on save. Nothing is sent while typing —
  // the overlay is composited locally from `captionGroups` — so the edited text
  // reaches the server only through the save action.
  const draftTranscript = draft?.transcript?.trim() || ''

  /**
   * Build the preview caption timeline on the client.
   *
   * No backend call: the live preview must never touch the API while editing.
   * The stored word timings (`clip.words`) arrive once with the clip detail and
   * are rebased and grouped here (mirroring the backend's `words_from_payload`),
   * so captions stay locked to the audio. When the transcript is edited the
   * timings fall back to even distribution, exactly like the render.
   */
  const captionGroups = useMemo(
    () =>
      buildClientTimeline({
        transcript: draft?.transcript,
        start: draft?.start,
        end: draft?.end,
        captionStyle: draft?.captionStyle,
        clipWords: clip?.words,
      }),
    [draft?.transcript, draft?.start, draft?.end, draft?.captionStyle, clip?.words],
  )

  // Give the canvas a `ref` so the fullscreen handler can promote the *container*
  // rather than the bare <video>.
  const canvasRef = useRef(null)

  /**
   * Fullscreen the whole preview stage, not just the video node.
   *
   * The native `<video>` fullscreen control promotes the video element alone, and
   * the caption `<canvas>` is a sibling — so captions stayed behind in the page
   * and fullscreen playback carried no captions at all.
   *
   * Listening for `fullscreenchange` was not enough: it fires *after* the video is
   * already the fullscreen element, and re-requesting on the container there is
   * ignored. Instead the element's own `requestFullscreen` is redirected to the
   * container, so whichever path the browser takes — the native control, a
   * keyboard shortcut, or the `webkitEnterFullscreen` used by iOS Safari — the
   * stage is promoted and the video *and* canvas fill the screen together.
   */
  useEffect(() => {
    const stage = canvasRef.current
    const video = videoRef.current
    if (!stage || !video) return undefined

    const requestOnStage = () => {
      const request =
        stage.requestFullscreen ||
        stage.webkitRequestFullscreen ||
        stage.msRequestFullscreen
      // No container API (very old browsers): leave native video fullscreen in
      // place rather than breaking playback entirely.
      if (!request) return undefined
      return request.call(stage)
    }

    // Redirect the video's own fullscreen request to the container.
    const originalRequest = video.requestFullscreen
    const originalWebkitRequest = video.webkitRequestFullscreen
    const originalEnterFullscreen = video.webkitEnterFullscreen

    if (originalRequest) {
      video.requestFullscreen = function redirectedFullscreen(options) {
        const result = requestOnStage()
        return result || originalRequest.call(this, options)
      }
    }
    if (originalWebkitRequest) {
      video.webkitRequestFullscreen = function redirectedWebkitFullscreen() {
        return requestOnStage() || originalWebkitRequest.call(this)
      }
    }
    // iOS Safari promotes the video directly and never calls requestFullscreen.
    if (originalEnterFullscreen) {
      video.webkitEnterFullscreen = function redirectedEnterFullscreen() {
        return requestOnStage() || originalEnterFullscreen.call(this)
      }
    }

    /**
     * Safety net for browsers where the redirect above cannot take hold.
     *
     * Some engines expose `requestFullscreen` in a way that silently ignores an
     * instance-level assignment, so the video — not the stage — becomes the
     * fullscreen element. When that happens the caption canvas is left behind in
     * the page and fullscreen playback shows no captions. Re-promoting the stage
     * here repairs it: `fullscreenchange` fires *after* the video is already
     * fullscreen, and exiting and re-entering on the container is the only way to
     * hand the fullscreen element over. The 100ms delay lets the browser settle
     * its own fullscreen transition first, otherwise the nested request is
     * rejected as not user-initiated.
     */
    const handleFullscreenChange = () => {
      const current =
        document.fullscreenElement || document.webkitFullscreenElement || null
      if (current !== video) return undefined
      window.setTimeout(() => {
        // Only intervene if the video is still the fullscreen element — the user
        // may have already exited.
        const still = document.fullscreenElement || document.webkitFullscreenElement
        if (still !== video) return
        const exit = document.exitFullscreen || document.webkitExitFullscreen
        if (!exit) return
        Promise.resolve(exit.call(document))
          .then(() => requestOnStage())
          .catch(() => {
            // The promotion was refused (not user-initiated, or the container API
            // is unavailable). Native video fullscreen stays, which is the same
            // fallback as a browser without a container API.
          })
      }, 100)
      return undefined
    }
    document.addEventListener('fullscreenchange', handleFullscreenChange)
    document.addEventListener('webkitfullscreenchange', handleFullscreenChange)

    return () => {
      if (originalRequest) video.requestFullscreen = originalRequest
      if (originalWebkitRequest) video.webkitRequestFullscreen = originalWebkitRequest
      if (originalEnterFullscreen) video.webkitEnterFullscreen = originalEnterFullscreen
      document.removeEventListener('fullscreenchange', handleFullscreenChange)
      document.removeEventListener('webkitfullscreenchange', handleFullscreenChange)
    }
  }, [hasPlayableVideo, previewMediaUrl])

  /**
   * Constrain preview playback to the clip's cut.
   *
   * Two clocks are possible. The trimmed companion render runs 0..clip duration,
   * so it is already the cut and no clamping is needed — seeking to the in-point
   * there would jump past the file's own end and the captions would never draw.
   * The episode fallback runs over the source timeline, so playback must start at
   * the in-point and jump back when it passes the out-point — the same loop the
   * `loop` attribute gave the rendered MP4, but scoped to the cut.
   */
  useEffect(() => {
    // Skip until the preview <video> exists. It is keyed on the media URL and
    // only rendered once the clip has a render, so the node can be replaced —
    // both values are read here so a remount re-runs this effect.
    if (!hasPlayableVideo || !previewMediaUrl) return undefined
    if (previewIsClipLocal) return undefined
    const video = videoRef.current
    if (!video) return undefined
    const start = Math.max(Number(draft?.start) || 0, 0)
    const end = Number(draft?.end) || 0
    if (end <= start) return undefined

    const seekToStart = () => {
      // Only correct a position outside the cut, so a scrub *inside* the cut is
      // preserved instead of snapping back to the in-point.
      if (video.currentTime < start - 0.05 || video.currentTime >= end) {
        try {
          video.currentTime = start
        } catch {
          // Seeking before metadata is ready throws in some browsers; the
          // `loadedmetadata` handler below retries once it is.
        }
      }
    }

    const handleLoaded = () => seekToStart()
    video.addEventListener('loadedmetadata', handleLoaded)
    // `loadedmetadata` may already have fired (cached media), in which case the
    // listener above would never run.
    if (video.readyState >= 1) seekToStart()

    let frameHandle = 0
    const tick = () => {
      frameHandle = window.requestAnimationFrame(tick)
      if (video.paused) return
      if (video.currentTime >= end) {
        try {
          video.currentTime = start
        } catch {
          /* ignore — retried on the next tick */
        }
      }
    }
    frameHandle = window.requestAnimationFrame(tick)

    return () => {
      video.removeEventListener('loadedmetadata', handleLoaded)
      window.cancelAnimationFrame(frameHandle)
    }
  }, [draft?.start, draft?.end, hasPlayableVideo, previewMediaUrl, previewIsClipLocal])

  // Keep the blurred background copy in step with the foreground video. It is
  // muted and non-interactive, so it only needs its currentTime mirrored; the
  // foreground's own loop effect already bounds playback to the cut window.
  useEffect(() => {
    const source = videoRef.current
    const mirror = blurVideoRef.current
    if (!source || !mirror) return undefined

    const sync = () => {
      try {
        if (Math.abs(mirror.currentTime - source.currentTime) > 0.08) {
          mirror.currentTime = source.currentTime
        }
      } catch {
        /* ignore — metadata may not be ready yet */
      }
      if (mirror.paused !== source.paused) {
        if (source.paused) mirror.pause()
        else mirror.play().catch(() => {})
      }
    }
    source.addEventListener('timeupdate', sync)
    source.addEventListener('play', sync)
    source.addEventListener('pause', sync)
    return () => {
      source.removeEventListener('timeupdate', sync)
      source.removeEventListener('play', sync)
      source.removeEventListener('pause', sync)
    }
  }, [hasPlayableVideo, framing.blur_background, previewMediaUrl])

  if (!clip || !draft) {
    return (
      <div className="page">
        <div className="skeleton" style={{ height: 320 }} />
      </div>
    )
  }

  const patchStyle = (patch) =>
    setDraft((current) => ({ ...current, captionStyle: { ...current.captionStyle, ...patch } }))
  const patchFraming = (patch) =>
    setDraft((current) => ({ ...current, framing: { ...current.framing, ...patch } }))

  /**
   * Apply a caption preset.
   *
   * The preset is merged **onto the current style** rather than replacing it, so
   * any field the preset does not define keeps the user's value instead of
   * silently reverting to a default. The preset's own `preset` name is recorded
   * so the gallery can show which theme is active.
   */
  const applyPreset = (preset) =>
    setDraft((current) => ({
      ...current,
      captionStyle: { ...current.captionStyle, ...(preset.style || {}), preset: preset.name },
    }))

  /**
   * Persist the edited transcript and re-render the clip with burned captions.
   *
   * `waitForClipRender` dispatches the render job (carrying the edited text so
   * the burned captions and the stored transcript can never drift apart), polls
   * it to completion and refreshes the shared `clips` state, so the preview
   * picks up the new MP4 on its own. This wrapper only translates the settled
   * job into editor messages.
   *
   * Returns true when the re-render finished successfully, false when the
   * caller should surface an error.
   */
  async function commitRender() {
    const settled = await waitForClipRender(id, {
      transcript: draftTranscript,
      captionStyle: draft.captionStyle,
      // Send framing too, otherwise the re-render keeps the source geometry and
      // silently discards the output size / zoom / blur the user chose.
      framing: draft.framing,
    })
    if (!settled) return false

    const status = String(settled.status || '').toLowerCase()
    if (['failure', 'failed', 'error', 'revoked', 'timeout'].includes(status)) {
      setSaveError(
        settled.error_message ||
          (status === 'timeout'
            ? 'Render is still running — check the worker logs.'
            : 'Render failed. Check the Celery worker logs.'),
      )
      return false
    }

    setSavedNote('Saved ✓ Captions applied')
    // The commit refreshes the shared clip state; re-fetch locally too so a clip
    // the editor loaded on its own (not present in `clips`) is still updated.
    await fetchClip(id).then(setRemote).catch(() => {})
    return true
  }

  /**
   * Save the whole edit and re-render the clip.
   *
   * Persists the title, hook, summary, cut boundaries, caption style, framing and
   * transcript, then **always** re-renders. Every save therefore:
   *
   * 1. re-renders the clip with captions burned from the edited text,
   * 2. overwrites the previous `clip_{id}_vertical.mp4` and its caption-free
   *    companion, so the new video substitutes the old one at the same URL.
   *
   * The save **must** trigger a render: the PATCH that stores the edit deletes the
   * previous render (and its companion), so without one the preview would have no
   * file to play at all. Rendering even when the transcript is unchanged keeps the
   * captions and video consistent with the settings on screen.
   *
   * There is no automatic application while editing: this handler is the only
   * write path.
   */
  async function handleSave(event) {
    event.preventDefault()
    if (!draft) return
    setSaving(true)
    setSavedNote(null)
    setSaveError(null)
    try {
      // 1. Persist the edit — metadata, styling, framing *and* the transcript —
      //    so it is stored even if the render later fails.
      try {
        const updated = await updateClip(id, {
          title: draft.title,
          hook: draft.hook,
          summary: draft.summary,
          transcript_text: draftTranscript,
          start_time: draft.start,
          end_time: draft.end,
          caption_style: draft.captionStyle,
          framing: draft.framing,
        })
        if (updated) setRemote(updated)
      } catch (error) {
        // Prefer the API's own explanation. Only fall back to the generic copy
        // for transport/5xx failures, otherwise a real `detail` gets masked by
        // "check the API is running". Saving the metadata is what failed, so the
        // render is not attempted.
        const detail = describeError(error, '')
        setSaveError(
          isUnexpectedError(error) || !detail
            ? 'Save failed — check the API and worker are running.'
            : detail,
        )
        return
      }

      // 2. Burn the edited captions onto a fresh render. The stale render is gone
      //    by now, so this is what makes the preview playable again —
      //    `commitRender` resolves only once the job has settled, so the preview
      //    swaps to the new file as soon as it is written.
      await commitRender()
    } finally {
      setSaving(false)
    }
  }

  /**
   * True when the draft differs from what the clip was rendered with. Drives the
   * "Draft styling" badge so the user can tell at a glance whether the preview
   * is showing their pending edits or the stored render.
   *
   * Covers every previewable section: caption style and framing (including the
   * render toggles), the title card, and the cut boundaries.
   */
  const hasPendingPreviewEdits =
    JSON.stringify(draft.captionStyle) !==
      JSON.stringify({ ...DEFAULT_CAPTION_STYLE, ...clip.caption_style }) ||
    JSON.stringify(draft.framing) !== JSON.stringify({ ...DEFAULT_FRAMING, ...clip.framing }) ||
    draft.title !== clip.title ||
    draft.start !== clip.start_time ||
    draft.end !== clip.end_time

  /**
   * CSS for applying the draft's 9:16 framing to the preview — computed entirely
   * on the client so the Zoom / Focus X / Focus Y / output size and render
   * toggles update the preview instantly. The renderer does the real crop
   * server-side; here we approximate it with `object-fit` (contain vs cover per
   * `blur_background`) plus a `transform: scale(zoom)` anchored at the focus
   * point, and a blurred copy of the video behind it when `blur_background` is
   * on (the classic vertical clip "blurred letterbox bars" look).
   */
  const zoom = Math.max(Number(framing.zoom) || 1, 1)
  const focusX = Math.min(Math.max(Number(framing.focus_x) || 0.5, 0), 1)
  const focusY = Math.min(Math.max(Number(framing.focus_y) || 0.5, 0), 1)
  const outputWidth = Number(framing.output_width) || 1080
  const outputHeight = Number(framing.output_height) || 1920
  const blurStrength = Math.min(Math.max(Number(framing.background_blur_strength) || 0, 0), 151)

  const previewFrameStyle = {
    aspectRatio: `${outputWidth} / ${outputHeight}`,
  }
  // The foreground uses `object-fit: contain` so the preview reads exactly like
  // the clip detail page (the rendered clip is shown whole, letterboxed, with no
  // aggressive side-crop). Zoom is then applied as `transform: scale(zoom)`
  // anchored at the focus point, which re-crops around the point of interest.
  //
  // With `contain` the 16:9 source is letterboxed inside the 9:16 frame; scaling
  // it up around a focus near the frame centre zooms into the picture rather
  // than the bars, matching how the detail page's clean frame looks at rest.
  const previewCropStyle = {
    objectFit: 'contain',
    transform: `scale(${zoom})`,
    transformOrigin: `${focusX * 100}% ${focusY * 100}%`,
  }
  // Blurred background fill: a muted copy of the source behind the foreground
  // so the letterbox bars (top/bottom of a wide source) show blurred footage
  // instead of black, matching the renderer's `blur_background`.
  const previewBlurStyle = blurStrength
    ? { filter: `blur(${blurStrength}px)`, transform: 'scale(1.3)' }
    : undefined

  return (
    <div className="page">
      <form onSubmit={handleSave} className="stack stack--lg">
        <header className="row row--between">
          <div>
            <Link to={`/clips/${clip.id}`} className="dim">← Back to clip</Link>
            <h1>Edit clip</h1>
          </div>
          <div className="row">
            {savedNote && <span className="badge badge--success">{savedNote}</span>}
            <button type="submit" className="btn btn--primary" disabled={saving}>
              {saving && <span className="btn__spinner" />}
              {saving ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </header>

        <div className="editor-grid">
          <div className="stack stack--md">
            <section className="card stack">
              <h3>Text</h3>
              <div className="field">
                <label className="field__label" htmlFor="edit-title">Title</label>
                <input
                  id="edit-title"
                  className="input"
                  value={draft.title}
                  onChange={(event) =>
                    setDraft((c) => ({ ...c, title: event.target.value }))
                  }
                />
              </div>
              <div className="field">
                <label className="field__label" htmlFor="edit-hook">Hook</label>
                <textarea
                  id="edit-hook"
                  className="input textarea"
                  rows={2}
                  value={draft.hook}
                  onChange={(event) =>
                    setDraft((c) => ({ ...c, hook: event.target.value }))
                  }
                />
              </div>
              <div className="field">
                <label className="field__label" htmlFor="edit-summary">Summary</label>
                <textarea
                  id="edit-summary"
                  className="input textarea"
                  rows={3}
                  value={draft.summary}
                  onChange={(event) =>
                    setDraft((c) => ({ ...c, summary: event.target.value }))
                  }
                />
              </div>
            </section>

            <section className="card stack">
              <h3>Timing</h3>
              <div className="grid grid--2">
                <div className="field">
                  <label className="field__label" htmlFor="edit-start">
                    Start · {formatTimecode(draft.start)}
                  </label>
                  <input
                    id="edit-start"
                    className="input"
                    type="number"
                    step="0.1"
                    min="0"
                    value={draft.start}
                    onChange={(event) =>
                      setDraft((c) => ({ ...c, start: Number(event.target.value) }))
                    }
                  />
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="edit-end">
                    End · {formatTimecode(draft.end)}
                  </label>
                  <input
                    id="edit-end"
                    className="input"
                    type="number"
                    step="0.1"
                    min="0"
                    value={draft.end}
                    onChange={(event) =>
                      setDraft((c) => ({ ...c, end: Number(event.target.value) }))
                    }
                  />
                </div>
              </div>
              <span className="dim">
                Length: {formatDuration(Math.max(0, draft.end - draft.start))}
              </span>
            </section>

            <section className="card stack">
              <div className="row row--between">
                <h3>Transcript &amp; captions</h3>
              </div>
              <p className="dim" style={{ margin: 0, fontSize: '0.8rem' }}>
                Edit the transcript, then press <strong>Save changes</strong> to save everything
                and generate the updated video — the captions are burned in immediately.
              </p>
              <div className="field">
                <label className="field__label" htmlFor="edit-transcript">
                  Transcript
                </label>
                <textarea
                  id="edit-transcript"
                  className="input"
                  rows={6}
                  value={draft.transcript}
                  onChange={(event) => setDraft((c) => ({ ...c, transcript: event.target.value }))}
                  placeholder="What the speaker says in this clip…"
                />
                <span className="dim">
                  {draft.transcript.trim().split(/\s+/).filter(Boolean).length} words
                </span>
                {saveError && (
                  <span className="field__error" role="alert">{saveError}</span>
                )}
              </div>

              <span className="dim" style={{ fontSize: '0.78rem' }}>
                <strong>Save changes</strong> is the only action that writes a clip: it stores the
                title, cut, caption styling, framing and this transcript, then immediately burns
                matching captions into a fresh render. Nothing is applied while you type.
              </span>
            </section>

            <section className="card stack">
              <h3>Caption style</h3>
              {presets.length > 0 && (
                <div className="row row--wrap">
                  {presets.map((preset) => {
                    const isActive = draft.captionStyle.preset === preset.name
                    return (
                      <button
                        key={preset.name}
                        type="button"
                        className={`btn btn--sm ${isActive ? 'btn--primary' : 'btn--ghost'}`}
                        onClick={() => applyPreset(preset)}
                        aria-pressed={isActive}
                        title={`Apply the ${preset.label} caption theme`}
                      >
                        {preset.label}
                      </button>
                    )
                  })}
                </div>
              )}
              <div className="grid grid--2">
                {COLOR_FIELDS.map(([field, label]) => (
                  <div className="field" key={field}>
                    <label className="field__label" htmlFor={`style-${field}`}>
                      {label} color
                    </label>
                    <div className="row">
                      <input
                        type="color"
                        value={draft.captionStyle[field]}
                        onChange={(event) => patchStyle({ [field]: event.target.value })}
                        aria-label={`${label} color`}
                      />
                      <input
                        id={`style-${field}`}
                        className="input"
                        value={draft.captionStyle[field]}
                        onChange={(event) => patchStyle({ [field]: event.target.value })}
                      />
                    </div>
                  </div>
                ))}
                <div className="field">
                  <label className="field__label" htmlFor="style-position">Position</label>
                  <select
                    id="style-position"
                    className="input"
                    value={draft.captionStyle.position}
                    onChange={(event) => patchStyle({ position: event.target.value })}
                  >
                    <option value="bottom">Bottom</option>
                    <option value="center">Center</option>
                    <option value="top">Top</option>
                  </select>
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="style-wpl">
                    Words per line: {draft.captionStyle.words_per_line}
                  </label>
                  <input
                    id="style-wpl"
                    type="range"
                    min="1"
                    max="8"
                    value={draft.captionStyle.words_per_line}
                    onChange={(event) =>
                      patchStyle({ words_per_line: Number(event.target.value) })
                    }
                  />
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="style-maxlines">
                    Max lines: {draft.captionStyle.max_lines}
                  </label>
                  <input
                    id="style-maxlines"
                    type="range"
                    min="1"
                    max="4"
                    value={draft.captionStyle.max_lines}
                    onChange={(event) =>
                      patchStyle({ max_lines: Number(event.target.value) })
                    }
                  />
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="style-size">
                    Font size: {draft.captionStyle.font_size}
                  </label>
                  <input
                    id="style-size"
                    type="range"
                    min="24"
                    max="140"
                    value={draft.captionStyle.font_size}
                    onChange={(event) =>
                      patchStyle({ font_size: Number(event.target.value) })
                    }
                  />
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="style-margin">
                    Bottom margin: {draft.captionStyle.bottom_margin}
                  </label>
                  <input
                    id="style-margin"
                    type="range"
                    min="0"
                    max="800"
                    step="10"
                    value={draft.captionStyle.bottom_margin}
                    onChange={(event) =>
                      patchStyle({ bottom_margin: Number(event.target.value) })
                    }
                  />
                </div>
              </div>
              <div className="row row--wrap">
                <label className="row dim" style={{ cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={draft.captionStyle.uppercase}
                    onChange={(event) => patchStyle({ uppercase: event.target.checked })}
                  />
                  UPPERCASE
                </label>
                <label className="row dim" style={{ cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={draft.captionStyle.shadow}
                    onChange={(event) => patchStyle({ shadow: event.target.checked })}
                  />
                  Shadow
                </label>
              </div>
            </section>

            <section className="card stack">
              <h3>9:16 Framing</h3>
              <div className="grid grid--2">
                <div className="field">
                  <label className="field__label" htmlFor="frame-mode">Mode</label>
                  <select
                    id="frame-mode"
                    className="input"
                    value={draft.framing.mode}
                    onChange={(event) => patchFraming({ mode: event.target.value })}
                  >
                    <option value="auto_face">Auto face tracking</option>
                    <option value="center">Centered</option>
                    <option value="manual">Manual focus</option>
                  </select>
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="frame-output">Output size</label>
                  <select
                    id="frame-output"
                    className="input"
                    value={`${draft.framing.output_width}x${draft.framing.output_height}`}
                    onChange={(event) => {
                      const [width, height] = event.target.value.split('x').map(Number)
                      patchFraming({ output_width: width, output_height: height })
                    }}
                  >
                    {OUTPUT_SIZES.map((size) => (
                      <option key={size.label} value={`${size.width}x${size.height}`}>
                        {size.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="frame-zoom">
                    Zoom: {Number(draft.framing.zoom).toFixed(2)}×
                  </label>
                  <input
                    id="frame-zoom"
                    type="range"
                    min="1"
                    max="2.5"
                    step="0.05"
                    value={draft.framing.zoom}
                    onChange={(event) => patchFraming({ zoom: Number(event.target.value) })}
                  />
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="frame-fps">
                    FPS: {draft.framing.fps}
                  </label>
                  <input
                    id="frame-fps"
                    type="range"
                    min="15"
                    max="60"
                    step="5"
                    value={draft.framing.fps}
                    onChange={(event) => patchFraming({ fps: Number(event.target.value) })}
                  />
                </div>
                {draft.framing.mode === 'manual' && (
                  <>
                    <div className="field">
                      <label className="field__label" htmlFor="frame-x">
                        Focus X: {Number(draft.framing.focus_x).toFixed(2)}
                      </label>
                      <input
                        id="frame-x"
                        type="range"
                        min="0"
                        max="1"
                        step="0.01"
                        value={draft.framing.focus_x}
                        onChange={(event) =>
                          patchFraming({ focus_x: Number(event.target.value) })
                        }
                      />
                    </div>
                    <div className="field">
                      <label className="field__label" htmlFor="frame-y">
                        Focus Y: {Number(draft.framing.focus_y).toFixed(2)}
                      </label>
                      <input
                        id="frame-y"
                        type="range"
                        min="0"
                        max="1"
                        step="0.01"
                        value={draft.framing.focus_y}
                        onChange={(event) =>
                          patchFraming({ focus_y: Number(event.target.value) })
                        }
                      />
                    </div>
                  </>
                )}
              </div>
            </section>

            <section className="card stack">
              <h3>Render toggles</h3>
              <div className="row row--wrap">
                <label className="row dim" style={{ cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={draft.framing.blur_background}
                    onChange={(event) =>
                      patchFraming({ blur_background: event.target.checked })
                    }
                  />
                  Blurred background fill
                </label>
                <label className="row dim" style={{ cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={draft.framing.smooth_tracking}
                    onChange={(event) =>
                      patchFraming({ smooth_tracking: event.target.checked })
                    }
                  />
                  Smooth tracking
                </label>
                <label className="row dim" style={{ cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={draft.framing.burn_captions}
                    onChange={(event) => patchFraming({ burn_captions: event.target.checked })}
                  />
                  Burn captions into the render
                </label>
              </div>
              {draft.framing.blur_background && (
                <div className="field">
                  <label className="field__label" htmlFor="frame-blur">
                    Background blur: {draft.framing.background_blur_strength}
                  </label>
                  <input
                    id="frame-blur"
                    type="range"
                    // A Gaussian kernel below 3 is clamped to 3 in the renderer
                    // (`kernel = max(3, ...)`), so a minimum of 1 left the bottom
                    // of the slider doing nothing. Start at the real floor.
                    min="3"
                    max="151"
                    step="2"
                    value={draft.framing.background_blur_strength}
                    onChange={(event) =>
                      patchFraming({ background_blur_strength: Number(event.target.value) })
                    }
                  />
                </div>
              )}
            </section>
          </div>

          <aside className="editor-aside stack stack--md">
            <section className="card stack stack--sm editor-preview-card">
              <div className="row row--between editor-preview-card__head">
                <h3>Live preview</h3>
                <span className="badge badge--muted editor-preview-card__badge">
                  {renderInFlight
                    ? progressPercent !== null
                      ? `Rendering ${progressPercent}%`
                      : 'Rendering…'
                    : !clip.output_path
                      ? 'Awaiting render'
                      : hasPendingPreviewEdits
                        ? 'Draft preview'
                        : 'Rendered clip'}
                </span>
              </div>

              {/* The moving picture is the episode's *source* media, not the
                  rendered MP4: the render already has its captions burned into
                  the pixels, so the draft overlay would sit on top of a second,
                  stale caption track. The source carries no captions, leaving
                  the canvas below as the only caption track. */}
              <div className="editor-preview-device">
                <div
                  className="phone-frame editor-preview-device__frame"
                  style={previewFrameStyle}
                >
                  <div
                    ref={canvasRef}
                    className={`phone-frame__canvas${
                      showRenderOverlay ? ' phone-frame__canvas--busy' : ''
                    }`}
                  >
                    {hasPlayableVideo ? (
                      <>
                        {/* Blurred background fill: a muted, non-interactive copy of
                            the source behind the letterboxed foreground. It fills
                            the 9:16 frame so the bars of a wide (16:9) source show
                            blurred footage instead of black, matching the
                            renderer's `blur_background` and the detail page. */}
                        {framing.blur_background && previewBlurStyle && (
                          <video
                            ref={blurVideoRef}
                            aria-hidden="true"
                            muted
                            playsInline
                            preload="metadata"
                            src={previewMediaUrl}
                            className="phone-frame__media"
                            style={{ objectFit: 'cover', ...previewBlurStyle }}
                          />
                        )}
                        <video
                          id="editor-preview-video"
                          ref={videoRef}
                          key={previewMediaUrl}
                          controls
                          playsInline
                          preload="metadata"
                          src={previewMediaUrl}
                          className="phone-frame__media"
                          style={previewCropStyle}
                        />
                        {/* The draft captions are drawn locally each animation
                            frame from the client-built timeline, so they track
                            playback exactly instead of lagging behind it.
                            `timeOffset` rebases the clip-relative timeline onto
                            the playing media's own clock — 0 for the trimmed
                            companion render, the clip start for the episode
                            fallback. This is a transparent canvas — it must not
                            carry `.phone-frame__media`, whose opaque black
                            background would cover the video. */}
                        <CaptionOverlay
                          videoRef={videoRef}
                          timeline={captionGroups}
                          style={draft?.captionStyle}
                          framing={draft?.framing}
                          title={draft?.title}
                          timeOffset={captionTimeOffset}
                          videoKey={previewMediaUrl}
                          className="phone-frame__draft-frame"
                        />
                      </>
                    ) : clip.thumbnail_path ? (
                      <img
                        src={clipThumbnailUrl(clip.id)}
                        alt="Clip thumbnail"
                        className="phone-frame__media"
                      />
                    ) : (
                      <div className="phone-frame__placeholder">
                        <span>Preview</span>
                        <p className="dim">
                          Render this clip once to preview it here with your edited captions.
                        </p>
                      </div>
                    )}

                    {/* Blurred progress state for a save in flight. The media
                        behind it keeps playing, dimmed and blurred, so the user
                        sees the clip they are updating while the worker reports
                        how far along it is. */}
                    {showRenderOverlay && (
                      <div className="editor-preview-progress" role="status" aria-live="polite">
                        <div className="editor-preview-progress__spinner" aria-hidden="true" />
                        <div className="editor-preview-progress__label">
                          <strong>
                            {jobProgress?.message || 'Burning captions into a fresh render…'}
                          </strong>
                          <span className="dim">
                            {progressPercent !== null
                              ? `${progressPercent}% · running in the background`
                              : 'Queued in the background…'}
                          </span>
                        </div>
                        <div className="progress editor-preview-progress__bar">
                          <div
                            className="progress__bar"
                            style={{
                              // A settled 0% reads as "stuck"; nudge it so the bar
                              // shows activity until the first real tick arrives.
                              width: `${progressPercent !== null ? progressPercent : 4}%`,
                            }}
                          />
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {saveError && (
                <span className="field__error editor-preview-error" role="alert">
                  {saveError}
                </span>
              )}
              <span className="dim editor-preview-hint">
                {renderInFlight ? (
                  <>
                    {savingHere
                      ? 'Your changes are being processed in the background.'
                      : 'A render that was already running has been picked back up.'}{' '}
                    The preview stays blurred below with live progress until the updated video
                    is ready.
                  </>
                ) : hasPendingPreviewEdits ? (
                  <>
                    Showing your <strong>draft</strong> caption styling over the clip. Press
                    <strong> Save changes</strong> to save everything and generate an updated
                    video with these captions burned in.
                  </>
                ) : (
                  <>
                    Previewing the clip's rendered media with your saved styling. Edit the text,
                    timing, caption style or framing and the draft captions update live; press
                    <strong> Save changes</strong> to save everything and generate the updated
                    video.
                  </>
                )}
              </span>
            </section>
          </aside>
        </div>
      </form>
    </div>
  )
}