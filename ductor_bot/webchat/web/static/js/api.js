let csrf = null;
let credentialChanging = false, credentialEpoch = 0;
export function isCredentialChanging() { return credentialChanging; }
export function beginCredentialChange() { credentialChanging = true; ++credentialEpoch; }
export function finishCredentialChange(success) {
  credentialChanging = false;
  ++credentialEpoch;
  if (!success) checkSession();
}
export function checkSession(active = () => true) {
  const epoch = credentialEpoch;
  return api('/auth/state').then(state => {
    if (!active() || credentialChanging || epoch !== credentialEpoch) return;
    if (state.authenticated) {
      // Another tab may have rotated the shared session cookie; adopt its CSRF token.
      if (state.csrf) csrf = state.csrf;
      return;
    }
    csrf = null;
    document.dispatchEvent(new CustomEvent('auth-required'));
  }).catch(() => {});
}
// A login retires older requests even when the new session returns the same CSRF token.
export function setCsrf(value) { csrf = value; ++credentialEpoch; }

export class ApiError extends Error {
  constructor(status, body) {
    super(body.error || 'generic');
    this.status = status;
    this.code = body.error || 'generic';
    this.retryAfter = body.retry_after;
  }
}

export async function api(path, { method = 'GET', body, binary = false, headers: extra = {}, signal } = {}) {
  const epoch = credentialEpoch;
  const headers = { ...extra };
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    headers['X-Dtw-Client'] = 'web';
    if (csrf) headers['X-Dtw-Csrf'] = csrf;
  }
  if (body !== undefined) headers['Content-Type'] = binary ? 'application/octet-stream' : 'application/json';
  let response;
  try {
    response = await fetch(`/api${path}`, {
      method, headers, signal, credentials: 'same-origin', cache: 'no-store',
      ...(body === undefined ? {} : { body: binary ? body : JSON.stringify(body) }),
    });
  } catch { throw new ApiError(503, { error: 'bot_unreachable' }); }
  let data;
  try { data = await response.json(); }
  catch { throw new ApiError(response.status, { error: 'generic' }); }
  if (!response.ok) {
    if (response.status === 403 && data.error === 'auth_required'
        && !credentialChanging && epoch === credentialEpoch) {
      csrf = null;
      document.dispatchEvent(new CustomEvent('auth-required'));
    }
    throw new ApiError(response.status, data);
  }
  return data;
}
