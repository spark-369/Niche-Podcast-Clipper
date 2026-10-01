/** Clip endpoints: highlight generation, editing, rendering and downloads. */

import api, { getAccessToken } from './client'

export const clipsApi = {
  async generate(episodeId, options = {}) {
    const { data } = await api.post(
      '/clips/generate',
      {
        clip_count: options.clipCount ?? 5,
        min_duration_seconds: options.minDuration ?? 15,
        max_duration_seconds: options.maxDuration ?? 60,
        temperature: options.temperature ?? 0.4,
        focus: options.focus || null,
        use_cache: options.useCache ?? true,
        ...(options.captionStyle ? { caption_style: options.captionStyle } : {}),
        ...(options.framing ? { framing: options.framing } : {}),
      },
      { params: { episode_id: episodeId } },
    )
    return data
  },

  async list({ episodeId, minScore = 0, limit = 100 } = {}) {
    const params = { min_score: minScore, limit }
    if (episodeId) params.episode_id = episodeId
    const { data } = await api.get('/clips', { params })
    return data
  },

  async get(clipId) {
    const { data } = await api.get(`/clips/${clipId}`)
    return data
  },

  async update(clipId, payload) {
    const { data } = await api.patch(`/clips/${clipId}`, payload)
    return data
  },

  async remove(clipId) {
    await api.delete(`/clips/${clipId}`)
  },

  async render(clipId, { captionStyle, framing, transcript, priority = 5 } = {}) {
    const body = { priority }
    if (captionStyle) body.caption_style = captionStyle
    if (framing) body.framing = framing
    // The edited caption text, persisted by the render endpoint so the burned
    // captions can never drift away from the stored transcript.
    if (transcript) body.transcript = transcript
    const { data } = await api.post(`/clips/${clipId}/render`, body)
    return data
  },

  async status(clipId) {
    const { data } = await api.get(`/clips/${clipId}/status`)
    return data
  },

  async jobs(clipId) {
    const { data } = await api.get(`/clips/${clipId}/jobs`)
    return data
  },

  async presets() {
    const { data } = await api.get('/clips/presets')
    return data
  },

  /**
   * Fetch the whole caption timeline for a clip (one small JSON payload).
   *
   * The editor composites captions locally from this, on every animation
   * frame, instead of asking the server for a re-encoded JPEG several times a
   * second — which queued up and made the preview freeze.
   */
  async captionTimeline(clipId, options = {}) {
    const params = {}
    const { transcript, start_time, end_time, caption_style } = options
    if (transcript !== undefined && transcript !== null) params.transcript = transcript
    if (start_time !== undefined && start_time !== null) params.start_time = start_time
    if (end_time !== undefined && end_time !== null) params.end_time = end_time
    if (caption_style) params.caption_style = JSON.stringify(caption_style)
    const { data } = await api.get(`/clips/${clipId}/caption-timeline`, { params })
    return data
  },

  /**
   * Fetch a preview JPEG of the current crop + caption styling.
   * `options` may include
   * `{ timestamp, caption_style, framing, title, start_time, end_time }`.
   * The draft title and cut bounds are sent so the Text and Timing sections
   * preview live too — without them the endpoint could only read the stored
   * values, so those edits appeared to do nothing until a save.
   * Returns an object URL that the caller is responsible for revoking.
   */
  async previewFrame(clipId, options = {}) {
    const params = {}
    const { timestamp, caption_style, framing, title, start_time, end_time, transcript } =
      options
    if (timestamp !== undefined && timestamp !== null) params.timestamp = timestamp
    if (caption_style) params.caption_style = JSON.stringify(caption_style)
    if (framing) params.framing = JSON.stringify(framing)
    if (title !== undefined && title !== null) params.title = title
    if (start_time !== undefined && start_time !== null) params.start_time = start_time
    if (end_time !== undefined && end_time !== null) params.end_time = end_time
    // The draft transcript so edited caption text is previewed before saving.
    if (transcript !== undefined && transcript !== null) params.transcript = transcript
    const { data } = await api.get(`/clips/${clipId}/preview`, {
      params,
      responseType: 'blob',
    })
    return URL.createObjectURL(data)
  },

  /**
   * Stream URL for the rendered MP4.
   *
   * `version` busts the browser cache. A re-render overwrites the *same*
   * `clip_{id}_vertical.mp4` path, so the URL never changes — without a
   * changing query the preview <video> would keep replaying the bytes it
   * already fetched. Pass anything that changes when the render changes
   * (e.g. the clip's `updated_at`).
   */
  downloadUrl(clipId, version) {
    const token = getAccessToken()
    const params = new URLSearchParams()
    if (token) params.set('token', token)
    if (version !== undefined && version !== null) params.set('v', String(version))
    const query = params.toString()
    return `${api.defaults.baseURL}/clips/${clipId}/download${query ? `?${query}` : ''}`
  },

  /**
   * Stream URL for the caption-free companion render used by the editor preview.
   *
   * The editor draws the draft captions over this file on a canvas. Unlike the
   * episode's source media it carries the render's own audio and framing;
   * unlike the deliverable it has no burned captions, so the canvas stays the
   * only caption track.
   *
   * `version` busts the browser cache — a re-render overwrites the same path.
   */
  previewVideoUrl(clipId, version) {
    const token = getAccessToken()
    const params = new URLSearchParams()
    if (token) params.set('token', token)
    if (version !== undefined && version !== null) params.set('v', String(version))
    const query = params.toString()
    return `${api.defaults.baseURL}/clips/${clipId}/preview-video${query ? `?${query}` : ''}`
  },

  thumbnailUrl(clipId, version) {
    const token = getAccessToken()
    const params = new URLSearchParams()
    if (token) params.set('token', token)
    if (version !== undefined && version !== null) params.set('v', String(version))
    const query = params.toString()
    return `${api.defaults.baseURL}/clips/${clipId}/thumbnail${query ? `?${query}` : ''}`
  },

  /** Authenticated blob download that preserves the server filename. */
  async download(clipId, filename) {
    const { data } = await api.get(`/clips/${clipId}/download`, {
      responseType: 'blob',
    })
    const url = URL.createObjectURL(data)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename || `clip-${clipId}.mp4`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
  },

  /** Auth header helper for <video>/<img> elements that bypass axios. */
  authHeader() {
    const token = getAccessToken()
    return token ? { Authorization: `Bearer ${token}` } : {}
  },
}

export default clipsApi
