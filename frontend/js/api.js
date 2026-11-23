const ROOT = '/api';
let csrfToken = '';
const isPublicAuth = path => /^\/auth\/(config|login|register)$/.test(path);

async function readJson(response) {
  const type = response.headers.get('content-type') || '';
  if (!type.includes('application/json')) return null;
  return response.json();
}

export async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const publicAuth = isPublicAuth(path);
  if (!publicAuth && method !== 'GET' && !csrfToken) {
    const sessionResponse = await fetch(ROOT + '/auth/me', { credentials: 'same-origin', cache: 'no-store' });
    if (sessionResponse.status === 401) {
      if (location.pathname !== '/login') location.replace('/login');
      throw new Error('Sign in to continue.');
    }
    const session = await readJson(sessionResponse);
    csrfToken = session?.csrf_token || '';
  }
  const headers = options.body === undefined ? {} : { 'Content-Type': 'application/json' };
  if (!publicAuth && method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken;
  const response = await fetch(ROOT + path, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: 'same-origin',
    cache: 'no-store'
  });
  const type = response.headers.get('content-type') || '';
  if (!response.ok) {
    if (response.status === 401 && location.pathname !== '/login' && !publicAuth) location.replace('/login');
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).error || message; } catch (_) {}
    throw new Error(message);
  }
  if (options.raw || type.includes('text/csv') || type.includes('spreadsheet')) return response;
  const result = await response.json();
  if (result?.csrf_token) csrfToken = result.csrf_token;
  return result;
}

export function query(values = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : '';
}

export async function download(path, filename) {
  const response = await api(path, { raw: true });
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}
