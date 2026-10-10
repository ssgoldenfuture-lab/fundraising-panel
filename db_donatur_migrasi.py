"""
db_donatur_migrasi.py — migrasi data ASLI dari Google Sheets (DB MASTER 2026)
ke tabel db_donatur. Dijalankan manual, SEKALI PER-CS (migrasi bertahap,
bukan sekaligus semua CS sekaligus) — sesuai keputusan yang sudah diambil.

Beda kebijakan dibanding db_donatur_parser.proses_batch (fitur paste harian):
- No HP CS / Divisi / Nama Label / Panggilan diambil APA ADANYA per-baris dari
  sheet, TIDAK di-override pakai satu nilai untuk seluruh batch — karena satu
  CS bisa punya db di lebih dari satu Divisi (contoh nyata: Rani pegang SSU
  & OWN sekaligus).
- Kalau no_hp SUDAH ADA di db_donatur, baris itu di-SKIP OTOMATIS dan dicatat
  ke file log CSV — TIDAK masuk antrian interaktif seperti fitur paste harian.
  Alasan: volume migrasi bisa ratusan/ribuan baris, antrian interaktif tidak
  masuk akal untuk direview satu-satu di skala ini.
- Idempotent by design: dijalankan ulang untuk CS yang sama itu aman — baris
  yang sudah ke-insert otomatis ke-skip (bukan dobel/error), baris yang
  belum lanjut ke-insert. Ini yang bikin proses ini boleh diinterupsi kapan
  saja (mati listrik, koneksi putus, dll) dan dilanjut tanpa drama.
- DEFAULT dry-run: TIDAK menulis apa-apa ke database sampai eksplisit dikasih
  --execute. Jalankan dry-run dulu, buka file log CSV-nya, baru --execute
  kalau sudah yakin.

Auth: pakai Service Account (SA_KEY_FILE di .env, path ke file JSON), BUKAN
GOOGLE_API_KEY yang dipakai sheets.py — karena data ini sensitif (nama + no HP
donatur asli), sheet-nya tetap PRIVATE, cuma di-share ke email Service Account.

MENJALANKAN DI SERVER — sebagai user yang SAMA dengan service web (mis. www-data),
BUKAN root. Database-nya mode WAL: kalau script jalan sebagai root, file -wal/-shm
bisa terbuat milik root dan aplikasi web lalu gagal menulis ("attempt to write a
readonly database"). Karena itu script MENOLAK jalan sebagai root kecuali diberi
--izinkan-root. Bentuk perintahnya:
    sudo -u <user-service-web> <python-venv-aplikasi> db_donatur_migrasi.py ... --log-dir <folder-privat>
File log CSV berisi nama + no HP ASLI. Taruh di folder privat di luar folder aplikasi
(--log-dir, atau env MIGRASI_LOG_DIR). Dibuat dengan izin 600; hapus setelah dicek.
Sebelum --execute, script membuat backup database (API backup SQLite, aman untuk
database yang sedang dipakai) di folder yang sama, izin 600. Matikan dengan --no-backup.

Cara pakai:
    # 1. Cek dulu apa DB MASTER 2026 bisa dibaca (dry-run, baca doang):
    python db_donatur_migrasi.py --spreadsheet-id XXXX --sheet-name "DB MASTER 2026" --cs Rani

    # 2. Kalau laporan dry-run-nya masuk akal, baru jalankan beneran:
    python db_donatur_migrasi.py --spreadsheet-id XXXX --sheet-name "DB MASTER 2026" --cs Rani --execute
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone

import aiosqlite
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("db_donatur_migrasi")

# SENGAJA BEDA dari mekanisme sheets.py (GOOGLE_API_KEY + sheet link-public) dan
# calendar_gfi.py (iCal URL public) — dua-duanya baca sumber yang PUBLIK/anonim.
# DB MASTER isinya nama + no HP donatur asli, jadi sheet-nya TETAP PRIVATE,
# cuma di-share ke satu alamat email robot (Service Account), bukan "Anyone
# with the link". Ini pola PERTAMA di codebase ini yang pakai Service Account
# — bukan reuse pola yang sudah terbukti, jadi ditest lebih hati-hati.
SA_KEY_FILE = os.getenv("SA_KEY_FILE", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "fundraising.db")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _izin_kunci_terlalu_longgar(path: str) -> bool:
    """
    True kalau file kunci bisa diakses user lain (bit 'others' terisi). Hanya bermakna
    di POSIX; di Windows bit izin file tidak mencerminkan ACL, jadi selalu False.
    """
    if os.name != "posix":
        return False
    return bool(os.stat(path).st_mode & 0o007)


def _load_sheets_service():
    """Bikin Google Sheets API client read-only pake Service Account. Blocking —
    selalu dipanggil lewat asyncio.to_thread(), jangan dipanggil langsung dari
    kode async."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if not SA_KEY_FILE:
        raise RuntimeError(
            "SA_KEY_FILE belum di-set di .env (nama variabel env, path ke file JSON "
            "Service Account — lihat .env.example)."
        )
    if not os.path.exists(SA_KEY_FILE):
        raise RuntimeError(
            f"SA_KEY_FILE di .env nunjuk ke '{SA_KEY_FILE}', tapi file itu nggak "
            "ketemu di folder ini. Pastikan file JSON Service Account-nya ada di path itu."
        )
    if _izin_kunci_terlalu_longgar(SA_KEY_FILE):
        log.warning(
            "PERINGATAN: file kunci Service Account bisa dibaca user lain. "
            "Batasi izinnya (640 atau 600) sebelum dipakai."
        )
    creds = service_account.Credentials.from_service_account_file(SA_KEY_FILE, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


async def fetch_sheet_rows(
    spreadsheet_id: str, sheet_name: str, range_suffix: str = "A2:G"
) -> list[list[str]]:
    """
    Baca rows mentah dari Google Sheets API pakai Service Account (read-only).
    Range mulai dari baris 2 (baris 1 = header, sengaja dilewati).

    google-api-python-client itu SYNC/blocking, jadi dijalankan di thread
    terpisah (asyncio.to_thread) biar nggak nge-block event loop FastAPI
    kalau nanti dipanggil dari konteks web juga.
    """

    def _blocking_fetch():
        service = _load_sheets_service()
        range_name = f"{sheet_name}!{range_suffix}"
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
        )
        return result.get("values", [])

    return await asyncio.to_thread(_blocking_fetch)


def cs_matches(no_hp_cs: str, cs_name: str) -> bool:
    """
    Cocokin baris ke CS tertentu berdasarkan TOKEN PERTAMA di kolom No HP CS.
    Contoh: "Rani 1 628112380705" -> token pertama "Rani".
    Match EXACT (case-insensitive) ke token pertama saja, biar "Rani" nggak
    kena tersambar nama lain yang cuma kebetulan mengandung kata serupa.
    """
    if not no_hp_cs or not no_hp_cs.strip():
        return False
    first_token = no_hp_cs.strip().split()[0]
    return first_token.lower() == cs_name.strip().lower()


def parse_rows_for_cs(raw_rows: list[list[str]], cs_name: str) -> list[dict]:
    """
    Transform rows mentah dari Sheets API jadi list dict, sudah difilter
    buat CS tertentu. Dipisah dari fetch_sheet_rows() biar gampang ditest
    tanpa perlu manggil API beneran.
    """
    cocok = []
    for i, r in enumerate(raw_rows, start=2):  # start=2: baris 1 = header

        def col(idx, default=""):
            return r[idx].strip() if idx < len(r) and r[idx] else default

        no_hp = col(0)
        no_hp_cs = col(3)
        if not no_hp:
            continue
        if not cs_matches(no_hp_cs, cs_name):
            continue
        cocok.append(
            {
                "row_sheet": i,
                "no_hp": no_hp,
                "panggilan": col(1),
                "nama_donatur": col(2),
                "no_hp_cs": no_hp_cs,
                "nama_label": col(4),
                "divisi": col(6),
            }
        )
    return cocok


def _awalan(no_hp: str) -> str:
    """Golongan awalan nomor, untuk laporan. Belum ada normalisasi (itu Fase 2)."""
    if no_hp.startswith("62"):
        return "62"
    if no_hp.startswith("0"):
        return "0"
    if no_hp.startswith("8"):
        return "8"
    if no_hp.startswith("+"):
        return "+"
    return "lain"


_AWAL_RUMUS = ("=", "+", "-", "@", "\t", "\r")


def _aman_csv(nilai):
    """
    Cegah formula injection: sel CSV yang diawali = + - @ dieksekusi sebagai rumus saat
    dibuka di Excel. Nama donatur itu input pihak luar, jadi diberi awalan '. HANYA
    berlaku untuk file log; data yang masuk ke database tidak diubah.
    """
    if isinstance(nilai, str) and nilai and nilai[0] in _AWAL_RUMUS:
        return "'" + nilai
    return nilai


def _tolak_jika_root(izinkan_root: bool = False) -> None:
    """Hindari file -wal/-shm milik root yang bikin aplikasi web gagal menulis."""
    if izinkan_root or not hasattr(os, "geteuid"):
        return
    if os.geteuid() == 0:
        raise SystemExit(
            "DITOLAK: jangan jalankan sebagai root. Jalankan sebagai user yang sama "
            "dengan service web (sudo -u <user-service-web> ...), supaya file -wal/-shm "
            "database tidak jadi milik root. Kalau memang sengaja: tambahkan --izinkan-root."
        )


def _siapkan_file_privat(path: str) -> None:
    """
    Buat file KOSONG baru dengan izin 600 sejak awal (tidak ada jeda 644). Dipanggil
    SEBELUM ada yang ditulis ke database, supaya folder yang salah/tidak bisa ditulis
    membuat script gagal di awal, bukan setelah data sudah masuk tanpa jejak log.
    Gagal kalau file sudah ada (tidak menimpa). Izin 600 tetap melekat saat diisi nanti.
    """
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))


def buat_backup_db(db_path: str, tujuan_dir: str, ts: str) -> str | None:
    """
    Backup database lewat API backup SQLite (konsisten walau database sedang dipakai
    aplikasi web). File backup izin 600. Return path-nya, atau None kalau db belum ada.
    """
    if not os.path.exists(db_path):
        log.warning("Database %s belum ada, backup dilewati.", db_path)
        return None
    stem = os.path.splitext(os.path.basename(db_path))[0]
    dest = os.path.join(tujuan_dir, f"backup_{stem}_{ts}.db")
    os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


async def migrasi_cs(
    spreadsheet_id: str,
    sheet_name: str,
    cs_name: str,
    dry_run: bool = True,
    log_dir: str | None = None,
    backup: bool = False,
    _raw_rows_override: list[list[str]] | None = None,  # buat testing, skip network
) -> dict:
    raw_rows = (
        _raw_rows_override
        if _raw_rows_override is not None
        else await fetch_sheet_rows(spreadsheet_id, sheet_name)
    )
    log.info(f"Total baris mentah dari sheet: {len(raw_rows)}")

    cocok = parse_rows_for_cs(raw_rows, cs_name)
    log.info(f"Baris cocok untuk CS '{cs_name}': {len(cocok)}")

    # Ringkasan agregat dihitung langsung dari data hasil parse (bukan dari CSV
    # yang dibuka di Excel), supaya bisa dicek tanpa perlu membuka data donatur.
    divisi_counts = Counter((r["divisi"] or "(kosong)") for r in cocok)
    baris_kontrol = [
        r["row_sheet"]
        for r in cocok
        if any(
            isinstance(v, str) and any(ord(ch) < 32 for ch in v)
            for k, v in r.items()
            if k != "row_sheet"
        )
    ]

    # Golongan awalan nomor (62 / 0 / 8 / + / lain). Belum dinormalisasi: nomor yang sama
    # dalam format berbeda (0811... vs 62811...) dianggap BEDA oleh UNIQUE no_hp. Laporan ini
    # buat memutuskan perlu tidaknya audit sebelum CS berikutnya.
    awalan_no_hp = Counter(_awalan(r["no_hp"]) for r in cocok)

    log_dir = os.path.expanduser(log_dir or os.getenv("MIGRASI_LOG_DIR") or ".")
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    mode = "dryrun" if dry_run else "execute"
    cs_aman = re.sub(r"[^A-Za-z0-9_-]", "_", cs_name)  # nama file tidak boleh membawa / atau ..
    log_filename = os.path.join(log_dir, f"migrasi_log_{cs_aman}_{mode}_{ts}.csv")
    _siapkan_file_privat(log_filename)  # gagal DI AWAL kalau folder log tidak bisa dipakai

    backup_file = None
    if backup and not dry_run:
        backup_file = await asyncio.to_thread(buat_backup_db, DB_PATH, log_dir, ts)

    masuk = 0
    skip_sudah_ada = 0
    skip_duplikat_dalam_sheet = 0
    seen: set[str] = set()
    log_rows = []

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        for row in cocok:
            # Nomor yang sama muncul lagi di baris lain pada sheet. Tanpa pengecekan ini, dry-run
            # menghitung keduanya "masuk" (tidak ada yang di-insert), sementara --execute
            # men-skip yang kedua, jadi laporan dry-run tidak sama dengan hasil sungguhan.
            # Yang pertama menang (sama seperti perilaku saat --execute).
            if row["no_hp"] in seen:
                skip_duplikat_dalam_sheet += 1
                log_rows.append(
                    {**row, "hasil": "AKAN_skip_duplikat_dalam_sheet" if dry_run else "skip_duplikat_dalam_sheet",
                     "existing_id": ""}
                )
                continue
            seen.add(row["no_hp"])

            async with db.execute(
                "SELECT id FROM db_donatur WHERE no_hp = ?", (row["no_hp"],)
            ) as cur:
                existing = await cur.fetchone()

            if existing is not None:
                skip_sudah_ada += 1
                log_rows.append(
                    {**row, "hasil": "skip_sudah_ada" if not dry_run else "AKAN_skip_sudah_ada",
                     "existing_id": existing["id"]}
                )
                continue

            if not dry_run:
                await db.execute(
                    "INSERT INTO db_donatur "
                    "(no_hp, panggilan, nama_donatur, no_hp_cs, nama_label, divisi) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["no_hp"], row["panggilan"], row["nama_donatur"],
                        row["no_hp_cs"], row["nama_label"], row["divisi"],
                    ),
                )
                await db.commit()  # commit per-baris, bukan sekali di akhir
                log_rows.append({**row, "hasil": "masuk", "existing_id": ""})
            else:
                log_rows.append({**row, "hasil": "AKAN_masuk", "existing_id": ""})

            masuk += 1

    # utf-8-sig = UTF-8 + BOM: tanpa BOM, Excel salah menebak encoding dan emoji/
    # karakter khusus di nama tampil berantakan (kolom di sebelahnya ikut bergeser).
    with open(log_filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "row_sheet", "no_hp", "panggilan", "nama_donatur", "no_hp_cs",
                "nama_label", "divisi", "hasil", "existing_id",
            ],
        )
        writer.writeheader()
        for r in log_rows:
            writer.writerow({k: _aman_csv(v) for k, v in r.items()})

    return {
        "cs": cs_name,
        "dry_run": dry_run,
        "total_baris_sheet": len(raw_rows),
        "total_cocok_cs_ini": len(cocok),
        "divisi_counts": dict(divisi_counts),
        "awalan_no_hp": dict(awalan_no_hp),
        "jumlah_baris_karakter_kontrol": len(baris_kontrol),
        "baris_sheet_karakter_kontrol": baris_kontrol[:20],
        "masuk": masuk,
        "skip_sudah_ada": skip_sudah_ada,
        "skip_duplikat_dalam_sheet": skip_duplikat_dalam_sheet,
        "backup_file": backup_file,
        "log_file": log_filename,
    }


async def _main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spreadsheet-id", required=True, help="ID spreadsheet DB MASTER 2026 (dari URL)")
    parser.add_argument("--sheet-name", default="DB MASTER 2026", help="Nama tab persis")
    parser.add_argument("--cs", required=True, help='Nama CS, mis. "Rani"')
    parser.add_argument("--execute", action="store_true", help="Jalankan beneran (default: dry-run)")
    parser.add_argument("--log-dir", default=None,
                        help="Folder untuk log CSV & backup (default: env MIGRASI_LOG_DIR, atau folder aktif). "
                             "Isinya data donatur asli: pakai folder privat.")
    parser.add_argument("--no-backup", action="store_true", help="Lewati backup database sebelum --execute")
    parser.add_argument("--izinkan-root", action="store_true", help="Izinkan jalan sebagai root (tidak disarankan)")
    args = parser.parse_args()
    _tolak_jika_root(args.izinkan_root)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = await migrasi_cs(
        args.spreadsheet_id, args.sheet_name, args.cs, dry_run=not args.execute,
        log_dir=args.log_dir, backup=not args.no_backup,
    )
    print("\n=== HASIL ===")
    for k, v in result.items():
        print(f"{k}: {v}")
    if result["dry_run"]:
        print(f"\nIni DRY-RUN — belum ada yang ditulis ke database.")
        print(f"Cek isi {result['log_file']} dulu, baru jalankan ulang dengan --execute kalau sudah yakin.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
