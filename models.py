"""
models.py — Database schema & helpers (SQLite via aiosqlite)
"""
import aiosqlite, os, bcrypt
from pathlib import Path

DB_PATH = Path(__file__).parent / "fundraising.db"

CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS donations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    row_hash      TEXT    UNIQUE,          -- sha1(tanggal+nominal+hp+cs) — upsert guard
    tanggal       TEXT    NOT NULL,        -- ISO date YYYY-MM-DD
    donor_name    TEXT,
    donor_phone   TEXT,                    -- normalized: 08xx...
    ig_username   TEXT,
    nominal       INTEGER NOT NULL,
    kode_program  TEXT,
    asal_donasi   TEXT,
    cs            TEXT,
    platform      TEXT,
    bulan         TEXT,
    keterangan    TEXT,
    is_institusional INTEGER DEFAULT 0,   -- 1 = dikecualikan dari ranking CS
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_donations_tanggal   ON donations(tanggal);
CREATE INDEX IF NOT EXISTS idx_donations_cs        ON donations(cs);
CREATE INDEX IF NOT EXISTS idx_donations_program   ON donations(kode_program);
CREATE INDEX IF NOT EXISTS idx_donations_phone     ON donations(donor_phone);
CREATE INDEX IF NOT EXISTS idx_donations_inst      ON donations(is_institusional);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT UNIQUE NOT NULL,
    pw_hash    TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'staff',  -- 'admin' | 'staff'
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS faq_submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question_raw  TEXT    NOT NULL,
    topik         TEXT,                    -- gabungan topik yang dicentang, dipisah koma
    saran         TEXT,
    status        TEXT    NOT NULL DEFAULT 'pending',
    submitted_at  TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS faq_entries (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    status                TEXT    NOT NULL,
    category              TEXT,
    question_public       TEXT    NOT NULL,
    answer                TEXT,
    rencana_pembahasan    TEXT,
    ranah_divisi          TEXT,
    jalur_disarankan      TEXT,
    source_submission_id  INTEGER REFERENCES faq_submissions(id),
    updated_by            INTEGER REFERENCES users(id),
    updated_at            TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at   TEXT DEFAULT (datetime('now')),
    rows_fetched INTEGER,
    rows_upserted INTEGER,
    status      TEXT,   -- 'ok' | 'error'
    message     TEXT
);

CREATE TABLE IF NOT EXISTS sheet_sources (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    label          TEXT NOT NULL,          -- Nama tampilan, mis. "Rekap 2024"
    spreadsheet_id TEXT NOT NULL,
    sheet_name     TEXT NOT NULL DEFAULT 'rekap seluruh cs',
    is_active      INTEGER DEFAULT 1,      -- 1=aktif, 0=nonaktif
    last_synced_at TEXT,
    last_row_count INTEGER DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS institutional_exclusion (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    donor_name  TEXT NOT NULL,
    tanggal     TEXT NOT NULL,
    nominal     INTEGER NOT NULL,
    note        TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Seed donasi institusional awal
INSERT OR IGNORE INTO institutional_exclusion (id, donor_name, tanggal, nominal, note) VALUES
    (1, 'PT Pegadaian',      '2026-01-05', 231956329, 'Donasi kemitraan institusi'),
    (2, 'Prozis Ibnu Abbas', '2026-02-10', 389076480, 'Donasi kemitraan institusi');
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        # Seed default sheet source dari .env jika belum ada
        from dotenv import load_dotenv
        load_dotenv()
        import os
        sid  = os.getenv('SPREADSHEET_ID', '')
        sname = os.getenv('SHEET_NAME', 'rekap seluruh cs')
        if sid:
            await db.execute("""
                INSERT OR IGNORE INTO sheet_sources (id, label, spreadsheet_id, sheet_name, is_active)
                VALUES (1, 'Rekap Utama 2026', ?, ?, 1)
            """, (sid, sname))
        await db.commit()

async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, pw_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), pw_hash.encode())

async def create_user(username: str, password: str, role: str = "staff"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (username, pw_hash, role) VALUES (?, ?, ?)",
            (username, hash_password(password), role)
        )
        await db.commit()

async def get_user(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ) as cur:
            return await cur.fetchone()
