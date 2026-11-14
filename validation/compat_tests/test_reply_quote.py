# SPDX-License-Identifier: AGPL-3.0-only









from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import tenshi_config
from telegram_tenshi_logger import parse_reply_to
from tenshi_utils import (
    format_reply_quote_block,
    render_user_text_with_quote,
)
from tenshi_reply import build_messages_payload_from_chat_history


def _ts(month: int, day: int, hour: int, minute: int, year: int = 2026) -> int:
    """按项目 TIMEZONE_OFFSET 把「本地钟面时间」换成 unix 时间戳。"""
    offset = int(getattr(tenshi_config, "TIMEZONE_OFFSET", 8) or 8)
    dt = datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=offset)))
    return int(dt.timestamp())


QUOTE_TS = _ts(10, 26, 13, 20)
USER_TS = _ts(10, 26, 12, 58)


def _quote_payload(**overrides) -> dict:
    payload = {
        "message_id": 12187,
        "from_id": 97383295,
        "from_is_bot": True,
        "date": QUOTE_TS,
        "text": "碗！谁！刷！的！",
        "quote_text": "",
        "has_media": False,
        "media_type": "",
    }
    payload.update(overrides)
    return payload


class ParseReplyToTest(unittest.TestCase):
    """坏输入绝不能让 logger 崩：一律当无引用（None）。"""

    def test_none_and_empty_like(self):
        for raw in (None, "", "   ", "\n\t", "null", "NULL", "  null  "):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_reply_to(raw))

    def test_bad_json_and_non_dict(self):
        for raw in (
            "not json",
            "{bad json}",
            "[]",
            "[1, 2]",
            '""',
            '"just a string"',
            "123",
            123,
            [1, 2],
            True,
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_reply_to(raw))

    def test_full_payload_from_bot(self):
        result = parse_reply_to(json.dumps(_quote_payload()))
        self.assertEqual(
            result,
            {
                "from": "bot",
                "message_id": 12187,
                "date": QUOTE_TS,
                "text": "碗！谁！刷！的！",
            },
        )

    def test_full_payload_from_user(self):
        result = parse_reply_to(json.dumps(_quote_payload(from_is_bot=False)))
        self.assertEqual(result["from"], "user")

    def test_quote_text_and_media_fields_kept(self):
        result = parse_reply_to(
            json.dumps(
                _quote_payload(
                    from_is_bot=False,
                    text="",
                    quote_text="谁！刷！",
                    has_media=True,
                    media_type="photo",
                )
            )
        )
        self.assertEqual(result["from"], "user")
        self.assertEqual(result["quote_text"], "谁！刷！")
        self.assertIs(result["has_media"], True)
        self.assertEqual(result["media_type"], "photo")
        self.assertNotIn("text", result, "空 text 不应塞空字符串键")
        self.assertNotIn("quote_text_extra", result)

    def test_text_truncated_to_1500(self):
        result = parse_reply_to(json.dumps(_quote_payload(text="x" * 2000, quote_text="")))
        self.assertEqual(len(result["text"]), 1500)

    def test_quote_text_truncated_to_500(self):
        result = parse_reply_to(json.dumps(_quote_payload(quote_text="y" * 800)))
        self.assertEqual(len(result["quote_text"]), 500)

    def test_media_type_whitelist(self):
        for media_type in ("photo", "document", "voice", "audio", "sticker", "video", "video_note", "animation"):
            with self.subTest(media_type=media_type):
                result = parse_reply_to(
                    json.dumps(_quote_payload(text="", has_media=True, media_type=media_type))
                )
                self.assertEqual(result["media_type"], media_type)
        unknown = parse_reply_to(
            json.dumps(_quote_payload(text="", has_media=True, media_type="web_page"))
        )
        self.assertNotIn("media_type", unknown)
        self.assertIs(unknown["has_media"], True, "has_media 已置真时仍保留媒体标记")

    def test_media_type_alone_implies_has_media(self):
        result = parse_reply_to(json.dumps(_quote_payload(text="", has_media=False, media_type="sticker")))
        self.assertIs(result["has_media"], True)

    def test_media_type_is_case_and_space_tolerant(self):
        result = parse_reply_to(json.dumps(_quote_payload(text="", media_type=" Photo ")))
        self.assertEqual(result["media_type"], "photo")

    def test_no_content_returns_none(self):
        self.assertIsNone(
            parse_reply_to(
                json.dumps(
                    {
                        "message_id": 0,
                        "from_is_bot": False,
                        "date": 0,
                        "text": "   ",
                        "quote_text": "",
                        "has_media": False,
                        "media_type": "",
                    }
                )
            )
        )

    def test_message_id_only_still_counts_as_quote(self):
        result = parse_reply_to(json.dumps({"message_id": 42}))
        self.assertEqual(result["message_id"], 42)
        self.assertEqual(result["from"], "user")

    def test_malformed_field_types_are_normalized(self):
        result = parse_reply_to(
            json.dumps(
                {
                    "message_id": "abc",
                    "date": "not-a-ts",
                    "from_is_bot": "false",
                    "text": 123,
                    "quote_text": None,
                    "has_media": "false",
                    "media_type": "photo",
                }
            )
        )

        self.assertEqual(result, {"from": "user", "has_media": True, "media_type": "photo"})

    def test_dict_input_is_accepted(self):
        self.assertEqual(parse_reply_to(_quote_payload(from_is_bot=False))["from"], "user")


class FormatReplyQuoteBlockTest(unittest.TestCase):
    """引用块渲染格式（中文括号 / 「」 / 北京时间 %m-%d %H:%M）。"""

    def setUp(self):
        self._saved_max = getattr(tenshi_config, "REPLY_QUOTE_MAX_CHARS", 600)

    def tearDown(self):
        tenshi_config.REPLY_QUOTE_MAX_CHARS = self._saved_max

    def test_no_quote_returns_empty(self):
        self.assertEqual(format_reply_quote_block({}), "")
        self.assertEqual(format_reply_quote_block({"user_query": "早"}), "")
        self.assertEqual(format_reply_quote_block({"reply_to": None}), "")
        self.assertEqual(format_reply_quote_block({"reply_to": "bad"}), "")
        self.assertEqual(format_reply_quote_block({"reply_to": {}}), "")

    def test_bot_text_quote(self):
        msg = {"reply_to": {"from": "bot", "date": QUOTE_TS, "text": "碗！谁！刷！的！"}}
        self.assertEqual(
            format_reply_quote_block(msg),
            "（引用她 10-26 13:20 的消息：「碗！谁！刷！的！」）",
        )

    def test_user_text_quote(self):
        msg = {"reply_to": {"from": "user", "date": USER_TS, "text": "上次说的那个汤"}}
        self.assertEqual(
            format_reply_quote_block(msg),
            "（引用他 10-26 12:58 的消息：「上次说的那个汤」）",
        )

    def test_partial_quote_prefers_quote_text(self):
        msg = {
            "reply_to": {
                "from": "bot",
                "date": QUOTE_TS,
                "text": "碗！谁！刷！的！整段原文",
                "quote_text": "谁！刷！",
            }
        }
        self.assertEqual(
            format_reply_quote_block(msg),
            "（引用她 10-26 13:20 的消息片段：「谁！刷！」）",
        )

    def test_media_only_quote(self):
        msg = {
            "reply_to": {
                "from": "user",
                "date": QUOTE_TS,
                "has_media": True,
                "media_type": "photo",
            }
        }
        self.assertEqual(
            format_reply_quote_block(msg),
            "（引用他的图片，10-26 13:20）",
        )

    def test_media_type_labels(self):
        cases = {
            "photo": "图片",
            "document": "文件",
            "voice": "语音",
            "audio": "音频",
            "sticker": "贴纸",
            "video": "视频",
            "video_note": "圆形视频",
            "animation": "动图",
        }
        for media_type, label in cases.items():
            with self.subTest(media_type=media_type):
                msg = {"reply_to": {"from": "bot", "date": QUOTE_TS, "has_media": True, "media_type": media_type}}
                self.assertEqual(format_reply_quote_block(msg), f"（引用她的{label}，10-26 13:20）")

    def test_unknown_media_type_falls_back_to_message(self):
        msg = {"reply_to": {"from": "user", "date": QUOTE_TS, "has_media": True, "media_type": "web_page"}}
        self.assertEqual(format_reply_quote_block(msg), "（引用他的消息，10-26 13:20）")

    def test_media_with_text_renders_as_text_quote(self):
        msg = {
            "reply_to": {
                "from": "user",
                "date": QUOTE_TS,
                "text": "你看这张",
                "has_media": True,
                "media_type": "photo",
            }
        }
        self.assertEqual(
            format_reply_quote_block(msg),
            "（引用他 10-26 13:20 的消息：「你看这张」）",
        )

    def test_missing_time_omits_time(self):
        msg = {"reply_to": {"from": "bot", "text": "没有时间"}}
        self.assertEqual(format_reply_quote_block(msg), "（引用她 的消息：「没有时间」）")
        media = {"reply_to": {"from": "user", "has_media": True, "media_type": "photo"}}
        self.assertEqual(format_reply_quote_block(media), "（引用他的图片）")

    def test_invalid_time_omits_time(self):
        msg = {"reply_to": {"from": "user", "date": "bad", "text": "时间坏了"}}
        self.assertEqual(format_reply_quote_block(msg), "（引用他 的消息：「时间坏了」）")

    def test_newlines_become_br(self):
        msg = {"reply_to": {"from": "user", "date": QUOTE_TS, "text": "第一行\n第二行"}}
        self.assertEqual(
            format_reply_quote_block(msg),
            "（引用他 10-26 13:20 的消息：「第一行<br>第二行」）",
        )

    def test_bot_markup_is_sanitized(self):
        msg = {
            "reply_to": {
                "from": "bot",
                "date": QUOTE_TS,
                "text": '<toolcalls><invoke name="note_patch"></invoke></toolcalls>碗！',
            }
        }
        rendered = format_reply_quote_block(msg)
        self.assertNotIn("toolcalls", rendered.lower())
        self.assertNotIn("invoke", rendered.lower())
        self.assertEqual(rendered, "（引用她 10-26 13:20 的消息：「碗！」）")

    def test_long_text_truncated_with_ellipsis_and_whole_brackets(self):
        tenshi_config.REPLY_QUOTE_MAX_CHARS = 10
        msg = {"reply_to": {"from": "user", "date": USER_TS, "text": "一二三四五六七八九十甲乙丙"}}
        rendered = format_reply_quote_block(msg)
        self.assertTrue(rendered.startswith("（引用他 10-26 12:58 的消息：「"))
        self.assertTrue(rendered.endswith("」）"))
        self.assertIn("…", rendered)
        self.assertEqual(
            rendered,
            "（引用他 10-26 12:58 的消息：「一二三四五六七八九…」）",
        )

    def test_max_chars_argument_overrides_config(self):
        tenshi_config.REPLY_QUOTE_MAX_CHARS = 600
        msg = {"reply_to": {"from": "user", "date": USER_TS, "text": "一二三四五"}}
        self.assertIn("一二三四五", format_reply_quote_block(msg, max_chars=100))
        self.assertEqual(
            format_reply_quote_block(msg, max_chars=3),
            "（引用他 10-26 12:58 的消息：「一二…」）",
        )


class RenderUserTextWithQuoteTest(unittest.TestCase):
    def test_without_quote_returns_input_verbatim(self):
        text = "第一行\n第二行  \t尾巴"
        self.assertEqual(render_user_text_with_quote({}, text), text)
        self.assertEqual(render_user_text_with_quote({"user_query": text}, text), text)

    def test_with_quote_prefixes_block_and_br(self):
        msg = {"reply_to": {"from": "bot", "date": QUOTE_TS, "text": "原句"}}
        self.assertEqual(
            render_user_text_with_quote(msg, "我的回复"),
            "（引用她 10-26 13:20 的消息：「原句」）<br>我的回复",
        )

    def test_empty_user_text_returns_block_only(self):
        msg = {"reply_to": {"from": "user", "date": USER_TS, "text": "原句"}}
        self.assertEqual(
            render_user_text_with_quote(msg, ""),
            "（引用他 10-26 12:58 的消息：「原句」）",
        )

    def test_non_dict_msg_is_treated_as_no_quote(self):
        self.assertEqual(render_user_text_with_quote(None, "正文"), "正文")
        self.assertEqual(render_user_text_with_quote("bad", "正文"), "正文")


class BuildMessagesPayloadQuoteTest(unittest.TestCase):
    """历史上下文：带引用的 user content 含引用块，不带的那条与旧输出逐字节一致。"""

    def test_history_with_and_without_quote(self):
        now = time.time()
        plain_turn = {
            "msg_id": "1",
            "time": now - 1200,
            "user_query": "早上好",
            "bot_answer": "早。",
            "replied_flag": True,
        }
        quote_turn = {
            "msg_id": "2",
            "time": now - 600,
            "user_query": "引用测试",
            "replied_flag": True,
            "reply_to": {
                "message_id": 12187,
                "from": "bot",
                "date": QUOTE_TS,
                "text": "原句",
            },
        }
        payload = build_messages_payload_from_chat_history(
            [quote_turn, plain_turn], max_messages=12
        )
        self.assertEqual(len(payload), 3)


        self.assertEqual(payload[0], {"role": "user", "content": "（20分钟前） 早上好"})
        self.assertEqual(payload[1], {"role": "assistant", "content": "（20分钟前） 早。"})


        self.assertEqual(
            payload[2]["content"],
            "（10分钟前） （引用她 10-26 13:20 的消息：「原句」）<br>引用测试",
        )


if __name__ == "__main__":
    unittest.main()
