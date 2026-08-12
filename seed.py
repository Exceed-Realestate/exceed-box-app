"""Seed the database with the scoring rules, the team, the 7-step sequence,
and the same ten people who appear in the Exceed Box demo — with Morita's
real timeline, dated exactly as the demo dates it.

Run:  python3 seed.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")
from app import db, ingest, scoring, tasks   # noqa: E402

NOW = datetime.utcnow()


def d(month, day, hour=9):
    return datetime(2026, month, day, hour, 0, 0)


# ── the ten rules (D2 D4 D5 D5a D6 D8) ───────────────────────────────────────
RULES = [
    # kind,                pts, decays, ja,                   en
    ("open",                 1, 1, "メール開封",        "Opened an email"),
    ("click",                3, 1, "リンククリック",     "Clicked a link"),
    ("page_view",            3, 1, "LP閲覧",            "Viewed a landing page"),
    ("booking_page_view",    5, 1, "相談ページ閲覧",     "Viewed the booking page"),
    ("reply",               10, 1, "返信",              "Replied"),
    ("booking_completed",   20, 0, "予約完了",          "Booked a consultation"),
    ("wants_meeting",       30, 0, "対面希望",          "Wants to meet in person"),
    ("corporate_deal",      30, 0, "法人案件",          "Corporate buyer"),
    ("partnership",         30, 0, "パートナー案件",     "Partnership deal"),
    ("high_budget",         30, 0, "高予算",            "High budget"),
]

STAFF = [
    ("ユパン",        "Yupan",       "初回相談担当",       "agent"),
    ("チアキ",        "Chiaki",      "移住・ライフスタイル", "agent"),
    ("ドバイデスク",   "Dubai Desk",  "現地情報",           "agent"),
    ("ロンボクデスク", "Lombok Desk", "視察・開発案件",      "agent"),
    ("藤原 浩平",     "Fujiwara",    "BtoB営業（9月〜）",   "manager"),
    ("Teruo Shimada", "Teruo",       "CEO",               "ceo"),
]

SEQUENCE = [
    (1,  0, "ドバイは本当に高いのか？",               "先入観を崩す",       "記事を読む"),
    (2,  7, "ドバイで安く生活する方法",               "興味を喚起",         "生活費チェックリスト"),
    (3, 14, "日本人・ノマド・経営者がドバイを選ぶ理由", "移住ニーズを特定",    "無料相談"),
    (4, 21, "ドバイ不動産を見る前に知るべきこと",       "投資教育",           "物件相談"),
    (5, 28, "ロンボク開発の将来性",                   "視察ニーズを特定",    "視察相談"),
    (6, 35, "海外不動産で失敗する人の共通点",          "不安を解消",         "30分相談"),
    (7, 42, "相談事例・購入パターン",                 "商談化",             "カレンダー予約"),
]

# name, company, channel, email, phone, purpose, region, stage
PEOPLE = [
    ("森田 賢二",   "青山商事株式会社",     "business_card", "k.morita@aoyama-s.example", "03-1234-1111", "business_base", "dubai",  "responded"),
    ("西田 綾香",   "個人投資家",           "csv",           "a.nishida@example.jp",      None,           "second_home",   "lombok", "responded"),
    ("久保 大地",   "フリーランス",         "sns",           "kubo@example.jp",           None,           "relocation",    "dubai",  "booked"),
    ("長谷川 里奈", "株式会社ベレーザ",     "lp_form",       "r.hasegawa@bereza.example", None,           "relocation",    "dubai",  "responded"),
    ("石川 颯太",   "石川ホールディングス", "gohighlevel",   "ishikawa@ihd.example",      None,           "investment",    "dubai",  "nurturing"),
    ("田村 由紀",   None,                   "sns",           "tamura@example.jp",         None,           "unknown",       "lombok", "nurturing"),
    ("藤井 直子",   "フジイデザイン",       "gmail",         "fujii@fujii-d.example",     None,           "relocation",    "dubai",  "nurturing"),
    ("安藤 美香",   None,                   "csv",           "ando@example.jp",           None,           "unknown",       "dubai",  "nurturing"),
    ("小野 博",     "小野パートナーズ",     "business_card", "ono@ono-p.example",         "03-1234-2222", "unknown",       "dubai",  "nurturing"),
    ("森 武",       "モリエステート",       "csv",           "mori@mori-e.example",       None,           "investment",    "dubai",  "new"),
]


def main():
    db.reset()
    con = db.connect()

    for kind, pts, decays, ja, en in RULES:
        con.execute("""INSERT INTO scoring_rules (event_kind, points, decays, label_ja, label_en)
                       VALUES (?,?,?,?,?)""", (kind, pts, decays, ja, en))
    con.execute("""INSERT INTO settings (key,value,note) VALUES
        ('notify_threshold','40','D8 — score at which a rep is notified'),
        ('decay_half_life_days','60','D8 — behaviour points halve here'),
        ('decay_zero_days','120','D8 — behaviour points reach zero here'),
        ('high_budget_threshold_aed','2000000',
         'D6 — PLACEHOLDER. Balraj to confirm with the Japan office chief (Yupon).')""")

    for name, en, role, access in STAFF:
        con.execute("INSERT INTO staff (name,name_en,role,access) VALUES (?,?,?,?)",
                    (name, en, role, access))
    con.commit()

    con.execute("INSERT INTO sequences (id,name) VALUES (1,'ドバイ・ロンボク再活性')")
    for no, off, subj, purpose, cta in SEQUENCE:
        con.execute("""INSERT INTO sequence_steps
                       (sequence_id, step_no, offset_days, subject, purpose, cta)
                       VALUES (1,?,?,?,?,?)""", (no, off, subj, purpose, cta))
    con.commit()

    owners = {1: 1, 2: 4, 3: 2, 4: 2}          # rough demo assignment
    ids = {}
    for i, (name, company, channel, email, phone, purpose, region, stage) in enumerate(PEOPLE, 1):
        res = ingest.upsert_lead(con, name=name, company=company, channel=channel,
                                 email=email, phone=phone,
                                 note="6/20 展示会・ユパンが交換" if channel == "business_card" else None,
                                 seen_at=d(6, 20) if channel == "business_card" else d(6, 1),
                                 owner_id=owners.get(i))
        lid = res["lead_id"]
        ids[name] = lid
        con.execute("UPDATE leads SET purpose=?, stage=? WHERE id=?", (purpose, stage, lid))
        con.execute("INSERT INTO lead_regions (lead_id, region, inferred_from) VALUES (?,?,?)",
                    (lid, region, "seed"))
    con.commit()

    # ── D13: Morita arrives twice more. One person, three channels. ──────────
    morita = ids["森田 賢二"]
    ingest.upsert_lead(con, name="森田 賢二", company="青山商事株式会社", channel="csv",
                       email="k.morita@aoyama-s.example", seen_at=d(6, 25))
    ingest.upsert_lead(con, name="森田 賢二", company="青山商事株式会社",
                       channel="gohighlevel", phone="03-1234-1111", seen_at=d(7, 1))

    # ── Morita's timeline, exactly as the demo dates it ──────────────────────
    con.execute("""INSERT INTO sends (send_id, lead_id, to_email, sent_at)
                   VALUES ('e_7Kq2mB', ?, 'k.morita@aoyama-s.example', ?)""",
                (morita, d(6, 23).isoformat(sep=" ")))
    tl = [
        ("open",              d(6, 23, 8),  "第1週メール「ドバイは本当に高いのか？」"),
        ("click",             d(6, 28, 10), "生活費チェックリスト"),
        ("page_view",         d(7, 2, 11),  "/corporate-relocation"),
        ("reply",             d(7, 5, 14),  "役員数名の移転を検討"),
        ("corporate_deal",    d(7, 6, 9),   "AI read of reply"),
        ("booking_page_view", d(8, 12, 8),  "/book"),
    ]
    for kind, when, detail in tl:
        scoring.record(con, morita, kind, detail=detail,
                       send_id="e_7Kq2mB" if kind in ("open", "click") else None,
                       source="sendgrid" if kind in ("open", "click") else "ai",
                       occurred_at=when)

    # a couple of others so the team view has something in it
    scoring.record(con, ids["西田 綾香"], "reply", detail="ロンボク視察希望", occurred_at=d(8, 11))
    scoring.record(con, ids["西田 綾香"], "wants_meeting", detail="AI read of reply",
                   source="ai", occurred_at=d(8, 11))
    scoring.record(con, ids["久保 大地"], "booking_completed", detail="7/10 ドバイ移住相談",
                   occurred_at=d(7, 10))
    scoring.record(con, ids["長谷川 里奈"], "click", detail="生活費チェックリスト",
                   occurred_at=d(8, 10))

    # tasks (D12) — with owners, real due dates, and a reason
    tasks.create(con, morita, "call",
                 reason=scoring.explain(con, morita)["summary"],
                 owner_id=1, due_at=NOW - timedelta(days=3), created_by="ai")
    tasks.create(con, ids["西田 綾香"], "send_material",
                 reason=scoring.explain(con, ids["西田 綾香"])["summary"],
                 owner_id=4, due_at=NOW - timedelta(days=1), created_by="ai")
    tasks.create(con, ids["長谷川 里奈"], "send_booking_link",
                 reason=scoring.explain(con, ids["長谷川 里奈"])["summary"],
                 owner_id=2, due_at=NOW + timedelta(hours=6), created_by="ai")
    con.commit()

    # ── report ───────────────────────────────────────────────────────────────
    print("seeded → %s\n" % db.DB_PATH)
    print("  staff %d · rules %d · sequence steps %d · leads %d · events %d" % (
        db.scalar(con, "SELECT count(*) FROM staff"),
        db.scalar(con, "SELECT count(*) FROM scoring_rules"),
        db.scalar(con, "SELECT count(*) FROM sequence_steps"),
        db.scalar(con, "SELECT count(*) FROM leads WHERE merged_into IS NULL"),
        db.scalar(con, "SELECT count(*) FROM events")))

    ch = ingest.channels(con, morita)
    print("\n  森田 賢二 arrived %d times → ONE lead (D13):" % len(ch))
    for c in ch:
        print("      %-14s %s" % (c["channel"], c["seen_at"][:10]))

    ex = scoring.explain(con, morita)
    print("\n  score = %d  (the demo hardcodes 92)" % ex["score"])
    for c in ex["components"]:
        print("      %-18s %-11s base %+3d  ×%.2f  = %+5.1f   %s" % (
            c["kind"], "(%s)" % c["group"], c["base_points"], c["decay_factor"],
            c["points"], c["occurred_at"][:10]))
    print("\n      → %s" % ex["summary"])
    print("\n  Note: June's points are worn down because June was ~%d days ago." %
          (NOW - d(6, 23)).days)
    print("  That is D8 working. The demo's 92 never moves; this number does.")
    con.close()


if __name__ == "__main__":
    main()
