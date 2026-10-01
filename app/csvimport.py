"""SPEC-V2 §7 — the CSV/Excel importer.

D11 ⭐: "the importer must interrogate the file on upload" — two phases:

  analyze()  parse headers + a dedupe preview, stash the file, ask questions
  commit()   apply a confirmed mapping + explicit answers, write real rows

Dedupe runs ON IMPORT (D13), through the same ingest.upsert_lead() every
other channel uses — a CSV/Excel row can never create a duplicate lead that a
business card or an LP form would have merged.

Consent is the largest legal exposure named in SPEC-V2: "the 25,000 rows with
consent = unknown are the largest legal exposure in the project." commit()
requires an explicit consent_basis answer and defaults to 'unknown' rather
than guessing upward — ingest.py's CONSENT_BY_CHANNEL already treats CSV as
'unknown' by default; this module only ever raises that basis when the human
running the import explicitly says so, via `answers.consent_basis`.

.xlsx support (SPEC-V2 §7 "CSV/Excel 取込"): parsed server-side with
openpyxl rather than in the app, so the wizard flow the app already has
(pick file -> analyze -> map -> preview -> consent -> commit) never has to
know which format it is looking at. A workbook with more than one sheet is
interrogated for which one BEFORE any row is parsed (D11) — see
parse_xlsx_bytes() and analyze()'s early return below.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import uuid
from typing import Optional

import openpyxl

from . import ingest, media

CONSENT_BASES = ("explicit", "implied", "ambiguous", "unknown")
XLSX_EXTENSIONS = (".xlsx", ".xlsm")


def _is_xlsx_filename(filename: str) -> bool:
    return (filename or "").lower().endswith(XLSX_EXTENSIONS)


def parse_csv_bytes(data: bytes) -> tuple[list[str], list[dict]]:
    """Returns (headers, rows) — rows as header->value dicts, in file order.
    Tries utf-8-sig first (Excel's favourite BOM), falls back to shift_jis
    (still common for older Japanese CRM exports) rather than raising."""
    text = None
    for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("could not decode file as utf-8, shift_jis or cp932")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    rows = [dict(r) for r in reader]
    return headers, rows


def parse_xlsx_bytes(data: bytes, sheet_name: Optional[str] = None) -> tuple[list[str], Optional[str], Optional[list[str]], Optional[list[dict]]]:
    """Returns (sheet_names, chosen_sheet_name, headers, rows).

    When the workbook has more than one sheet and `sheet_name` was not
    supplied, `chosen_sheet_name`/`headers`/`rows` all come back `None` —
    the caller (analyze()) must ask which sheet before a single row is
    parsed, exactly the "interrogate the file" principle CSV already
    follows for ambiguous columns (D11).

    Any file that is not a genuine, readable .xlsx workbook (corrupt upload,
    a renamed non-Excel file, a password-protected file) raises ValueError —
    never an openpyxl-internal exception type the caller wouldn't know how
    to turn into a structured 422.
    """
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise ValueError("could not read this file as an Excel (.xlsx) workbook: %s" % e)

    try:
        sheet_names = list(wb.sheetnames)
        if not sheet_names:
            raise ValueError("this workbook has no sheets")

        if sheet_name is None:
            if len(sheet_names) > 1:
                return sheet_names, None, None, None
            sheet_name = sheet_names[0]
        elif sheet_name not in sheet_names:
            raise ValueError("no such sheet in this workbook: %s" % sheet_name)

        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            raise ValueError("this sheet has no rows")

        headers = [(str(h).strip() if h is not None else "") for h in header_row]
        if not any(headers):
            raise ValueError("no header row could be parsed from this sheet")

        rows: list[dict] = []
        for r in rows_iter:
            if r is None or all(v is None for v in r):
                continue  # a genuinely blank row — not a row with blank cells
            row = {}
            for i, h in enumerate(headers):
                if not h:
                    continue  # unnamed column — nothing to map it to
                v = r[i] if i < len(r) else None
                row[h] = "" if v is None else str(v)
            rows.append(row)
        return sheet_names, sheet_name, headers, rows
    finally:
        wb.close()


def analyze(con: sqlite3.Connection, *, filename: str, data: bytes,
           imported_by_user_id: str, preview_limit: int = 500,
           sheet_name: Optional[str] = None) -> dict:
    """Phase 1: parse, stash the file, run a dedupe preview on the first
    `preview_limit` rows (a 25,000-row file is not re-matched twice — commit()
    re-derives the real counts as it writes), and ask the standard questions.
    Returns the token commit() needs.

    .xlsx workbooks with more than one sheet are interrogated FIRST (D11) —
    this returns `{"needs_sheet_selection": True, "sheets": [...]}` without
    stashing anything or touching a single row, and the caller re-calls with
    the chosen `sheet_name` once the human has answered."""
    is_xlsx = _is_xlsx_filename(filename)
    if is_xlsx:
        sheet_names, chosen_sheet, headers, rows = parse_xlsx_bytes(data, sheet_name=sheet_name)
        if chosen_sheet is None:
            return {"needs_sheet_selection": True, "sheets": sheet_names, "filename": filename or "import.xlsx"}
        sheet_name = chosen_sheet
    else:
        headers, rows = parse_csv_bytes(data)
    if not headers:
        raise ValueError("no header row could be parsed from this file")

    mapping = ingest.guess_mapping(headers)
    questions = ingest.import_questions(headers, rows[:5])

    email_col = next((h for h, f in mapping.items() if f == "email"), None)
    phone_col = next((h for h, f in mapping.items() if f == "phone"), None)
    name_col = next((h for h, f in mapping.items() if f == "name"), None)
    company_col = next((h for h, f in mapping.items() if f == "company"), None)

    new_count = dup_count = 0
    for row in rows[:preview_limit]:
        lead_id, _ = ingest.find_existing(
            con,
            email=row.get(email_col) if email_col else None,
            phone=row.get(phone_col) if phone_col else None,
            name=row.get(name_col) if name_col else None,
            company=row.get(company_col) if company_col else None,
        )
        if lead_id:
            dup_count += 1
        else:
            new_count += 1

    token = uuid.uuid4().hex
    rel_path = media.save("imports", filename or ("import.xlsx" if is_xlsx else "import.csv"), data)

    con.execute(
        """INSERT INTO imports (filename, rows_seen, token, file_path, status,
                                headers_json, imported_by_user_id, sheet_name)
           VALUES (?,?,?,?,'analyzed',?,?,?)""",
        (filename or ("import.xlsx" if is_xlsx else "import.csv"), len(rows), token, rel_path,
         json.dumps(headers), imported_by_user_id, sheet_name if is_xlsx else None))
    con.commit()

    return {
        "token": token,
        "columns": headers,
        "guessed_mapping": mapping,
        "questions": questions,
        "sample_rows": rows[:10],
        "rows_seen": len(rows),
        "preview_new": new_count,
        "preview_duplicate": dup_count,
        "preview_capped": len(rows) > preview_limit,
        "preview_rows_checked": min(len(rows), preview_limit),
    }


def commit(con: sqlite3.Connection, *, token: str, mapping: dict, answers: dict,
          committed_by_user_id: str, owner_user_id: str = None) -> dict:
    """Phase 2: re-read the stashed file, apply the CONFIRMED mapping (never
    the guess silently), and write every row through the same dedupe path
    every other channel uses."""
    row = con.execute("SELECT * FROM imports WHERE token=?", (token,)).fetchone()
    if not row:
        raise KeyError("no such import token — was analyze() called first?")
    if row["status"] == "committed":
        raise ValueError("this import was already committed")

    name_col = next((h for h, f in mapping.items() if f == "name"), None)
    if not name_col:
        raise ValueError("mapping must map at least one column to 'name'")

    consent_basis = answers.get("consent_basis", "unknown")
    if consent_basis not in CONSENT_BASES:
        consent_basis = "unknown"

    data = media.read(row["file_path"])
    if _is_xlsx_filename(row["filename"]):
        # Re-reads the SAME sheet analyze() resolved to and stashed on this
        # row — never re-asks (D11 only interrogates once per import).
        _, _, _, rows = parse_xlsx_bytes(data, sheet_name=row["sheet_name"])
    else:
        _, rows = parse_csv_bytes(data)

    company_col = next((h for h, f in mapping.items() if f == "company"), None)
    title_col = next((h for h, f in mapping.items() if f == "title"), None)
    email_col = next((h for h, f in mapping.items() if f == "email"), None)
    phone_col = next((h for h, f in mapping.items() if f == "phone"), None)

    created = merged = skipped = 0
    lead_ids = []
    for r in rows:
        name = (r.get(name_col) or "").strip() if name_col else ""
        if not name:
            skipped += 1
            continue
        res = ingest.upsert_lead(
            con, name=name, channel="csv",
            company=(r.get(company_col) or None) if company_col else None,
            title=(r.get(title_col) or None) if title_col else None,
            email=(r.get(email_col) or None) if email_col else None,
            phone=(r.get(phone_col) or None) if phone_col else None,
            note="imported via %s (%s)" % ("Excel" if _is_xlsx_filename(row["filename"]) else "CSV", row["filename"]),
            owner_id=None,
        )
        lead_ids.append(res["lead_id"])
        if res["created"]:
            created += 1
        else:
            merged += 1
        if owner_user_id:
            con.execute("UPDATE leads SET owner_user_id=? WHERE id=? AND owner_user_id IS NULL",
                       (owner_user_id, res["lead_id"]))
        # SPEC-V2 §7: consent is set EXPLICITLY per import, never guessed
        # upward past what the human running the import actually answered.
        if consent_basis != "unknown":
            ingest._set_consent_if_stronger(con, res["lead_id"], "csv")
            con.execute(
                """UPDATE lead_consent SET basis=?, obtained_via=?, updated_at=datetime('now')
                   WHERE lead_id=? AND basis != 'withdrawn'""",
                (consent_basis, answers.get("consent_note", "declared at CSV import"),
                 res["lead_id"]))

    import json
    con.execute(
        """UPDATE imports SET rows_created=?, rows_merged=?, rows_skipped=?,
                              status='committed', mapping_json=?, answers_json=?,
                              committed_at=datetime('now')
            WHERE token=?""",
        (created, merged, skipped, json.dumps(mapping), json.dumps(answers), token))
    con.commit()

    return {
        "import_id": row["id"], "token": token, "filename": row["filename"],
        "rows_seen": row["rows_seen"], "rows_created": created, "rows_merged": merged,
        "rows_skipped": skipped, "consent_basis_applied": consent_basis,
        "lead_ids": lead_ids,
    }
