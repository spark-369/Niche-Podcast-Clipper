/** Background job and service-health endpoints. */

import api from './client'

export const jobsApi = {
  async list({ episodeId, clipId, jobType, status, limit = 50 } = {}) {
    const params = { limit }
    if (episodeId) params.episode_id = episodeId
    if (clipId) params.clip_id = clipId
    if (jobType) params.job_type = jobType
    if (status) params.status = status
    const { data } = await api.get('/jobs', { params })
    return data
  },

  async get(jobId) {
    const { data } = await api.get(`/jobs/${jobId}`)
    return data
  },

  async revoke(jobId, terminate = false) {
    const { data } = await api.post(`/jobs/${jobId}/revoke`, null, {
      params: { terminate },
    })
    return data
  },

  async workerPing() {
    const { data } = await api.get('/jobs/worker/ping')
    return data
  },
}

export const healthApi = {
  async status() {
    const { data } = await api.get('/health')
    return data
  },

  async models() {
    const { data } = await api.get('/health/models')
    return data
  },

  async worker() {
    const { data } = await api.get('/health/worker')
    return data
  },
}

export default jobsApi
