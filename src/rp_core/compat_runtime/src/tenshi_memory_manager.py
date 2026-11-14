# SPDX-License-Identifier: AGPL-3.0-only
import sqlite3
import json
import os
import time
import uuid
import logging
import numpy as np
import faiss
from datetime import datetime, timezone, timedelta
import tenshi_config
from error_text import format_exception_inline


logger = logging.getLogger("TenshiMemory")

class TenshiMemoryManager:
    def __init__(self, db_path=None):
        if db_path is None:

            data_dir = os.path.join(os.environ["RP_RUNTIME_ROOT"], "data")
            os.makedirs(data_dir, exist_ok=True)
            self.db_path = os.path.join(data_dir, 'tenshi_memory.db')
        else:
            self.db_path = db_path

        self.dimension = None
        self.index = None
        self.id_map = {}
        self.reverse_id_map = {}

        self._init_db()
        self._load_faiss_index()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)

        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        return conn

    def _init_db(self):
        """初始化 SQLite 表结构。"""
        conn = self._get_conn()
        c = conn.cursor()



        c.execute('''
            CREATE TABLE IF NOT EXISTS memory_items (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                created_at REAL,
                type TEXT,  -- 'event', 'chat', 'knowledge', 'summary'
                importance_score REAL DEFAULT 0.0,
                metadata_json TEXT,
                content_hash TEXT UNIQUE
            )
        ''')


        try:
            c.execute("SELECT content_hash FROM memory_items LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("正在迁移 DB：添加 content_hash 列...")
            try:
                c.execute("ALTER TABLE memory_items ADD COLUMN content_hash TEXT")
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_content_hash ON memory_items(content_hash)")
            except Exception as e:
                logger.error(f"迁移失败：{format_exception_inline(e)}")


        c.execute('''
            CREATE TABLE IF NOT EXISTS memory_vectors (
                id TEXT PRIMARY KEY,
                embedding BLOB,
                FOREIGN KEY(id) REFERENCES memory_items(id) ON DELETE CASCADE
            )
        ''')


        try:
            c.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    id UNINDEXED,
                    content,
                    tokenize='porter'
                )
            ''')
        except Exception as e:
            logger.warning(f"FTS5 初始化失败（可能不支持）：{format_exception_inline(e)}")




        c.execute('''
            CREATE TABLE IF NOT EXISTS embedding_cache (
                text_hash TEXT PRIMARY KEY,
                embedding BLOB,
                updated_at REAL,
                embedding_model TEXT
            )
        ''')


        try:
            c.execute("SELECT embedding_model FROM embedding_cache LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("正在迁移 DB：添加 embedding_cache.embedding_model 列...")
            try:
                c.execute("ALTER TABLE embedding_cache ADD COLUMN embedding_model TEXT")
            except Exception as e:
                logger.error(f"迁移失败：{format_exception_inline(e)}")

        conn.commit()
        conn.close()

    def rebuild_index(self):
        """从 SQLite 重建 FAISS 索引（清空重建）。"""
        logger.info("正在从数据库重建 FAISS 索引...")
        self._load_faiss_index()

    def _load_faiss_index(self):
        """将 DB 中的全部向量加载到内存中的 FAISS 索引。"""
        start_time = time.time()
        conn = self._get_conn()
        c = conn.cursor()

        c.execute("SELECT id, embedding FROM memory_vectors")
        rows = c.fetchall()
        conn.close()

        self.id_map = {}
        self.reverse_id_map = {}

        if not rows:
            logger.info("数据库中未找到向量，等待首次写入后再初始化索引。")
            self.index = None
            return

        vectors = []
        ids = []


        try:
            first_vec = np.frombuffer(rows[0][1], dtype=np.float32)
            self.dimension = first_vec.shape[0]
            logger.info(f"检测到向量维度：{self.dimension}")

            self.index = faiss.IndexFlatIP(self.dimension)

            valid_vectors = []

            for idx, (uuid_str, blob) in enumerate(rows):
                vec = np.frombuffer(blob, dtype=np.float32)
                if vec.shape[0] != self.dimension:
                    logger.warning(f"向量维度不匹配：{uuid_str}：{vec.shape[0]} != {self.dimension}")
                    continue

                valid_vectors.append(vec)

                current_id = len(valid_vectors) - 1
                self.id_map[current_id] = uuid_str
                self.reverse_id_map[uuid_str] = current_id

            if valid_vectors:
                vectors_np = np.array(valid_vectors).astype('float32')
                faiss.normalize_L2(vectors_np)
                self.index.add(vectors_np)

            logger.info(f"已加载 {len(valid_vectors)} 个向量到 FAISS，用时 {time.time() - start_time:.4f}s。")
        except Exception as e:
            logger.error(f"加载 FAISS 索引失败：{format_exception_inline(e)}", exc_info=True)
            self.index = None

    def add_memory(self, content, embedding, memory_type="chat", importance=0.0, metadata=None, created_at=None):
        """
        添加一条记忆（幂等：通过 content_hash 去重）。
        """
        if not content:
            return None


        import hashlib
        content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

        mem_id = str(uuid.uuid4())
        if created_at is None:
            created_at = time.time()

        if metadata is None:
            metadata = {}


        embedding_blob = None
        if embedding is not None:
            if isinstance(embedding, list):
                embedding = np.array(embedding, dtype=np.float32)
            else:
                embedding = embedding.astype(np.float32)

            dim = embedding.shape[0]


            if self.index is None:
                self.dimension = dim
                self.index = faiss.IndexFlatIP(self.dimension)
                logger.info(f"已初始化 FAISS 索引，维度={self.dimension}")
            elif dim != self.dimension:
                logger.error(f"embedding 维度不匹配：{dim} != {self.dimension}")
                return None


            faiss.normalize_L2(embedding.reshape(1, -1))
            embedding_blob = embedding.tobytes()

        conn = self._get_conn()
        try:
            c = conn.cursor()


            c.execute("SELECT id, type, IFNULL(importance_score, 0.0) FROM memory_items WHERE content_hash = ?", (content_hash,))
            row = c.fetchone()
            if row:
                existing_id = row[0]
                existing_type = row[1]
                try:
                    existing_importance = float(row[2] or 0.0)
                except Exception:
                    existing_importance = 0.0



                if (existing_type == "chat") and (memory_type == "chat"):
                    try:
                        new_importance = float(importance if importance is not None else existing_importance)
                    except Exception:
                        new_importance = existing_importance


                    if abs(new_importance - existing_importance) > 1e-6:
                        c.execute(
                            "UPDATE memory_items SET importance_score = ? WHERE id = ?",
                            (new_importance, existing_id),
                        )
                        conn.commit()
                return existing_id


            c.execute('''
                INSERT INTO memory_items (id, content, created_at, type, importance_score, metadata_json, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (mem_id, content, created_at, memory_type, importance, json.dumps(metadata, ensure_ascii=False), content_hash))

            c.execute('INSERT INTO memory_fts (id, content) VALUES (?, ?)', (mem_id, content))

            if embedding is not None:
                c.execute('INSERT INTO memory_vectors (id, embedding) VALUES (?, ?)', (mem_id, embedding_blob))

            conn.commit()


            if embedding is not None and self.index is not None:
                internal_id = self.index.ntotal
                self.index.add(embedding.reshape(1, -1))
                self.id_map[internal_id] = mem_id
                self.reverse_id_map[mem_id] = internal_id

            return mem_id

        except sqlite3.IntegrityError:
             logger.warning(f"content_hash={content_hash} 触发 IntegrityError")
             conn.rollback()
             return None
        except Exception as e:
            logger.error(f"添加记忆失败：{e!r}", exc_info=True)
            conn.rollback()
            return None
        finally:
            conn.close()

    def delete_memory_by_metadata(self, key, value):
        """
        按 JSON metadata 的 key/value 删除记忆，并重建索引以确保一致性。
        同时清理被删除行对应的 embedding_cache。
        """
        conn = self._get_conn()
        try:
            c = conn.cursor()
            c.execute(
                f"""
                SELECT id, content_hash
                FROM memory_items
                WHERE json_extract(metadata_json, '$.{key}') = ?
                """,
                (value,),
            )
            rows = c.fetchall()

            if not rows:
                return 0

            ids = [r[0] for r in rows]
            hashes = [r[1] for r in rows if r[1]]



            placeholders = ",".join(["?"] * len(ids))
            c.execute(f"DELETE FROM memory_vectors WHERE id IN ({placeholders})", ids)
            c.execute(f"DELETE FROM memory_fts WHERE id IN ({placeholders})", ids)
            c.executemany("DELETE FROM memory_items WHERE id = ?", [(i,) for i in ids])

            if hashes:
                ph2 = ",".join(["?"] * len(hashes))
                c.execute(f"DELETE FROM embedding_cache WHERE text_hash IN ({ph2})", hashes)

            conn.commit()
            logger.info(f"已从 DB 删除 {len(ids)} 条记忆（{key}={value}）")


            self.rebuild_index()

            return len(ids)

        except Exception as e:
            logger.error(f"删除记忆失败：{format_exception_inline(e)}")
            conn.rollback()
            return 0
        finally:
            conn.close()

    def reconcile_experiences(self, allowed_tags: set[str]) -> int:
        """
        确保 SQLite 中不存在 metadata.tag 不在 allowed_tags 内的 'experience' 行。
        这是 shared_experience_index 变化时缺失的“删除传播”步骤。
        返回删除行数。
        """
        allowed = {str(t).strip() for t in (allowed_tags or set()) if str(t).strip()}
        conn = self._get_conn()
        deleted = 0
        try:
            c = conn.cursor()
            rows = c.execute(
                """
                SELECT id, content_hash
                FROM memory_items
                WHERE type = 'experience'
                """,
            ).fetchall()
            if not rows:
                return 0

            to_delete_ids = []
            to_delete_hashes = []
            for mid, ch in rows:
                try:
                    meta_row = c.execute(
                        "SELECT metadata_json FROM memory_items WHERE id = ?",
                        (mid,),
                    ).fetchone()
                    meta_json = meta_row[0] if meta_row else None
                    meta = json.loads(meta_json) if meta_json else {}
                except Exception:
                    meta = {}
                tag = str((meta or {}).get("tag") or "").strip()
                if not tag or tag not in allowed:
                    to_delete_ids.append(mid)
                    if ch:
                        to_delete_hashes.append(ch)

            if not to_delete_ids:
                return 0

            ph = ",".join(["?"] * len(to_delete_ids))
            c.execute(f"DELETE FROM memory_vectors WHERE id IN ({ph})", to_delete_ids)
            c.execute(f"DELETE FROM memory_fts WHERE id IN ({ph})", to_delete_ids)
            c.execute(f"DELETE FROM memory_items WHERE id IN ({ph})", to_delete_ids)
            deleted = c.rowcount if c.rowcount is not None else len(to_delete_ids)

            if to_delete_hashes:
                ph2 = ",".join(["?"] * len(to_delete_hashes))
                c.execute(f"DELETE FROM embedding_cache WHERE text_hash IN ({ph2})", to_delete_hashes)

            conn.commit()
        except Exception as e:
            logger.error(f"Failed to reconcile experiences: {format_exception_inline(e)}", exc_info=True)
            conn.rollback()
            return 0
        finally:
            conn.close()

        try:
            self.rebuild_index()
        except Exception:
            pass

        return int(deleted)

    def reconcile_events(self, allowed_event_ids: set[str]) -> int:
        """
        Ensure SQLite has NO 'event' rows whose metadata.event_id is not in allowed_event_ids.
        Returns number of deleted rows.
        """
        allowed = {str(e).strip() for e in (allowed_event_ids or set()) if str(e).strip()}
        conn = self._get_conn()
        deleted = 0
        try:
            c = conn.cursor()
            rows = c.execute(
                """
                SELECT id, content_hash
                FROM memory_items
                WHERE type = 'event'
                """,
            ).fetchall()
            if not rows:
                return 0

            to_delete_ids = []
            to_delete_hashes = []
            for mid, ch in rows:
                try:
                    meta_row = c.execute(
                        "SELECT metadata_json FROM memory_items WHERE id = ?",
                        (mid,),
                    ).fetchone()
                    meta_json = meta_row[0] if meta_row else None
                    meta = json.loads(meta_json) if meta_json else {}
                except Exception:
                    meta = {}
                eid = str((meta or {}).get("event_id") or "").strip()
                if not eid or eid not in allowed:
                    to_delete_ids.append(mid)
                    if ch:
                        to_delete_hashes.append(ch)

            if not to_delete_ids:
                return 0

            ph = ",".join(["?"] * len(to_delete_ids))
            c.execute(f"DELETE FROM memory_vectors WHERE id IN ({ph})", to_delete_ids)
            c.execute(f"DELETE FROM memory_fts WHERE id IN ({ph})", to_delete_ids)
            c.execute(f"DELETE FROM memory_items WHERE id IN ({ph})", to_delete_ids)
            deleted = c.rowcount if c.rowcount is not None else len(to_delete_ids)

            if to_delete_hashes:
                ph2 = ",".join(["?"] * len(to_delete_hashes))
                c.execute(f"DELETE FROM embedding_cache WHERE text_hash IN ({ph2})", to_delete_hashes)

            conn.commit()
        except Exception as e:
            logger.error(f"Failed to reconcile events: {format_exception_inline(e)}", exc_info=True)
            conn.rollback()
            return 0
        finally:
            conn.close()

        try:
            self.rebuild_index()
        except Exception:
            pass

        return int(deleted)

    def dedupe_experiences(self) -> dict:
        """
        Deduplicate 'experience' rows by normalized metadata.tag (strip + collapse whitespace).
        Keeps the newest created_at per tag, deletes the rest (and clears vectors/fts/cache).
        Returns stats dict.
        """
        import re

        def norm_tag(t: str) -> str:
            t = str(t or "").strip()
            t = re.sub(r"\s+", " ", t)
            return t

        conn = self._get_conn()
        stats = {"total": 0, "kept": 0, "deleted": 0, "tags": 0}
        try:
            c = conn.cursor()
            rows = c.execute(
                """
                SELECT id, created_at, content_hash, metadata_json
                FROM memory_items
                WHERE type = 'experience'
                """,
            ).fetchall()
            stats["total"] = len(rows)
            if not rows:
                return stats

            by_tag = {}
            for mid, created_at, ch, meta_json in rows:
                try:
                    meta = json.loads(meta_json) if meta_json else {}
                except Exception:
                    meta = {}
                tag = norm_tag((meta or {}).get("tag") or "")
                if not tag:

                    tag = "__MISSING_TAG__"
                cur = by_tag.get(tag)

                ts = float(created_at or 0.0)
                if not cur or ts > cur["ts"]:
                    by_tag[tag] = {"keep_id": mid, "ts": ts}

            keep_ids = {v["keep_id"] for v in by_tag.values()}
            stats["tags"] = len(by_tag)
            stats["kept"] = len(keep_ids)

            delete_ids = []
            delete_hashes = []
            for mid, _created_at, ch, _meta_json in rows:
                if mid in keep_ids:
                    continue
                delete_ids.append(mid)
                if ch:
                    delete_hashes.append(ch)

            if delete_ids:
                ph = ",".join(["?"] * len(delete_ids))
                c.execute(f"DELETE FROM memory_vectors WHERE id IN ({ph})", delete_ids)
                c.execute(f"DELETE FROM memory_fts WHERE id IN ({ph})", delete_ids)
                c.execute(f"DELETE FROM memory_items WHERE id IN ({ph})", delete_ids)
                stats["deleted"] = c.rowcount if c.rowcount is not None else len(delete_ids)

                if delete_hashes:
                    ph2 = ",".join(["?"] * len(delete_hashes))
                    c.execute(f"DELETE FROM embedding_cache WHERE text_hash IN ({ph2})", delete_hashes)

            conn.commit()
        except Exception as e:
            logger.error(f"Failed to dedupe experiences: {format_exception_inline(e)}", exc_info=True)
            conn.rollback()
            return stats
        finally:
            conn.close()

        try:
            self.rebuild_index()
        except Exception:
            pass

        return stats

    def prune_chat_memories(
        self,
        now_ts: float | None = None,
        keep_all_days: int = 7,
        keep_important_days: int = 30,
        important_threshold: float = 0.6,
        permanent_threshold: float = 1.2,
    ) -> int:
        """
        Prune chat memories to control DB growth.

        Rules:
        - Keep ALL chats newer than keep_all_days.
        - Keep chats newer than keep_important_days if importance_score >= important_threshold.
        - Keep chats forever if importance_score >= permanent_threshold.
        """
        if now_ts is None:
            now_ts = time.time()


        keep_all_days = max(0, int(keep_all_days))
        keep_important_days = max(keep_all_days, int(keep_important_days))

        cutoff_all = now_ts - (keep_all_days * 86400)
        cutoff_important = now_ts - (keep_important_days * 86400)

        conn = self._get_conn()
        deleted = 0
        try:
            c = conn.cursor()


            rows = c.execute(
                """
                SELECT id, content_hash
                FROM memory_items
                WHERE type = 'chat'
                  AND (
                        (created_at < ? AND IFNULL(importance_score, 0.0) < ?)
                        OR
                        (created_at < ? AND IFNULL(importance_score, 0.0) < ?)
                      )
                """,
                (cutoff_important, permanent_threshold, cutoff_all, important_threshold),
            ).fetchall()
            ids = [r[0] for r in rows]
            hashes = [r[1] for r in rows if r[1]]
            if not ids:
                return 0


            placeholders = ",".join(["?"] * len(ids))
            c.execute(f"DELETE FROM memory_vectors WHERE id IN ({placeholders})", ids)
            c.execute(f"DELETE FROM memory_fts WHERE id IN ({placeholders})", ids)
            c.execute(f"DELETE FROM memory_items WHERE id IN ({placeholders})", ids)
            deleted = c.rowcount if c.rowcount is not None else len(ids)



            if hashes:
                ph2 = ",".join(["?"] * len(hashes))
                c.execute(f"DELETE FROM embedding_cache WHERE text_hash IN ({ph2})", hashes)

            conn.commit()
        except Exception as e:
            logger.error(f"Failed to prune chat memories: {format_exception_inline(e)}", exc_info=True)
            conn.rollback()
            return 0
        finally:
            conn.close()


        try:
            self.rebuild_index()
        except Exception:
            pass

        return int(deleted)

    def clear_non_chat_importance_scores(self) -> int:
        """
        Ensure DB does NOT store importance_score for non-chat items.
        Chat importance_score is used for retention policy and MUST be preserved.
        """
        conn = self._get_conn()
        updated = 0
        try:
            c = conn.cursor()
            c.execute(
                """
                UPDATE memory_items
                SET importance_score = 0.0
                WHERE type IS NOT NULL
                  AND type != 'chat'
                  AND IFNULL(importance_score, 0.0) != 0.0
                """
            )
            updated = c.rowcount or 0
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to clear non-chat importance scores: {format_exception_inline(e)}", exc_info=True)
            conn.rollback()
            return 0
        finally:
            conn.close()
        return int(updated)

    def get_cached_embedding(self, text_hash, model=None):
        """读取 embedding 缓存。

        * model=None：保持旧行为（只按 hash 取，兼容现有脚本的单参调用）；
        * model 指定：严格校验 embedding_model，旧 Gemini 行（NULL）与其它模型的
          行一律按 miss 返回 None，避免把旧向量混进新检索/FAISS。
        """
        conn = self._get_conn()
        try:
            c = conn.cursor()
            if model is None:
                c.execute(
                    "SELECT embedding FROM embedding_cache WHERE text_hash = ?",
                    (text_hash,),
                )
            else:
                c.execute(
                    "SELECT embedding FROM embedding_cache "
                    "WHERE text_hash = ? AND embedding_model = ?",
                    (text_hash, str(model)),
                )
            row = c.fetchone()
            if row and row[0]:
                return np.frombuffer(row[0], dtype=np.float32)
            return None
        finally:
            conn.close()

    def save_cached_embedding(self, text_hash, embedding, model=None):
        """写入 embedding 缓存；model=None 时保持旧行为（写 NULL 模型列）。"""
        if embedding is None: return
        conn = self._get_conn()
        try:
            if isinstance(embedding, list):
                embedding = np.array(embedding, dtype=np.float32)
            else:
                embedding = embedding.astype(np.float32)

            blob = embedding.tobytes()
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, updated_at, embedding_model)
                VALUES (?, ?, ?, ?)
            ''', (text_hash, blob, time.time(), model))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to save embedding cache: {format_exception_inline(e)}")
        finally:
            conn.close()

    def search_hybrid(self, query_text, query_vector=None, top_k=5, alpha=0.5, type_filter=None, type_quota=None):
        """
        Hybrid search with optional type filtering and quota.
        type_filter: list of strings e.g. ['chat', 'event']
        type_quota: dict of {type: min_count} e.g. {'event': 2, 'chat': 2}
        """
        results = {}


        if query_vector is not None and self.index is not None and self.index.ntotal > 0:
            if isinstance(query_vector, list):
                query_vector = np.array(query_vector, dtype=np.float32)
            else:
                query_vector = query_vector.astype(np.float32)

            if query_vector.shape[0] != self.dimension:
                logger.warning(f"Query vector dim mismatch: {query_vector.shape[0]} != {self.dimension}")
            else:
                faiss.normalize_L2(query_vector.reshape(1, -1))


                k_search = min(top_k * 4, self.index.ntotal)
                D, I = self.index.search(query_vector.reshape(1, -1), k_search)

                for rank, (score, idx) in enumerate(zip(D[0], I[0])):
                    if idx == -1: continue
                    mem_id = self.id_map.get(idx)
                    if mem_id:
                        results[mem_id] = results.get(mem_id, 0) + (score * alpha)


        if query_text:
            conn = self._get_conn()
            try:
                c = conn.cursor()

                import re
                query_sanitized = re.sub(r'[^\w\s]', '', query_text)
                query_sanitized = query_sanitized.replace('"', '""')

                if query_sanitized.strip():
                    c.execute(f'''
                        SELECT id, rank FROM memory_fts
                        WHERE memory_fts MATCH ?
                        ORDER BY rank
                        LIMIT {top_k * 4}
                    ''', (query_sanitized,))

                    rows = c.fetchall()
                    for rank, (mem_id, fts_score) in enumerate(rows):
                        score_proxy = 1.0 / (rank + 1)
                        results[mem_id] = results.get(mem_id, 0) + (score_proxy * (1 - alpha))
            except Exception as e:
                logger.warning(f"FTS search failed: {format_exception_inline(e)}")
            finally:
                conn.close()


        if not results:
            return []

        sorted_ids = sorted(results.items(), key=lambda x: x[1], reverse=True)
        final_results = []

        conn = self._get_conn()
        c = conn.cursor()


        type_clause = ""
        params = []
        if type_filter:
            placeholders = ','.join(['?'] * len(type_filter))
            type_clause = f"AND type IN ({placeholders})"
            params.extend(type_filter)






        candidate_ids = [mid for mid, score in sorted_ids]
        if not candidate_ids:
            conn.close()
            return []

        id_placeholders = ','.join(['?'] * len(candidate_ids))
        sql = f'''
            SELECT id, content, created_at, type, importance_score, metadata_json
            FROM memory_items
            WHERE id IN ({id_placeholders}) {type_clause}
        '''
        params = candidate_ids + params

        try:
            c.execute(sql, params)
            rows = c.fetchall()
            row_map = {r[0]: r for r in rows}


            def process_row(mem_id, score, row):
                content, created_at, m_type, importance, meta_json = row[1], row[2], row[3], row[4], row[5]
                try: meta = json.loads(meta_json)
                except: meta = {}
                return {
                    "id": mem_id,
                    "content": content,
                    "score": float(score),
                    "created_at": created_at,
                    "type": m_type,
                    "importance": importance,
                    "metadata": meta
                }


            if type_quota:
                quota_counts = {t: 0 for t in type_quota}
                quota_results = []
                remaining_results = []


                candidates_with_details = []
                for mem_id, score in sorted_ids:
                    if mem_id in row_map:
                        candidates_with_details.append(process_row(mem_id, score, row_map[mem_id]))


                used_ids = set()


                for item in candidates_with_details:
                    m_type = item['type']
                    if m_type in type_quota and quota_counts[m_type] < type_quota[m_type]:
                        quota_results.append(item)
                        quota_counts[m_type] += 1
                        used_ids.add(item['id'])


                for item in candidates_with_details:
                    if item['id'] not in used_ids:
                        remaining_results.append(item)









                final_candidates = quota_results


                slots_needed = max(0, top_k - len(final_candidates))
                final_candidates.extend(remaining_results[:slots_needed])



                final_results = sorted(final_candidates, key=lambda x: x['score'], reverse=True)

            else:

                for mem_id, score in sorted_ids:
                    if len(final_results) >= top_k:
                        break

                    if mem_id in row_map:
                        final_results.append(process_row(mem_id, score, row_map[mem_id]))

        except Exception as e:
            logger.error(f"Search retrieval failed: {format_exception_inline(e)}")
        finally:
            conn.close()

        return final_results

    def search_by_date_range(self, start_date: str, end_date: str | None = None, per_type_limit: int = 6):
        """
        按日期范围直接检索记忆，不依赖 embedding。

        - chat: 使用 created_at 时间戳，按北京时间（UTC+8）自然日过滤
        - event: 使用 metadata.event_id 前缀中的 YYYY-MM-DD
        - experience: 使用 metadata.source_event_id 前缀中的 YYYY-MM-DD
        """
        start_raw = str(start_date or "").strip()
        end_raw = str(end_date or start_date or "").strip()
        if not start_raw or not end_raw:
            return []

        try:
            start_dt = datetime.strptime(start_raw, "%Y-%m-%d")
            end_dt = datetime.strptime(end_raw, "%Y-%m-%d")
        except ValueError:
            logger.warning(f"Invalid date range for direct memory lookup: {start_raw} ~ {end_raw}")
            return []

        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt
            start_raw, end_raw = end_raw, start_raw

        local_tz = timezone(timedelta(hours=int(tenshi_config.TIMEZONE_OFFSET)))
        start_ts = datetime(
            start_dt.year, start_dt.month, start_dt.day, 0, 0, 0, tzinfo=local_tz
        ).timestamp()
        end_ts = datetime(
            end_dt.year, end_dt.month, end_dt.day, 23, 59, 59, tzinfo=local_tz
        ).timestamp()

        conn = self._get_conn()
        try:
            c = conn.cursor()
            sql = """
                SELECT id, content, created_at, type, importance_score, metadata_json
                FROM memory_items
                WHERE
                    (
                        type = 'chat'
                        AND created_at IS NOT NULL
                        AND created_at >= ?
                        AND created_at <= ?
                    )
                    OR (
                        type = 'event'
                        AND substr(json_extract(metadata_json, '$.event_id'), 1, 10) >= ?
                        AND substr(json_extract(metadata_json, '$.event_id'), 1, 10) <= ?
                    )
                    OR (
                        type = 'experience'
                        AND substr(json_extract(metadata_json, '$.source_event_id'), 1, 10) >= ?
                        AND substr(json_extract(metadata_json, '$.source_event_id'), 1, 10) <= ?
                    )
                ORDER BY created_at DESC
            """
            c.execute(sql, (start_ts, end_ts, start_raw, end_raw, start_raw, end_raw))
            rows = c.fetchall()
        except Exception as e:
            logger.error(f"Direct date-range memory lookup failed: {format_exception_inline(e)}")
            return []
        finally:
            conn.close()

        out = []
        type_counts = {"event": 0, "experience": 0, "chat": 0}
        max_per_type = max(1, int(per_type_limit or 6))

        for row in rows:
            mem_id, content, created_at, m_type, importance, meta_json = row
            if m_type in type_counts and type_counts[m_type] >= max_per_type:
                continue
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except Exception:
                meta = {}
            out.append(
                {
                    "id": mem_id,
                    "content": content,
                    "score": 1.0,
                    "created_at": created_at,
                    "type": m_type,
                    "importance": importance,
                    "metadata": meta,
                }
            )
            if m_type in type_counts:
                type_counts[m_type] += 1

        return out

    def get_stats(self):
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT count(*) FROM memory_items")
        count = c.fetchone()[0]


        try:
            c.execute("SELECT count(*) FROM embedding_cache")
            cache_count = c.fetchone()[0]
        except:
            cache_count = 0

        conn.close()
        return {
            "total_memories": count,
            "vector_index_size": self.index.ntotal if self.index else 0,
            "dimension": self.dimension,
            "embedding_cache_size": cache_count
        }

    def prune_orphan_embedding_cache(self) -> int:
        """
        Remove embedding_cache rows whose text_hash is not referenced by memory_items.content_hash.
        This keeps embedding_cache aligned with persisted memory items.
        """
        conn = self._get_conn()
        deleted = 0
        try:
            c = conn.cursor()


            c.execute(
                """
                DELETE FROM embedding_cache
                WHERE text_hash NOT IN (
                    SELECT content_hash FROM memory_items
                    WHERE content_hash IS NOT NULL AND content_hash != ''
                )
                """
            )
            deleted = c.rowcount or 0
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to prune orphan embedding_cache: {format_exception_inline(e)}", exc_info=True)
            conn.rollback()
            return 0
        finally:
            conn.close()
        return int(deleted)

if __name__ == "__main__":

    print("Initializing Memory Manager...")
    mgr = TenshiMemoryManager()
    print(mgr.get_stats())
