# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
import io
import json
from pathlib import Path


def test_live_check_uses_only_readonly_telegram_methods(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "verify_live", Path(__file__).parents[1] / "scripts/verify_live.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv(
        "PERCEPTION_SCHEDULE_SYNC_URL", "https://example.invalid/internal/perception-schedule"
    )
    monkeypatch.setenv("PERCEPTION_SCHEDULE_SYNC_SECRET", "synthetic-schedule-key")
    monkeypatch.setenv("TG_TOKEN", "synthetic-bot-token")
    monkeypatch.setattr(
        module,
        "urlopen",
        lambda *a, **k: io.StringIO(json.dumps({"service": "rp-core", "enabled": True})),
    )
    calls = []

    def post(url, payload, **kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/getMe"):
            return {"ok": True, "result": {"is_bot": True}}
        if url.endswith("/getWebhookInfo"):
            return {
                "ok": True,
                "result": {"url": "https://example.invalid/", "pending_update_count": 0},
            }
        return {"event_id": "synthetic-event"}

    monkeypatch.setattr(module, "post_json", post)
    monkeypatch.setattr(module, "wait_receipt", lambda value: value == "synthetic-event")
    monkeypatch.setattr("sys.argv", ["verify_live", "--control", "probe"])
    module.main()
    output = capsys.readouterr().out
    result = json.loads(output)
    assert calls == ["getMe", "getWebhookInfo", "probe"]
    assert result["receipt_completed"] is True
    assert result["chat_messages_sent"] == 0
    assert "synthetic-bot-token" not in output
    assert "synthetic-schedule-key" not in output
