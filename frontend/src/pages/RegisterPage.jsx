/**
 * RegisterPage - create a new account, then auto-login.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { describeError } from '../api/client'

export default function RegisterPage() {
  const { register } = useAuth()
  const navigate = useNavigate()
  const [form, setForm] = useState({ fullName: '', email: '', password: '', confirm: '' })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  function updateField(field) {
    return (event) => setForm((current) => ({ ...current, [field]: event.target.value }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)
    if (form.password !== form.confirm) {
      setError('Passwords do not match')
      return
    }
    setSubmitting(true)
    try {
      await register({ email: form.email, password: form.password, fullName: form.fullName })
      navigate('/dashboard')
    } catch (err) {
      setError(describeError(err, 'Could not create the account'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="page page--narrow">
      <div className="auth-card">
        <div className="center">
          <div className="auth-card__glyph">🚀</div>
          <h1>Create your workspace</h1>
          <p>Run the whole clipper pipeline on your own machine.</p>
        </div>

        <form onSubmit={handleSubmit} className="stack">
          <div className="field">
            <label className="field__label" htmlFor="reg-name">
              Display name <span className="dim">(optional)</span>
            </label>
            <input
              id="reg-name"
              className="input"
              type="text"
              autoComplete="name"
              value={form.fullName}
              onChange={updateField('fullName')}
              placeholder="Alex Doe"
            />
          </div>

          <div className="field">
            <label className="field__label" htmlFor="reg-email">
              Email
            </label>
            <input
              id="reg-email"
              className="input"
              type="email"
              autoComplete="email"
              required
              value={form.email}
              onChange={updateField('email')}
              placeholder="you@example.com"
            />
          </div>

          <div className="grid grid--2">
            <div className="field">
              <label className="field__label" htmlFor="reg-password">
                Password
              </label>
              <input
                id="reg-password"
                className="input"
                type="password"
                autoComplete="new-password"
                required
                minLength={8}
                value={form.password}
                onChange={updateField('password')}
                placeholder="At least 8 characters"
              />
            </div>
            <div className="field">
              <label className="field__label" htmlFor="reg-confirm">
                Confirm password
              </label>
              <input
                id="reg-confirm"
                className="input"
                type="password"
                autoComplete="new-password"
                required
                value={form.confirm}
                onChange={updateField('confirm')}
                placeholder="Repeat password"
              />
            </div>
          </div>

          {error && <div className="field__error center">{error}</div>}

          <button type="submit" className="btn btn--primary btn--block" disabled={submitting}>
            {submitting && <span className="btn__spinner" />}
            {submitting ? 'Creating account…' : 'Create account'}
          </button>
        </form>

        <p className="center dim">
          Already registered? <Link to="/login">Sign in</Link>
        </p>
      </div>
    </div>
  )
}