import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './context/AuthContext'
import Layout from './components/Layout'

import HomePage from './pages/HomePage'
import LoginPage from './pages/LoginPage'
import RegisterPage from './pages/RegisterPage'
import DashboardPage from './pages/DashboardPage'
import EpisodeDetailPage from './pages/EpisodeDetailPage'
import ClipDetailPage from './pages/ClipDetailPage'
import ClipEditorPage from './pages/ClipEditorPage'
import ProfilePage from './pages/ProfilePage'

function SessionGate({ children }) {
  const { initialising } = useAuth()
  if (initialising) {
    return (
      <div className="page center">
        <div className="stat">
          <p className="dim">Checking session…</p>
        </div>
      </div>
    )
  }
  return children
}

function ProtectedRoute({ children }) {
  const { isAuthenticated } = useAuth()
  if (!isAuthenticated) return <Navigate to="/login" replace />
  return children
}

function PublicOnlyRoute({ children }) {
  const { isAuthenticated } = useAuth()
  if (isAuthenticated) return <Navigate to="/dashboard" replace />
  return children
}

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route
          path="/login"
          element={
            <SessionGate>
              <PublicOnlyRoute>
                <LoginPage />
              </PublicOnlyRoute>
            </SessionGate>
          }
        />
        <Route
          path="/register"
          element={
            <SessionGate>
              <PublicOnlyRoute>
                <RegisterPage />
              </PublicOnlyRoute>
            </SessionGate>
          }
        />
        <Route
          path="/dashboard"
          element={
            <SessionGate>
              <ProtectedRoute>
                <DashboardPage />
              </ProtectedRoute>
            </SessionGate>
          }
        />
        <Route
          path="/episodes/:episodeId"
          element={
            <SessionGate>
              <ProtectedRoute>
                <EpisodeDetailPage />
              </ProtectedRoute>
            </SessionGate>
          }
        />
        <Route
          path="/clips/:clipId"
          element={
            <SessionGate>
              <ProtectedRoute>
                <ClipDetailPage />
              </ProtectedRoute>
            </SessionGate>
          }
        />
        <Route
          path="/clips/:clipId/edit"
          element={
            <SessionGate>
              <ProtectedRoute>
                <ClipEditorPage />
              </ProtectedRoute>
            </SessionGate>
          }
        />
        <Route
          path="/profile"
          element={
            <SessionGate>
              <ProtectedRoute>
                <ProfilePage />
              </ProtectedRoute>
            </SessionGate>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  )
}
