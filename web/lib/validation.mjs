// SPDX-License-Identifier: AGPL-3.0-only
import Ajv from 'ajv';
import {readFileSync} from 'node:fs';

const schema = JSON.parse(readFileSync(new URL('./model-config.schema.json', import.meta.url), 'utf8'));
const structural = new Ajv({allErrors: true, strict: false}).compile(schema);
export const CONFIG_PATH = 'src/rp_core/compat_runtime/model-config.json';
export const SCHEMA = schema;

export function validateConfig(value) {
  if (!structural(value)) {
    return structural.errors.slice(0, 12).map(e => `${e.instancePath || '/'}: ${e.message}`);
  }
  const errors = [];
  if (/(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{30,}|[0-9]{8,12}:[A-Za-z0-9_-]{30,})/.test(JSON.stringify(value))) {
    errors.push('疑似凭据的值不能保存到公开配置');
  }
  const ids = new Set(value.models.map(m => m.id));
  if (ids.size !== value.models.length) errors.push('模型 ID 不能重复');
  if (new Set(value.providers.map(p => p.key)).size !== value.providers.length) errors.push('厂商不能重复');
  const routeCatalog = {
    claude:['proxy3','proxy1_base','proxy1_prime','proxy1_max','proxy2','openrouter','official'],
    gemini:['proxy3','proxy1','proxy2','openrouter','official'],
    gpt:['proxy3','proxy1','proxy2','openrouter','official'],
    grok:['proxy2','proxy1','openrouter','official'],
    deepseek:['official','fireworks_pro','openrouter'],
    glm:['official','openrouter'],qwen:['official'],magmarouter:['official'],openrouter:['official'],
  };
  for (const [provider,policy] of Object.entries(value.path_policies || {})) {
    const valid = new Set(routeCatalog[provider] || []);
    for (const key of ['order','disabled']) {
      const items = policy?.[key] || [];
      if (new Set(items).size !== items.length || items.some(route => !valid.has(route))) {
        errors.push(`通路配置无效：${provider}/${key}`);
      }
    }
  }
  const refs = [...value.low_tier_pool, ...value.summary_models, ...Object.values(value.dialog_tiers).flat()];
  if (refs.some(m => !ids.has(m))) errors.push('路由链引用了未注册模型');
  const required = {claude: ['pro','flash','lite'], gemini: ['pro','flash','lite'], deepseek: ['pro','flash'],
    gpt: [null], glm: ['pro','flash'], magmarouter: [null], openrouter: [null], grok: [null]};
  for (const [provider, tiers] of Object.entries(required)) {
    for (const tier of tiers) {
      if (!value.models.some(m => m.provider === provider && (tier === null || m.tier === tier))) {
        errors.push(`运行时必需的 ${provider}/${tier || '全部'} 模型池不能为空`);
      }
    }
  }
  if (!value.models.some(m => m.provider === 'gemini' && m.vision)) errors.push('需保留 Gemini 视觉模型');
  if (value.summary_models.some(id => value.models.find(m => m.id === id)?.provider !== 'claude')) {
    errors.push('总结客户端只接受 Claude 模型');
  }
  return errors;
}
