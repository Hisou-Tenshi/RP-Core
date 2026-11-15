// SPDX-License-Identifier: AGPL-3.0-only
import {createHash, randomUUID} from 'node:crypto';
import {CONFIG_PATH} from './validation.mjs';

export class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function settings(env = process.env) {
  const repository = env.RP_PUBLIC_REPOSITORY || '';
  const branch = env.RP_PUBLIC_BRANCH || 'main';
  const origin = env.RP_APP_ORIGIN || 'http://localhost:3000';
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository) || !/^[A-Za-z0-9._/-]+$/.test(branch)) {
    throw new HttpError(503, '请先配置公开框架仓库');
  }
  return {repository, branch, origin: new URL(origin).origin,
    adminIds: new Set((env.RP_ADMIN_IDS || '').split(',').map(x => x.trim()).filter(Boolean))};
}

export async function github(path, {token, method = 'GET', body, fetcher = fetch} = {}) {
  const response = await fetcher(`https://api.github.com${path}`, {
    method, headers: {Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'RP-Core-console/1',
      ...(token ? {Authorization: `Bearer ${token}`} : {}),
      ...(body ? {'Content-Type': 'application/json'} : {})},
    ...(body ? {body: JSON.stringify(body)} : {}), signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    const status = response.status;
    throw new HttpError(status === 409 || status === 422 ? 409 : status === 401 || status === 403 ? 403 : 502,
      status === 409 || status === 422 ? '配置已在别处更新，请重新载入后再保存' :
      status === 401 || status === 403 ? 'GitHub 授权已失效或没有仓库写权限，请重新登录' : 'GitHub 暂时无法完成请求');
  }
  return response.status === 204 ? null : response.json();
}

export async function readPublic(path, {env = process.env, fetcher = fetch} = {}) {
  const {repository, branch} = settings(env);
  // Raw GitHub responses can remain cached after a successful Contents API write,
  // even with no-cache headers. A unique read URL prevents stale blob SHAs from
  // trapping the editor in repeated optimistic-concurrency conflicts.
  const url = `https://raw.githubusercontent.com/${repository}/${encodeURIComponent(branch)}/${path}?rp_read=${randomUUID()}`;
  const response = await fetcher(url, {cache: 'no-store', headers: {'User-Agent': 'RP-Core-console/1', 'Cache-Control': 'no-cache'},
    signal: AbortSignal.timeout(15000)});
  if (response.status === 404) return null;
  if (!response.ok) throw new HttpError(502, '暂时无法读取公开仓库');
  const text = await response.text();
  if (Buffer.byteLength(text) > 2_000_000) throw new HttpError(502, '公开文件超过大小限制');
  return {data: JSON.parse(text), sha: createHash('sha1').update(`blob ${Buffer.byteLength(text)}\0`).update(text).digest('hex'),
    config_hash: createHash('sha256').update(text).digest('hex')};
}

export async function instances(options) {
  const registry = await readPublic('config/instances.json', options);
  return (registry?.data.instances || []).map(i => ({id: i.id, environment: i.environment}));
}

export async function requireWriter(token, {env = process.env, fetcher = fetch} = {}) {
  const {repository, adminIds} = settings(env);
  if (!token?.accessToken || !adminIds.has(String(token.githubId))) throw new HttpError(401, '请使用获准的 GitHub 账号登录');
  const user = await github('/user', {token: token.accessToken, fetcher});
  if (String(user.id) !== String(token.githubId)) throw new HttpError(403, '登录身份校验失败');
  const repo = await github(`/repos/${repository}`, {token: token.accessToken, fetcher});
  if (!repo.permissions?.push || repo.private) throw new HttpError(403, '需要公开框架仓库的写权限');
  return {id: String(user.id), login: user.login};
}

export async function writeConfig(config, sha, token, options = {}) {
  const {repository, branch} = settings(options.env);
  const actor = await requireWriter(token, options);
  return github(`/repos/${repository}/contents/${CONFIG_PATH}`, {token: token.accessToken, method: 'PUT', fetcher: options.fetcher,
    body: {message: `Update shared model configuration via console (@${actor.login})`, branch, sha,
      content: Buffer.from(JSON.stringify(config, null, 2) + '\n').toString('base64')}});
}
