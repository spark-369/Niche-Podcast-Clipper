/**
 * LoginPage - email/password sign-in against the JWT backend.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { describeError } from '../api/client'

export default function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(email, password)
      navigate('/dashboard')
    } catch (err) {
      setError(describeError(err, 'Could not sign in'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="page page--narrow">
      <div className="auth-card">
        <div className="center">
          <div className="auth-card__glyph">🔐</div>
          <h1>Welcome back</h1>
          <p>Sign in to your local clipper workspace.</p>
        </div>

        <form onSubmit={handleSubmit} className="stack">
          <div className="field">
            <label className="field__label" htmlFor="login-email">
              Email
            </label>
            <input
              id="login-email"
              className="input"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="you@example.com"
            />
          </div>

          <div className="field">
            <label className="field__label" htmlFor="login-password">
              Password
            </label>
            <input
              id="login-password"
              className="input"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="••••••••"
            />
          </div>

          {error && <div className="field__error center">{error}</div>}

          <button type="submit" className="btn btn--primary btn--block" disabled={submitting}>
            {submitting && <span className="btn__spinner" />}
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="center dim">
          No account yet? <Link to="/register">Create one</Link>
        </p>
      </div>
    </div>
  )
}