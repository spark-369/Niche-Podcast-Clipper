/**
 * AuthContext - global JWT session state.
 *
 * Holds the access/refresh token pair plus the current user, and exposes
 * login/register/logout helpers to the rest of the app.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import authApi from '../api/auth'
import {
  clearTokens,
  getAccessToken,
  getRefreshToken,
  getStoredUser,
  setTokens,
  storeUser,
} from '../api/client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => getStoredUser())
  const [token, setToken] = useState(() => getAccessToken())
  const [initialising, setInitialising] = useState(Boolean(getAccessToken()))

  const persistSession = useCallback((tokenPayload, nextUser) => {
    setTokens(tokenPayload)
    setToken(tokenPayload.access_token)
    if (nextUser) {
      storeUser(nextUser)
      setUser(nextUser)
    }
  }, [])

  const signOut = useCallback(async () => {
    const refreshToken = getRefreshToken()
    if (refreshToken) {
      try {
        await authApi.logout(refreshToken)
      } catch {
        // Best effort: the local session is cleared regardless.
      }
    }
    clearTokens()
    setToken(null)
    setUser(null)
  }, [])

  // Validate the stored token on first mount.
  useEffect(() => {
    let cancelled = false

    async function bootstrap() {
      if (!getAccessToken()) {
        setInitialising(false)
        return
      }
      try {
        const profile = await authApi.me()
        if (!cancelled) {
          storeUser(profile)
          setUser(profile)
        }
      } catch {
        if (!cancelled) {
          clearTokens()
          setUser(null)
          setToken(null)
        }
      } finally {
        if (!cancelled) setInitialising(false)
      }
    }

    bootstrap()
    return () => {
      cancelled = true
    }
  }, [])

  // React to a failed silent refresh coming from the axios interceptor.
  useEffect(() => {
    const handler = () => {
      setToken(null)
      setUser(null)
    }
    window.addEventListener('npc:signed-out', handler)
    return () => window.removeEventListener('npc:signed-out', handler)
  }, [])

  const login = useCallback(
    async (email, password) => {
      const tokenPayload = await authApi.login(email, password)
      persistSession(tokenPayload, null)
      const profile = await authApi.me()
      persistSession(tokenPayload, profile)
      return profile
    },
    [persistSession],
  )

  const register = useCallback(
    async ({ email, password, fullName }) => {
      await authApi.register({ email, password, fullName })
      return login(email, password)
    },
    [login],
  )

  const updateProfile = useCallback(async (payload) => {
    const profile = await authApi.updateProfile(payload)
    storeUser(profile)
    setUser(profile)
    return profile
  }, [])

  const value = useMemo(
    () => ({
      user,
      token,
      isAuthenticated: Boolean(token && user),
      initialising,
      login,
      register,
      signOut,
      updateProfile,
    }),
    [user, token, initialising, login, register, signOut, updateProfile],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside an <AuthProvider>')
  return context
}

export default AuthContext
