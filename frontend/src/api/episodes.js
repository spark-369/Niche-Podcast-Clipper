/** Episode endpoints: upload, list, transcribe and transcript access. */

import api from './client'
import { getAccessToken } from './client'

export const episodesApi = {
  async list({ skip = 0, limit = 50, status } = {}) {
    const params = { skip, limit }
    if (status) params.status = status
    const { data } = await api.get('/episodes', { params })
    return data
  },

  async get(episodeId) {
    const { data } = await api.get(`/episodes/${episodeId}`)
    return data
  },

  /**
   * Upload a media file with progress reporting.
   * @param {File} file
   * @param {{title?: string, description?: string, autoTranscribe?: boolean}} options
   * @param {(percent: number) => void} [onProgress]
   */
  async upload(file, options = {}, onProgress) {
    const form = new FormData()
    form.append('file', file)
    if (options.title) form.append('title', options.title)
    if (options.description) form.append('description', options.description)
    form.append('auto_transcribe', String(Boolean(options.autoTranscribe)))

    const { data } = await api.post('/episodes/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (event) => {
        if (onProgress && event.total) {
          onProgress(Math.round((event.loaded / event.total) * 100))
        }
      },
    })
    return data
  },

  async update(episodeId, { title, description }) {
    const payload = {}
    if (title !== undefined) payload.title = title
    if (description !== undefined) payload.description = description
    const { data } = await api.patch(`/episodes/${episodeId}`, payload)
    return data
  },

  async remove(episodeId) {
    await api.delete(`/episodes/${episodeId}`)
  },

  async transcribe(episodeId, { language = null, wordTimestamps = true, force = false } = {}) {
    const { data } = await api.post(`/episodes/${episodeId}/transcribe`, {
      language,
      word_timestamps: wordTimestamps,
      force,
    })
    return data
  },

  async transcript(episodeId) {
    const { data } = await api.get(`/episodes/${episodeId}/transcript`)
    return data
  },

  async stats(episodeId) {
    const { data } = await api.get(`/episodes/${episodeId}/stats`)
    return data
  },

  mediaUrl(episodeId) {
    const token = getAccessToken()
    const url = `${api.defaults.baseURL}/episodes/${episodeId}/media`
    return token ? `${url}?token=${encodeURIComponent(token)}` : url
  },
}

export default episodesApi
