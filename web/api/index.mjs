// SPDX-License-Identifier: AGPL-3.0-only
import {handle} from '../lib/app.mjs';
import {settings} from '../lib/github.mjs';

export default async function handler(req, res) {
  try {
    const {origin} = settings();
    // Vercel keeps the incoming req.url for rewrites; never trust forwarded hosts.
    const url = new URL(req.url, origin);
    const headers = new Headers();
    for (const [key, value] of Object.entries(req.headers)) {
      if (value !== undefined) headers.set(key, Array.isArray(value) ? value.join(', ') : value);
    }
    const trusted = new URL(origin);
    headers.delete('forwarded');
    headers.set('host', trusted.host);
    headers.set('x-forwarded-host', trusted.host);
    headers.set('x-forwarded-proto', trusted.protocol.slice(0,-1));
    const init = {method: req.method, headers};
    if (!['GET', 'HEAD'].includes(req.method)) {
      if (req.body !== undefined) {
        init.body = typeof req.body === 'string' || Buffer.isBuffer(req.body) ? req.body :
          headers.get('content-type')?.includes('application/x-www-form-urlencoded') ? new URLSearchParams(req.body).toString() : JSON.stringify(req.body);
      } else {
        let length = 0; const chunks = [];
        for await (const chunk of req) {
          length += chunk.length;
          if (length > 260000) {res.statusCode = 413; res.end('Request too large'); return;}
          chunks.push(chunk);
        }
        init.body = Buffer.concat(chunks);
      }
    }
    const response = await handle(new Request(url, init));
    res.statusCode = response.status;
    for (const [key, value] of response.headers) {
      if (key !== 'set-cookie') res.setHeader(key, value);
    }
    const cookies = response.headers.getSetCookie();
    if (cookies.length) res.setHeader('Set-Cookie', cookies);
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch {
    res.statusCode = 503;
    res.setHeader('Cache-Control', 'no-store');
    res.end(JSON.stringify({error: '管理站环境尚未就绪'}));
  }
}
