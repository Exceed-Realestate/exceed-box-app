#!/usr/bin/env python3
"""Seed REFERENCE data only, idempotently. Safe to run against a real database.

The difference from seed.py matters and is the whole reason this file exists:

    seed.py                     this script
    ───────────────────────     ─────────────────────────────────────────
    calls db.reset() first      never drops anything
    SQLite only                 SQLite or Postgres
    invents ~60 demo leads,     inserts zero leads, zero people, zero
    people, sends, events       events — nothing that could be mistaken
                                for a real business record
    one-shot fixture            idempotent; re-running changes nothing

What it seeds is the configuration the product cannot start without: the ten
scoring rules (D2/D4/D5/D5a/D6/D8), the four settings values, the seven-step
nurture sequence as *editable drafts* (D17), the meeting types, and the booking
timezone row.

    python scripts/bootstrap_reference.py            # uses DATABASE_URL if set
    python scripts/bootstrap_reference.py --dry-run  # report, change nothing
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db                                         # noqa: E402

# ── the ten rules (D2 D4 D5 D5a D6 D8) ───────────────────────────────────────
RULES = [
    ("open",                 1, True,  "メール開封",        "Opened an email"),
    ("click",                3, True,  "リンククリック",     "Clicked a link"),
    ("page_view",            3, True,  "LP閲覧",            "Viewed a landing page"),
    ("booking_page_view",    5, True,  "相談ページ閲覧",     "Viewed the booking page"),
    ("reply",               10, True,  "返信",              "Replied"),
    ("booking_completed",   20, False, "予約完了",          "Booked a consultation"),
    ("wants_meeting",       30, False, "対面希望",          "Wants to meet in person"),
    ("corporate_deal",      30, False, "法人案件",          "Corporate buyer"),
    ("partnership",         30, False, "パートナー案件",     "Partnership deal"),
    ("high_budget",         30, False, "高予算",            "High budget"),
]

SETTINGS = [
    ("notify_threshold", "40", "D8 — score at which a rep is notified"),
    ("decay_half_life_days", "60", "D8 — behaviour points halve here"),
    ("decay_zero_days", "120", "D8 — behaviour points reach zero here"),
    ("high_budget_threshold_aed", "2000000",
     "D6 — PROVISIONAL. Never approved as a commercial fact. Balraj to confirm "
     "with the Japan office chief before this figure is relied on."),
]

# D17 — the seven reference emails. Editable drafts, not locked copy.
SEQUENCE = [
    (1,  0, "ドバイは本当に高いのか？", "先入観を崩す",
     "Do you actually believe Dubai is unaffordable?",
     "ドバイは本当に高いと思っていますか？", "記事を読む"),
    (2,  7, "ドバイで安く生活する方法", "興味を喚起",
     "Could you picture actually affording to live there?",
     "実際に住めそうだと思えますか？", "生活費チェックリスト"),
    (3, 14, "日本人・ノマド・経営者がドバイを選ぶ理由", "移住ニーズを特定",
     "Does relocating actually apply to someone like you?",
     "移住は自分にも関係があると思いますか？", "無料相談"),
    (4, 21, "ドバイ不動産を見る前に知るべきこと", "投資教育",
     "Are you ready to look at a real property, not just the idea?",
     "アイデアではなく実際の物件を見る準備はできていますか？", "物件相談"),
    (5, 28, "ロンボク — 次に来る開発地", "第二の選択肢を提示",
     "Would a developing market interest you more than a finished one?",
     "完成した市場より開発中の市場に興味がありますか？", "ロンボク資料"),
    (6, 35, "海外不動産で失敗する人の共通点", "信頼を築く",
     "What would stop you from buying overseas?",
     "海外購入をためらう理由は何ですか？", "失敗事例集"),
    (7, 42, "相談から購入までの実例", "行動を促す",
     "Ready to talk to someone who has done this before?",
     "経験者に相談してみませんか？", "無料相談を予約"),
]

MEETING_TYPES = [
    ("consult_30", "Free 30-min consultation", "無料30分相談", 30),
    ("site_inspection", "Site inspection", "視察", 120),
    ("showroom_visit", "Showroom visit", "来店", 60),
]


# The proposal's four SNS content patterns (p10–p13). They were only ever in
# seed.py, the SQLite demo seeder, so the live SNS screen came up empty —
# found 2026-09-18 by checking the running system against the proposal.
SNS_PATTERNS = [
    ("A", "ドバイの思い込みを壊す生活情報", "Breaking assumptions about Dubai living",
     "「ドバイは高い・怖い」という思い込みを、実際の生活情報で崩す投稿群。",
     "Posts that dismantle the 'Dubai is expensive/scary' assumption with real cost-of-living info."),
    ("B", "ノマド・経営者向け移住導線", "Relocation funnel for nomads and business owners",
     "リモートワーカー・経営者に向けた移住メリットの訴求。",
     "Targets remote workers and business owners with the practical case for relocating."),
    ("C", "ロンボク視察・開発ストーリー", "Lombok inspection and development story",
     "ロンボク開発の進捗を追うストーリー投稿で視察希望者を集める。",
     "Follows the Lombok development's progress to attract inspection-stage interest."),
    ("D", "海外不動産の失敗回避・相談導線", "Avoiding overseas property mistakes — consultation funnel",
     "海外不動産でよくある失敗を防ぐ情報から、無料相談への導線。",
     "Warns against common overseas property mistakes, funnelling into a free consultation."),
]


def _exists(con, sql, args=()) -> bool:
    return con.execute(sql, args).fetchone() is not None


def bootstrap(con=None, dry_run: bool = False) -> dict:
    own = con is None
    con = con or db.connect()
    added = {"scoring_rules": 0, "settings": 0, "sequences": 0,
             "sequence_steps": 0, "booking_meeting_types": 0, "booking_settings": 0,
             "sns_patterns": 0}
    try:
        for kind, pts, decays, ja, en in RULES:
            if _exists(con, "SELECT 1 FROM scoring_rules WHERE event_kind=?", (kind,)):
                continue
            added["scoring_rules"] += 1
            if not dry_run:
                con.execute(
                    "INSERT INTO scoring_rules (event_kind, points, decays, label_ja, label_en)"
                    " VALUES (?,?,?,?,?)", (kind, pts, bool(decays), ja, en))

        for key, value, note in SETTINGS:
            if _exists(con, "SELECT 1 FROM settings WHERE key=?", (key,)):
                continue
            added["settings"] += 1
            if not dry_run:
                con.execute("INSERT INTO settings (key,value,note) VALUES (?,?,?)",
                            (key, value, note))

        if not _exists(con, "SELECT 1 FROM sequences WHERE id=1"):
            added["sequences"] += 1
            if not dry_run:
                con.execute("INSERT INTO sequences (id,name) VALUES (1,?)",
                            ("ドバイ・ロンボク再活性",))
        for no, off, subj, purpose, qen, qja, cta in SEQUENCE:
            if _exists(con, "SELECT 1 FROM sequence_steps WHERE sequence_id=1 AND step_no=?", (no,)):
                continue
            added["sequence_steps"] += 1
            if not dry_run:
                con.execute(
                    "INSERT INTO sequence_steps (sequence_id, step_no, offset_days, subject,"
                    " purpose, question, question_ja, cta) VALUES (1,?,?,?,?,?,?,?)",
                    (no, off, subj, purpose, qen, qja, cta))

        for key, en, ja, mins in MEETING_TYPES:
            if _exists(con, "SELECT 1 FROM booking_meeting_types WHERE key=?", (key,)):
                continue
            added["booking_meeting_types"] += 1
            if not dry_run:
                con.execute(
                    "INSERT INTO booking_meeting_types (key, label_en, label_ja, duration_minutes)"
                    " VALUES (?,?,?,?)", (key, en, ja, mins))

        for key, title_ja, title_en, desc_ja, desc_en in SNS_PATTERNS:
            if _exists(con, "SELECT 1 FROM sns_patterns WHERE key=?", (key,)):
                continue
            added["sns_patterns"] += 1
            if not dry_run:
                con.execute(
                    "INSERT INTO sns_patterns (key, title_ja, title_en, description_ja,"
                    " description_en) VALUES (?,?,?,?,?)",
                    (key, title_ja, title_en, desc_ja, desc_en))

        if not _exists(con, "SELECT 1 FROM booking_settings WHERE id=1"):
            added["booking_settings"] += 1
            if not dry_run:
                con.execute(
                    "INSERT INTO booking_settings (id, jst_gst_gap_hours, business_hours_start,"
                    " business_hours_end, slot_length_minutes, timezone, note)"
                    " VALUES (1, 5, '09:00', '18:00', 30, 'Asia/Tokyo', ?)",
                    ("JST is 5 hours ahead of GST (Dubai) — 09:00 JST is 04:00 GST. "
                     "The decision log said 4 hours twice; verified wrong (D27b).",))

        if not dry_run:
            con.commit()
        return added
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be inserted, change nothing")
    args = ap.parse_args()
    where = "Postgres" if db.DIALECT == "postgres" else "SQLite %s" % db.DB_PATH
    print("target: %s" % where)
    result = bootstrap(dry_run=args.dry_run)
    verb = "would insert" if args.dry_run else "inserted"
    total = sum(result.values())
    for table, n in result.items():
        if n:
            print("  %s %d into %s" % (verb, n, table))
    print("nothing to do — reference data already present" if total == 0
          else "%s %d rows" % (verb, total))

    # Seeding the singleton config rows with explicit ids does NOT advance
    # Postgres' identity counters, so the first row a real user creates collides
    # with a duplicate-key error. Found by trying to add a second nurture
    # sequence. Always resync after seeding; it is idempotent and a no-op on
    # SQLite.
    if not args.dry_run:
        fixed = db.resync_identities(verbose=True)
        print("identity counters: %s" % (", ".join(fixed) if fixed else "all in sync"))
