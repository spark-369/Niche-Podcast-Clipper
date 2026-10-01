/**
 * Thin axios wrapper around the FastAPI backend.
 *
 * Access tokens are held in memory + localStorage; a response interceptor
 * transparently refreshes them via /auth/refresh on a 401 response.
 */

import axios from 'axios'

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || '/api/v1'

const ACCESS_TOKEN_KEY = 'npc.access_token'
const REFRESH_TOKEN_KEY = 'npc.refresh_token'
const USER_KEY = 'npc.user'

export function getAccessToken() {
  return localStorage.getItem(ACCESS_TOKEN_KEY)
}

export function getRefreshToken() {
  return localStorage.getItem(REFRESH_TOKEN_KEY)
}

export function setTokens({ access_token, refresh_token }) {
  if (access_token) localStorage.setItem(ACCESS_TOKEN_KEY, access_token)
  if (refresh_token) localStorage.setItem(REFRESH_TOKEN_KEY, refresh_token)
}

export function clearTokens() {
  localStorage.removeItem(ACCESS_TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

export function getStoredUser() {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function storeUser(user) {
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user))
  else localStorage.removeItem(USER_KEY)
}

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 120000,
})

api.interceptors.request.use((config) => {
  const token = getAccessToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

let refreshPromise = null

async function refreshAccessToken() {
  const refreshToken = getRefreshToken()
  if (!refreshToken) throw new Error('No refresh token available')

  // Use a bare axios call so the interceptor cannot recurse.
  const { data } = await axios.post(
    `${API_BASE_URL}/auth/refresh`,
    { refresh_token: refreshToken },
    { headers: { 'Content-Type': 'application/json' } },
  )
  setTokens(data)
  return data.access_token
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const { response, config } = error
    const isAuthEndpoint = config?.url?.includes('/auth/')

    if (response?.status === 401 && !config?._retried && !isAuthEndpoint) {
      config._retried = true
      try {
        if (!refreshPromise) {
          refreshPromise = refreshAccessToken().finally(() => {
            refreshPromise = null
          })
        }
        const token = await refreshPromise
        config.headers.Authorization = `Bearer ${token}`
        return api(config)
      } catch (refreshError) {
        clearTokens()
        window.dispatchEvent(new Event('npc:signed-out'))
        return Promise.reject(refreshError)
      }
    }

    return Promise.reject(error)
  },
)

/**
 * True when the failure is *not* the user's request.
 *
 * `describeError` is for showing the user why their action failed, so error
 * messages that are really internal exceptions (an axios "status code 500"
 * string, a traceback pass-through, or our own generic save copy) must not
 * overwrite a real API `detail` such as "No transcript is stored for this
 * clip yet." Use this to decide whether to prefer a caught error's message
 * over a generic one.
 */
export function isUnexpectedError(error) {
  const status = error?.response?.status
  // A transport failure or a 5xx is a server fault, never user input.
  if (!error?.response) return true
  if (typeof status === 'number' && status >= 500) return true
  const message = String(error?.message || '')
  return /status code \d+|Network Error|timeout of \d+ms/i.test(message)
}

/**
 * Extract a human readable message from an axios error.
 *
 * When the backend returns a ``request_id`` (from an unhandled 500 or a
 * validation 422), surface it in the message so support can correlate a user
 * report with the server logs — the same id also tags every log line for that
 * request on the backend.
 */
export function describeError(error, fallback = 'Something went wrong') {
  const detail = error?.response?.data?.detail
  const requestId = error?.response?.data?.request_id
  let message

  if (typeof detail === 'string') message = detail
  else if (Array.isArray(detail) && detail.length > 0) {
    message = detail.map((item) => item.msg || JSON.stringify(item)).join(', ')
  } else if (error?.message === 'Network Error') {
    message = 'Cannot reach the API. Is the FastAPI server running on port 8000?'
  } else {
    message = error?.message || fallback
  }

  if (requestId) {
    return `${message} (reference: ${requestId})`
  }
  return message
}

/**
 * A correlation id a failed request returned from the backend, if any. Useful
 * for logging / error-reporting UIs without altering the user-visible message.
 */
export function requestIdFromError(error) {
  return error?.response?.data?.request_id || error?.response?.headers?.['x-request-id'] || null
}

export default api
