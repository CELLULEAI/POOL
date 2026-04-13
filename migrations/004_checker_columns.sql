-- 004 — Checker ladder : score fiabilite + compteur echecs

ALTER TABLE workers ADD COLUMN IF NOT EXISTS checker_score REAL DEFAULT 1.0;
ALTER TABLE workers ADD COLUMN IF NOT EXISTS checker_fails INTEGER DEFAULT 0;
ALTER TABLE workers ADD COLUMN IF NOT EXISTS checker_total INTEGER DEFAULT 0;
ALTER TABLE workers ADD COLUMN IF NOT EXISTS checker_passed INTEGER DEFAULT 0;
