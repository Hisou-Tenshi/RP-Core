# SPDX-License-Identifier: AGPL-3.0-only





import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import bond_reflect as br

CARD = {
    "version": 1,
    "updated_at": "2026-10-27",
    "standing": "我跟他在一起了，从五月三号算起。",
    "settled": [
        {"id": "together", "text": "五月三号他问我愿不愿意交往，我答了「又没说不愿意」。", "since": "2026-05-03", "src": []},
        {"id": "pet-names", "text": "他叫我老婆、宝，我嫌肉麻，可从没让他改过口。", "since": "2026-10-21", "src": []},
    ],
    "recent": [],
}


def msg(mid, ts, q="", a=""):
    return {"msg_id": mid, "time": ts, "user_query": q, "bot_answer": a}


DAY = "2026-10-28"
TS = 1793150000


def ev(id_, day, kind, speaker="天子", target="", serious=False, status="pending", quote="原话"):
    return {"id": id_, "day": day, "kind": kind, "speaker": speaker, "quote": quote, "gist": "",
            "target": target, "serious": serious, "status": status}


class QuoteTest(unittest.TestCase):
    def test_normalize_ignores_br_ws_stickers_markdown(self):
        self.assertEqual(br.normalize_quote("谁是你老婆<br>〔贴纸：变态〕 **少** 来"), "谁是你老婆少来")
        self.assertEqual(br.normalize_quote("“又没说不愿意”"), br.normalize_quote("「又没说不愿意」"))

    def test_locate_quote_requires_right_speaker(self):
        lines = br.dialogue_lines([msg("1", TS, q="老婆最好啦", a="谁是你老婆<br>行吧，咱妈")])
        self.assertEqual(br.locate_quote("行吧，咱妈", "天子", lines), "1")
        self.assertIsNone(br.locate_quote("老婆最好啦", "天子", lines))
        self.assertIsNone(br.locate_quote("编出来的话", "天子", lines))

    def test_system_trigger_lines_are_not_user_speech(self):
        lines = br.dialogue_lines([msg("p", TS, q="[系统级感知层触发 - 天子主动起念]", a="起床")])
        self.assertEqual([l["speaker"] for l in lines], ["天子"])


class EvidenceValidationTest(unittest.TestCase):
    def setUp(self):
        self.lines = br.dialogue_lines([
            msg("1", TS, q="以后我们去看海吧", a="行，说好了，明年夏天<br>谁是你老婆"),
        ])
        self.ids = {"together", "pet-names"}

    def test_accept_rejects(self):
        ok, rej = br.validate_evidence_items([
            {"kind": "commit", "speaker": "天子", "quote": "说好了，明年夏天"},
            {"kind": "affirm", "speaker": "用户", "quote": "以后我们去看海吧"},
            {"kind": "affirm", "speaker": "天子", "quote": "我最喜欢你了"},
            {"kind": "gossip", "speaker": "天子", "quote": "说好了"},
            {"kind": "rupture", "speaker": "天子", "quote": "谁是你老婆", "target": "nonexistent", "serious": True},
        ], self.lines, self.ids)
        self.assertEqual([o["kind"] for o in ok], ["commit", "rupture"])
        self.assertEqual(ok[1]["target"], "")
        self.assertEqual(sorted(r["reason"] for r in rej), ["bad_kind", "kind_requires_tenshi", "quote_not_found"])


class GateTest(unittest.TestCase):
    def led(self, evidence, last=""):
        return {"evidence": evidence, "last_reflect_day": last}

    def test_nothing_pending(self):
        self.assertFalse(br.should_reflect(self.led([]), DAY)[0])

    def test_single_casual_denial_does_not_trigger(self):
        L = self.led([ev("e1", DAY, "rupture", target="pet-names")])
        self.assertFalse(br.should_reflect(L, DAY)[0])

    def test_single_sweet_affirm_does_not_trigger(self):
        L = self.led([ev("e1", DAY, "affirm")])
        self.assertFalse(br.should_reflect(L, DAY)[0])

    def test_same_day_pile_up_needs_second_day(self):
        L = self.led([ev("e1", DAY, "affirm"), ev("e2", DAY, "affirm"), ev("e3", DAY, "commit")])
        go, why = br.should_reflect(L, DAY)
        self.assertFalse(go, why)

    def test_accumulated_over_days_triggers(self):
        L = self.led([ev("e1", "2026-10-27", "affirm"), ev("e2", DAY, "commit")])
        self.assertTrue(br.should_reflect(L, DAY)[0])

    def test_cooldown_blocks_accumulated_not_strong(self):
        L = self.led([ev("e1", "2026-10-27", "affirm"), ev("e2", DAY, "commit")], last="2026-10-27")
        self.assertFalse(br.should_reflect(L, DAY)[0])
        L["evidence"].append(ev("e3", DAY, "milestone"))
        self.assertTrue(br.should_reflect(L, DAY)[0])

    def test_serious_rupture_triggers(self):
        L = self.led([ev("e1", DAY, "rupture", serious=True, target="together")], last=DAY)
        self.assertTrue(br.should_reflect(L, DAY)[0])

    def test_considered_does_not_count(self):
        L = self.led([ev("e1", "2026-10-27", "milestone", status="considered")])
        self.assertFalse(br.should_reflect(L, DAY)[0])


class ValidateOpsTest(unittest.TestCase):
    def run_v(self, ops, evidence, card=None):
        return br.validate_ops(ops, copy.deepcopy(card or CARD), {"evidence": evidence}, DAY)

    def test_add_needs_pending_tenshi_affirm(self):
        evs = [ev("p1", DAY, "affirm", speaker="天子"), ev("c1", "2026-10-20", "affirm", status="considered"),
               ev("u1", DAY, "milestone", speaker="用户")]
        op = {"op": "add", "id": "sea-trip", "text": "我答应了明年夏天跟他去看海。"}
        ok, rej = self.run_v([{**op, "evidence": ["c1"]}], evs)
        self.assertEqual(rej[0]["reason"], "no_pending_evidence")
        ok, rej = self.run_v([{**op, "evidence": ["u1"]}], evs)
        self.assertEqual(rej[0]["reason"], "add_needs_tenshi_affirm")
        ok, rej = self.run_v([{**op, "evidence": ["p1"]}], evs)
        self.assertEqual(len(ok), 1)
        ok, rej = self.run_v([{**op, "evidence": ["nope"]}], evs)
        self.assertEqual(rej[0]["reason"], "no_pending_evidence")

    def test_text_rules(self):
        evs = [ev("p1", DAY, "affirm")]
        for text, reason in (("短", "too_short"), ("下次他再叫老婆我就认", "banned:下次"),
                             ("我嘴硬，其实认了他是我老公。", "banned:嘴硬"),
                             ("我第一次叫了他老公，他高兴坏了。", "banned:第一次")):
            ok, rej = self.run_v([{"op": "add", "id": "x-y", "text": text, "evidence": ["p1"]}], evs)
            self.assertEqual(rej[0]["reason"], f"text:{reason}", text)

    def test_add_bad_or_existing_id_and_dupe_text(self):
        evs = [ev("p1", DAY, "affirm")]
        ok, rej = self.run_v([{"op": "add", "id": "together", "text": "我跟他在一起了，没变过。", "evidence": ["p1"]}], evs)
        self.assertEqual(rej[0]["reason"], "bad_or_existing_id")
        ok, rej = self.run_v([{"op": "add", "id": "dupe", "text": CARD["settled"][1]["text"], "evidence": ["p1"]}], evs)
        self.assertEqual(rej[0]["reason"], "duplicate_text")

    def test_one_casual_denial_cannot_retract(self):
        evs = [ev("r1", DAY, "rupture", target="pet-names")]
        ok, rej = self.run_v([{"op": "retract", "id": "pet-names", "reason": "她说谁是你老婆", "evidence": ["r1"]}], evs)
        self.assertFalse(ok)
        self.assertTrue(rej[0]["reason"].startswith("retract_insufficient"))

    def test_repeated_ruptures_can_retract_unless_repaired(self):
        evs = [ev("r1", "2026-10-20", "rupture", target="pet-names", status="considered"),
               ev("r2", "2026-10-24", "rupture", target="pet-names", status="considered"),
               ev("r3", DAY, "rupture", target="pet-names")]
        op = {"op": "retract", "id": "pet-names", "reason": "连着几天认真不许他这么叫", "evidence": ["r3", "r2"]}
        ok, rej = self.run_v([op], evs)
        self.assertEqual(len(ok), 1, rej)
        evs.append(ev("f1", DAY, "repair", target="pet-names", speaker="用户"))
        ok, rej = self.run_v([op], evs)
        self.assertFalse(ok)

    def test_old_ruptures_outside_window_dont_count(self):
        evs = [ev("r1", "2026-09-01", "rupture", target="pet-names", status="considered"),
               ev("r2", "2026-09-05", "rupture", target="pet-names", status="considered"),
               ev("r3", DAY, "rupture", target="pet-names")]
        ok, rej = self.run_v([{"op": "retract", "id": "pet-names", "reason": "x", "evidence": ["r3"]}], evs)
        self.assertFalse(ok)

    def test_serious_rupture_retracts(self):
        evs = [ev("r1", DAY, "rupture", target="together", serious=True)]
        ok, rej = self.run_v([{"op": "retract", "id": "together", "reason": "认真分手", "evidence": ["r1"]}], evs)
        self.assertEqual(len(ok), 1)

    def test_retract_by_user_quote_rejected(self):
        evs = [ev("r1", DAY, "rupture", target="together", serious=True, speaker="用户")]
        ok, rej = self.run_v([{"op": "retract", "id": "together", "reason": "x", "evidence": ["r1"]}], evs)
        self.assertEqual(rej[0]["reason"], "retract_needs_rupture")

    def test_standing_needs_milestone(self):
        evs = [ev("a1", DAY, "affirm"), ev("m1", DAY, "milestone")]
        op = {"op": "standing", "text": "我答应嫁给他了，从十月二十八号算起。"}
        ok, rej = self.run_v([{**op, "evidence": ["a1"]}], evs)
        self.assertEqual(rej[0]["reason"], "standing_needs_milestone")
        ok, rej = self.run_v([{**op, "evidence": ["m1"]}], evs)
        self.assertEqual(len(ok), 1)

    def test_caps(self):
        evs = [ev("p1", DAY, "affirm")]
        ops = [{"op": "add", "id": f"item-{i}", "text": f"我认了第{i}件新的事情，不是随口说的。", "evidence": ["p1"]}
               for i in range(5)]
        ok, rej = self.run_v(ops, evs)
        self.assertEqual(len(ok), 2)
        self.assertIn("max_adds", [r["reason"] for r in rej])

    def test_amend(self):
        evs = [ev("p1", DAY, "affirm"), ev("r1", DAY, "rupture")]
        ok, rej = self.run_v([{"op": "amend", "id": "pet-names", "text": "他叫我老婆、宝，我嫌肉麻，自己也叫过他宝。", "evidence": ["r1"]}], evs)
        self.assertEqual(rej[0]["reason"], "amend_needs_affirm_or_repair")
        ok, rej = self.run_v([{"op": "amend", "id": "pet-names", "text": "他叫我老婆、宝，我嫌肉麻，自己也叫过他宝。", "evidence": ["p1"]}], evs)
        self.assertEqual(len(ok), 1)


class ApplyOpsTest(unittest.TestCase):
    def test_apply_and_retire(self):
        card = copy.deepcopy(CARD)
        ledger = {"evidence": [ev("p1", DAY, "affirm", quote="说好了"), ev("r1", DAY, "rupture", serious=True)], "retired": []}
        ops = [
            {"op": "add", "id": "sea-trip", "text": "我答应了明年夏天跟他去看海。", "evidence": ["p1"]},
            {"op": "amend", "id": "pet-names", "text": "他叫我老婆、宝，我嫌肉麻，自己也叫过他宝。", "evidence": ["p1"]},
            {"op": "retract", "id": "together", "reason": "认真分手", "evidence": ["r1"]},
        ]
        changes = br.apply_ops(card, ledger, ops, DAY)
        self.assertEqual([c["op"] for c in changes], ["add", "amend", "retract"])
        ids = [s["id"] for s in card["settled"]]
        self.assertEqual(ids, ["pet-names", "sea-trip"])
        self.assertEqual(card["version"], 2)
        self.assertEqual(ledger["retired"][0]["id"], "together")
        self.assertEqual(changes[1]["from"], CARD["settled"][1]["text"])
        new = next(s for s in card["settled"] if s["id"] == "sea-trip")
        self.assertEqual(new["evidence"][0]["quote"], "说好了")

    def test_no_ops_no_version_bump(self):
        card = copy.deepcopy(CARD)
        self.assertEqual(br.apply_ops(card, {"evidence": []}, [], DAY), [])
        self.assertEqual(card["version"], 1)


class MoodTest(unittest.TestCase):
    def test_mood_expiry_and_rules(self):
        card = copy.deepcopy(CARD)
        self.assertTrue(br.apply_mood(card, {"text": "他又喝了酒，我还有点气。", "days": 2}, DAY))
        self.assertEqual(card["recent"][0]["until"], "2026-10-30")
        self.assertFalse(br.apply_mood(card, {"text": "下次他再喝我就骂他。", "days": 1}, DAY))
        self.assertEqual(len(card["recent"]), 1)
        self.assertTrue(br.apply_mood(card, None, "2026-10-31"))
        self.assertEqual(card["recent"], [])


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.card = os.path.join(self.tmp, "bond_state.json")
        self.led = os.path.join(self.tmp, "bond_ledger.json")
        json.dump(CARD, open(self.card, "w", encoding="utf-8"), ensure_ascii=False)
        self.calls = []

    def fake(self, reply):
        def _llm(prompt, label):
            self.calls.append(label)
            return json.dumps(reply(label), ensure_ascii=False)
        return _llm

    def test_denial_day_records_but_card_unchanged(self):
        msgs = [msg("1", TS, q="早哇老婆", a="谁是你老婆，少蹬鼻子上脸<br>早")]
        llm = self.fake(lambda label: {"evidence": [
            {"kind": "rupture", "speaker": "天子", "quote": "谁是你老婆", "target": "pet-names", "serious": False}],
            "mood": None})
        res = br.collect_day_evidence(msgs, DAY, llm, card_path=self.card, ledger_path_=self.led)
        self.assertEqual(len(res["added"]), 1)
        rec = br.reflect(DAY, llm, card_path=self.card, ledger_path_=self.led)
        self.assertFalse(rec["ran"])
        self.assertEqual(self.calls, ["bond.evidence"])
        self.assertEqual(json.load(open(self.card, encoding="utf-8"))["settled"], CARD["settled"])

    def test_milestone_reflects_and_marks_considered(self):
        msgs = [msg("1", TS, q="那我们订婚吧", a="……行，订就订，本小姐答应了")]

        def reply(label):
            if label == "bond.evidence":
                return {"evidence": [{"kind": "milestone", "speaker": "天子", "quote": "本小姐答应了",
                                      "gist": "我答应订婚了"}], "mood": {"text": "答应了他，心里一直在跳。", "days": 2}}
            return {"ops": [
                {"op": "add", "id": "engaged", "text": "十月二十八号他说订婚，我答应了。", "evidence": [f"ev-{DAY}-1"]},
                {"op": "retract", "id": "together", "reason": "顺手删", "evidence": [f"ev-{DAY}-1"]},
            ], "notes": "订婚"}

        llm = self.fake(reply)
        br.collect_day_evidence(msgs, DAY, llm, card_path=self.card, ledger_path_=self.led)
        rec = br.reflect(DAY, llm, card_path=self.card, ledger_path_=self.led)
        self.assertTrue(rec["ran"])
        self.assertEqual([c["op"] for c in rec["applied"]], ["add"])
        self.assertEqual(rec["rejected"][0]["reason"], "retract_needs_rupture")
        card = json.load(open(self.card, encoding="utf-8"))
        self.assertIn("engaged", [s["id"] for s in card["settled"]])
        self.assertIn("together", [s["id"] for s in card["settled"]])
        self.assertEqual(card["recent"][0]["text"], "答应了他，心里一直在跳。")
        led = json.load(open(self.led, encoding="utf-8"))
        self.assertTrue(all(e["status"] == "considered" for e in led["evidence"]))
        self.assertEqual(led["last_reflect_day"], DAY)

        self.assertFalse(br.reflect(DAY, llm, card_path=self.card, ledger_path_=self.led)["ran"])

    def test_bad_llm_output_keeps_pending(self):
        msgs = [msg("1", TS, q="订婚吧", a="本小姐答应了")]
        llm = self.fake(lambda label: {"evidence": [{"kind": "milestone", "speaker": "天子", "quote": "本小姐答应了"}]}
                        if label == "bond.evidence" else {})
        br.collect_day_evidence(msgs, DAY, llm, card_path=self.card, ledger_path_=self.led)
        rec = br.reflect(DAY, lambda p, l: "不是 JSON", card_path=self.card, ledger_path_=self.led)
        self.assertEqual(rec["notes"], "llm_no_json")
        led = json.load(open(self.led, encoding="utf-8"))
        self.assertEqual(led["evidence"][0]["status"], "pending")

    def test_dedupe_same_quote_same_day(self):
        msgs = [msg("1", TS, q="订婚吧", a="本小姐答应了")]
        llm = self.fake(lambda label: {"evidence": [{"kind": "milestone", "speaker": "天子", "quote": "本小姐答应了"}]})
        br.collect_day_evidence(msgs, DAY, llm, card_path=self.card, ledger_path_=self.led)
        br.collect_day_evidence(msgs, DAY, llm, card_path=self.card, ledger_path_=self.led)
        self.assertEqual(len(json.load(open(self.led, encoding="utf-8"))["evidence"]), 1)


class RealCardEvidenceTest(unittest.TestCase):
    """仓库卡片的每条定论都带至少一条原话证据，且字样过得了卡片规则（seed 的"第一次"已除外）。"""

    def test_real_card(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bond_state.json")
        if not os.path.exists(p):
            self.skipTest("no card")
        card = json.load(open(p, encoding="utf-8"))
        for s in card["settled"]:
            self.assertTrue(s.get("evidence"), s["id"])
            for e in s["evidence"]:
                self.assertTrue(e.get("quote") and e.get("msg_id"), s["id"])
            self.assertEqual(br.bad_text_reason(s["text"]), "", s["id"])


if __name__ == "__main__":
    unittest.main()
