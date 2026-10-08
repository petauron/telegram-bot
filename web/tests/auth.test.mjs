import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'


const vite = await createServer({
  appType: 'custom',
  logLevel: 'silent',
  server: { middlewareMode: true },
})
const { authReducer, INITIAL_AUTH_STATE, isAuthenticated } = await vite.ssrLoadModule('/src/auth.js')
const { api, isUnauthorized } = await vite.ssrLoadModule('/src/api.js')
const {
  AuthCheckingView,
  LoginView,
  loginErrorMessage,
} = await vite.ssrLoadModule('/src/components/LoginView.jsx')

after(async () => {
  await vite.close()
})

test('auth gate blocks data state until session or login succeeds', () => {
  assert.equal(INITIAL_AUTH_STATE.status, 'checking')
  assert.equal(isAuthenticated(INITIAL_AUTH_STATE), false)
  const anonymous = authReducer(INITIAL_AUTH_STATE, { type: 'SESSION_ANONYMOUS' })
  assert.equal(anonymous.status, 'anonymous')
  assert.equal(isAuthenticated(anonymous), false)
  const authenticated = authReducer(anonymous, { type: 'LOGIN_SUCCEEDED', username: 'admin' })
  assert.equal(authenticated.status, 'authenticated')
  assert.equal(isAuthenticated(authenticated), true)
})

test('business 401 and logout return to the anonymous gate', () => {
  const authenticated = { status: 'authenticated', username: 'admin', notice: '' }
  const unauthorized = authReducer(authenticated, { type: 'BUSINESS_UNAUTHORIZED' })
  assert.equal(unauthorized.status, 'anonymous')
  assert.match(unauthorized.notice, /重新登录/)
  const loggedOut = authReducer(authenticated, { type: 'LOGOUT_SUCCEEDED' })
  assert.equal(loggedOut.status, 'anonymous')
  assert.equal(loggedOut.username, null)
})

test('login screen is accessible and never renders a credential value', () => {
  const checking = renderToStaticMarkup(React.createElement(AuthCheckingView))
  assert.match(checking, /正在检查登录状态/)
  const login = renderToStaticMarkup(React.createElement(LoginView, {
    onAuthenticate() {},
    onAuthenticated() {},
  }))
  assert.match(login, /登录管理界面/)
  assert.match(login, /autoComplete="username"/)
  assert.match(login, /autoComplete="current-password"/)
  assert.match(login, /type="password"/)
  assert.doesNotMatch(login, /使用服务器配置的管理员账号登录/)
  assert.doesNotMatch(login, /HttpOnly Cookie/)
  assert.doesNotMatch(login, /WEB_USERNAME|WEB_PASSWORD/)
  assert.doesNotMatch(login, /a-secure-test-password/)
})

test('login failures use generic UI errors', () => {
  assert.equal(loginErrorMessage({ status: 401 }), '用户名或密码不正确。')
  assert.equal(loginErrorMessage({ status: 422 }), '用户名或密码不正确。')
  assert.equal(loginErrorMessage({ status: 429 }), '登录尝试过多，请稍后再试。')
})

test('api exposes a typed unauthorized error for the auth gate', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: '需要登录' }),
    { status: 401, headers: { 'Content-Type': 'application/json' } },
  )
  try {
    await assert.rejects(
      api('/api/stats'),
      (error) => isUnauthorized(error) && error.status === 401,
    )
  } finally {
    globalThis.fetch = originalFetch
  }
})
