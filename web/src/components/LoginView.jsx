import { useState } from 'react'
import { Activity, ShieldCheck } from './Icons'


export function loginErrorMessage(error) {
  return error?.status === 429
    ? '登录尝试过多，请稍后再试。'
    : '用户名或密码不正确。'
}


export function AuthCheckingView() {
  return (
    <main className="auth-page" aria-busy="true">
      <section className="auth-card auth-checking" aria-label="正在检查登录状态">
        <div className="auth-brand"><span className="auth-mark"><Activity size={24} /></span><strong>群讯雷达</strong></div>
        <p>正在检查登录状态…</p>
      </section>
    </main>
  )
}


export function LoginView({ notice = '', onAuthenticate, onAuthenticated }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(notice)

  async function handleSubmit(event) {
    event.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError('')
    try {
      const session = await onAuthenticate({ username, password })
      setPassword('')
      onAuthenticated(session)
    } catch (requestError) {
      setError(loginErrorMessage(requestError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="auth-page">
      <section className="auth-card" aria-labelledby="login-title">
        <div className="auth-brand"><span className="auth-mark"><ShieldCheck size={25} /></span><strong>群讯雷达</strong></div>
        <div className="auth-heading">
          <h1 id="login-title">登录管理界面</h1>
        </div>

        <form className="auth-form" onSubmit={handleSubmit} noValidate>
          <label htmlFor="login-username">用户名</label>
          <input
            id="login-username"
            name="username"
            type="text"
            autoComplete="username"
            maxLength={128}
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={submitting}
            required
          />

          <label htmlFor="login-password">密码</label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            maxLength={1024}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
            required
          />

          <div className="auth-error" role="alert" aria-live="polite">
            {error || '\u00a0'}
          </div>

          <button className="button primary auth-submit" type="submit" disabled={submitting || !username || !password}>
            {submitting ? '登录中…' : '登录'}
          </button>
        </form>
      </section>
    </main>
  )
}
