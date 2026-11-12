let csrf = null;
export function setCsrf(value) { csrf = value; }

export class ApiError extends Error {
  constructor(status, body) {
    super(body.error || 'generic');
    this.status = status;
    this.code = body.error || 'generic';
    this.retryAfter = body.retry_after;
  }
}

export async function api(path, { method = 'GET', body } = {}) {
  const headers = {};
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    headers['X-Dtw-Client'] = 'web';
    if (csrf) headers['X-Dtw-Csrf'] = csrf;
  }
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(`/api${path}`, {
      method, headers, credentials: 'same-origin', cache: 'no-store',
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch { throw new ApiError(503, { error: 'bot_unreachable' }); }
  let data;
  try { data = await response.json(); }
  catch { throw new ApiError(response.status, { error: 'generic' }); }
  if (!response.ok) {
    if (response.status === 403 && data.error === 'auth_required') {
      csrf = null;
      document.dispatchEvent(new CustomEvent('auth-required'));
    }
    throw new ApiError(response.status, data);
  }
  return data;
}
