# SPDX-License-Identifier: AGPL-3.0-only















from __future__ import annotations

import hashlib
import os
import pickle
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import tenshi_config
from embedding_client import EmbeddingClient, EmbeddingError, build_embedding_client
import rag_modules
from tenshi_memory_manager import TenshiMemoryManager

DIM = 8


def _vec_for(index: int, dim: int = DIM) -> np.ndarray:
    """确定性可断言向量：第 0 维 = index + 1，其余填 1。"""
    vec = np.ones(dim, dtype=np.float32)
    vec[0] = float(index + 1)
    return vec


class _FakeEmbeddings:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._handler(kwargs)


class _FakeOpenAI:
    def __init__(self, handler):
        self.embeddings = _FakeEmbeddings(handler)


class _FakeBadRequest(Exception):
    """模拟 openai.BadRequestError（400），供批上限动态变化的拆批测试使用。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.status_code = 400


def _text_index(text: str) -> int:
    return int(str(text).lstrip("t") or 0)


def _make_handler(*, dim: int = DIM, fail_times: int = 0, purpose_vectors: bool = False):
    """返回 (handler, state)；handler 模拟 OpenAI embeddings.create 的返回。"""
    state = {"calls": 0, "slept": 0.0}

    def handler(kwargs):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise RuntimeError("simulated upstream failure")
        purpose = (kwargs.get("extra_body") or {}).get("text_type")
        data = []
        for i, text in enumerate(kwargs["input"]):
            if purpose_vectors:
                vec = np.full(dim, 1.0, dtype=np.float32)
                vec[0] = 1.0 if purpose == "document" else 2.0
            else:
                vec = _vec_for(_text_index(text), dim)
            data.append(SimpleNamespace(index=i, embedding=vec.tolist()))
        return SimpleNamespace(data=data)

    return handler, state


def _build_client(handler, **kwargs) -> EmbeddingClient:
    return EmbeddingClient(
        client=_FakeOpenAI(handler),
        api_key="test-key",
        base_url="https://fake.local/v1",
        model="fake-embedding",
        dim=kwargs.pop("dim", DIM),
        retry_base_sec=0.0,
        sleep_fn=lambda _s: None,
        **kwargs,
    )


def _make_rag(tmpdir: str, client=None, dim: int = DIM):
    """绕过 __init__（避免读真实 DB/KB）构造一个只带缓存与 client 的 rag 实例。"""
    rag = rag_modules.TenshiRAG.__new__(rag_modules.TenshiRAG)
    rag.embedding_cache = {}
    rag.cache_modified = False
    rag.is_github_actions = False
    rag.embedding_model = client.model if client is not None else tenshi_config.EMBEDDING_MODEL
    rag.embedding_dim = client.dim if client is not None else dim
    rag.client = client
    rag.memory_manager = TenshiMemoryManager(db_path=os.path.join(tmpdir, "mem.db"))
    rag._embedding_failure_logged = False
    return rag


class BatchAndDimensionTest(unittest.TestCase):
    def test_batches_are_at_most_20_and_keep_order(self):
        handler, state = _make_handler()
        client = _build_client(handler, batch_size=20)
        texts = [f"t{i}" for i in range(60)]
        matrix = client.embed_batch(texts, purpose="document")
        calls = client._client.embeddings.calls
        self.assertEqual([len(c["input"]) for c in calls], [20, 20, 20])
        self.assertEqual(matrix.shape, (60, DIM))

        self.assertEqual([float(v) for v in matrix[:, 0]], [float(i + 1) for i in range(60)])
        self.assertEqual(state["calls"], 3)

    def test_batch_size_is_clamped_to_endpoint_limit(self):
        client = _build_client(_make_handler()[0], batch_size=999)
        self.assertEqual(client.batch_size, 20)
        client0 = _build_client(_make_handler()[0], batch_size=0)
        self.assertEqual(client0.batch_size, 1)

    def test_dimension_mismatch_raises(self):
        handler, _state = _make_handler(dim=4)
        client = _build_client(handler, dim=8)
        with self.assertRaises(EmbeddingError) as ctx:
            client.embed("t0", purpose="document")
        self.assertIn("维度不符", str(ctx.exception))

    def test_endpoint_batch_limit_rejection_splits_batch(self):
        """端点批上限动态变化（例如又降到 8）时：自动二分拆批，顺序仍正确。"""
        limit = 8
        succeeded: list[int] = []

        def handler(kwargs):
            if len(kwargs["input"]) > limit:
                raise _FakeBadRequest(
                    "Error code: 400 - batch size is invalid, it should not be larger "
                    f"than {limit}.: input.contents"
                )
            succeeded.append(len(kwargs["input"]))
            data = []
            for i, text in enumerate(kwargs["input"]):
                data.append(SimpleNamespace(index=i, embedding=_vec_for(_text_index(text)).tolist()))
            return SimpleNamespace(data=data)

        client = _build_client(handler, batch_size=20)
        texts = [f"t{i}" for i in range(60)]
        matrix = client.embed_batch(texts, purpose="document")
        self.assertEqual(matrix.shape, (60, DIM))
        self.assertEqual([float(v) for v in matrix[:, 0]], [float(i + 1) for i in range(60)])
        calls = client._client.embeddings.calls

        self.assertEqual([len(c["input"]) for c in calls[:5]], [20, 10, 5, 5, 10])
        self.assertTrue(all(n <= limit for n in succeeded))

    def test_retry_then_success(self):
        handler, state = _make_handler(fail_times=2)
        client = _build_client(handler, max_attempts=3)
        vec = client.embed("t0")
        self.assertEqual(vec.shape[0], DIM)
        self.assertEqual(state["calls"], 3)

    def test_retry_exhausted_raises_clear_error(self):
        handler, state = _make_handler(fail_times=3)
        client = _build_client(handler, max_attempts=3)
        with self.assertRaises(EmbeddingError) as ctx:
            client.embed("t0")
        self.assertIn("已重试 3 次", str(ctx.exception))
        self.assertEqual(state["calls"], 3)

    def test_empty_text_rejected(self):
        client = _build_client(_make_handler()[0])
        with self.assertRaises(EmbeddingError):
            client.embed("   ")

    def test_long_text_truncated_to_max_input_chars(self):
        client = _build_client(_make_handler(purpose_vectors=True)[0], max_input_chars=10)
        client.embed("x" * 50)
        sent = client._client.embeddings.calls[0]["input"][0]
        self.assertEqual(len(sent), 10)
        self.assertEqual(sent, "x" * 10)

    def test_embed_batch_empty_returns_zero_rows(self):
        client = _build_client(_make_handler()[0])
        matrix = client.embed_batch([], purpose="document")
        self.assertEqual(matrix.shape, (0, DIM))


class PurposePassthroughTest(unittest.TestCase):
    def test_text_type_forwarded_to_extra_body(self):
        handler, _state = _make_handler()
        client = _build_client(handler)
        client.embed("t1", purpose="query")
        client.embed("t1", purpose="document")
        client.embed("t1")
        calls = client._client.embeddings.calls
        self.assertEqual(calls[0]["extra_body"], {"text_type": "query"})
        self.assertEqual(calls[1]["extra_body"], {"text_type": "document"})
        self.assertEqual(calls[2]["extra_body"], {"text_type": "document"})
        self.assertEqual(calls[0]["dimensions"], client.dim)
        self.assertEqual(calls[0]["model"], client.model)

    def test_invalid_purpose_rejected(self):
        client = _build_client(_make_handler()[0])
        with self.assertRaises(EmbeddingError):
            client.embed("t1", purpose="search")


class NoKeyTest(unittest.TestCase):
    def test_missing_key_does_not_raise_at_construction(self):
        with mock.patch.object(tenshi_config, "QWEN_API_KEY", None):
            client = build_embedding_client()
        self.assertFalse(client.configured)
        with self.assertRaises(EmbeddingError) as ctx:
            client.embed("hello")
        self.assertIn("QWEN_API_KEY", str(ctx.exception))

    def test_rag_returns_zero_vector_of_embedding_dim_without_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(tenshi_config, "QWEN_API_KEY", None):
                client = build_embedding_client()
            self.assertFalse(client.configured)
            rag = _make_rag(tmpdir, client=client, dim=client.dim)
            emb = rag.get_embedding("无 key 时的文本")
            self.assertIsInstance(emb, np.ndarray)
            self.assertEqual(emb.shape, (int(tenshi_config.EMBEDDING_DIM),))
            self.assertFalse(np.any(emb))
            self.assertEqual(rag.embedding_cache, {})


class RagCacheTest(unittest.TestCase):
    def test_query_and_document_cache_keys_are_separated(self):
        handler, state = _make_handler(purpose_vectors=True)
        client = _build_client(handler)
        with tempfile.TemporaryDirectory() as tmpdir:
            rag = _make_rag(tmpdir, client=client)
            text = "同一段文本"
            doc_vec = rag.get_embedding(text, purpose="document")
            cached_doc_vec = rag.get_embedding(text, purpose="document")
            query_vec = rag.get_embedding(text, purpose="query")

            doc_key = hashlib.md5(text.encode("utf-8")).hexdigest()
            query_key = hashlib.md5(("__query__|" + text).encode("utf-8")).hexdigest()
            self.assertIn(doc_key, rag.embedding_cache)
            self.assertIn(query_key, rag.embedding_cache)
            self.assertNotEqual(doc_key, query_key)
            self.assertEqual(len(rag.embedding_cache), 2)

            self.assertEqual(state["calls"], 2)
            self.assertFalse(np.array_equal(doc_vec, query_vec))
            self.assertTrue(np.array_equal(doc_vec, cached_doc_vec))

            mgr = rag.memory_manager
            self.assertIsNotNone(mgr.get_cached_embedding(doc_key, model=client.model))
            self.assertIsNotNone(mgr.get_cached_embedding(query_key, model=client.model))
            self.assertIsNone(mgr.get_cached_embedding(doc_key, model="other-model"))

    def test_sqlite_cache_model_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TenshiMemoryManager(db_path=os.path.join(tmpdir, "mem.db"))
            vec = np.ones(DIM, dtype=np.float32)
            mgr.save_cached_embedding("h1", vec, model="m1")
            self.assertIsNotNone(mgr.get_cached_embedding("h1", model="m1"))
            self.assertIsNone(mgr.get_cached_embedding("h1", model="m2"))

            self.assertIsNotNone(mgr.get_cached_embedding("h1"))

            mgr.save_cached_embedding("legacy", vec)
            self.assertIsNone(mgr.get_cached_embedding("legacy", model="m1"))

    def test_pkl_old_format_is_ignored_and_v2_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pkl_path = os.path.join(tmpdir, "embedding_cache.pkl")
            rag = _make_rag(tmpdir, client=None)
            with mock.patch.object(tenshi_config, "CACHE_FILE_PATH", pkl_path):

                with open(pkl_path, "wb") as f:
                    pickle.dump({"abc": np.ones(1024, dtype=np.float32)}, f)
                rag.load_cache()
                self.assertEqual(rag.embedding_cache, {})


                rag.embedding_cache = {"h": _vec_for(3)}
                rag.cache_modified = True
                rag.save_cache()
                with open(pkl_path, "rb") as f:
                    payload = pickle.load(f)
                self.assertEqual(payload["format"], 2)
                self.assertEqual(payload["model"], rag.embedding_model)
                self.assertEqual(payload["dim"], rag.embedding_dim)
                self.assertEqual(set(payload["embeddings"]), {"h"})

                rag2 = _make_rag(tmpdir, client=None)
                rag2.load_cache()
                self.assertEqual(set(rag2.embedding_cache), {"h"})
                self.assertTrue(np.array_equal(rag2.embedding_cache["h"], _vec_for(3)))


                rag.embedding_model = "another-model"
                rag.embedding_cache = {"h": _vec_for(3)}
                rag.cache_modified = True
                rag.save_cache()
                rag3 = _make_rag(tmpdir, client=None)
                rag3.load_cache()
                self.assertEqual(rag3.embedding_cache, {})


class SourceAssertionsTest(unittest.TestCase):
    def test_rag_no_longer_imports_google_genai(self):
        with open(os.path.join(SRC, "rag_modules.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("from google import genai", source)
        self.assertNotIn("google.genai", source)
        self.assertNotIn("gemini-embedding", source)
        self.assertNotIn("models/embedding-001", source)
        self.assertIn("build_embedding_client", source)


if __name__ == "__main__":
    unittest.main()
