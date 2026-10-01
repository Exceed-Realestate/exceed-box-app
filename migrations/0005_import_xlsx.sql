-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 0005 — .xlsx import support (SPEC-V2 §7)
--
-- imports.sheet_name records which worksheet a multi-sheet .xlsx upload
-- resolved to (D11: the wizard asks when there is more than one — see
-- app/csvimport.py's parse_xlsx_bytes()). NULL for CSV imports and for
-- single-sheet workbooks, where there is nothing to ask.
-- ═══════════════════════════════════════════════════════════════════════════
ALTER TABLE imports ADD COLUMN IF NOT EXISTS sheet_name TEXT;
