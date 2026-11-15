# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
import pickle
import hashlib
import logging
import warnings
import threading
from concurrent.futures import Future
from _rp_cancellation import CallCancelled, ensure_active
import numpy as np
import tenshi_config
from embedding_client import build_embedding_client
from tenshi_utils import setup_logger
from tenshi_memory_manager import TenshiMemoryManager
from error_text import format_exception_inline



logger = setup_logger("TenshiRAG", tenshi_config.DEBUG_LOG_PATH, level=logging.DEBUG)


warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")


CACHE_FORMAT_VERSION = 2

QUERY_CACHE_KEY_PREFIX = "__query__|"

class TenshiRAG:
    _coordination_init_lock = threading.Lock()

    def _ensure_embedding_coordination(self):
        # Restored/test instances may bypass __init__.
        with self._coordination_init_lock:
            if not hasattr(self, "_embedding_lock"):
                self._embedding_lock = threading.RLock()
                self._embedding_inflight = {}

    def __init__(self, db_path=None, *, memory_manager=None):
        self._embedding_lock = threading.RLock()
        self._embedding_inflight = {}
        self.embedding_cache = {}
        self.cache_modified = False
        self.kb_data = {}
        self.history_data = {}
        self.memory_pool = []
        self.narrative_memory_index = []
        self.history_pool = []
        self.chat_logs = []


        self.is_github_actions = tenshi_config.GITHUB_ACTIONS



        self.client = build_embedding_client()
        self.embedding_model = self.client.model
        self.embedding_dim = self.client.dim
        self._embedding_failure_logged = False


        self.memory_manager = memory_manager if memory_manager is not None else TenshiMemoryManager(db_path=db_path)

        self.load_cache()
        self.load_data()

    def load_cache(self):
        if self.is_github_actions:
            logger.info("检测到运行环境：GitHub Actions。")

        if os.path.exists(tenshi_config.CACHE_FILE_PATH):
            try:
                with open(tenshi_config.CACHE_FILE_PATH, 'rb') as f:
                    payload = __import__("_rp_cache").load(f)




                if not isinstance(payload, dict) or payload.get("format") != CACHE_FORMAT_VERSION:
                    logger.warning(
                        "embedding 缓存为旧格式（Gemini 时代），已忽略；本次会重新生成向量。"
                    )
                    self.embedding_cache = {}
                    return

                cache_model = payload.get("model")
                cache_dim = payload.get("dim")
                if cache_model != self.embedding_model or int(cache_dim or 0) != self.embedding_dim:
                    logger.warning(
                        f"embedding 缓存模型/维度不匹配（缓存 {cache_model}/{cache_dim}，"
                        f"当前 {self.embedding_model}/{self.embedding_dim}），已忽略。"
                    )
                    self.embedding_cache = {}
                    return

                raw_embeddings = payload.get("embeddings")
                if not isinstance(raw_embeddings, dict):
                    logger.warning("embedding 缓存 v2 结构缺少 embeddings，已忽略。")
                    self.embedding_cache = {}
                    return

                loaded = {}
                for k, v in raw_embeddings.items():
                    vec = np.asarray(v, dtype=np.float32)
                    if vec.shape == (self.embedding_dim,):
                        loaded[k] = vec
                self.embedding_cache = loaded
                logger.info(f"已从缓存加载 {len(self.embedding_cache)} 条 embedding。")
            except Exception as e:
                logger.error(f"加载缓存失败：{format_exception_inline(e)}")
                self.embedding_cache = {}
        else:
            if self.is_github_actions:
                 logger.warning("在 Actions 环境中未找到 embedding 缓存文件。本次运行会更慢且成本更高。")

    def save_cache(self):
        self._ensure_embedding_coordination()
        if not self.cache_modified:
            return

        if self.is_github_actions:
            logger.warning("正在 Actions 环境中保存缓存：若未缓存/提交，该数据会在运行结束后丢失。")

        try:
            temp_path = tenshi_config.CACHE_FILE_PATH + ".tmp"


            with self._embedding_lock:
                snapshot = dict(self.embedding_cache)
            payload = {
                "format": CACHE_FORMAT_VERSION,
                "model": self.embedding_model,
                "dim": self.embedding_dim,
                "embeddings": {
                    k: np.asarray(v, dtype=np.float32)
                    for k, v in snapshot.items()
                    if np.asarray(v).shape == (self.embedding_dim,)
                },
            }
            with open(temp_path, 'wb') as f:
                pickle.dump(payload, f)
            if os.path.exists(tenshi_config.CACHE_FILE_PATH):
                os.replace(temp_path, tenshi_config.CACHE_FILE_PATH)
            else:
                os.rename(temp_path, tenshi_config.CACHE_FILE_PATH)
            logger.info(f"已保存 embedding 缓存（原子写入，v2，{len(payload['embeddings'])} 条）。")
            self.cache_modified = False
        except Exception as e:
            logger.error(f"保存缓存失败：{format_exception_inline(e)}")

    def load_data(self):

        if os.path.exists(tenshi_config.KB_FILE_PATH):
            try:
                from kb_storage import load_kb_core, migrate_monolithic_kb_if_needed

                self.kb_data = load_kb_core()
                migrate_monolithic_kb_if_needed(self.kb_data)
                self.memory_pool = []
                self.narrative_memory_index = []
                logger.info("已加载知识库 core（narrative 由 DB 检索）。")
            except Exception as e:
                logger.error(f"加载知识库失败：{format_exception_inline(e)}")


        if os.path.exists(tenshi_config.HISTORY_FILE_PATH):
            try:
                with open(tenshi_config.HISTORY_FILE_PATH, 'r', encoding='utf-8') as f:
                    self.history_data = json.load(f)

                self.history_pool = self.history_data.get('history', []) + self.history_data.get('encyclopedia', [])
                logger.info("已加载世界观历史。")
            except Exception as e:
                logger.error(f"加载世界观历史失败：{format_exception_inline(e)}")


        if os.path.exists(tenshi_config.LOG_FILE_PATH):
            try:
                with open(tenshi_config.LOG_FILE_PATH, 'r', encoding='utf-8') as f:
                    logs = json.load(f)


                self.chat_logs = []
                for conv in logs:
                    for msg in conv.get('chat_history', []):

                        user_q = msg.get('user_query', '').strip()
                        bot_a = msg.get('bot_answer', '').strip()

                        if user_q:
                            self.chat_logs.append(f"User: {user_q}\nTenshi: {bot_a}")
                        elif bot_a:
                            self.chat_logs.append(f"Tenshi: {bot_a}")

                logger.info(f"已加载 {len(self.chat_logs)} 条聊天日志条目。")
            except Exception as e:
                logger.error(f"加载聊天日志失败：{format_exception_inline(e)}")

    @staticmethod
    def _embedding_cache_key(text, purpose):
        """embedding 缓存 key。

        * document：md5(text) —— 与 memory_items.content_hash 及 prune 逻辑耦合，不能改；
        * query：md5("__query__|" + text) —— 与文档向量分离，避免同一字符串的
          查询向量被当成文档向量（text_type 不同，向量本身不同）。
        """
        if purpose == "query":
            return hashlib.md5((QUERY_CACHE_KEY_PREFIX + text).encode('utf-8')).hexdigest()
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def _cached_embedding(self, text_hash):
        manager = getattr(self, "memory_manager", None)
        if manager:
            cached = manager.get_cached_embedding(text_hash, model=self.embedding_model)
            if cached is not None and np.asarray(cached).shape == (self.embedding_dim,):
                return np.asarray(cached, dtype=np.float32)
        cached = self.embedding_cache.get(text_hash)
        if cached is not None and np.asarray(cached).shape == (self.embedding_dim,):
            return np.asarray(cached, dtype=np.float32)
        return None

    def get_embedding(self, text, purpose="document"):
        return self.get_embeddings([text], purpose=purpose)[0]

    def get_embeddings(self, texts, purpose="document"):
        """Batch cache misses and share concurrent work for identical requests.

        Keys include model, dimension and purpose. The lock only covers cache/
        flight bookkeeping; different text batches can perform network I/O in parallel.
        Failed/zero vectors are shared with current waiters but never cached.
        """
        ensure_active()
        self._ensure_embedding_coordination()
        purpose = purpose if purpose in ("query", "document") else "document"
        zero = lambda: np.zeros(self.embedding_dim, dtype=np.float32)
        pending, leaders = [], {}
        with self._embedding_lock:
            for text in texts:
                if not text:
                    pending.append(zero())
                    continue
                value = str(text)
                text_hash = self._embedding_cache_key(value, purpose)
                cached = self._cached_embedding(text_hash)
                if cached is not None:
                    pending.append(cached)
                    continue
                key = (self.embedding_model, self.embedding_dim, purpose, text_hash)
                future = self._embedding_inflight.get(key)
                if future is None:
                    future = Future()
                    self._embedding_inflight[key] = future
                    leaders[key] = (value, future)
                pending.append(future)
        if leaders:
            try:
                values = [value for value, _ in leaders.values()]
                ensure_active()
                try:
                    if len(values) > 1 and callable(getattr(self.client, "embed_batch", None)):
                        vectors = self.client.embed_batch(values, purpose=purpose)
                    else:
                        vectors = [self.client.embed(value, purpose=purpose) for value in values]
                    if len(vectors) != len(values):
                        raise ValueError("Embedding batch length mismatch")
                except Exception as error:
                    logger.warning(f"embedding 调用失败，当前批次不缓存：{format_exception_inline(error)}")
                    vectors = [zero() for _ in values]
                ensure_active()
                for (key, (_, future)), vector in zip(leaders.items(), vectors):
                    emb = np.asarray(vector, dtype=np.float32)
                    if emb.shape != (self.embedding_dim,) or not np.isfinite(emb).all():
                        emb = zero()
                    with self._embedding_lock:
                        if np.any(emb):
                            self.embedding_cache[key[-1]] = emb
                            self.cache_modified = True
                            if getattr(self, "memory_manager", None):
                                self.memory_manager.save_cached_embedding(
                                    key[-1], emb, model=self.embedding_model
                                )
                        future.set_result(emb)
            except BaseException as error:
                for _, future in leaders.values():
                    if not future.done():
                        if isinstance(error, CallCancelled):
                            future.set_result(zero())
                        else:
                            future.set_exception(error)
                raise
            finally:
                with self._embedding_lock:
                    for key in leaders:
                        self._embedding_inflight.pop(key, None)
        results = []
        for value in pending:
            ensure_active()
            if isinstance(value, Future):
                # Respect a waiting caller's own cancellation/deadline as well.
                while not value.done():
                    ensure_active()
                    try:
                        value.result(timeout=0.05)
                    except TimeoutError:
                        continue
                value = value.result()
            results.append(np.asarray(value, dtype=np.float32).copy())
        return results


    def retrieve_items(self, query, items, top_k=3, embedding_key=None, threshold=None):
        """
        对字符串或对象列表执行 RAG 检索。
        embedding_key：若设置，当 item 为 dict 时使用该 key 的值做 embedding。
        threshold：纳入结果的最小余弦相似度阈值。None 表示不设下限。
        """
        if not query or not items:
            return []

        logger.debug(f"正在检索，query：{query}")

        query_embedding = self.get_embedding(query, purpose="query")
        if np.all(query_embedding == 0):
             logger.warning("query embedding 为零向量，已中止检索。")
             return []


        candidates = []
        candidate_embeddings = []



        for item in items:
            text_content = ""
            embedding_text = ""

            if isinstance(item, str):
                text_content = item
                embedding_text = item
            elif isinstance(item, dict):
                text_content = json.dumps(item, ensure_ascii=False)


                if embedding_key and embedding_key in item:
                    embedding_text = item[embedding_key]

                elif 'description' in item:
                    embedding_text = item['description']
                elif 'content' in item:
                    embedding_text = item['content']
                elif 'summary' in item:
                    embedding_text = item['summary']
                else:
                    embedding_text = text_content

            if not embedding_text: continue


            try:


                emb = self.get_embedding(embedding_text, purpose="document")
                if np.all(emb == 0): continue

                candidates.append((item, text_content))
                candidate_embeddings.append(emb)
            except Exception as e:
                logger.error(f"为条目生成 embedding 失败：{format_exception_inline(e)}")
                continue

        if not candidates:
            return []






        candidate_matrix = np.array(candidate_embeddings)


        query_norm = np.linalg.norm(query_embedding)
        if query_norm == 0: return []
        query_unit = query_embedding / query_norm



        candidate_norms = np.linalg.norm(candidate_matrix, axis=1, keepdims=True)

        candidate_norms[candidate_norms == 0] = 1e-10
        candidate_matrix_unit = candidate_matrix / candidate_norms


        scores = np.dot(candidate_matrix_unit, query_unit)

        scored_items = []
        for i, score in enumerate(scores):
            item, text_content = candidates[i]

            if threshold is None or score >= float(threshold):
                scored_items.append((score, text_content))

        scored_items.sort(key=lambda x: x[0], reverse=True)
        top_items = [x[1] for x in scored_items[:top_k]]
        return top_items


    def search_history(self, query, top_k=3):

        return self.retrieve_items(query, self.history_pool, top_k=top_k)


    def search_web(self, query, top_k=3, region="wt-wt"):
        """
        Web 搜索（按优先级选择提供方）：
        1) Tavily Search API（若配置了 TAVILY_KEY）
        2) DuckDuckGo（ddgs / duckduckgo_search）回退
        """
        import time
        import requests

        def _format_results(rows):

            MAX_TITLE_CHARS = 140
            MAX_SNIPPET_CHARS = 700
            formatted = []
            for r in rows or []:
                title = r.get("title") or "No Title"
                link = r.get("href") or r.get("url") or "No Link"
                snippet = r.get("body") or r.get("content") or r.get("snippet") or "No Content"
                title = str(title)
                snippet = str(snippet)
                if len(title) > MAX_TITLE_CHARS:
                    title = title[:MAX_TITLE_CHARS] + "…"
                if len(snippet) > MAX_SNIPPET_CHARS:
                    snippet = snippet[:MAX_SNIPPET_CHARS] + "…"
                formatted.append(f"Title: {title}\nLink: {link}\nSnippet: {snippet}")
            return "\n---\n".join(formatted).strip()

        def _tavily_search():
            api_key = getattr(tenshi_config, "TAVILY_KEY", None) or os.getenv("TAVILY_KEY")
            if not api_key:
                return None, "TAVILY_KEY missing"

            url = "https://api.tavily.com/search"
            payload = {
                "api_key": api_key,
                "query": query,
                "max_results": int(top_k),
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
            }


            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code != 200:
                return None, f"Tavily HTTP {resp.status_code}: {resp.text[:200]}"
            data = resp.json() if resp.content else {}
            results = data.get("results") or []

            rows = []
            for r in results:
                rows.append({
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "content": r.get("content"),
                })
            return rows, None

        def _ddg_search():
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=top_k, region=region)), None

        logger.info(f"正在进行 Web 搜索：{query}（Region：{region}）")
        max_retries = 3
        last_err = None


        for attempt in range(max_retries):
            try:
                rows, err = _tavily_search()
                if rows:
                    logger.info(f"Tavily 搜索成功，结果数：{len(rows)}")
                    return _format_results(rows)
                last_err = err or "Tavily returned no results"
                logger.warning(f"Tavily 第 {attempt+1}/{max_retries} 次尝试无结果：{last_err}")
            except Exception as e:
                last_err = str(e)
                logger.warning(f"Tavily 第 {attempt+1}/{max_retries} 次尝试失败：{format_exception_inline(e)}")
            time.sleep(1)


        last_ddg_err = None
        for attempt in range(max_retries):
            try:
                rows, err = _ddg_search()
                if rows:
                    logger.info(f"DDG 搜索成功，结果数：{len(rows)}")
                    return _format_results(rows)
                last_ddg_err = err or "DDG returned no results"
                logger.warning(f"DDG 第 {attempt+1}/{max_retries} 次尝试无结果：{last_ddg_err}")
            except ImportError:
                logger.error("未安装 ddgs/duckduckgo-search。")
                return "Web search unavailable (library missing)."
            except Exception as e:
                last_ddg_err = str(e)
                logger.warning(f"DDG 第 {attempt+1}/{max_retries} 次尝试失败：{format_exception_inline(e)}")
            time.sleep(1)


        logger.warning(f"Web 搜索失败。Tavily_err={last_err} ddg_err={last_ddg_err}")
        return "No web search results found."

    def search_local(self, query, top_k=3):
        """仅检索本地记忆与聊天日志（DB 混合检索）。"""

        q_emb = self.get_embedding(query, purpose="query")

        quota = {'event': 2, 'experience': 2, 'chat': 2}
        results = self.memory_manager.search_hybrid(query, q_emb, top_k=top_k * 3, type_filter=['event', 'experience', 'chat'], type_quota=quota)

        memories = []
        shared = []
        chats = []

        for r in results:
            if r['type'] == 'event': memories.append(r['content'])
            elif r['type'] == 'experience': shared.append(r['content'])
            elif r['type'] == 'chat': chats.append(r['content'])
            else: memories.append(r['content'])

        combined_text = ""
        if memories:
            combined_text += "### Memories:\n" + "\n---\n".join(memories) + "\n\n"
        if shared:
            combined_text += "### Shared Experiences:\n" + "\n---\n".join(shared) + "\n\n"
        if chats:
             combined_text += "### Past Conversations:\n" + "\n---\n".join(chats) + "\n\n"

        return combined_text.strip()
