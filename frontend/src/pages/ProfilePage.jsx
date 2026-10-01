/**
 * ProfilePage - update display name / password, inspect the JWT session.
 */

import { useState } from 'react'
import { useAuth } from '../context/AuthContext'
import { describeError } from '../api/client'

export default function ProfilePage() {
  const { user, updateProfile, signOut } = useAuth()
  const [fullName, setFullName] = useState(user?.full_name || '')
  const [password, setPassword] = useState('')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState(null)
  const [error, setError] = useState(null)

  async function handleSubmit(event) {
    event.preventDefault()
    setMessage(null)
    setError(null)
    setSaving(true)
    try {
      await updateProfile({ fullName, password: password || undefined })
      setPassword('')
      setMessage('Profile updated')
    } catch (err) {
      setError(describeError(err, 'Could not update the profile'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="page page--narrow stack stack--lg">
      <header>
        <h1>Profile</h1>
        <p className="muted">Manage your account and inspect the active session.</p>
      </header>

      <form onSubmit={handleSubmit} className="card stack">
        <div className="field">
          <label className="field__label" htmlFor="profile-name">
            Display name
          </label>
          <input
            id="profile-name"
            className="input"
            value={fullName}
            onChange={(event) => setFullName(event.target.value)}
            placeholder="Your name"
          />
        </div>

        <div className="field">
          <label className="field__label" htmlFor="profile-email">
            Email
          </label>
          <input id="profile-email" className="input" value={user?.email || ''} disabled />
          <span className="field__hint">The account email cannot be changed.</span>
        </div>

        <div className="field">
          <label className="field__label" htmlFor="profile-password">
            New password
          </label>
          <input
            id="profile-password"
            className="input"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="Leave blank to keep the current password"
          />
        </div>

        {message && <div className="badge badge--success">{message}</div>}
        {error && <div className="field__error">{error}</div>}

        <div className="row">
          <button type="submit" className="btn btn--primary" disabled={saving}>
            {saving && <span className="btn__spinner" />}
            {saving ? 'Saving…' : 'Save changes'}
          </button>
          <button type="button" className="btn btn--danger" onClick={signOut}>
            Sign out everywhere
          </button>
        </div>
      </form>

      <section className="card stack stack--sm">
        <h3>Session</h3>
        <div className="row dim mono">
          <span>User ID: {user?.id}</span>
        </div>
        <p className="dim">
          Access tokens rotate every 30 minutes and refresh silently through
          <code> /auth/refresh</code>. Signing out revokes the refresh token in Redis.
        </p>
      </section>
    </div>
  )
}