/**
 * Layout - application shell with sticky header, responsive navigation and
 * footer. Children are rendered inside the shared `.page` container.
 */

import { useState } from 'react'
import { NavLink, Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

function initials(nameOrEmail) {
  const source = (nameOrEmail || '').trim()
  if (!source) return '?'
  const parts = source.split(/[\s@.]+/).filter(Boolean)
  const letters = parts.slice(0, 2).map((part) => part[0].toUpperCase())
  return letters.join('') || source[0].toUpperCase()
}

export default function Layout({ children }) {
  const { user, isAuthenticated, signOut } = useAuth()
  const navigate = useNavigate()
  const [menuOpen, setMenuOpen] = useState(false)

  async function handleSignOut() {
    setMenuOpen(false)
    await signOut()
    navigate('/login')
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <Link to="/" className="brand">
          <span className="brand__mark">🎙️</span>
          <span className="brand__text">Niche Podcast Clipper</span>
          <span className="brand__badge">local&nbsp;AI</span>
        </Link>

        {isAuthenticated && (
          <nav className="nav-links">
            <NavLink
              to="/dashboard"
              className={({ isActive }) => `nav-link${isActive ? ' nav-link--active' : ''}`}
            >
              Dashboard
            </NavLink>
            <NavLink
              to="/profile"
              className={({ isActive }) => `nav-link${isActive ? ' nav-link--active' : ''}`}
            >
              Profile
            </NavLink>
          </nav>
        )}

        <div className="spacer" />

        {isAuthenticated ? (
          <div className="user-chip">
            <span className="user-chip__avatar">{initials(user?.full_name || user?.email)}</span>
            <span className="user-chip__name truncate">{user?.full_name || user?.email}</span>
            <button type="button" className="btn btn--ghost btn--sm" onClick={handleSignOut}>
              Sign out
            </button>
          </div>
        ) : (
          <div className="row">
            <Link to="/login" className="btn btn--ghost btn--sm">
              Sign in
            </Link>
            <Link to="/register" className="btn btn--primary btn--sm">
              Get started
            </Link>
          </div>
        )}
      </header>

      <main className="app-main">{children}</main>

      <footer className="app-footer">
        <span className="dim">
          Niche Podcast Clipper · on-premise Whisper + Flan-T5 · no cloud, no token fees
        </span>
        <span className="spacer" />
        <a href="http://localhost:8000/docs" target="_blank" rel="noreferrer">
          API docs
        </a>
      </footer>
    </div>
  )
}