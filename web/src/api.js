const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export async function api(path, options = {}) {
  const { headers = {}, ...requestOptions } = options
  const method = String(requestOptions.method || 'GET').toUpperCase()
  const writeHeaders = SAFE_METHODS.has(method)
    ? {}
    : { 'Content-Type': 'application/json', 'X-Requested-With': 'admin-ui' }
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...requestOptions,
    method,
    headers: {
      ...writeHeaders,
      ...headers,
    },
  })

  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`
    try {
      const payload = await response.json()
      if (payload.detail) message = typeof payload.detail === 'string' ? payload.detail : message
    } catch {
      // Keep the status-only message when the response is not JSON.
    }
    throw new ApiError(message, response.status)
  }
  return response.json()
}

export function isUnauthorized(error) {
  return error instanceof ApiError && error.status === 401
}

export function params(values) {
  const query = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) query.set(key, value)
  })
  return query.toString()
}
