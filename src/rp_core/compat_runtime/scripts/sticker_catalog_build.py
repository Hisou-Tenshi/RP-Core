# SPDX-License-Identifier: AGPL-3.0-only



















from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

MODEL = "google/gemini-3.8-flash"
CELL = 360
COLS = 3
PER_GRID = COLS * COLS


def _load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))






def download(sets: list[str], work: Path) -> list[dict]:
    token = os.environ["TELEGRAM_TOKEN"]
    api = f"https://api.telegram.org/bot{token}"
    (work / "raw").mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    with httpx.Client(timeout=60) as c:
        for set_name in sets:
            r = c.get(f"{api}/getStickerSet", params={"name": set_name}).json()
            if not r.get("ok"):
                raise SystemExit(f"getStickerSet {set_name} failed: {r.get('description')}")
            res = r["result"]
            for idx, st in enumerate(res["stickers"]):
                key = f"{set_name}_{idx:03d}"
                path = work / "raw" / f"{key}.webp"
                if not path.exists():
                    f = c.get(f"{api}/getFile", params={"file_id": st["file_id"]}).json()
                    if not f.get("ok"):
                        print(f"  skip {key}: {f.get('description')}", file=sys.stderr)
                        continue
                    blob = c.get(f"https://api.telegram.org/file/bot{token}/{f['result']['file_path']}")
                    blob.raise_for_status()
                    path.write_bytes(blob.content)
                items.append({
                    "key": key,
                    "set": set_name,
                    "set_title": res.get("title", ""),
                    "index": idx,
                    "file_id": st["file_id"],
                    "file_unique_id": st["file_unique_id"],
                    "emoji": st.get("emoji", ""),
                    "is_animated": st.get("is_animated", False),
                    "is_video": st.get("is_video", False),
                })
            print(f"{set_name}: {len(res['stickers'])} stickers")
    (work / "stickers_meta.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return items






def _font(size: int):
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFCompact.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _open_sticker(path: Path) -> Image.Image:
    im = Image.open(path).convert("RGBA")
    bg = Image.new("RGBA", im.size, (236, 236, 236, 255))
    bg.alpha_composite(im)
    return bg.convert("RGB")


def make_grids(items: list[dict], work: Path) -> list[dict]:
    """返回 [{grid, members:[key...]}]，编号 1..9 按行优先。"""
    gdir = work / "grids"
    gdir.mkdir(parents=True, exist_ok=True)
    font = _font(40)
    grids = []
    for gi in range(0, len(items), PER_GRID):
        chunk = items[gi:gi + PER_GRID]
        canvas = Image.new("RGB", (CELL * COLS, CELL * COLS), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        for j, it in enumerate(chunk):
            im = _open_sticker(work / "raw" / f"{it['key']}.webp")
            im.thumbnail((CELL - 16, CELL - 16))
            x0, y0 = (j % COLS) * CELL, (j // COLS) * CELL
            canvas.paste(im, (x0 + (CELL - im.width) // 2, y0 + (CELL - im.height) // 2))
            draw.rectangle([x0, y0, x0 + CELL - 1, y0 + CELL - 1], outline=(150, 150, 150), width=2)
            draw.rectangle([x0 + 2, y0 + 2, x0 + 58, y0 + 52], fill=(220, 30, 30))
            draw.text((x0 + 14, y0 + 4), str(j + 1), fill=(255, 255, 255), font=font)
        name = f"grid_{gi // PER_GRID:03d}.jpg"
        canvas.save(gdir / name, quality=88)
        grids.append({"grid": name, "members": [it["key"] for it in chunk]})
    (work / "grids.json").write_text(json.dumps(grids, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(grids)} grids")
    return grids






CAPTION_PROMPT = """这是一张拼图，里面有 {n} 张 Telegram 贴纸（表情包），每格左上角红底白字是编号 1~{n}。浅灰底色是透明背景，不是图的一部分。

背景：我在给一个聊天机器人挑贴纸。机器人扮演东方 Project 的比那名居天子（蓝色长发、戴桃子帽子的天人大小姐，傲娇、好胜、要面子、无聊了爱找事），在 Telegram 私聊里跟一个熟悉的男生聊天。她不会发和自己身份冲突、需要特定外部语境才能看懂、或者纯粹是别人专属梗的图；粗口/嘲讽类她其实可以发，只要符合她的性格。

请逐格看图，按编号输出一个 JSON 数组（只输出 JSON，不要别的文字），每个元素：
{{
  "n": 编号,
  "characters": "画的是谁（认得出的东方角色写名字，如 博丽灵梦 / 比那名居天子 / fumo 版琪露诺；认不出就写外观）",
  "is_tenshi": true/false（是否天子本人或天子 fumo）,
  "text_on_image": "图上的文字原文，没有就空字符串",
  "visual": "一句话：画面里在干什么、表情动作",
  "mood": "一句话：发这张图传达的是什么劲儿/情绪/意思，用聊天时的大白话（比如：嫌弃地斜眼看你、得意洋洋、装死不想理、震惊、委屈巴巴）",
  "usable": "yes" / "maybe" / "no"（天子在私聊里自然地发出这张合不合适）,
  "reason": "usable 的理由，一句话",
  "name": "建议短名，2~5 个汉字，是这张图的劲儿而不是画面（如 嫌弃、得意、装死、震惊、委屈、晚安）",
  "category": "大类，从这些里选一个：开心得意 / 嫌弃无语 / 生气不爽 / 委屈难过 / 震惊疑惑 / 害羞撒娇 / 摆烂困倦 / 挑衅嘲讽 / 问候日常 / 其他"
}}"""


def _extract_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    m = re.search(r"\[[\s\S]*\]", t)
    return json.loads(m.group(0) if m else t)


def _caption_one(grid: dict, work: Path, client: httpx.Client, key: str) -> dict:
    out_path = work / "captions" / (grid["grid"].rsplit(".", 1)[0] + ".json")
    if out_path.exists():
        return json.loads(out_path.read_text(encoding="utf-8"))
    b64 = base64.b64encode((work / "grids" / grid["grid"]).read_bytes()).decode()
    n = len(grid["members"])
    body = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": CAPTION_PROMPT.format(n=n)},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "temperature": 0.2,
    }
    last = None
    for attempt in range(4):
        try:
            r = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
                timeout=180,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            arr = _extract_json(text)
            if not isinstance(arr, list) or len(arr) != n:
                raise ValueError(f"expected {n} entries, got {len(arr) if isinstance(arr, list) else type(arr)}")
            rec = {"grid": grid["grid"], "members": grid["members"], "captions": arr}
            out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  ok {grid['grid']}")
            return rec
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    print(f"  FAIL {grid['grid']}: {last}", file=sys.stderr)
    return {"grid": grid["grid"], "members": grid["members"], "captions": None, "error": str(last)}


def caption(grids: list[dict], work: Path) -> list[dict]:
    key = os.environ["OPENROUTER_API_KEY"]
    (work / "captions").mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client, ThreadPoolExecutor(max_workers=6) as ex:
        recs = list(ex.map(lambda g: _caption_one(g, work, client, key), grids))
    meta = {m["key"]: m for m in json.loads((work / "stickers_meta.json").read_text(encoding="utf-8"))}
    merged = []
    for rec in recs:
        for j, k in enumerate(rec["members"]):
            cap = None
            if rec.get("captions"):
                cap = next((c for c in rec["captions"] if int(c.get("n", -1)) == j + 1), None)
            merged.append({**meta[k], "grid": rec["grid"], "cell": j + 1, "caption": cap})
    (work / "captions_merged.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    bad = [r["grid"] for r in recs if not r.get("captions")]
    print(f"captioned {len(merged) - sum(1 for m in merged if not m['caption'])}/{len(merged)}; failed grids: {bad}")
    return merged






_SET_ALIAS = {"F": "fumofumofumofumo", "K": "KevinMX_1"}


def pack(work: Path, curation: Path, out: Path) -> None:
    meta = {m["key"]: m for m in json.loads((work / "stickers_meta.json").read_text(encoding="utf-8"))}
    cur = json.loads(curation.read_text(encoding="utf-8"))
    cats = cur["categories"]
    stickers, seen = [], set()
    for short, name, cat, desc in cur["stickers"]:
        alias, idx = short.split("_")
        m = meta[f"{_SET_ALIAS[alias]}_{int(idx):03d}"]
        if name in seen:
            raise SystemExit(f"duplicate name: {name}")
        if cat not in cats:
            raise SystemExit(f"unknown category {cat} for {name}")
        seen.add(name)
        stickers.append({
            "name": name, "category": cat, "desc": desc,
            "file_id": m["file_id"], "file_unique_id": m["file_unique_id"],
            "set": m["set"], "index": m["index"],
        })
    stickers.sort(key=lambda s: cats.index(s["category"]))
    out.write_text(
        json.dumps({"version": 1, "categories": cats, "stickers": stickers}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"packed {len(stickers)} stickers → {out}")


def main() -> None:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["download", "grid", "caption", "all", "pack"])
    ap.add_argument("--sets", nargs="+", default=["fumofumofumofumo", "KevinMX_1"])
    ap.add_argument("--workdir", default="/tmp/tenshi_stickers")
    a = ap.parse_args()
    work = Path(a.workdir)
    work.mkdir(parents=True, exist_ok=True)
    if a.stage == "pack":
        root = Path(__file__).resolve().parent.parent
        pack(work, root / "scripts" / "sticker_curation.json", root / "src" / "config" / "stickers.json")
        return
    if a.stage in ("download", "all"):
        items = download(a.sets, work)
    else:
        items = json.loads((work / "stickers_meta.json").read_text(encoding="utf-8"))
    if a.stage in ("grid", "all"):
        grids = make_grids(items, work)
    elif a.stage == "caption":
        grids = json.loads((work / "grids.json").read_text(encoding="utf-8"))
    if a.stage in ("caption", "all"):
        caption(grids, work)


if __name__ == "__main__":
    main()
