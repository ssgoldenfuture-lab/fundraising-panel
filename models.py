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
    catatan_konteks       TEXT,
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

-- ── Database Donatur (Fase 1a) ───────────────────────────────────────────
-- Gabungan DB MASTER + DB per-CS (Google Sheets) jadi satu tabel.
-- no_hp adalah SATU-SATUNYA identifier/patokan (bukan kombinasi nama+no_hp) —
-- sesuai cara kerja manual yang sudah berjalan.
CREATE TABLE IF NOT EXISTS db_donatur (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    no_hp         TEXT    UNIQUE NOT NULL,   -- identifier utama, exact-match (belum dinormalisasi 62/8/0)
    panggilan     TEXT,                      -- Kak/Pak/Bu dst — dipakai merge-tag broadcast
    nama_donatur  TEXT,                      -- bebas isinya (kadang bukan nama asli, kadang nomor lain)
    no_hp_cs      TEXT,                      -- format gabungan "NamaCS urutan no_hp_cs", mis. "Rani 1 628112380705"
    nama_label    TEXT,                      -- label segmentasi, bisa gabungan dipisah "~"
    divisi        TEXT,                      -- asal-usul db (OWN/SS/Paid Traffic dst), bukan divisi struktural
    catatan_cs    TEXT,                      -- satu-satunya kolom yang nanti boleh diedit CS (read-only lainnya)
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_db_donatur_no_hp_cs ON db_donatur(no_hp_cs);
CREATE INDEX IF NOT EXISTS idx_db_donatur_divisi   ON db_donatur(divisi);

-- Antrian review untuk db yang bentrok (exact-match no_hp) pas proses input massal.
-- existing_id selalu merujuk ke row yang SUDAH ada di db_donatur — baik itu row lama
-- beneran, maupun row yang baru saja di-insert dari baris LAIN dalam batch paste yang
-- sama (dua kasus ini ditangani dengan cara yang identik, lihat db_donatur_parser.py).
CREATE TABLE IF NOT EXISTS db_duplikat_antrian (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    no_hp           TEXT    NOT NULL,
    existing_id     INTEGER NOT NULL REFERENCES db_donatur(id),
    nama_baru       TEXT,
    no_hp_cs_baru   TEXT,
    divisi_baru     TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending | kept_old | replaced | skipped
    resolved_by     TEXT,                     -- username (session cuma simpan username, bukan id)
    resolved_at     TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_db_dup_status ON db_duplikat_antrian(status);
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
