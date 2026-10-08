export const INITIAL_AUTH_STATE = Object.freeze({
  status: 'checking',
  username: null,
  notice: '',
})

export function authReducer(state, action) {
  switch (action.type) {
    case 'SESSION_AUTHENTICATED':
    case 'LOGIN_SUCCEEDED':
      return { status: 'authenticated', username: action.username, notice: '' }
    case 'SESSION_ANONYMOUS':
    case 'LOGOUT_SUCCEEDED':
      return { status: 'anonymous', username: null, notice: action.notice || '' }
    case 'SESSION_FAILED':
      return {
        status: 'anonymous',
        username: null,
        notice: '无法确认登录状态，请检查网络后重试。',
      }
    case 'BUSINESS_UNAUTHORIZED':
      return {
        status: 'anonymous',
        username: null,
        notice: '登录已失效，请重新登录。',
      }
    default:
      return state
  }
}

export function isAuthenticated(state) {
  return state.status === 'authenticated' && Boolean(state.username)
}
