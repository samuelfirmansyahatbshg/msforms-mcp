async ({url, method, body}) => {
  const info = window.OfficeFormServerInfo || {};
  const target = new URL(url, location.origin);
  if (target.origin !== location.origin || !(target.pathname.startsWith('/formapi/api/') ||
      (method === 'GET' && target.pathname.startsWith('/formapi/msgraph/v1.0/'))))
    throw new Error('Refused request outside the Forms API');
  const response = await fetch(target.href, {
    method, credentials: 'same-origin',
    headers: {
      accept: 'application/json', 'content-type': 'application/json',
      'odata-version': '4.0', 'odata-maxverion': '4.0',
      'x-ms-form-request-source': 'ms-formweb',
      'x-ms-form-request-ring': (info.ring || 'Business').toLowerCase(),
      'x-correlationid': crypto.randomUUID(),
      'x-usersessionid': info.serverSessionId || crypto.randomUUID(),
      __requestverificationtoken: info.antiForgeryToken || '',
    },
    body: body === null ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { /* never return HTML */ }
  return {status: response.status, data, retryAfter: response.headers.get('retry-after')};
}
