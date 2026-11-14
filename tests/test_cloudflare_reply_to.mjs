import assert from "node:assert/strict";
import { extractReplyTo } from "../integrations/cloudflare/full.mjs";

let checks = 0;
let passed = 0;

function check(name, fn) {
  checks++;
  try {
    fn();
    passed++;
    console.log(`  ok ${checks}. ${name}`);
  } catch (e) {
    console.error(`  FAIL ${checks}. ${name}`);
    throw e;
  }
}

try {
  check("普通文字引用：字段映射完整，quote_text 为空", () => {
    const out = extractReplyTo({
      message_id: 500,
      text: "这是回复",
      reply_to_message: {
        message_id: 499,
        from: { id: 12345678, is_bot: false },
        date: 1730000123,
        text: "被回复的消息"
      }
    });
    assert.deepEqual(out, {
      message_id: 499,
      from_id: 12345678,
      from_is_bot: false,
      date: 1730000123,
      text: "被回复的消息",
      quote_text: "",
      has_media: false,
      media_type: ""
    });
  });

  check("部分引用：message.quote.text 进入 quote_text", () => {
    const out = extractReplyTo({
      text: "引用后半段",
      quote: { text: "被引用的一小段" },
      reply_to_message: {
        message_id: 600,
        from: { id: 777, is_bot: true },
        date: 1730000200,
        text: "被引用消息的完整正文"
      }
    });
    assert.equal(out.quote_text, "被引用的一小段");
    assert.equal(out.text, "被引用消息的完整正文");
    assert.equal(out.message_id, 600);
    assert.equal(out.from_id, 777);
    assert.equal(out.from_is_bot, true);
  });

  check("部分引用：只有 quote、没有 reply_to_message 也能提取（其余字段回默认）", () => {
    const out = extractReplyTo({ text: "嗯？", quote: { text: "只有引用片段" } });
    assert.deepEqual(out, {
      message_id: 0,
      from_id: 0,
      from_is_bot: false,
      date: 0,
      text: "",
      quote_text: "只有引用片段",
      has_media: false,
      media_type: ""
    });
  });

  check("媒体引用 photo：has_media=true, media_type=photo", () => {
    const out = extractReplyTo({
      reply_to_message: {
        message_id: 701,
        from: { id: 1001, is_bot: false },
        date: 1730000300,
        photo: [{ file_id: "small" }, { file_id: "large" }]
      }
    });
    assert.equal(out.has_media, true);
    assert.equal(out.media_type, "photo");
    assert.equal(out.text, "");
  });

  check("媒体引用 document：边 caption 取到 text，media_type=document", () => {
    const out = extractReplyTo({
      reply_to_message: {
        message_id: 702,
        from: { id: 1001, is_bot: false },
        date: 1730000301,
        document: { file_id: "doc-1", file_name: "a.pdf" },
        caption: "文档说明"
      }
    });
    assert.equal(out.has_media, true);
    assert.equal(out.media_type, "document");
    assert.equal(out.text, "文档说明");
  });

  check("媒体优先级：按 photo→document→voice→audio→sticker→video→video_note→animation 取第一个", () => {
    const out = extractReplyTo({
      reply_to_message: {
        message_id: 703,
        from: { id: 1, is_bot: false },
        date: 1,
        animation: { file_id: "anim" },
        video: { file_id: "vid" },
        sticker: { file_id: "stk" },
        audio: { file_id: "aud" },
        voice: { file_id: "voi" },
        document: { file_id: "doc" },
        photo: [{ file_id: "pho" }]
      }
    });
    assert.equal(out.media_type, "photo");

    const videoNote = extractReplyTo({
      reply_to_message: {
        message_id: 704,
        from: { id: 1, is_bot: false },
        date: 1,
        video_note: { file_id: "vn" },
        animation: { file_id: "anim" }
      }
    });
    assert.equal(videoNote.media_type, "video_note");
    assert.equal(videoNote.has_media, true);
  });

  check("无 from（频道/匿名/被删除账号）：from_id=0, from_is_bot=false", () => {
    const out = extractReplyTo({
      reply_to_message: { message_id: 801, date: 1730000400, text: "没有发送者" }
    });
    assert.equal(out.message_id, 801);
    assert.equal(out.from_id, 0);
    assert.equal(out.from_is_bot, false);
    assert.equal(out.date, 1730000400);
  });

  check("from 类型异常（字符串 / null）：安全回默认", () => {
    const a = extractReplyTo({ reply_to_message: { message_id: 802, from: "bad", text: "x" } });
    assert.equal(a.from_id, 0);
    assert.equal(a.from_is_bot, false);
    const b = extractReplyTo({ reply_to_message: { message_id: 803, from: null, text: "x" } });
    assert.equal(b.from_id, 0);
    assert.equal(b.from_is_bot, false);
  });

  check("无引用：普通消息 / 空 message 返回 null", () => {
    assert.equal(extractReplyTo({ message_id: 1, text: "普通消息", date: 1 }), null);
    assert.equal(extractReplyTo({}), null);
  });

  check("超长裁剪：reply text 截断到 1500，quote_text 截断到 500", () => {
    const longReply = "回".repeat(3000);
    const longQuote = "引".repeat(2000);
    const out = extractReplyTo({
      quote: { text: longQuote },
      reply_to_message: {
        message_id: 901,
        from: { id: 9, is_bot: false },
        date: 2,
        text: longReply
      }
    });
    assert.equal(out.text.length, 1500);
    assert.equal(out.text, "回".repeat(1500));
    assert.equal(out.quote_text.length, 500);
    assert.equal(out.quote_text, "引".repeat(500));
  });

  check("边界：恰好 1500 / 500 不裁剪，caption 过长同样裁剪", () => {
    const exact = extractReplyTo({
      quote: { text: "q".repeat(500) },
      reply_to_message: { message_id: 902, from: { id: 9, is_bot: false }, date: 2, text: "t".repeat(1500) }
    });
    assert.equal(exact.text.length, 1500);
    assert.equal(exact.quote_text.length, 500);

    const caption = extractReplyTo({
      reply_to_message: {
        message_id: 903,
        from: { id: 9, is_bot: false },
        date: 2,
        document: { file_id: "d" },
        caption: "c".repeat(2500)
      }
    });
    assert.equal(caption.text.length, 1500);
    assert.equal(caption.media_type, "document");
  });

  check("异常输入不抛异常：null / undefined / 字符串 / 数字 / 数组", () => {
    assert.equal(extractReplyTo(null), null);
    assert.equal(extractReplyTo(undefined), null);
    assert.equal(extractReplyTo("not-a-message"), null);
    assert.equal(extractReplyTo(42), null);
    assert.equal(extractReplyTo([]), null);
    assert.equal(extractReplyTo({ reply_to_message: "oops" }), null);
    assert.equal(extractReplyTo({ quote: 123 }), null);
  });

  check("异常输入不抛异常：reply_to_message 为空对象 / 字段类型全错", () => {
    const empty = extractReplyTo({ reply_to_message: {} });
    assert.deepEqual(empty, {
      message_id: 0,
      from_id: 0,
      from_is_bot: false,
      date: 0,
      text: "",
      quote_text: "",
      has_media: false,
      media_type: ""
    });

    const weird = extractReplyTo({
      reply_to_message: {
        message_id: "abc",
        from: { id: "not-a-number", is_bot: "yes" },
        date: {},
        text: null,
        caption: undefined
      }
    });
    assert.equal(weird.message_id, 0);
    assert.equal(weird.from_id, 0);
    assert.equal(weird.date, 0);
    assert.equal(weird.text, "");
    assert.equal(typeof weird.from_is_bot, "boolean");
  });

  check("异常输入不抛异常：带副作用的 getter / 循环引用", () => {
    const hostile = {};
    Object.defineProperty(hostile, "reply_to_message", {
      enumerable: true,
      get() {
        throw new Error("boom");
      }
    });
    const origError = console.error;
    console.error = () => {};
    try {
      assert.equal(extractReplyTo(hostile), null);
    } finally {
      console.error = origError;
    }

    const circular = { message_id: 1, from: { id: 2 } };
    circular.reply_to_message = circular;
    const out = extractReplyTo(circular);
    assert.equal(out.message_id, 1);
    assert.equal(out.from_id, 2);
  });

  console.log(`\nPASS: extractReplyTo 契约全部通过（${passed}/${checks} 组用例）`);
  process.exit(0);
} catch (e) {
  console.error(`\nFAIL: ${passed}/${checks} 组用例通过，详见上方断言错误。`);
  console.error(e);
  process.exit(1);
}
