# SPDX-License-Identifier: AGPL-3.0-only
import os
import json
import logging
import sys
import time


sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from tenshi_memory_manager import TenshiMemoryManager
from rag_modules import TenshiRAG
from tenshi_utils import format_chat_memory_content
import tenshi_config
from error_text import format_exception_inline



logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Migration")


def migrate(force: bool = False):
    logger.info("开始从 JSON 迁移到 SQLite...")


    mem_mgr = TenshiMemoryManager()


    logger.info("清空现有数据库，准备全量迁移...")
    conn = mem_mgr._get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM memory_items")
    c.execute("DELETE FROM memory_vectors")
    c.execute("DELETE FROM memory_fts")

    if force:
        logger.info("已启用 --force：清空 embedding_cache，触发全量向量重建（耗时/耗费更高）...")
        try:
            c.execute("DELETE FROM embedding_cache")
        except Exception:
            pass
    conn.commit()
    conn.close()


    mem_mgr.rebuild_index()

    rag = TenshiRAG()


    logs_data = []
    if os.path.exists(tenshi_config.LOG_FILE_PATH):
        try:
            with open(tenshi_config.LOG_FILE_PATH, 'r', encoding='utf-8') as f:
                logs_data = json.load(f)
        except Exception as e:
            logger.error(f"加载原始日志失败：{format_exception_inline(e)}")

    count = 0
    skipped = 0


    logger.info(f"开始迁移重大事件：{len(rag.memory_pool)} 条...")
    for event in rag.memory_pool:

        desc = event.get('description', '')
        if not desc:
            logger.warning(f"事件 {event.get('event_id')} 缺少描述，已跳过。")
            continue

        content = desc


        meta = {
            "event_id": event.get('event_id'),
            "emotional_impact": event.get('emotional_impact'),
            "mood_context": event.get('mood_context'),
            "original_source": "knowledge_base.json"
        }


        importance = 0.0

        emb = rag.get_embedding(content)

        mem_mgr.add_memory(
            content=content,
            embedding=emb,
            memory_type="event",
            importance=importance,
            metadata=meta
        )
        count += 1


    logger.info(f"开始迁移共同经历：{len(rag.narrative_memory_index)} 条...")
    for item in rag.narrative_memory_index:
        tag = item.get('tag', '')

        desc = item.get('origin', item.get('description', ''))


        if not desc:
            logger.warning(f"共同经历「{tag}」缺少 origin/description，将仅使用 tag 作为内容。")

        content = f"{tag}: {desc}"

        meta = {
            "tag": tag,
            "source_event_id": item.get('source_event_id'),
            "original_source": "knowledge_base.json"
        }

        emb = rag.get_embedding(content)

        mem_mgr.add_memory(
            content=content,
            embedding=emb,
            memory_type="experience",

            importance=0.0,
            metadata=meta
        )
        count += 1


    logger.info(f"开始迁移聊天日志：来自 {len(logs_data)} 个会话...")

    chat_count = 0
    for conv in logs_data:
        for msg in conv.get('chat_history', []):
            user_q = msg.get('user_query', '').strip()
            bot_a = msg.get('bot_answer', '').strip()
            ts = msg.get('time', time.time())



            content = format_chat_memory_content(ts=ts, user_query=user_q, bot_answer=bot_a)
            if not content:
                continue

            if len(content) < 15: continue

            emb = rag.get_embedding(content)

            mem_mgr.add_memory(
                content=content,
                embedding=emb,
                memory_type="chat",


                importance=float(msg.get("importance_score", 0.0) or 0.0),
                metadata={"original_source": "tenshi_log.json"},
                created_at=ts
            )
            chat_count += 1
            count += 1

            if count % 100 == 0:
                logger.info(f"已迁移 {count} 条...")

    logger.info(f"迁移完成。新增：{count}（其中聊天：{chat_count}）")


    stats = mem_mgr.get_stats()
    logger.info(f"数据库统计：{stats}")

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Migrate JSON memory into SQLite (tenshi_memory.db).")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Also wipe embedding_cache to force regenerating all embeddings (slow/costly).",
    )
    args = ap.parse_args()
    migrate(force=bool(args.force))
