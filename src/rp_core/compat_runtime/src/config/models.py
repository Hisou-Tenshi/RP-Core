# SPDX-License-Identifier: AGPL-3.0-only


































from __future__ import annotations

import json
import os
import random
import time as _time
from dataclasses import dataclass
from typing import Iterable

from config.providers import MAGMAROUTER_API_KEY, OPENROUTER_API_KEY
from _rp_model_config import CONFIG as MODEL_CONFIG

__all__ = [

    "ModelSpec",
    "ProviderSpec",
    "PROVIDERS",
    "MODELS",

    "MODEL_REGISTRY",
    "MODEL_TIER_RANK",
    "MODEL_TIER_LABELS",
    "FALLBACK_PROVIDER_ORDER",
    "PROVIDER_LABELS",
    "model_spec",
    "model_provider",
    "model_generation",
    "model_tier",
    "provider_models",
    "sort_models_by_generation",
    "provider_ranked_models",
    "disaster_recovery_models",
    "provider_for_model",

    "CLAUDE_MAX_MODELS",
    "CLAUDE_PRO_MODELS",
    "CLAUDE_FLASH_MODELS",
    "CLAUDE_LITE_MODELS",
    "DEEPSEEK_PRO_MODELS",
    "DEEPSEEK_FLASH_MODELS",
    "GEMINI_PRO_MODELS",
    "GEMINI_FLASH_MODELS",
    "GEMINI_LITE_MODELS",
    "GEMINI_IMAGE_MODELS",
    "GEMINI_ALL_MODELS",
    "MAGMAROUTER_MODELS",
    "OPENROUTER_MODELS",
    "GROK_MODELS",
    "GPT_FLASH_MODELS",
    "GPT_MODELS",
    "GLM_PRO_MODELS",
    "GLM_FLASH_MODELS",
    "KIMI_MODELS",
    "KIMI_PRO_MODELS",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "EMBEDDING_MAX_INPUT_CHARS",

    "OPENROUTER_FALLBACK_MAP",
    "openrouter_fallback_slug",

    "DIALOG_TIER_MAX_MODELS",
    "DIALOG_TIER_HIGH_MODELS",
    "DIALOG_TIER_MEDIUM_MODELS",
    "DIALOG_TIER_LOW_MODELS",
    "DIALOG_TIER_MINIMAL_MODELS",
    "DIALOG_TIER_ORDER",
    "DIALOG_TIER_MODELS",
    "DIALOG_TIER_HEAD_MODELS",
    "DIALOG_FALLBACK_MODELS",
    "LOW_TIER_FLASH_POOL",
    "low_tier_flash_models",
    "low_tier_random_primary",
    "normalize_dialog_tier",
    "dialog_tier_models",
    "dialog_chain_from_tier",
    "dialog_model_chain",

    "TASK_MODEL_PRIMARY",
    "task_model_primary",
    "task_model_chain",
    "dedupe_models",
    "deepseek_dialog_models",
    "deepseek_preprocess_models",
    "deepseek_merge_models",
    "deepseek_dialog_primary",
    "deepseek_preprocess_primary",
    "deepseek_context_compression_primary",
    "deepseek_primary",
    "claude_dialog_models",
    "claude_lite_models",
    "claude_lite_primary",
    "claude_summary_model_chain",
    "gemini_dialog_models",
    "gemini_pro_models",
    "gemini_image_models",
    "gemini_lite_primary",
    "gemini_image_primary",
    "magmarouter_models",
    "magmarouter_primary",
    "openrouter_models",
    "openrouter_primary",
    "grok_models",
    "grok_primary",
    "grok_fallback",
    "grok_slot_models",
    "grok_slot_primary",
    "grok_slot_rewrite_model",
    "gpt_flash_models",
    "gpt_flash_primary",
    "gpt_models",
    "gpt_primary",
    "glm_models",
    "glm_pro_models",
    "glm_flash_models",
    "glm_pro_primary",
    "glm_flash_primary",
    "kimi_models",
    "kimi_primary",
]


























MODEL_TIER_RANK: dict[str, int] = {"max": 0, "pro": 1, "flash": 2, "lite": 3}
MODEL_TIER_LABELS: dict[str, str] = {"max": "Max", "pro": "Pro", "flash": "Flash", "lite": "Lite"}


@dataclass(frozen=True)
class ModelSpec:
    """一个模型的静态事实(唯一登记处)。

    generation 用厂商自己的数字口径:Claude 5.5 = 5.5、Gemini 3.1 = 3.1、
    DeepSeek v4 = 4.0、MagmaRouter 里的 qwen3.8 = 3.8。
    非数字命名(如 magma-1)按厂商发布序给一个可解释的值即可,不影响同厂比较。

    slug:OpenRouter 上同款模型的 slug;空字符串 = 无 OpenRouter 兜底。
    order:同厂商列表内的手动序号(越小越前);None = 按代际/等级自动排序。
           只有「顺序不能靠代际表达」的组才需要填(见 Claude Lite / MagmaRouter)。
    vision:是否能做图片识别 → 自动进入 GEMINI_IMAGE_MODELS 一类的视觉列表。
    in_chain:是否进入主对话容灾组/链(日期快照仅供探活,不参与排序链)。
    """

    id: str
    provider: str
    tier: str
    generation: float
    slug: str = ""
    note: str = ""
    in_chain: bool = True
    vision: bool = False
    order: int | None = None

    @property
    def sort_key(self) -> tuple[float, int, str]:
        """同厂内排序键:代际降序 → 等级升序 → 原 id 稳定兜底。"""
        return (-self.generation, MODEL_TIER_RANK.get(self.tier, 99), self.id)


@dataclass(frozen=True)
class ProviderSpec:
    """一家厂商(唯一登记处)。

    in_fallback_chain:是否参与容灾组厂商轮转;False = 只做专用槽(OpenRouter 等)。
    route:模型 id → 通路槽的别名;仅 MagmaRouter / OpenRouter 需要(= "grok")。
    model_prefixes:未注册模型 id 的厂商前缀兜底(provider_for_model 用)。
    """

    key: str
    label: str
    in_fallback_chain: bool = True
    route: str | None = None
    model_prefixes: tuple[str, ...] = ()



PROVIDERS: tuple[ProviderSpec, ...] = tuple(ProviderSpec(**{**item, "model_prefixes": tuple(item["model_prefixes"])}) for item in MODEL_CONFIG["providers"])


MODELS: tuple[ModelSpec, ...] = tuple(ModelSpec(**item) for item in MODEL_CONFIG["models"])

MODEL_REGISTRY: dict[str, ModelSpec] = {spec.id: spec for spec in MODELS}


def model_spec(model_id: str) -> ModelSpec | None:
    """按原生 id 取注册表条目(自动剥掉 `#tail` 等后缀)。"""
    if not model_id:
        return None
    base = str(model_id).strip()
    if "#" in base:
        base = base.split("#", 1)[0].strip()
    return MODEL_REGISTRY.get(base)


def model_provider(model_id: str) -> str | None:
    spec = model_spec(model_id)
    return spec.provider if spec else None


def model_generation(model_id: str) -> float:
    """模型代际;未注册的返回 0.0(排序时垫底)。"""
    spec = model_spec(model_id)
    return spec.generation if spec else 0.0


def model_tier(model_id: str) -> str:
    spec = model_spec(model_id)
    return spec.tier if spec else "pro"






_PROVIDER_BY_KEY: dict[str, ProviderSpec] = {spec.key: spec for spec in PROVIDERS}


PROVIDER_LABELS: dict[str, str] = {spec.key: spec.label for spec in PROVIDERS}




FALLBACK_PROVIDER_ORDER: tuple[str, ...] = tuple(
    spec.key for spec in PROVIDERS if spec.in_fallback_chain
)


def _provider_list_sort_key(spec: ModelSpec) -> tuple[int, int, float, int, str]:
    """厂商列表内排序键:显式 order 的排前面(按 order),其余按代际/等级自动排。"""
    tail = (-spec.generation, MODEL_TIER_RANK.get(spec.tier, 99), spec.id)
    if spec.order is None:
        return (1, 0, *tail)
    return (0, spec.order, *tail)


def provider_models(
    provider: str,
    tier: str | tuple[str, ...] | None = None,
    *,
    vision: bool | None = None,
) -> list[str]:
    """某厂商的模型列表(新 → 旧;厂商内 order 优先,其次代际/等级)。

    tier / vision 为筛选条件;不传 = 该厂商全部模型。tier 传元组 = 同时取多档。
    """
    specs = [spec for spec in MODELS if spec.provider == provider]
    if tier is not None:
        tiers = (tier,) if isinstance(tier, str) else tuple(tier)
        specs = [spec for spec in specs if spec.tier in tiers]
    if vision is not None:
        specs = [spec for spec in specs if spec.vision is vision]
    specs.sort(key=_provider_list_sort_key)
    return [spec.id for spec in specs]



CLAUDE_MAX_MODELS = provider_models("claude", "max")
CLAUDE_PRO_MODELS = provider_models("claude", "pro")
CLAUDE_FLASH_MODELS = provider_models("claude", "flash")
CLAUDE_LITE_MODELS = provider_models("claude", "lite")

DEEPSEEK_PRO_MODELS = provider_models("deepseek", "pro")
DEEPSEEK_FLASH_MODELS = provider_models("deepseek", "flash")


GEMINI_PRO_MODELS = provider_models("gemini", "pro")
GEMINI_FLASH_MODELS = [m for m in provider_models("gemini", "flash") if MODEL_REGISTRY[m].in_chain]
GEMINI_LITE_MODELS = provider_models("gemini", "lite")
GEMINI_IMAGE_MODELS = provider_models("gemini", vision=True)
GEMINI_ALL_MODELS = provider_models("gemini")

MAGMAROUTER_MODELS = provider_models("magmarouter")
OPENROUTER_MODELS = provider_models("openrouter")
GROK_MODELS = provider_models("grok")





GPT_MODELS = provider_models("gpt")
# 前置路由只使用进入主链的 GPT 模型；注册但暂不调用的模型不参与任何调用链。
GPT_FLASH_MODELS = [m for m in GPT_MODELS if MODEL_REGISTRY[m].in_chain]


GLM_PRO_MODELS = provider_models("glm", "pro")
GLM_FLASH_MODELS = provider_models("glm", "flash")

KIMI_MODELS = provider_models("kimi")
KIMI_PRO_MODELS = provider_models("kimi", "pro")


EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL") or MODEL_CONFIG["embedding"]["model"]
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM") or MODEL_CONFIG["embedding"]["dimensions"])
EMBEDDING_MAX_INPUT_CHARS = int(os.getenv("EMBEDDING_MAX_INPUT_CHARS") or MODEL_CONFIG["embedding"]["max_input_chars"])


def dedupe_models(*parts: Iterable[str] | None) -> list[str]:
    """按顺序去重(保序)。"""
    seen: set[str] = set()
    out: list[str] = []
    for group in parts:
        for m in group or []:
            if isinstance(m, str) and m.strip() and m not in seen:
                seen.add(m)
                out.append(m)
    return out







def _provider_pool(provider: str) -> list[str]:
    """该厂商在容灾组里的全部模型 id(去重;按注册表声明顺序)。

    只取生成类模型;in_fallback_chain=False 的厂商(OpenRouter 等)不参与容灾排序。
    """
    provider_spec = _PROVIDER_BY_KEY.get(provider)
    if provider_spec is None or not provider_spec.in_fallback_chain:
        return []
    return [
        spec.id
        for spec in MODELS
        if spec.provider == provider and spec.in_chain
    ]


def sort_models_by_generation(models: Iterable[str]) -> list[str]:
    """同一厂商内排序:代际新的在前;同代际 Pro → Flash → Lite。

    未注册的模型排到最后(按原顺序稳定保留)。
    """
    items = [m for m in models or [] if m]
    known = [m for m in items if m in MODEL_REGISTRY]
    unknown = [m for m in items if m not in MODEL_REGISTRY]
    known.sort(key=lambda m: MODEL_REGISTRY[m].sort_key)
    return known + unknown


def provider_ranked_models(provider: str) -> list[str]:
    """某厂商的「最新最好 → 最旧最弱」模型顺序表。"""
    return sort_models_by_generation(_provider_pool(provider))


def _is_lite_model(model_id: str) -> bool:
    """该模型是否登记为 Lite 档（未登记的模型按非 Lite 处理）。"""
    spec = MODEL_REGISTRY.get(model_id)
    return spec is not None and spec.tier == "lite"


def _demote_leading_lite(models: list[str]) -> list[str]:
    """让 Lite 档给「旧高级模型」让位：单厂商序列的第 1 名不得是 Lite。

    口径（2026-10 用户调整，2026-11 补充）：**容灾组第一轮不出现 Lite**。
    第一轮 = 各厂各出一个非 Lite 模型；某厂只有 Lite 时该厂第一轮不占位，
    不为凑满第一轮把 Lite 硬塞进去（GPT 只有 gpt-6-luna 一个候选）。
    做法：若某厂剔除档位链首后**第 1 名恰好是 Lite**（典型：Claude 的两个更好的
    模型被档位链首去重剃掉，剩下 haiku-5-5），就找该厂序列里**下一个非 Lite 替补**
    换位，让它替 Lite 出第一轮；Lite 从第二轮开始出现。
    """
    if not models:
        return models
    first = MODEL_REGISTRY.get(models[0])
    if first is None or first.tier != "lite":
        return models
    for index in range(1, len(models)):
        other = MODEL_REGISTRY.get(models[index])
        if other is not None and other.tier != "lite":
            models[0], models[index] = models[index], models[0]
            break
    return models


def disaster_recovery_models(
    tier_head_models: Iterable[str] | None = None,
    provider_order: Iterable[str] | None = None,
) -> list[str]:
    """生成容灾组:各厂最新最好的模型轮转交错。

    规则(用户 2026-10 指定,2026-11 补充):
      1. 各厂内部:代际最新 → 同代际 Pro → Flash → Lite;
      2. 各厂之间按 FALLBACK_PROVIDER_ORDER 依次取第 1 名组成第一轮,
         再取第 2 名……以此类推;
      3. **第一轮不出现 Lite**:某厂第一轮候选若只有 Lite(GPT 只有
         gpt-6-luna),该厂第一轮不占位,模型顺延到第二轮它在厂商轮转里的
         位置,不为了凑满第一轮把 Lite 硬塞进去(2026-11 用户口径);
      4. 已经作为某个档位链首的模型(tier_head_models)从容灾组剔除,
         避免 low 档兜底时先撞上最贵的顶配模型;
      5. 未出现在 provider_order 里的厂商不参与。

    返回去重后的模型 id 列表(不会包含 `#tail` 别名)。
    """
    order = tuple(provider_order or FALLBACK_PROVIDER_ORDER)
    heads = set(tier_head_models or [])


    ranked = [
        _demote_leading_lite([m for m in provider_ranked_models(p) if m not in heads])
        for p in order
    ]


    first_round: list[str] = []
    rest: list[list[str]] = []
    for lst in ranked:
        pick = next((i for i, m in enumerate(lst) if not _is_lite_model(m)), None)
        if pick is None:
            rest.append(list(lst))
            continue
        first_round.append(lst[pick])
        rest.append([m for i, m in enumerate(lst) if i != pick])

    interleaved: list[str] = list(first_round)
    max_len = max((len(q) for q in rest), default=0)
    for index in range(max_len):
        for q in rest:
            if index < len(q):
                interleaved.append(q[index])
    return dedupe_models(interleaved)














OPENROUTER_FALLBACK_MAP: dict[str, str] = {
    spec.id: spec.slug for spec in MODELS if spec.slug
}


def _resolve_openrouter_fallback_map() -> dict[str, str]:
    """读环境变量覆盖后的映射(import 期调用一次并缓存)。

    OPENROUTER_FALLBACK_MAP(JSON 字符串)会整体覆盖/增补默认映射;
    解析失败时忽略并沿用默认值。
    """
    resolved = dict(OPENROUTER_FALLBACK_MAP)
    raw = os.getenv("OPENROUTER_FALLBACK_MAP")
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, dict):
                for key, value in extra.items():
                    if isinstance(key, str) and isinstance(value, str) and str(value).strip():
                        resolved[str(key).strip()] = str(value).strip()
        except Exception:
            pass
    return resolved


_OPENROUTER_FALLBACK_MAP_RESOLVED: dict[str, str] = _resolve_openrouter_fallback_map()


def openrouter_fallback_slug(native_model_id: str) -> str | None:
    """原生模型 id → OpenRouter slug;无对应同款时返回 None。

    会先剥掉降级链尾部别名后缀(`<id>#tail`)。
    """
    m = (native_model_id or "").strip()
    if not m:
        return None
    if "#" in m:
        m = m.split("#", 1)[0].strip()
    if not m:
        return None
    return _OPENROUTER_FALLBACK_MAP_RESOLVED.get(m)














DIALOG_TIER_MAX_MODELS = list(MODEL_CONFIG["dialog_tiers"]["max"])
DIALOG_TIER_HIGH_MODELS = list(MODEL_CONFIG["dialog_tiers"]["high"])
DIALOG_TIER_MEDIUM_MODELS = list(MODEL_CONFIG["dialog_tiers"]["medium"])
DIALOG_TIER_LOW_MODELS: list[str] = list(MODEL_CONFIG["dialog_tiers"]["low"])
DIALOG_TIER_MINIMAL_MODELS: list[str] = list(MODEL_CONFIG["dialog_tiers"]["minimal"])

DIALOG_TIER_ORDER = ("max", "high", "medium", "low", "minimal")
DIALOG_TIER_MODELS: dict[str, list[str]] = {
    "max": DIALOG_TIER_MAX_MODELS,
    "high": DIALOG_TIER_HIGH_MODELS,
    "medium": DIALOG_TIER_MEDIUM_MODELS,
    "low": DIALOG_TIER_LOW_MODELS,
    "minimal": DIALOG_TIER_MINIMAL_MODELS,
}


DIALOG_TIER_HEAD_MODELS = dedupe_models(*DIALOG_TIER_MODELS.values())


DIALOG_FALLBACK_MODELS: list[str] = disaster_recovery_models(DIALOG_TIER_HEAD_MODELS)



LOW_TIER_FLASH_POOL = list(MODEL_CONFIG["low_tier_pool"])


def low_tier_flash_models() -> list[str]:
    """low 档随机池(副本)。"""
    return list(LOW_TIER_FLASH_POOL)


def low_tier_random_primary(seed: float | int | None = None) -> str | None:
    """以时间为 seed 从 low 档随机池抽一个(每次主对话调用重新抽)。"""
    pool = low_tier_flash_models()
    if not pool:
        return None
    return random.Random(_time.time() if seed is None else seed).choice(pool)


def normalize_dialog_tier(grade_tier: str | None) -> str:
    """档位名归一化:认不出的(含空值)按 medium。"""
    tier = str(grade_tier or "").strip().lower()
    return tier if tier in DIALOG_TIER_ORDER else "medium"


def dialog_tier_models(grade_tier: str | None) -> list[str]:
    """某一档自己的模型列表(副本;不含后续档位与兜底段)。"""
    return list(DIALOG_TIER_MODELS[normalize_dialog_tier(grade_tier)])


def _append_deepseek_tail(chain: list[str]) -> list[str]:
    """链尾兜底:显式追加一个「尾部专用」的 DeepSeek 档位(`<id>#tail`)。

    候选模型从 DEEPSEEK_PRO/FLASH 列表取(不手写 id);两档都被链占用时
    仍复用 Pro 档尾部别名。若注册表里没有任何 DeepSeek 模型,则不追加
    (不再硬塞一个不存在的模型名)。
    """
    tail_candidates = list(DEEPSEEK_PRO_MODELS) + list(DEEPSEEK_FLASH_MODELS)
    if not tail_candidates:
        return chain
    on_chain = set(chain)
    for candidate in tail_candidates:
        if candidate not in on_chain:
            chain.append(f"{candidate}#tail")
            return chain
    chain.append(f"{tail_candidates[0]}#tail")
    return chain


def dialog_chain_from_tier(grade_tier: str) -> list[str]:
    """从该档(含)到链尾:本档及其后各档 + 容灾组 + 链尾 DeepSeek。"""
    start = DIALOG_TIER_ORDER.index(normalize_dialog_tier(grade_tier))
    head = [m for tier in DIALOG_TIER_ORDER[start:] for m in DIALOG_TIER_MODELS[tier]]
    return _append_deepseek_tail(dedupe_models(head, DIALOG_FALLBACK_MODELS))


def dialog_model_chain() -> list[str]:
    """主对话统一降级链(= max 档起到链尾)。"""
    return dialog_chain_from_tier("max")










CLAUDE_SUMMARY_MODEL = MODEL_CONFIG["summary_models"][0] if MODEL_CONFIG["summary_models"] else CLAUDE_PRO_MODELS[0]
CLAUDE_SUMMARY_MODELS: list[str] = list(MODEL_CONFIG["summary_models"])

TASK_MODEL_PRIMARY: dict[str, str] = {

    "dialog_max": DIALOG_TIER_MAX_MODELS[0],
    "dialog_high": DIALOG_TIER_HIGH_MODELS[0],
    "dialog_medium": DIALOG_TIER_MEDIUM_MODELS[0],
    "dialog_low": DIALOG_TIER_LOW_MODELS[0],
    "dialog_minimal": DIALOG_TIER_MINIMAL_MODELS[0],


    "image_description": GEMINI_IMAGE_MODELS[0],
    "pre_routing": GPT_FLASH_MODELS[0],
    "intent_analysis": DEEPSEEK_FLASH_MODELS[0],
    "auxiliary": DEEPSEEK_FLASH_MODELS[0],
    "nsfw": MAGMAROUTER_MODELS[0],
    "nsfw_rewrite": MAGMAROUTER_MODELS[0],

    "perception": CLAUDE_FLASH_MODELS[0],

    "summary": CLAUDE_SUMMARY_MODEL,
    "scoring": DEEPSEEK_FLASH_MODELS[0],
    "context_compression": DEEPSEEK_FLASH_MODELS[0],
    "relationship": CLAUDE_SUMMARY_MODEL,

    "embedding": EMBEDDING_MODEL,
}


def task_model_primary(task: str) -> str:
    """任务 → 首选模型;未知任务抛 KeyError(避免静默用错模型)。"""
    key = str(task or "").strip()
    if key not in TASK_MODEL_PRIMARY:
        raise KeyError(f"unknown task model key: {task!r}")
    return TASK_MODEL_PRIMARY[key]


def task_model_chain(task: str) -> list[str]:
    """任务 → 降级链。只覆盖有明确多级链的任务;其余回退到 [首选]。"""
    key = str(task or "").strip()
    if key == "dialog_max":
        return dialog_chain_from_tier("max")
    if key == "dialog_high":
        return dialog_chain_from_tier("high")
    if key == "dialog_medium":
        return dialog_chain_from_tier("medium")
    if key == "dialog_low":
        return dialog_chain_from_tier("low")
    if key == "dialog_minimal":
        return dialog_chain_from_tier("minimal")
    if key in ("auxiliary", "intent_analysis"):
        return dedupe_models(DEEPSEEK_FLASH_MODELS, MAGMAROUTER_MODELS, OPENROUTER_MODELS, GROK_MODELS)
    if key == "summary":
        return claude_summary_model_chain()
    if key == "scoring":
        return deepseek_preprocess_models()
    if key == "context_compression":
        return deepseek_preprocess_models()
    if key in ("image_description",):
        return list(GEMINI_IMAGE_MODELS)
    if key in ("pre_routing",):
        return dedupe_models(
            GPT_FLASH_MODELS,
            GEMINI_LITE_MODELS,
            GROK_MODELS,
            MAGMAROUTER_MODELS,
            OPENROUTER_MODELS,
            DEEPSEEK_FLASH_MODELS,
        )
    if key in ("nsfw",):
        return dedupe_models(MAGMAROUTER_MODELS, OPENROUTER_MODELS, GROK_MODELS)
    if key == "perception":
        return dialog_chain_from_tier("medium")
    return [task_model_primary(key)]







def deepseek_dialog_models() -> list[str]:
    """主对话 DeepSeek 段:Pro → Flash。"""
    return dedupe_models(DEEPSEEK_PRO_MODELS, DEEPSEEK_FLASH_MODELS)


def deepseek_preprocess_models() -> list[str]:
    """前置处理 / log_update:Flash → Pro。"""
    return dedupe_models(DEEPSEEK_FLASH_MODELS, DEEPSEEK_PRO_MODELS)


def deepseek_merge_models() -> list[str]:
    """log_update 总结/合并/修复(同 preprocess 链)。"""
    return deepseek_preprocess_models()


def deepseek_dialog_primary() -> str:
    return deepseek_dialog_models()[0]


def deepseek_preprocess_primary() -> str:
    return deepseek_preprocess_models()[0]


def deepseek_context_compression_primary() -> str:
    return DEEPSEEK_FLASH_MODELS[0]


def deepseek_primary() -> str:
    """兼容旧引用:主对话首选。"""
    return deepseek_dialog_primary()


def claude_dialog_models() -> list[str]:
    """兼容旧引用:主对话链里的 Claude 模型。"""
    return [m for m in dialog_model_chain() if m.startswith("claude-")]


def claude_lite_models() -> list[str]:
    """Claude 最廉价档(代理探活用,不进主对话四档静态链首)。"""
    return list(CLAUDE_LITE_MODELS)


def claude_lite_primary() -> str:
    return CLAUDE_LITE_MODELS[0]


def claude_summary_model_chain() -> list[str]:
    """log_update 总结用 Claude 链;未单独配置时与 claude_dialog_models 一致。"""
    preferred = CLAUDE_SUMMARY_MODEL
    explicit = CLAUDE_SUMMARY_MODELS
    base = list(explicit) if explicit else claude_dialog_models()
    if preferred:
        base = [preferred] + [m for m in base if m != preferred]
    return dedupe_models(base)


def gemini_dialog_models() -> list[str]:
    """主对话 Gemini 兜底:Pro → Flash → Lite。"""
    return dedupe_models(GEMINI_PRO_MODELS, GEMINI_FLASH_MODELS, GEMINI_LITE_MODELS)


def gemini_pro_models() -> list[str]:
    return list(GEMINI_PRO_MODELS)


def gemini_image_models() -> list[str]:
    return list(GEMINI_IMAGE_MODELS)


def gemini_lite_primary() -> str:
    """Gemini 最廉价档(前置分流/分级路由在 GPT 不可用时的次选)。"""
    return GEMINI_LITE_MODELS[0]


def gemini_image_primary() -> str:
    return GEMINI_IMAGE_MODELS[0]


def magmarouter_models() -> list[str]:
    return list(MAGMAROUTER_MODELS)


def magmarouter_primary() -> str:
    return MAGMAROUTER_MODELS[0]


def openrouter_models() -> list[str]:
    return list(OPENROUTER_MODELS)


def openrouter_primary() -> str:
    return OPENROUTER_MODELS[0]


def grok_models() -> list[str]:
    return list(GROK_MODELS)


def grok_primary() -> str:
    return GROK_MODELS[0]


def grok_fallback() -> str:
    return GROK_MODELS[1] if len(GROK_MODELS) > 1 else GROK_MODELS[0]


def grok_slot_models() -> list[str]:
    """MagmaRouter 主路 + OpenRouter 备选 + Grok 容灾(依次备选)。"""
    return dedupe_models(MAGMAROUTER_MODELS, OPENROUTER_MODELS, GROK_MODELS)


def grok_slot_primary() -> str:
    if MAGMAROUTER_API_KEY and MAGMAROUTER_MODELS:
        return magmarouter_primary()
    if OPENROUTER_API_KEY and OPENROUTER_MODELS:
        return openrouter_primary()
    return grok_primary()


def grok_slot_rewrite_model() -> str:
    """NSFW 改写等任务:MagmaRouter 主路;OpenRouter 备选;Grok reasoning 兜底。"""
    if MAGMAROUTER_API_KEY and MAGMAROUTER_MODELS:
        return magmarouter_primary()
    if OPENROUTER_API_KEY and OPENROUTER_MODELS:
        return openrouter_primary()
    return next((m for m in GROK_MODELS if "reasoning" in m), grok_primary())


def gpt_flash_models() -> list[str]:
    """GPT Flash 档(对话分级/NSFW 前置路由等快速前置任务优先)。"""
    return list(GPT_FLASH_MODELS)


def gpt_flash_primary() -> str:
    return GPT_FLASH_MODELS[0]


def gpt_models() -> list[str]:
    return list(GPT_MODELS)


def gpt_primary() -> str:
    return GPT_MODELS[0]


def glm_models() -> list[str]:
    """GLM 全部档位(Pro → Flash):容灾组与探活用。"""
    return dedupe_models(GLM_PRO_MODELS, GLM_FLASH_MODELS)


def glm_pro_models() -> list[str]:
    return list(GLM_PRO_MODELS)


def glm_flash_models() -> list[str]:
    return list(GLM_FLASH_MODELS)


def glm_pro_primary() -> str:
    return GLM_PRO_MODELS[0]


def glm_flash_primary() -> str:
    return GLM_FLASH_MODELS[0]


def kimi_models() -> list[str]:
    return list(KIMI_MODELS)


def kimi_primary() -> str:
    return KIMI_MODELS[0]







def provider_for_model(model_id: str) -> str:
    """模型 id → 降级链 provider 名。

    与旧 `tenshi_reply._provider_from_model_id` 完全同口径:
      * MagmaRouter / OpenRouter 自家模型 → "grok"(走 grok 槽:MagmaRouter→OR→Grok)
      * glm-* → "glm"(注意 MagmaRouter 的 glm-*-xploded 已在上一步归到 grok)
      * 含 "/" 或 OpenRouter 模型 → "grok"
      * 其余 → 注册表里的 provider;未注册的默认 "claude"(兼容旧行为)
    """
    mid = (model_id or "").strip()
    base = mid.split("#", 1)[0].strip()
    lower = base.lower()
    spec = MODEL_REGISTRY.get(base) or MODEL_REGISTRY.get(lower)
    if spec:
        route = _PROVIDER_BY_KEY[spec.provider].route
        if route:
            return route
    if lower.startswith("glm-"):
        return "glm"
    if "/" in lower:
        return "grok"
    if spec:
        return spec.provider
    for provider_spec in PROVIDERS:
        if any(lower.startswith(prefix) for prefix in provider_spec.model_prefixes):
            return provider_spec.route or provider_spec.key
    return "claude"
