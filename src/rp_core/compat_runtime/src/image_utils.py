# SPDX-License-Identifier: AGPL-3.0-only



from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import tenshi_config
from error_text import format_exception_inline


logger = logging.getLogger(__name__)

_CACHE_PATH = os.path.join(tenshi_config.PROJECT_ROOT, "data", "image_analysis_cache.json")
_CACHE: Optional[Dict[str, str]] = None


def _download_dir() -> str:
    return tenshi_config.DOWNLOAD_DIR_PATH


def resolve_local_file_path(path: str) -> Optional[str]:
    """Resolve a logged file path to an on-disk file (basename fallback)."""
    if not path or not isinstance(path, str):
        return None
    p = path.strip()
    if not p:
        return None
    if os.path.isfile(p):
        return os.path.abspath(p)
    fname = os.path.basename(p.replace("\\", "/"))
    if not fname:
        return None
    candidate = os.path.join(_download_dir(), fname)
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    return None


def resolve_local_file_paths(paths: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for p in paths or []:
        resolved = resolve_local_file_path(p)
        if resolved and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def file_content_hash(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        logger.warning(f"file_content_hash failed for {path}: {format_exception_inline(e)}")
        return None


def _load_cache() -> Dict[str, str]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = {}
    if os.path.isfile(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _CACHE = {str(k): str(v) for k, v in data.items() if v}
        except Exception as e:
            logger.warning(f"Failed to load image analysis cache: {format_exception_inline(e)}")
    return _CACHE


def _save_cache() -> None:
    global _CACHE
    if _CACHE is None:
        return
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save image analysis cache: {format_exception_inline(e)}")


def get_cached_image_analysis(content_hash: str) -> Optional[str]:
    if not content_hash:
        return None
    return _load_cache().get(content_hash)


def set_cached_image_analysis(content_hash: str, analysis_text: str) -> None:
    if not content_hash or not (analysis_text or "").strip():
        return
    cache = _load_cache()
    cache[content_hash] = analysis_text.strip()
    _save_cache()


def extract_saved_image_analysis(msg: dict) -> str:
    """Read prior caption analysis from bot_thinking.image_analysis."""
    if not isinstance(msg, dict):
        return ""
    bt = msg.get("bot_thinking")
    if not isinstance(bt, dict):
        return ""
    raw = bt.get("image_analysis") or ""
    if not isinstance(raw, str):
        return ""
    raw = raw.strip()
    if not raw:
        return ""
    marker = "### Image Analysis by Assistant:"
    if marker in raw:
        idx = raw.index(marker)
        return raw[idx + len(marker) :].strip()
    return raw


def normalize_image_for_vision(
    path: str,
    *,
    max_edge: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> Tuple[str, bool]:
    """
    Ensure image is within API-friendly size. Returns (path, was_modified).
  May write a sidecar .vision.jpg next to the original.
    """
    max_edge = int(max_edge or getattr(tenshi_config, "IMAGE_MAX_EDGE_PX", 2048) or 2048)
    max_bytes = int(max_bytes or getattr(tenshi_config, "IMAGE_MAX_BYTES", 4_500_000) or 4_500_000)
    try:
        size = os.path.getsize(path)
    except OSError:
        return path, False
    if size <= max_bytes:
        try:
            from PIL import Image

            with Image.open(path) as img:
                w, h = img.size
                if max(w, h) <= max_edge:
                    return path, False
        except Exception:
            return path, False

    try:
        from PIL import Image
    except ImportError:
        return path, False

    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            w, h = img.size
            scale = min(1.0, max_edge / max(w, h))
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
            out_path = path + ".vision.jpg"
            quality = 88
            while quality >= 50:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                data = buf.getvalue()
                if len(data) <= max_bytes:
                    with open(out_path, "wb") as out:
                        out.write(data)
                    return out_path, True
                quality -= 8
            with open(out_path, "wb") as out:
                img.save(out, format="JPEG", quality=50, optimize=True)
            return out_path, True
    except Exception as e:
        logger.warning(f"normalize_image_for_vision failed for {path}: {format_exception_inline(e)}")
        return path, False


def prepare_image_paths(paths: List[str]) -> List[str]:
    """Resolve, dedupe by content hash, normalize oversized images."""
    resolved = resolve_local_file_paths(paths)
    if not resolved:
        return []

    by_hash: Dict[str, str] = {}
    ordered: List[str] = []
    for p in resolved:
        ch = file_content_hash(p) or p
        if ch in by_hash:
            continue
        norm, _ = normalize_image_for_vision(p)
        by_hash[ch] = norm
        ordered.append(norm)
    return ordered


def stitch_images_vertical(
    paths: List[str],
    *,
    max_edge: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> Optional[str]:
    """Stitch multiple images into one JPEG in downloads/."""
    max_edge = int(max_edge or getattr(tenshi_config, "IMAGE_STITCH_MAX_EDGE_PX", 1600) or 1600)
    max_bytes = int(max_bytes or getattr(tenshi_config, "IMAGE_MAX_BYTES", 4_500_000) or 4_500_000)
    if len(paths) < 2:
        return None
    try:
        from PIL import Image
    except ImportError:
        logger.warning("PIL not available; cannot stitch images.")
        return None

    images = []
    try:
        for p in paths:
            with Image.open(p) as im:
                im = im.convert("RGB")
                w, h = im.size
                if max(w, h) > max_edge:
                    scale = max_edge / max(w, h)
                    im = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
                images.append(im.copy())
    except Exception as e:
        logger.error(f"stitch_images_vertical load failed: {format_exception_inline(e)}")
        return None

    if not images:
        return None

    width = max(im.width for im in images)
    total_h = sum(im.height for im in images)
    canvas = Image.new("RGB", (width, total_h), (255, 255, 255))
    y = 0
    for im in images:
        x = (width - im.width) // 2
        canvas.paste(im, (x, y))
        y += im.height

    out_path = os.path.join(_download_dir(), f"{time.time()}_stitched.jpg")
    os.makedirs(_download_dir(), exist_ok=True)
    quality = 88
    while quality >= 50:
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            with open(out_path, "wb") as f:
                f.write(data)
            logger.info(f"Stitched {len(paths)} images -> {out_path} ({len(data)} bytes)")
            return out_path
        quality -= 8
    canvas.save(out_path, format="JPEG", quality=50, optimize=True)
    return out_path


def consolidate_image_files(paths: List[str]) -> List[str]:
    """
    Prepare images for the main model: normalize, optional stitch when count exceeds threshold.
    """
    prepared = prepare_image_paths(paths)
    if not prepared:
        return []
    threshold = int(getattr(tenshi_config, "IMAGE_STITCH_THRESHOLD", 3) or 3)
    if len(prepared) >= threshold:
        stitched = stitch_images_vertical(prepared)
        if stitched:
            return [stitched]
    return prepared


def validate_image_readable(path: str) -> bool:
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def collect_files_from_message(msg: dict) -> List[str]:
    if not isinstance(msg, dict):
        return []
    raw = msg.get("local_files") or []
    if not isinstance(raw, list):
        return []
    return resolve_local_file_paths([str(x) for x in raw if x])


def build_image_analysis_note(
    files: List[str],
    *,
    saved_analysis: str = "",
    per_file_descriptions: Optional[Dict[str, str]] = None,
) -> str:
    """Text block for models that cannot receive binary images (e.g. Grok)."""
    parts: List[str] = []
    per_file_descriptions = per_file_descriptions or {}

    for fp in files or []:
        fname = os.path.basename(fp)
        if fname in per_file_descriptions and per_file_descriptions[fname]:
            parts.append(f"「{fname}」: {per_file_descriptions[fname]}")
            continue
        ch = file_content_hash(fp)
        cached = get_cached_image_analysis(ch) if ch else None
        if cached:
            parts.append(f"「{fname}」: {cached}")

    if saved_analysis and saved_analysis.strip():
        if saved_analysis.strip() not in "\n".join(parts):
            parts.append(saved_analysis.strip())

    if not parts:
        return ""
    return "\n[图片分析存档]\n" + "\n".join(parts)
