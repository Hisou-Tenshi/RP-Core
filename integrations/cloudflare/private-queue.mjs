// SPDX-License-Identifier: AGPL-3.0-only
import { github, readData, encode, safePath, privateCommitMessage, privateCommitIdentity } from './telegram.mjs';

export async function bindings(env) {
  for (const key of ['DATA_REPOSITORY', 'PUBLIC_REPOSITORY']) {
    if (!new RegExp('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$').test(env[key] || '')) throw new Error('configuration');
  }
  if (!/^[a-z][a-z0-9_-]{0,47}$/.test(env.INSTANCE_ID || '')) throw new Error('configuration');
  const io = await readData(env, 'io.json');
  if (io.schema_version !== 1 || io.driver !== 'tenshi-v1') throw new Error('full io required');
  const identity = await readData(env, io.files?.identity || 'identity.json');
  const users = identity.allowed_user_ids;
  const chats = identity.telegram?.allowed_chat_ids || [];
  const target = identity.telegram?.chat_id;
  if (!Array.isArray(users) || users.length > 1 || !Array.isArray(chats) || chats.length > 1
      || (users.length && chats.length && String(chats[0]) !== String(users[0]))
      || (users.length && target != null && String(target) !== String(users[0]))) {
    throw new Error('full engine requires one owner in a private chat');
  }
  return { io, identity };
}

export async function enqueueLegacy(env, task, payload) {
  const { io, identity } = await bindings(env);
  if (task === 'reply') {
    const allowed = (identity.allowed_user_ids || []).map(String);
    const chats = (identity.telegram?.allowed_chat_ids || allowed).map(String);
    if (!allowed.includes(String(payload.user_id)) || !chats.includes(String(payload.chat_id))) throw new Error('unauthorized');
  }
  const scheduled = task === 'perception' && payload.scheduled_force && payload.trigger_at_ms;
  const stable = task === 'reply' || scheduled ? payload : { ...payload, bucket: Math.floor(Date.now() / 60000) };
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify({ task, payload: stable })));
  const id = 'full-' + [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  const path = safePath(io.files?.inbox || 'inbox') + '/' + id + '.json';
  const location = 'repos/' + env.DATA_REPOSITORY + '/contents/' + path;
  const ref = '?ref=' + encodeURIComponent(env.DATA_BRANCH || 'main');
  const existing = await github(location + ref, env.DATA_TOKEN);
  if (existing.status === 404) {
    const saved = await github(location, env.DATA_TOKEN, { method: 'PUT', body: JSON.stringify({ message: privateCommitMessage('queue'), branch: env.DATA_BRANCH || 'main', content: encode(JSON.stringify({ full_task: task, legacy_payload: payload })), ...privateCommitIdentity(env) }) });
    if (!saved.ok && !(saved.status === 422 && (await github(location + ref, env.DATA_TOKEN)).ok)) throw new Error('private queue');
  } else if (!existing.ok) throw new Error('private queue');
  const credentials = [...new Set([env.DISPATCH_TOKEN, env.CALLING_GH_TOKEN, env['CALLING_GH_TOKEN '], env.GH_TOKEN].filter(Boolean))];
  let dispatched = false;
  for (const credential of credentials) {
    const response = await github('repos/' + env.PUBLIC_REPOSITORY + '/dispatches', credential, { method: 'POST', body: JSON.stringify({ event_type: 'rp_event', client_payload: { instance_id: env.INSTANCE_ID, event_id: id } }) });
    if (response.ok) { dispatched = true; break; }
  }
  if (!dispatched) throw new Error('dispatch');
  return id;
}
