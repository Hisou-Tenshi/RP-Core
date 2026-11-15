// SPDX-License-Identifier: AGPL-3.0-only
import {authReady, handleAuth, sessionToken} from './auth.mjs';
import {createHash} from 'node:crypto';
import {CONFIG_PATH, SCHEMA, validateConfig} from './validation.mjs';
import {HttpError, settings, readPublic, instances, requireWriter, writeConfig, github} from './github.mjs';

const json = (value, status = 200) => Response.json(value, {status, headers: {'Cache-Control': 'no-store', 'Vary': 'Cookie'}});
export function checkMutation(request, env = process.env) {
  if (request.headers.get('origin') !== settings(env).origin) throw new HttpError(403, '请求来源不匹配，请从管理页操作');
  if ((request.headers.get('content-type') || '').split(';')[0] !== 'application/json') throw new HttpError(415, '请使用 JSON 请求');
}

export async function handle(request, {env = process.env, fetcher = fetch, getSession = sessionToken} = {}) {
  const options = {env, fetcher};
  try {
    const {repository, branch, origin, adminIds} = settings(env);
    const url = new URL(request.url);
    const path = url.pathname;
    if (url.origin !== origin) throw new HttpError(403, '请使用配置的管理站域名');
    if (path.startsWith('/api/auth/')) {
      if (!authReady(env)) throw new HttpError(503, 'GitHub OAuth 尚未配置完成');
      return await handleAuth(request, env);
    }
    if (path === '/api/session' && request.method === 'GET') {
      const token = await getSession(request, env);
      const authorized = token?.accessToken && adminIds.has(String(token.githubId));
      return json({authenticated: Boolean(authorized), login: authorized ? token.login : null,
        oauth_ready: authReady(env), repository, branch});
    }
    if (path === '/api/config' && request.method === 'GET') {
      const doc = await readPublic(CONFIG_PATH, options);
      if (!doc) throw new HttpError(404, '找不到模型配置');
      return json({config: doc.data, sha: doc.sha, config_hash: doc.config_hash, schema: SCHEMA, repository, branch});
    }
    if (path === '/api/instances' && request.method === 'GET') return json({instances: await instances(options)});
    if (path === '/api/health' && request.method === 'GET') {
      const id = url.searchParams.get('instance') || '';
      const registered = await instances(options);
      if (!registered.some(i => i.id === id)) throw new HttpError(404, '未注册的实例');
      const [latest, history] = await Promise.all([readPublic(`health/${id}/latest.json`, options), readPublic(`health/${id}/history.json`, options)]);
      return json({available: Boolean(latest), latest: latest?.data || null, history: history?.data || []});
    }
    if (path === '/api/config' && request.method === 'PUT') {
      checkMutation(request, env);
      const token = await getSession(request, env);
      if (!token) throw new HttpError(401, '请先登录 GitHub');
      const text = await request.text();
      if (Buffer.byteLength(text) > 250000) throw new HttpError(413, '配置超过大小限制');
      const body = JSON.parse(text);
      if (!/^[a-f0-9]{40}$/.test(body.sha || '')) throw new HttpError(400, '缺少配置版本，请重新载入');
      const errors = validateConfig(body.config);
      if (errors.length) return json({error: '配置校验失败', details: errors}, 400);
      const saved = await writeConfig(body.config, body.sha, token, options);
      const config_hash = createHash('sha256').update(JSON.stringify(body.config, null, 2) + '\n').digest('hex');
      return json({saved: true, sha: saved.content.sha, config_hash, commit: saved.commit.sha, url: saved.commit.html_url});
    }
    if (path === '/api/probe' && request.method === 'POST') {
      checkMutation(request, env);
      const token = await getSession(request, env);
      await requireWriter(token, options);
      const text = await request.text();
      if (text.length > 200) throw new HttpError(413, '请求过大');
      const {instance_id: id} = JSON.parse(text);
      if (!(await instances(options)).some(i => i.id === id)) throw new HttpError(404, '未注册的实例');
      await github(`/repos/${repository}/actions/workflows/provider-health.yml/dispatches`, {
        token: token.accessToken, method: 'POST', fetcher, body: {ref: branch, inputs: {instance_id: id}},
      });
      return json({queued: true, url: `https://github.com/${repository}/actions/workflows/provider-health.yml`}, 202);
    }
    throw new HttpError(404, '没有这个接口');
  } catch (error) {
    if (error instanceof SyntaxError) return json({error: 'JSON 格式错误'}, 400);
    // Never return provider responses, request headers, OAuth codes, or stacks.
    return json({error: error instanceof HttpError ? error.message : '服务暂时不可用，请稍后重试'}, error.status || 503);
  }
}
