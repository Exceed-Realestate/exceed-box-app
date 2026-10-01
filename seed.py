"""Seed the database with the scoring rules, the team, the 7-step sequence,
the same ten people who appear in the Exceed Box demo — with Morita's real
timeline, dated exactly as the demo dates it — plus the auth/RBAC layer:
app_user logins in all four roles, and ~60 leads spread across every stage
and owner so every screen (Today, Leads, Pipeline, Dashboard, Team, Admin) is
populated for every role on first run.

Run:  python3 seed.py
"""
from __future__ import annotations

import json
import random
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, ".")
from app import db, ingest, media, scoring, tasks, tracking   # noqa: E402

NOW = datetime.utcnow()
random.seed(42)   # deterministic seed data — reruns produce the same demo


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

# ── app_user — the auth/RBAC login roster (SPEC.md), D20/D21's @exceed-re.ae ──
# email, display_name, role, office, staff_name-to-bridge (None = no legacy
# staff row; auth.ensure_staff_bridge() creates one the first time it is
# needed — e.g. Balraj granting a manual +30). "Pending Signup" is left
# inactive on purpose: it is the fail-closed auto-provisioned state an
# unrecognised login lands in until an admin flips it on (PATCH /api/users).
USERS = [
    ("balraj@exceed-re.ae",    "Balraj",         "admin",          "dubai", None),
    ("teruo@exceed-re.ae",     "Teruo Shimada",  "admin",          "tokyo", "Teruo Shimada"),
    ("fujiwara@exceed-re.ae",  "Fujiwara",       "office_manager", "tokyo", "藤原 浩平"),
    ("marketing@exceed-re.ae", "SNS Marketing",  "marketing",      "tokyo", None),
    ("yupan@exceed-re.ae",     "Yupan",          "sales",          "tokyo", "ユパン"),
    ("chiaki@exceed-re.ae",    "Chiaki",         "sales",          "tokyo", "チアキ"),
    ("dubaidesk@exceed-re.ae", "Dubai Desk",     "sales",          "dubai", "ドバイデスク"),
    ("lombokdesk@exceed-re.ae","Lombok Desk",    "sales",          "dubai", "ロンボクデスク"),
    ("pending@exceed-re.ae",   "Pending Signup", "sales",          "tokyo", None),
]

# no, offset_days, subject, purpose, question (EN — SPEC-V2 D9: "each mail is a
# question in disguise"), question_ja, cta
SEQUENCE = [
    (1,  0, "ドバイは本当に高いのか？",               "先入観を崩す",
     "Do you actually believe Dubai is unaffordable?", "ドバイは本当に高いと思っていますか？",
     "記事を読む"),
    (2,  7, "ドバイで安く生活する方法",               "興味を喚起",
     "Could you picture actually affording to live there?", "実際に住めそうだと思えますか？",
     "生活費チェックリスト"),
    (3, 14, "日本人・ノマド・経営者がドバイを選ぶ理由", "移住ニーズを特定",
     "Does relocating actually apply to someone like you?", "移住は自分にも関係があると思いますか？",
     "無料相談"),
    (4, 21, "ドバイ不動産を見る前に知るべきこと",       "投資教育",
     "Are you ready to look at a real property, not just the idea?",
     "アイデアではなく実際の物件を見る準備はできていますか？", "物件相談"),
    (5, 28, "ロンボク開発の将来性",                   "視察ニーズを特定",
     "Is Lombok worth a trip to see in person?", "ロンボクは実際に見に行く価値がありますか？",
     "視察相談"),
    (6, 35, "海外不動産で失敗する人の共通点",          "不安を解消",
     "What's actually stopping you from moving forward?", "何があなたを踏みとどまらせていますか？",
     "30分相談"),
    (7, 42, "相談事例・購入パターン",                 "商談化",
     "Are you ready to talk to a real person?", "実際に話す準備はできていますか？",
     "カレンダー予約"),
]

# name, company, channel, email, phone, purpose, region, stage
PEOPLE = [
    ("森田 賢二",   "青山商事株式会社",     "business_card", "k.morita@aoyama-s.example", "03-1234-1111", "business_base", "dubai",  "engaged"),
    ("西田 綾香",   "個人投資家",           "csv",           "a.nishida@example.jp",      None,           "second_home",   "lombok", "engaged"),
    ("久保 大地",   "フリーランス",         "sns",           "kubo@example.jp",           None,           "relocation",    "dubai",  "meeting_booked"),
    ("長谷川 里奈", "株式会社ベレーザ",     "lp_form",       "r.hasegawa@bereza.example", None,           "relocation",    "dubai",  "engaged"),
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
    staff_id_by_name = {r["name"]: r["id"] for r in con.execute("SELECT id,name FROM staff")}

    # ── app_user — one login per role, bridged to the matching staff row
    # where one exists (auth.ensure_staff_bridge() creates one on demand for
    # the rest, e.g. the first time Balraj grants a manual +30). ────────────
    user_id_by_name = {}
    for email, name, role, office, staff_name in USERS:
        uid = str(uuid.uuid4())
        is_active = 0 if email == "pending@exceed-re.ae" else 1
        con.execute(
            """INSERT INTO app_user (id, email, display_name, role, office, is_active, staff_id)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, email, name, role, office, is_active, staff_id_by_name.get(staff_name)))
        user_id_by_name[name] = uid
    con.commit()

    con.execute("INSERT INTO sequences (id,name) VALUES (1,'ドバイ・ロンボク再活性')")
    for no, off, subj, purpose, qen, qja, cta in SEQUENCE:
        con.execute("""INSERT INTO sequence_steps
                       (sequence_id, step_no, offset_days, subject, purpose, question, question_ja, cta)
                       VALUES (1,?,?,?,?,?,?,?)""", (no, off, subj, purpose, qen, qja, cta))
    con.commit()
    step_id_by_no = {r["step_no"]: r["id"] for r in
                     con.execute("SELECT id, step_no FROM sequence_steps WHERE sequence_id=1")}

    # staff_id -> app_user.id, for the people who bridge to both (D14 ownership
    # needs to be set on both owner_id, the legacy accountability column, and
    # owner_user_id, the RBAC-authoritative one — see schema.sql).
    staff_id_to_user_id = {staff_id_by_name[sn]: user_id_by_name[n]
                           for (_, n, _, _, sn) in USERS if sn and sn in staff_id_by_name}

    owners = {1: 1, 2: 4, 3: 2, 4: 2}          # rough demo assignment, PEOPLE index -> staff id
    ids = {}
    # SPEC-V2 §2 — the sequence-engagement simulation below (sends/opens/
    # clicks) replays each lead's real arrival date, so it needs to remember
    # both.
    lead_seen_at = {}
    lead_email = {}
    # sns_patterns doesn't exist yet at this point in the script (seeded
    # further down, alongside the rest of §3) — collect the associations now,
    # insert them once the FK target is there.
    sns_pattern_assignments = []
    for i, (name, company, channel, email, phone, purpose, region, stage) in enumerate(PEOPLE, 1):
        seen = d(6, 20) if channel == "business_card" else d(6, 1)
        res = ingest.upsert_lead(con, name=name, company=company, channel=channel,
                                 email=email, phone=phone,
                                 note="6/20 展示会・ユパンが交換" if channel == "business_card" else None,
                                 seen_at=seen,
                                 owner_id=owners.get(i))
        lid = res["lead_id"]
        ids[name] = lid
        lead_seen_at[lid] = seen
        lead_email[lid] = email
        con.execute("UPDATE leads SET purpose=?, stage=?, owner_user_id=? WHERE id=?",
                    (purpose, stage, staff_id_to_user_id.get(owners.get(i)), lid))
        con.execute("INSERT INTO lead_regions (lead_id, region, inferred_from) VALUES (?,?,?)",
                    (lid, region, "seed"))
        if channel == "sns":
            sns_pattern_assignments.append((lid, ["A", "B", "C", "D"][i % 4]))
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

    # ── ~52 more leads, spread across every stage/owner/purpose/region so
    # every screen (Leads list, Pipeline, Dashboard, Team) has something to
    # show for every one of the four roles, not just the ten demo people. ───
    SURNAMES = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤",
                "吉田", "山田", "佐々木", "山口", "松本", "井上", "木村", "林", "斎藤", "清水"]
    GIVEN = ["太郎", "健一", "真一", "陽子", "恵子", "直樹", "裕子", "智子", "一郎", "洋平",
             "美穂", "舞", "翔太", "大輔", "優子", "誠", "亮", "綾", "拓也", "彩"]
    COMPANIES = ["株式会社アルファ商事", "青空不動産", "フリーランス", "個人投資家", None,
                 "グローバルパートナーズ", "サンライズホールディングス", "ミライ設計",
                 "エバーグリーン株式会社", "個人", "ネクスト商会"]
    CHANNELS = ["csv", "sns", "lp_form", "gohighlevel", "business_card", "referral",
                "whatsapp", "line", "showroom", "property_finder", "gmail"]
    # SPEC.md D10 migration: six forward stages, and a SEPARATE exit_state —
    # a lead can be mid-funnel (stage) AND have exited (exit_state) at once.
    # 10 pairs, same length as the old single-column STAGES list, so the
    # existing i % len(...) cycling still gives the same spread.
    STAGE_EXITS = [
        ("new", None), ("nurturing", None), ("engaged", None),
        ("meeting_booked", None), ("in_negotiation", None), ("won", None),
        ("nurturing", "lost"), ("new", "too_early"),
        ("nurturing", "unreachable"), ("nurturing", "unsubscribed"),
    ]
    PURPOSES = ["investment", "relocation", "second_home", "business_base", "unknown"]
    REGIONS = ["dubai", "lombok", "japan"]
    # PEOPLE index cycle -> staff name. None = unassigned: before a booking,
    # D14 says nobody owns anyone, and that state has to exist in the demo
    # data too, not just the four people who already have an owner.
    OWNER_CYCLE = ["ユパン", "チアキ", "ドバイデスク", "ロンボクデスク", None, None, "藤原 浩平"]

    extra_ids = []
    for i in range(52):
        name = "%s %s" % (SURNAMES[i % len(SURNAMES)], GIVEN[(i * 3) % len(GIVEN)])
        company = COMPANIES[i % len(COMPANIES)]
        channel = CHANNELS[i % len(CHANNELS)]
        seen = d(6, 1) + timedelta(days=(i % 70))
        email = "lead%03d@example.jp" % i
        res = ingest.upsert_lead(con, name=name, company=company, channel=channel,
                                 email=email, seen_at=seen)
        lid = res["lead_id"]
        extra_ids.append(lid)
        lead_seen_at[lid] = seen
        lead_email[lid] = email
        if channel == "sns":
            sns_pattern_assignments.append((lid, ["A", "B", "C", "D"][i % 4]))

        owner_staff_name = OWNER_CYCLE[i % len(OWNER_CYCLE)]
        owner_staff_id = staff_id_by_name.get(owner_staff_name)
        stage, exit_state = STAGE_EXITS[i % len(STAGE_EXITS)]
        con.execute(
            """UPDATE leads SET purpose=?, stage=?, exit_state=?, owner_id=?, owner_user_id=?
               WHERE id=?""",
            (PURPOSES[i % len(PURPOSES)], stage, exit_state, owner_staff_id,
             staff_id_to_user_id.get(owner_staff_id), lid))
        con.execute(
            "INSERT OR IGNORE INTO lead_regions (lead_id, region, inferred_from) VALUES (?,?,?)",
            (lid, REGIONS[i % len(REGIONS)], "seed"))

        # vary the events so score/heat/escalation actually differ lead to lead
        for kind, off in random.sample(
                [("open", 1), ("click", 5), ("page_view", 10),
                 ("booking_page_view", 20), ("reply", 25)], k=random.randint(0, 3)):
            scoring.record(con, lid, kind, occurred_at=seen + timedelta(days=off))

        # a quarter of them get a task, spread from overdue to upcoming —
        # tasks at varying urgency (D12b), for a Today screen that isn't empty
        if owner_staff_id and i % 4 == 0:
            due = NOW + timedelta(days=random.choice([-6, -3, -1, 0, 1, 2, 5]))
            tasks.create(con, lid, random.choice(
                            ["call", "email", "follow_up", "send_material", "prepare"]),
                        reason=scoring.explain(con, lid)["summary"],
                        owner_id=owner_staff_id, due_at=due, created_by="ai")
    con.commit()

    # two of them owned by marketing directly — the edge case the matrix
    # actually allows ("edit a lead they own" ✅ for marketing) and the one
    # that proves redaction is per-lead, not per-role: marketing sees contact
    # details on these two and nowhere else.
    con.execute("UPDATE leads SET owner_user_id=? WHERE id IN (?,?)",
               (user_id_by_name["SNS Marketing"], extra_ids[3], extra_ids[13]))
    con.commit()

    # ═══════════════════════════════════════════════════════════════════════
    # SPEC-V2 — every new screen needs real seed data too.
    # ═══════════════════════════════════════════════════════════════════════

    # ── §2 nurture: replay each lead's real arrival date against the 7 steps,
    # so GET /api/sequences/{id}/stats has real send/open/click numbers. ─────
    for lid, seen in lead_seen_at.items():
        email = lead_email.get(lid)
        if not email:
            continue
        for no, off, subj, purpose, qen, qja, cta in SEQUENCE:
            step_sent_at = seen + timedelta(days=off)
            if step_sent_at > NOW:
                break     # this step has not "gone out" yet, relative to today
            send_id = "seq1-%d-s%d" % (lid, no)
            con.execute(
                """INSERT OR IGNORE INTO sends (send_id, lead_id, step_id, to_email, sent_at, status)
                   VALUES (?,?,?,?,?, 'sent')""",
                (send_id, lid, step_id_by_no[no], email, step_sent_at.isoformat(sep=" ")))
            if random.random() < 0.55:
                scoring.record(con, lid, "open", send_id=send_id, source="sendgrid",
                               occurred_at=step_sent_at + timedelta(hours=random.randint(1, 48)))
                if random.random() < 0.35:
                    scoring.record(con, lid, "click", send_id=send_id, source="sendgrid",
                                   occurred_at=step_sent_at + timedelta(hours=random.randint(2, 72)))
    con.commit()

    # ── §3 SNS: the four content patterns, verbatim, plus the funnel's two
    # manually-recorded steps (no analytics API is wired to any platform). ───
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
    for key, title_ja, title_en, desc_ja, desc_en in SNS_PATTERNS:
        con.execute("""INSERT INTO sns_patterns (key, title_ja, title_en, description_ja, description_en)
                       VALUES (?,?,?,?,?)""", (key, title_ja, title_en, desc_ja, desc_en))
    con.execute("""INSERT INTO sns_funnel_manual (step_key, count, note) VALUES
        ('sns_post', 148, 'manually counted across Instagram/X/note.com — no analytics API connected'),
        ('lp_view', 612, 'from each platform''s native post insights, recorded by hand weekly')""")
    # the pattern -> lead associations collected while creating leads above,
    # deferred until sns_patterns (the FK target) actually exists.
    for lid, pattern_key in sns_pattern_assignments:
        con.execute("INSERT OR IGNORE INTO lead_sns_pattern (lead_id, pattern_key) VALUES (?,?)",
                   (lid, pattern_key))
    con.commit()

    # ── §4 booking: settings, meeting types, which reps take which type. ─────
    con.execute("""INSERT INTO booking_settings (id, jst_gst_gap_hours, business_hours_start,
                                                  business_hours_end, slot_length_minutes, timezone, note)
                   VALUES (1, 5, '09:00', '18:00', 30, 'Asia/Tokyo',
                           'JST is 5 hours ahead of GST (Dubai) — 09:00 JST is 04:00 GST. FABLE-AUDIT.md: the decision log said 4 hours, twice; verified wrong.')""")
    con.execute("""INSERT INTO booking_meeting_types (key, label_en, label_ja, duration_minutes) VALUES
        ('consult_30', 'Free 30-min consultation', '無料30分相談', 30),
        ('site_inspection', 'Site inspection', '視察', 120),
        ('showroom_visit', 'Showroom visit', '来店', 60)""")
    for rep_name, mt in [("Chiaki", "consult_30"), ("Dubai Desk", "consult_30"),
                         ("Dubai Desk", "showroom_visit"), ("Lombok Desk", "site_inspection"),
                         ("Yupan", "showroom_visit"), ("Yupan", "consult_30")]:
        uid = user_id_by_name.get(rep_name)
        if uid:
            con.execute("INSERT OR IGNORE INTO booking_rep_meeting_types (user_id, meeting_type) VALUES (?,?)",
                       (uid, mt))
    con.commit()

    # ── §5 assign: priority-ordered rules with an explicit fallback, plus a
    # week of calendar events so the rep calendar view isn't empty. ──────────
    ASSIGNMENT_RULES = [
        (1, "region", "dubai", "Dubai Desk"),
        (2, "region", "lombok", "Lombok Desk"),
        (3, "purpose", "business_base", "Fujiwara"),
        (4, "purpose", "relocation", "Chiaki"),
    ]
    for priority, field, value, rep_name in ASSIGNMENT_RULES:
        con.execute("""INSERT INTO assignment_rules (priority, match_field, match_value, owner_user_id,
                                                      is_fallback) VALUES (?,?,?,?,0)""",
                   (priority, field, value, user_id_by_name[rep_name]))
    con.execute("""INSERT INTO assignment_rules (priority, match_field, match_value, owner_user_id,
                                                  is_fallback) VALUES (99, NULL, NULL, ?, 1)""",
               (user_id_by_name["Yupan"],))
    con.commit()

    MEETING_TITLES = {"consult_30": "無料30分相談 / Consultation",
                      "site_inspection": "視察 / Site inspection",
                      "showroom_visit": "来店対応 / Showroom visit"}
    week_anchor = (NOW - timedelta(days=NOW.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    CALENDAR_SEED = [
        ("Chiaki",       ids["長谷川 里奈"], "consult_30",       1, 10, 30),
        ("Dubai Desk",   ids["西田 綾香"],   "consult_30",       2, 14, 0),
        ("Lombok Desk",  ids["田村 由紀"],   "site_inspection",  3, 9,  0),
        ("Yupan",        ids["小野 博"],     "showroom_visit",   4, 15, 0),
    ]
    for rep_name, lead_id, mtype, day_offset, hour, minute in CALENDAR_SEED:
        uid = user_id_by_name[rep_name]
        starts = week_anchor + timedelta(days=day_offset, hours=hour, minutes=minute)
        duration = {"consult_30": 30, "site_inspection": 120, "showroom_visit": 60}[mtype]
        ends = starts + timedelta(minutes=duration)
        con.execute(
            """INSERT INTO calendar_events (owner_user_id, lead_id, type, title, starts_at, ends_at,
                                            created_by_user_id) VALUES (?,?,?,?,?,?,?)""",
            (uid, lead_id, mtype, MEETING_TITLES[mtype],
             starts.isoformat(sep=" ", timespec="minutes"), ends.isoformat(sep=" ", timespec="minutes"),
             user_id_by_name["Balraj"]))
    con.commit()

    # ── §6 card scan: one lead created through the scan pathway, with a real
    # (placeholder) image on disk and explicit, timestamped consent. ─────────
    scan_res = ingest.upsert_lead(con, name="高橋 誠一", channel="business_card",
                                  company="タカハシ商事", title="代表取締役",
                                  email="s.takahashi@example.jp", phone="090-1234-5678",
                                  note="8/2 展示会でスキャン", seen_at=d(8, 2))
    scan_lead_id = scan_res["lead_id"]
    card_image_path = media.save("business_cards", "takahashi_card.jpg",
                                 b"\xff\xd8\xff\xe0JFIF-placeholder-not-a-real-jpeg")
    con.execute("UPDATE leads SET card_image_path=?, owner_user_id=?, owner_id=? WHERE id=?",
               (card_image_path, user_id_by_name["Yupan"], staff_id_by_name.get("ユパン"), scan_lead_id))
    ingest.set_consent_explicit(con, scan_lead_id, via="business card scan — iPad consent checkbox",
                                when=d(8, 2))
    con.commit()

    # ── §8 integrations: every one honestly "not connected". ─────────────────
    INTEGRATIONS = [
        ("gohighlevel", "GoHighLevel", "GoHighLevel", 0, "No API key configured.",
         "A GoHighLevel API key, plus an audit of existing records and a conflict rule (D11).", 1),
        ("hubspot", "HubSpot", "HubSpot", 0, "Not connected — no account linked.",
         "A HubSpot private app token, if this becomes the CRM of record.", 0),
        ("gmail", "Gmail", "Gmail", 0, "Not connected — no OAuth grant.",
         "A Google Workspace OAuth grant for the shared inbox.", 0),
        ("google_calendar", "Google Calendar", "Google Calendar", 0, "Not connected.",
         "A Google Calendar OAuth grant per rep, plus a two-way sync rule.", 0),
        ("whatsapp", "WhatsApp", "WhatsApp", 0, "Not connected — no Business API access.",
         "WhatsApp Business API access via Meta.", 0),
        ("google_drive", "Google Drive", "Google Drive", 0, "Not connected.",
         "A Google Drive OAuth grant for shared document storage.", 0),
    ]
    for key, en, ja, connected, status_detail, needed, read_only in INTEGRATIONS:
        con.execute("""INSERT INTO integrations (key, label_en, label_ja, connected, status_detail,
                                                  what_is_needed, read_only)
                       VALUES (?,?,?,?,?,?,?)""", (key, en, ja, connected, status_detail, needed, read_only))
    con.commit()

    # ── §9 AI reply: one still awaiting a response, one already approved and
    # sent by a human (scored exactly the way POST /api/replies/{id}/send
    # scores it — source='human', never 'ai', since no model is wired). ──────
    con.execute("""INSERT INTO replies (lead_id, channel, subject, body, received_at, status)
                   VALUES (?,?,?,?,?, 'received')""",
               (ids["石川 颯太"], "email", "Re: ドバイ不動産を見る前に知るべきこと",
                "資料ありがとうございます。来月ドバイに行くので、その時に相談できますか？",
                d(8, 14, 10).isoformat(sep=" ")))
    r2_cur = con.execute(
        """INSERT INTO replies (lead_id, channel, subject, body, received_at, status,
                                verdict_json, human_reply_body, sent_at, sent_by_user_id)
           VALUES (?,?,?,?,?, 'sent', ?, ?, ?, ?)""",
        (ids["藤井 直子"], "email", "Re: 相談事例・購入パターン",
         "興味があります。一度お話を伺いたいです。", d(8, 5, 9).isoformat(sep=" "),
         json.dumps({"human": True, "wants_meeting": True, "partnership": False, "high_budget": False}),
         "ありがとうございます。来週の火曜10時はいかがでしょうか？", d(8, 5, 11).isoformat(sep=" "),
         user_id_by_name["Chiaki"]))
    r2_id = r2_cur.lastrowid
    outcome = tracking.record_human_approved_reply(
        con, ids["藤井 直子"], subject="Re: 相談事例・購入パターン",
        verdict={"human": True, "wants_meeting": True, "partnership": False, "high_budget": False},
        set_by=staff_id_by_name.get("チアキ"))
    con.execute("UPDATE replies SET scored_event_ids_json=? WHERE id=?",
               (json.dumps(outcome["event_ids"]), r2_id))
    con.commit()

    # ── §10 voice notes: real rows + real files on disk (not real audio —
    # transcription is not wired, so nothing here pretends otherwise). ───────
    placeholder_audio = b"RIFF\x00\x00\x00\x00WAVEfmt seed-placeholder-not-real-audio"
    note1 = media.save("voice_notes", "morita_note.m4a", placeholder_audio)
    con.execute("""INSERT INTO lead_notes (lead_id, author_user_id, audio_path, duration_seconds)
                   VALUES (?,?,?,?)""", (morita, user_id_by_name["Yupan"], note1, 42.5))
    note2 = media.save("voice_notes", "nishida_note.m4a", placeholder_audio)
    con.execute("""INSERT INTO lead_notes (lead_id, author_user_id, audio_path, duration_seconds)
                   VALUES (?,?,?,?)""", (ids["西田 綾香"], user_id_by_name["Dubai Desk"], note2, 18.0))
    con.commit()

    # ── §10 push: a couple of registered devices + their settings. ───────────
    con.execute("INSERT INTO push_tokens (user_id, token, platform) VALUES (?,?,?)",
               (user_id_by_name["Yupan"], "ExponentPushToken[seed-yupan-demo-token]", "ios"))
    con.execute("INSERT INTO push_tokens (user_id, token, platform) VALUES (?,?,?)",
               (user_id_by_name["Chiaki"], "ExponentPushToken[seed-chiaki-demo-token]", "ipados"))
    for name in ("Yupan", "Chiaki"):
        con.execute(
            """INSERT INTO push_settings (user_id, notify_new_lead, notify_hot_lead, notify_reply,
                                          notify_task_escalation_level) VALUES (?,1,1,1,2)""",
            (user_id_by_name[name],))
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
    for c in ex["breakdown"]:
        print("      %-18s %-11s base %+3d  ×%.2f  = %+5.1f   %s" % (
            c["kind"], "(%s)" % c["group"], c["base_points"], c["decay_factor"],
            c["points"], c["occurred_at"][:10]))
    print("\n      → %s" % ex["summary"])
    print("\n  Note: June's points are worn down because June was ~%d days ago." %
          (NOW - d(6, 23)).days)
    print("  That is D8 working. The demo's 92 never moves; this number does.")

    print("\n  app_user logins (%d) — one per role, plus one deliberately-inactive "
          "fail-closed example:" % db.scalar(con, "SELECT count(*) FROM app_user"))
    for r in con.execute("SELECT email, role, is_active FROM app_user ORDER BY role, email"):
        print("      %-26s %-16s %s" % (r["email"], r["role"],
              "active" if r["is_active"] else "INACTIVE — needs PATCH /api/users/{id}"))

    print("\n  To try the API as any of them (needs `EXCEEDBOX_DEV_AUTH=1` and NO "
          "SUPABASE_JWT_SECRET set on the server):")
    print("      curl -s 'localhost:8912/api/devtoken?email=balraj@exceed-re.ae' "
          "| python3 -c \"import sys,json;print(json.load(sys.stdin)['token'])\"")
    print("      curl -s localhost:8912/api/me -H \"Authorization: Bearer <token>\"")
    con.close()


if __name__ == "__main__":
    main()
