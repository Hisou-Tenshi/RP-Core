# SPDX-License-Identifier: AGPL-3.0-only
"""Public shadow guard inherited by every compatibility subprocess."""
import os
if os.environ.get("RP_SHADOW") == "1":
    import socket
    from types import SimpleNamespace
    import telegram
    _original_dns = socket.getaddrinfo
    def _safe_dns(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host)
        if name.lower() == "api.telegram.org" or (os.environ.get("RP_UNIT_TESTS") == "1" and name not in ("127.0.0.1", "localhost", "::1")):
            raise OSError("Telegram delivery is disabled in shadow mode.")
        return _original_dns(host, *args, **kwargs)
    socket.getaddrinfo = _safe_dns
    class ShadowBot:
        def __init__(self, *args, **kwargs):
            self.id = 1
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def initialize(self):
            return None
        async def shutdown(self):
            return None
        async def get_me(self):
            return SimpleNamespace(id=1, username="shadow_character")
        async def send_message(self, *args, **kwargs):
            import json
            from pathlib import Path
            if os.environ.get("RP_SHADOW_TRACE"):
                with Path(os.environ["RP_SHADOW_TRACE"]).open("a") as stream:
                    stream.write(json.dumps({"method": "send_message", "text": kwargs.get("text", "")}, ensure_ascii=False) + "\n")
            return SimpleNamespace(message_id=1, chat_id=kwargs.get("chat_id", 1), text=kwargs.get("text", ""))
        async def edit_message_text(self, *args, **kwargs):
            return SimpleNamespace(message_id=kwargs.get("message_id", 1))
        async def send_chat_action(self, *args, **kwargs):
            return True
        async def set_message_reaction(self, *args, **kwargs):
            return True
        async def send_sticker(self, *args, **kwargs):
            return SimpleNamespace(message_id=1)
        async def delete_message(self, *args, **kwargs):
            return True
        async def get_file(self, *args, **kwargs):
            raise RuntimeError("Use synthetic local attachments in shadow tests.")
    telegram.Bot = ShadowBot
