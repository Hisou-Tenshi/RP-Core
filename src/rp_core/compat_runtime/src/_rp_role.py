# SPDX-License-Identifier: AGPL-3.0-only
"""Read character data without importing any private Python module."""
import json
import os
from pathlib import Path
import re
from functools import lru_cache
from copy import deepcopy

@lru_cache(maxsize=8)
def _profile_snapshot(path, modified_ns, size, inode):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("instructions"), str):
        raise ValueError("A character-data profile with instructions is required.")
    return value


def profile():
    path = Path(os.environ["RP_CHARACTER_PATH"]).resolve()
    stat = path.stat()
    # Atomic replacement and same-size edits both invalidate the snapshot.
    return deepcopy(_profile_snapshot(str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))

def role_values():
    card = profile()
    role = card.get("role", {})
    full = card.get("name", "角色")
    defaults = {
        "ROLE_NAME_FULL": full, "ROLE_NAME_SHORT": full,
        "ROLE_NAME_EN": card.get("id", "character"),
        "ROLE_SELF_REFERENCE": "我", "ROLE_USER_REFERENCE": "对方",
        "ROLE_GIT_NAME": "RP character", "ROLE_GIT_EMAIL": "rp-character@example.invalid",
        "ROLE_NICKNAME_KEY": "天子称用户", "ROLE_MOOD_KEY": "tenshi",
    }
    values = {key: str(role.get(key, value)) for key, value in defaults.items()}
    values["PERSONA_CARD"] = card["instructions"]
    values["CHAT_SCORE_BOT_LABEL"] = values["ROLE_NAME_EN"]
    return values

def role_text(text):
    if not isinstance(text, str):
        return text
    values = role_values()
    text = text.replace("天子称用户", "__RP_LEGACY_NICKNAME_KEY__")
    # Speaker tags are an existing storage/protocol enum, not the role's name.
    protected = ["「天子」", '"天子|用户"']
    for index, value in enumerate(protected):
        text = text.replace(value, f"__RP_SPEAKER_{index}__")
    for source, target in [("比那名居天子", "ROLE_NAME_FULL"), ("地上之人", "ROLE_USER_REFERENCE"), ("本小姐", "ROLE_SELF_REFERENCE"), ("天子", "ROLE_NAME_SHORT")]:
        text = text.replace(source, values[target])
    text = re.sub(r"\bTenshi\b(?![.]json)", lambda _: values["ROLE_NAME_EN"], text)
    for index, value in enumerate(protected):
        text = text.replace(f"__RP_SPEAKER_{index}__", value)
    return text.replace("__RP_LEGACY_NICKNAME_KEY__", "天子称用户")

def weather_default(name):
    values = profile().get("weather_defaults", {})
    if name == "WEATHER_DEFAULT_PLACE_LABEL":
        return str(values.get(name, "未设置默认位置"))
    return float(values.get(name, 0.0))
