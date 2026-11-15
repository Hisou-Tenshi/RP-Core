# SPDX-License-Identifier: AGPL-3.0-only






















from __future__ import annotations

from _rp_cancellation import ensure_active

import logging
import random
import time
from typing import Any, Callable, Sequence

import numpy as np

import tenshi_config
from llm_compat import build_openai_compat_client

logger = logging.getLogger(__name__)



BATCH_SIZE_LIMIT = 20

MAX_ATTEMPTS = 3

PURPOSES = ("document", "query")


class EmbeddingError(RuntimeError):
    """embedding 调用失败（key 缺失 / 维度不符 / 重试耗尽 / 返回畸形）。"""


class _BatchSizeError(EmbeddingError):
    """端点以 400 拒绝批大小（上限会动态变化）时的内部信号，用于二分拆批兜底。"""


def _is_batch_size_rejection(exc: BaseException) -> bool:
    """判断异常是不是「批太大」的 400（不同 SDK/版本消息格式不完全一致）。"""
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            if int(status) != 400:
                return False
        except (TypeError, ValueError):
            pass
    lowered = str(exc).lower()
    return "batch size" in lowered or "input.contents" in lowered


class EmbeddingClient:
    """OpenAI 兼容的 Qwen embedding 客户端（同步、批量、带重试）。

    构造时**不抛异常**：key 缺失只把 ``configured`` 置为 False，真正的清晰
    异常留到调用时抛，避免无 key 环境（如离线单测/仅读缓存的脚本）import 即炸。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dim: int | None = None,
        max_input_chars: int | None = None,
        batch_size: int | None = None,
        client: Any | None = None,
        max_attempts: int | None = None,
        retry_base_sec: float = 1.0,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.model = str(model or tenshi_config.EMBEDDING_MODEL)
        self.dim = int(dim if dim is not None else tenshi_config.EMBEDDING_DIM)
        self.base_url = str(base_url or tenshi_config.QWEN_BASE_URL)
        self.max_input_chars = int(
            max_input_chars
            if max_input_chars is not None
            else tenshi_config.EMBEDDING_MAX_INPUT_CHARS
        )

        requested_batch = int(batch_size if batch_size is not None else BATCH_SIZE_LIMIT)
        self.batch_size = max(1, min(requested_batch, BATCH_SIZE_LIMIT))
        self.max_attempts = max(1, int(max_attempts if max_attempts is not None else MAX_ATTEMPTS))
        self._retry_base_sec = max(0.0, float(retry_base_sec))
        self._sleep = sleep_fn or time.sleep

        key = api_key if api_key is not None else getattr(tenshi_config, "QWEN_API_KEY", None)
        self.api_key = key or None
        self._client = client
        self.configured = bool(self.api_key) or client is not None

        if self._client is None and self.api_key:
            try:
                self._client = build_openai_compat_client(self.api_key, self.base_url)

                self._client.max_retries = 0
            except Exception as exc:
                logger.warning(
                    f"[embedding] 构造 OpenAI 兼容客户端失败，embedding 不可用："
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )
                self._client = None
                self.configured = False


    def embed(self, text: str, purpose: str = "document") -> np.ndarray:
        """单条文本 embedding，返回 ``(dim,)`` 的 float32 向量。"""
        return self.embed_batch([text], purpose=purpose)[0]

    def embed_batch(self, texts: Sequence[str], purpose: str = "document") -> np.ndarray:
        """批量 embedding，返回 ``(N, dim)`` 的 float32 矩阵，顺序与输入一致。"""
        self._ensure_configured()
        purpose = self._normalize_purpose(purpose)

        prepared = [self._prepare_text(t) for t in texts]
        if not prepared:
            return np.zeros((0, self.dim), dtype=np.float32)

        vectors: list[np.ndarray] = []
        for start in range(0, len(prepared), self.batch_size):
            batch = prepared[start : start + self.batch_size]
            vectors.extend(self._embed_chunk(batch, purpose))

        return np.asarray(vectors, dtype=np.float32)


    def _ensure_configured(self) -> None:
        if not self.configured or self._client is None:
            raise EmbeddingError(
                "Qwen embedding 客户端未配置：缺少 QWEN_API_KEY（或旧别名 QWEN_KEY）。"
                "记忆检索 embedding 已不再回退 Gemini，请检查 .env / 环境变量。"
            )

    @staticmethod
    def _normalize_purpose(purpose: str | None) -> str:
        value = str(purpose or "document").strip().lower()
        if value not in PURPOSES:
            raise EmbeddingError(
                f"不支持的 embedding purpose={purpose!r}，只允许 {PURPOSES}（文档用 document，查询用 query）"
            )
        return value

    def _prepare_text(self, text: str | None) -> str:
        """空文本直接拒绝；超长文本截断到 EMBEDDING_MAX_INPUT_CHARS 并告警。"""
        value = "" if text is None else str(text)
        if not value.strip():
            raise EmbeddingError("embedding 输入文本为空")
        if self.max_input_chars > 0 and len(value) > self.max_input_chars:
            logger.warning(
                f"[embedding] 文本超长已截断：{len(value)} -> {self.max_input_chars} 字符"
                f"（model={self.model}）"
            )
            value = value[: self.max_input_chars]
        return value

    def _embed_chunk(self, batch: list[str], purpose: str) -> list[np.ndarray]:
        """对一批文本取向量；端点嫌批太大时二分拆批递归重试（顺序不变）。"""
        try:
            response = self._request_with_retry(batch, purpose)
        except _BatchSizeError:
            if len(batch) <= 1:
                raise
            mid = len(batch) // 2
            logger.warning(
                f"[embedding] 端点拒绝批大小，自动二分拆批："
                f"{len(batch)} -> {mid}+{len(batch) - mid}（model={self.model}）"
            )
            return self._embed_chunk(batch[:mid], purpose) + self._embed_chunk(
                batch[mid:], purpose
            )
        return self._parse_response(response, expected=len(batch))

    def _request_with_retry(self, batch: list[str], purpose: str) -> Any:
        """单批请求：失败按 1s/2s/4s 指数退避重试，共 MAX_ATTEMPTS 次。

        「批太大」的 400 是确定性的，不浪费重试次数，直接抛给 _embed_chunk 拆批。
        """
        last_err: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                ensure_active()
                return self._client.embeddings.create(
                    model=self.model,
                    input=batch,
                    dimensions=self.dim,
                    extra_body={"text_type": purpose},
                )
            except Exception as exc:
                if _is_batch_size_rejection(exc):
                    raise _BatchSizeError(
                        f"端点拒绝批大小（{len(batch)} 条，model={self.model}）："
                        f"{type(exc).__name__}: {str(exc)[:200]}"
                    ) from exc
                last_err = exc
                if attempt >= self.max_attempts:
                    break
                delay = self._retry_base_sec * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                logger.warning(
                    f"[embedding] 第 {attempt}/{self.max_attempts} 次请求失败，"
                    f"{delay:.2f}s 后重试：{type(exc).__name__}: {str(exc)[:200]}"
                )
                self._sleep(delay)
        raise EmbeddingError(
            f"embedding 调用失败（model={self.model}, dim={self.dim}, "
            f"purpose={purpose}, 已重试 {self.max_attempts} 次）："
            f"{type(last_err).__name__}: {last_err}"
        ) from last_err

    def _parse_response(self, response: Any, expected: int) -> list[np.ndarray]:
        """按 index 归位并逐条校验维度（端点静默降级在这里暴露）。"""
        data = list(getattr(response, "data", None) or [])
        if len(data) != expected:
            raise EmbeddingError(
                f"embedding 返回条数异常：期望 {expected}，实际 {len(data)}（model={self.model}）"
            )

        ordered: list[np.ndarray | None] = [None] * expected
        fallback_pos = 0
        for pos, item in enumerate(data):
            raw = getattr(item, "embedding", None)
            if raw is None:
                raise EmbeddingError(f"embedding 返回缺少 embedding 字段（第 {pos} 条）")
            vec = np.asarray(raw, dtype=np.float32)
            index = getattr(item, "index", pos)
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = pos
            if not (0 <= index < expected) or ordered[index] is not None:

                while fallback_pos < expected and ordered[fallback_pos] is not None:
                    fallback_pos += 1
                index = fallback_pos if fallback_pos < expected else pos
            ordered[index] = vec

        missing = [i for i, v in enumerate(ordered) if v is None]
        if missing:
            raise EmbeddingError(f"embedding 返回缺少第 {missing[:5]} 条向量")

        for pos, vec in enumerate(ordered):
            assert vec is not None
            if vec.shape != (self.dim,):
                raise EmbeddingError(
                    f"embedding 维度不符：第 {pos} 条为 {tuple(vec.shape)}，期望 ({self.dim},)；"
                    f"model={self.model} dimensions={self.dim}。"
                    "注意 flash 版对 >=1536 的 dimensions 会被端点静默降级为 1024，"
                    "切 2560 需改用标准版 qwen3.7-text-embedding。"
                )
        return ordered


def build_embedding_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    dim: int | None = None,
    max_input_chars: int | None = None,
    batch_size: int | None = None,
    client: Any | None = None,
    max_attempts: int | None = None,
    retry_base_sec: float = 1.0,
    sleep_fn: Callable[[float], None] | None = None,
) -> EmbeddingClient:
    """工厂：默认读 tenshi_config 的 QWEN_BASE_URL / QWEN_API_KEY / EMBEDDING_*。

    参数只用于 A/B（重跑脚本切标准版 2560 等）与离线单测注入 mock client；
    业务代码一律调用 ``build_embedding_client()`` 不带参。
    """
    return EmbeddingClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        dim=dim,
        max_input_chars=max_input_chars,
        batch_size=batch_size,
        client=client,
        max_attempts=max_attempts,
        retry_base_sec=retry_base_sec,
        sleep_fn=sleep_fn,
    )
