// SPDX-License-Identifier: AGPL-3.0-only
// Optional low-latency ingress. The default polling deployment needs no Worker.
const reply = (status, text) => new Response(text, { status });

function sameSecret(left, right) {
  if (!left || !right || left.length !== right.length) return false;
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return difference === 0;
}

export function safePath(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_./-]+$/.test(value) || value.startsWith('/') || value.split('/').some(p => p === '..' || p === '.git')) throw new Error('binding');
  return value;
}

export function encode(text) {
  let binary = '';
  for (const byte of new TextEncoder().encode(text)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function decode(text) {
  return new TextDecoder().decode(Uint8Array.from(atob(text.replace(/\s/g, '')), c => c.charCodeAt(0)));
}

export async function github(path, token, options = {}) {
  return fetch('https://api.github.com/' + path, { ...options, headers: { Accept: 'application/vnd.github+json', Authorization: 'Bearer ' + token, 'User-Agent': 'RP-Core-ingress', 'Content-Type': 'application/json', ...(options.headers || {}) } });
}

export async function readData(env, path, optional = false) {
  const response = await github('repos/' + env.DATA_REPOSITORY + '/contents/' + safePath(path) + '?ref=' + encodeURIComponent(env.DATA_BRANCH || 'main'), env.DATA_TOKEN);
  if (optional && response.status === 404) return {};
  if (!response.ok) throw new Error('data');
  const file = await response.json();
  if (typeof file.content !== 'string') throw new Error('data');
  return JSON.parse(decode(file.content));
}

export async function normalize(update, identity) {
  const message = update.message || update.edited_message;
  if (!message) return null;
  const actor = message.from || {};
  if (actor.is_bot || !(identity.allowed_user_ids || []).map(String).includes(String(actor.id))) return null;
  const chat = String(message.chat.id);
  const allowedChats = (identity.telegram?.allowed_chat_ids || []).map(String);
  if (allowedChats.length && !allowedChats.includes(chat)) return null;
  const content = [];
  if (message.text) content.push({ type: 'text', text: message.text });
  if (message.location) content.push({ type: 'location', latitude: message.location.latitude, longitude: message.location.longitude });
  const photo = message.photo || [];
  const attachment = photo.length ? photo[photo.length - 1] : message.document;
  if (attachment) content.push({ type: 'attachment', reference: attachment.file_id, media_type: attachment.mime_type || (photo.length ? 'image/jpeg' : 'application/octet-stream'), caption: message.caption || '' });
  if (!content.length) return null;
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(chat + ':' + update.update_id));
  const id = 'telegram-' + [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, '0')).join('').slice(0, 40);
  return { id, session_id: chat, kind: 'message', occurred_at: new Date(message.date * 1000).toISOString().replace('.000Z', 'Z'), actor: { id: String(actor.id), display_name: String(actor.username || actor.first_name || actor.id) }, content, reply_to: String(message.message_id), extensions: { telegram: { message_id: String(message.message_id), chat_id: chat } } };
}

export default {
  async fetch(request, env) {
    if (request.method !== 'POST') return reply(405, 'POST required');
    if (!sameSecret(request.headers.get('X-Telegram-Bot-Api-Secret-Token'), env.WEBHOOK_SECRET)) return reply(403, 'Forbidden');
    try {
      for (const name of ['DATA_REPOSITORY', 'PUBLIC_REPOSITORY']) {
        if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(env[name] || '')) return reply(503, 'Not configured');
      }
      if (!/^[a-z][a-z0-9_-]{0,47}$/.test(env.INSTANCE_ID || '')) return reply(503, 'Not configured');
      const raw = await request.text();
      if (new TextEncoder().encode(raw).length > 65536) return reply(413, 'Too large');
      const io = await readData(env, 'io.json');
      if (io.schema_version !== 1 || !['native', 'tenshi-v1'].includes(io.driver)) return reply(503, 'Invalid data bindings');
      const identity = await readData(env, io.files?.identity || 'identity.json');
      const event = await normalize(JSON.parse(raw), identity);
      if (!event) return reply(200, 'Ignored');
      const directory = safePath(io.files?.inbox || 'inbox');
      const path = directory + '/' + event.id + '.json';
      const existing = await github('repos/' + env.DATA_REPOSITORY + '/contents/' + path + '?ref=' + encodeURIComponent(env.DATA_BRANCH || 'main'), env.DATA_TOKEN);
      if (existing.status === 404) {
        const saved = await github('repos/' + env.DATA_REPOSITORY + '/contents/' + path, env.DATA_TOKEN, { method: 'PUT', body: JSON.stringify({ message: 'Queue RP event [skip ci]', content: encode(JSON.stringify({ event })), branch: env.DATA_BRANCH || 'main' }) });
        if (!saved.ok && saved.status !== 422) return reply(503, 'Queue unavailable');
        if (saved.status === 422) {
          const checked = await github('repos/' + env.DATA_REPOSITORY + '/contents/' + path + '?ref=' + encodeURIComponent(env.DATA_BRANCH || 'main'), env.DATA_TOKEN);
          if (!checked.ok) return reply(503, 'Queue unavailable');
        }
      } else if (!existing.ok) return reply(503, 'Queue unavailable');
      const dispatched = await github('repos/' + env.PUBLIC_REPOSITORY + '/dispatches', env.DISPATCH_TOKEN, { method: 'POST', body: JSON.stringify({ event_type: 'rp_event', client_payload: { instance_id: env.INSTANCE_ID, event_id: event.id } }) });
      return dispatched.ok ? reply(200, 'Queued') : reply(503, 'Dispatch unavailable');
    } catch {
      return reply(503, 'Ingress unavailable');
    }
  }
};
