/** Authentication endpoints. */

import api from './client'

export const authApi = {
  /** OAuth2 password flow (form encoded) - matches the documented endpoint. */
  async token(email, password) {
    const form = new URLSearchParams()
    form.append('username', email)
    form.append('password', password)
    const { data } = await api.post('/auth/token', form, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    })
    return data
  },

  /** JSON login variant used by the SPA. */
  async login(email, password) {
    const { data } = await api.post('/auth/login', { email, password })
    return data
  },

  async register({ email, password, fullName }) {
    const { data } = await api.post('/auth/register', {
      email,
      password,
      full_name: fullName || null,
    })
    return data
  },

  async me() {
    const { data } = await api.get('/auth/me')
    return data
  },

  async updateProfile({ fullName, password }) {
    const payload = {}
    if (fullName !== undefined) payload.full_name = fullName
    if (password) payload.password = password
    const { data } = await api.patch('/auth/me', payload)
    return data
  },

  async logout(refreshToken) {
    const { data } = await api.post('/auth/logout', {
      refresh_token: refreshToken,
    })
    return data
  },
}

export default authApi
