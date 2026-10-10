"""
Test manual buat db_donatur_migrasi.py — pakai data contoh yang bentuknya
PERSIS kayak apa yang dibalikin Google Sheets API (list of list of string),
TANPA pernah manggil network sama sekali (pakai _raw_rows_override).
"""
import asyncio
import os
import sys

import aiosqlite

sys.path.insert(0, os.path.dirname(__file__))
from models import CREATE_SQL
from db_donatur_migrasi import cs_matches, parse_rows_for_cs, migrasi_cs

TEST_DB = "/tmp/test_db_donatur_migrasi.db"

# Simulasi persis kolom A-G DB MASTER, termasuk header yang HARUS dilewati
# (walau di sini nggak ada baris header karena fetch_sheet_rows sudah minta
# range A2:G — jadi raw_rows yang masuk ke parse_rows_for_cs itu memang
# sudah tanpa header).
RAW_ROWS_CONTOH = [
    # No HP, Panggilan, Nama Donatur, No HP CS, Nama Label, Duplikasi(diabaikan), Divisi
    ["6281111111101", "Kak", "Donatur Rani A", "Rani 1 628112380705", "DB FIX BC", "1", "SSU"],
    ["6281111111102", "Kak", "Donatur Rani B", "Rani 1 628112380705", "DB QURBAN", "1", "SSU"],
    ["6281111111103", "Kak", "Donatur Rani C", "Rani 2 628113333333", "", "1", "OWN"],   # Rani, divisi BEDA (kasus nyata)
    ["6281111111104", "Kak", "Donatur Faridah", "Faridah 1 628114444444", "", "1", "SSU"],  # BUKAN Rani
    ["", "Kak", "Nomor kosong, harus di-skip", "Rani 3 628115555555", "", "1", "SSU"],  # no_hp kosong
    ["6281111111105", "Kak", "No HP CS kosong", "", "", "1", "SSU"],  # no_hp_cs kosong -> ga match siapa2
    ["0811111111066", "Kak", "Format nomor leading-zero dipertahankan", "Rani 1 628112380705", "", "1", "SSU"],
]


def assert_eq(actual, expected, label):
    status = "OK  " if actual == expected else "GAGAL"
    print(f"[{status}] {label}: dapat={actual!r} harapan={expected!r}")
    if actual != expected:
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


async def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    async with aiosqlite.connect(TEST_DB) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

    # Patch DB_PATH modul ke file test SEBELUM test manapun yang nyentuh DB —
    # db_donatur_migrasi.py defaultnya nunjuk ke fundraising.db asli di repo,
    # yang di sandbox ini belum tentu ada/punya tabelnya.
    import db_donatur_migrasi
    db_donatur_migrasi.DB_PATH = TEST_DB

    print("=== TEST 1: cs_matches — exact match token pertama, case-insensitive ===")
    assert_eq(cs_matches("Rani 1 628112380705", "Rani"), True, "match normal")
    assert_eq(cs_matches("rani 1 628112380705", "Rani"), True, "match case-insensitive")
    assert_eq(cs_matches("Faridah 1 628114444444", "Rani"), False, "CS lain tidak match")
    assert_eq(cs_matches("", "Rani"), False, "string kosong tidak match")
    assert_eq(cs_matches("Raniapaja 1 628", "Rani"), False, "substring TANPA spasi tidak match (bukan token utuh)")
    print("-> cs_matches: LOLOS\n")

    print("=== TEST 2: parse_rows_for_cs — filter + transform ===")
    cocok = parse_rows_for_cs(RAW_ROWS_CONTOH, "Rani")
    assert_eq(len(cocok), 4, "jumlah baris cocok untuk Rani (3 normal + 1 leading-zero, baris kosong & CS lain ke-skip)")
    no_hps = [r["no_hp"] for r in cocok]
    assert_eq("6281111111104" in no_hps, False, "baris Faridah tidak ikut ke-filter")
    assert_eq("0811111111066" in [r["no_hp"] for r in cocok], True, "leading zero DIPERTAHANKAN apa adanya, tidak hilang")
    divisi_set = {r["divisi"] for r in cocok}
    assert_eq(divisi_set, {"SSU", "OWN"}, "1 CS (Rani) bisa punya lebih dari 1 divisi dalam hasil filter")
    print("-> parse_rows_for_cs: LOLOS\n")

    print("=== TEST 3: migrasi_cs dry_run=True — TIDAK menulis apa-apa ke DB ===")
    result_dry = await migrasi_cs(
        spreadsheet_id="dummy", sheet_name="dummy", cs_name="Rani",
        dry_run=True, _raw_rows_override=RAW_ROWS_CONTOH,
    )
    assert_eq(result_dry["dry_run"], True, "flag dry_run")
    assert_eq(result_dry["masuk"], 4, "dry-run: 4 baris AKAN masuk")
    assert_eq(result_dry["skip_sudah_ada"], 0, "dry-run: belum ada yang skip (DB masih kosong)")
    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) c FROM db_donatur") as cur:
            total_setelah_dryrun = (await cur.fetchone())["c"]
    assert_eq(total_setelah_dryrun, 0, "PENTING: dry-run beneran TIDAK nulis apa-apa ke db_donatur")
    os.remove(result_dry["log_file"])
    print("-> dry_run: LOLOS, DB tetap kosong setelahnya\n")

    print("=== TEST 4: migrasi_cs dry_run=False — beneran nulis ===")
    result_exec1 = await migrasi_cs(
        spreadsheet_id="dummy", sheet_name="dummy", cs_name="Rani",
        dry_run=False, _raw_rows_override=RAW_ROWS_CONTOH,
    )
    assert_eq(result_exec1["masuk"], 4, "eksekusi pertama: 4 baris masuk beneran")
    assert_eq(result_exec1["skip_sudah_ada"], 0, "eksekusi pertama: belum ada yang skip")
    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) c FROM db_donatur") as cur:
            total = (await cur.fetchone())["c"]
        async with db.execute(
            "SELECT divisi FROM db_donatur WHERE no_hp='6281111111103'"
        ) as cur:
            divisi_c = (await cur.fetchone())["divisi"]
    assert_eq(total, 4, "4 row beneran ada di db_donatur setelah eksekusi")
    assert_eq(divisi_c, "OWN", "baris ke-3 Rani (divisi beda) tersimpan dengan divisi OWN, bukan ke-generalisir SSU")
    os.remove(result_exec1["log_file"])
    print("-> eksekusi pertama: LOLOS\n")

    print("=== TEST 5: migrasi_cs dijalankan ULANG (simulasi interupsi & lanjut) — harus idempotent ===")
    result_exec2 = await migrasi_cs(
        spreadsheet_id="dummy", sheet_name="dummy", cs_name="Rani",
        dry_run=False, _raw_rows_override=RAW_ROWS_CONTOH,
    )
    assert_eq(result_exec2["masuk"], 0, "eksekusi KEDUA: 0 baris baru masuk (semua udah ada)")
    assert_eq(result_exec2["skip_sudah_ada"], 4, "eksekusi KEDUA: semua 4 baris ke-skip otomatis (BUKAN error, BUKAN dobel)")
    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) c FROM db_donatur") as cur:
            total_setelah_run_kedua = (await cur.fetchone())["c"]
    assert_eq(total_setelah_run_kedua, 4, "TOTAL TETAP 4 (tidak dobel) — inilah yang bikin proses ini aman diinterupsi & dilanjut")
    os.remove(result_exec2["log_file"])
    print("-> idempotency (jalan ulang setelah 'interupsi'): LOLOS\n")

    print("=== TEST 6: ringkasan agregat (divisi_counts, karakter kontrol) + BOM di CSV log ===")
    RAW_ROWS_KONTROL = [
        ["6282000000001", "Kak", "Nama Normal", "Rani 1 628112380705", "", "1", "SSU"],
        ["6282000000002", "Kak", "Nama dengan\nbaris baru", "Rani 1 628112380705", "", "1", "OWN"],
        ["6282000000003", "Kak", "Nama dengan\ttab", "Rani 2 628113333333", "", "1", "OWN"],
        ["6282000000004", "Kak", "Nama emoji 🌹❤", "Rani 2 628113333333", "", "1", ""],  # divisi kosong
    ]
    result6 = await migrasi_cs(
        spreadsheet_id="dummy", sheet_name="dummy", cs_name="Rani",
        dry_run=True, _raw_rows_override=RAW_ROWS_KONTROL,
    )
    assert_eq(
        result6["divisi_counts"], {"SSU": 1, "OWN": 2, "(kosong)": 1},
        "divisi_counts: semua nilai unik terhitung, divisi kosong tampil sebagai (kosong)",
    )
    assert_eq(sum(result6["divisi_counts"].values()), result6["total_cocok_cs_ini"],
              "jumlah divisi_counts = total_cocok_cs_ini (tidak ada baris 'hilang')")
    assert_eq(result6["jumlah_baris_karakter_kontrol"], 2, "2 baris punya newline/tab di dalam sel")
    assert_eq(result6["baris_sheet_karakter_kontrol"], [3, 4], "nomor baris sheet yang kena dilaporkan (bukan isinya)")

    with open(result6["log_file"], "rb") as f:
        kepala = f.read(3)
    assert_eq(kepala, b"\xef\xbb\xbf", "CSV log diawali BOM UTF-8 (supaya Excel baca emoji dengan benar)")
    with open(result6["log_file"], encoding="utf-8-sig", newline="") as f:
        isi_csv = f.read()
    assert_eq("🌹❤" in isi_csv, True, "emoji utuh saat CSV dibaca balik sebagai UTF-8")
    os.remove(result6["log_file"])
    print("-> ringkasan agregat + BOM: LOLOS\n")

    import csv
    import sqlite3
    import tempfile
    import db_donatur_migrasi as m

    async def jumlah_baris():
        async with aiosqlite.connect(TEST_DB) as db:
            async with db.execute("SELECT COUNT(*) FROM db_donatur") as cur:
                return (await cur.fetchone())[0]

    tmp_log = tempfile.mkdtemp(prefix="uji_migrasi_")

    print("=== TEST 7: nomor sama muncul 2x di sheet -> dry-run HARUS sama dengan --execute ===")
    RAW_DUP = [
        ["6283000000001", "Kak", "Pertama",  "Rani 1 628112380705", "", "1", "SSU"],
        ["6283000000002", "Kak", "Lain",     "Rani 1 628112380705", "", "1", "OWN"],
        ["6283000000001", "Kak", "Kembaran", "Rani 2 628113333333", "", "1", "SSU"],  # no_hp sama dgn baris 1
        ["6283000000003", "Kak", "Lain lagi", "Rani 1 628112380705", "", "1", "SSU"],
    ]
    sebelum = await jumlah_baris()
    dry = await migrasi_cs("x", "x", "Rani", dry_run=True, log_dir=tmp_log, _raw_rows_override=RAW_DUP)
    assert_eq((dry["masuk"], dry["skip_sudah_ada"], dry["skip_duplikat_dalam_sheet"]), (3, 0, 1),
              "dry-run: 3 masuk, 1 duplikat dalam sheet (bukan 4 masuk)")
    assert_eq(await jumlah_baris(), sebelum, "dry-run tidak menulis apa-apa")
    eks = await migrasi_cs("x", "x", "Rani", dry_run=False, log_dir=tmp_log, _raw_rows_override=RAW_DUP)
    assert_eq((eks["masuk"], eks["skip_sudah_ada"], eks["skip_duplikat_dalam_sheet"]),
              (dry["masuk"], dry["skip_sudah_ada"], dry["skip_duplikat_dalam_sheet"]),
              "PENTING: angka --execute SAMA PERSIS dengan dry-run")
    assert_eq(eks["masuk"] + eks["skip_sudah_ada"] + eks["skip_duplikat_dalam_sheet"], eks["total_cocok_cs_ini"],
              "masuk + skip = total cocok (tidak ada baris 'hilang' dari hitungan)")
    async with aiosqlite.connect(TEST_DB) as db:
        async with db.execute("SELECT no_hp_cs FROM db_donatur WHERE no_hp='6283000000001'") as cur:
            kept = (await cur.fetchone())[0]
    assert_eq(kept, "Rani 1 628112380705", "yang PERTAMA di sheet yang menang")
    print("-> duplikat dalam sheet: LOLOS\n")

    print("=== TEST 8: laporan golongan awalan nomor ===")
    RAW_AWAL = [
        ["6284000000001", "", "a", "Rani 1 62811", "", "1", "SSU"],
        ["08140000002",   "", "b", "Rani 1 62811", "", "1", "SSU"],
        ["8140000003",    "", "c", "Rani 1 62811", "", "1", "SSU"],
        ["+6284000004",   "", "d", "Rani 1 62811", "", "1", "SSU"],
        ["9990000005",    "", "e", "Rani 1 62811", "", "1", "SSU"],
        ["6284000000006", "", "f", "Rani 1 62811", "", "1", "SSU"],
    ]
    r = await migrasi_cs("x", "x", "Rani", dry_run=True, log_dir=tmp_log, _raw_rows_override=RAW_AWAL)
    assert_eq(r["awalan_no_hp"], {"62": 2, "0": 1, "8": 1, "+": 1, "lain": 1}, "hitungan awalan 62/0/8/+/lain")
    print("-> awalan nomor: LOLOS\n")

    print("=== TEST 9: log di folder privat, izin 600, nama file aman ===")
    log_baru = os.path.join(tmp_log, "folder_baru", "migrasi")
    r = await migrasi_cs("x", "x", "Rani", dry_run=True, log_dir=log_baru, _raw_rows_override=RAW_AWAL)
    assert_eq(os.path.dirname(r["log_file"]), log_baru, "folder log dibuat otomatis & dipakai")
    if os.name == "posix":
        assert_eq(oct(os.stat(r["log_file"]).st_mode & 0o777), "0o600", "file log izin 600, bukan 644")
        assert_eq(oct(os.stat(log_baru).st_mode & 0o777), "0o700", "folder log izin 700")
    r = await migrasi_cs("x", "x", "../../etc/evil", dry_run=True, log_dir=log_baru, _raw_rows_override=RAW_AWAL)
    assert_eq(os.path.dirname(os.path.abspath(r["log_file"])), os.path.abspath(log_baru),
              "nama CS berisi ../ tidak bisa nulis di luar folder log")
    os.environ["MIGRASI_LOG_DIR"] = os.path.join(tmp_log, "dari_env")
    r = await migrasi_cs("x", "x", "Rani", dry_run=True, _raw_rows_override=RAW_AWAL)
    assert_eq(os.path.dirname(r["log_file"]), os.environ["MIGRASI_LOG_DIR"], "env MIGRASI_LOG_DIR dihormati")
    os.environ.pop("MIGRASI_LOG_DIR")
    # folder log yang mustahil dipakai (path-nya ternyata file biasa) -> harus gagal SEBELUM menulis ke DB
    blok = os.path.join(tmp_log, "bukan_folder")
    open(blok, "w").write("x")
    n_awal = await jumlah_baris()
    gagal_di_awal = False
    try:
        await migrasi_cs("x", "x", "Rani", dry_run=False, log_dir=os.path.join(blok, "sub"),
                         _raw_rows_override=[["6289900000001", "", "z", "Rani 1 62811", "", "1", "SSU"]])
    except OSError:
        gagal_di_awal = True
    assert_eq(gagal_di_awal, True, "log_dir yang tidak bisa dipakai -> error")
    assert_eq(await jumlah_baris(), n_awal, "PENTING: error terjadi SEBELUM database ditulis (tidak ada data masuk tanpa log)")
    print("-> folder & izin log: LOLOS\n")

    print("=== TEST 10: formula injection di CSV dinetralkan, database TIDAK diubah ===")
    RAW_RUMUS = [
        ["6285000000001", "", '=HYPERLINK("http://x","klik")', "Rani 1 62811", "", "1", "SSU"],
        ["6285000000002", "", "+1+1",     "Rani 1 62811", "", "1", "SSU"],
        ["6285000000003", "", "@SUM(1)",  "Rani 1 62811", "", "1", "SSU"],
        ["6285000000004", "", "-2",       "Rani 1 62811", "", "1", "SSU"],
        ["6285000000005", "", "Nama Wajar", "Rani 1 62811", "", "1", "SSU"],
    ]
    r = await migrasi_cs("x", "x", "Rani", dry_run=False, log_dir=tmp_log, _raw_rows_override=RAW_RUMUS)
    with open(r["log_file"], encoding="utf-8-sig", newline="") as f:
        baris = {x["no_hp"]: x["nama_donatur"] for x in csv.DictReader(f)}
    assert_eq(baris["6285000000001"], "'=HYPERLINK(\"http://x\",\"klik\")", "= diberi awalan ' di CSV")
    assert_eq((baris["6285000000002"], baris["6285000000003"], baris["6285000000004"]),
              ("'+1+1", "'@SUM(1)", "'-2"), "+ @ - juga diberi awalan '")
    assert_eq(baris["6285000000005"], "Nama Wajar", "nama biasa tidak disentuh")
    async with aiosqlite.connect(TEST_DB) as db:
        async with db.execute("SELECT nama_donatur FROM db_donatur WHERE no_hp='6285000000001'") as cur:
            di_db = (await cur.fetchone())[0]
    assert_eq(di_db, '=HYPERLINK("http://x","klik")', "PENTING: di DATABASE nama tetap asli (awalan ' hanya di file log)")
    print("-> CSV injection: LOLOS\n")

    print("=== TEST 11: backup sebelum --execute ===")
    r = await migrasi_cs("x", "x", "Rani", dry_run=True, log_dir=tmp_log, backup=True, _raw_rows_override=RAW_AWAL)
    assert_eq(r["backup_file"], None, "dry-run tidak bikin backup")
    n_sebelum = await jumlah_baris()
    RAW_BARU = [["6286000000001", "", "x", "Rani 1 62811", "", "1", "SSU"],
                ["6286000000002", "", "y", "Rani 1 62811", "", "1", "SSU"]]
    r = await migrasi_cs("x", "x", "Rani", dry_run=False, log_dir=tmp_log, backup=True, _raw_rows_override=RAW_BARU)
    assert_eq(os.path.exists(r["backup_file"]), True, "file backup terbentuk")
    if os.name == "posix":
        assert_eq(oct(os.stat(r["backup_file"]).st_mode & 0o777), "0o600", "backup izin 600 (isinya data donatur)")
    con = sqlite3.connect(r["backup_file"])
    n_backup = con.execute("SELECT COUNT(*) FROM db_donatur").fetchone()[0]
    con.close()
    assert_eq(n_backup, n_sebelum, "backup berisi kondisi SEBELUM eksekusi")
    assert_eq(await jumlah_baris(), n_sebelum + 2, "eksekusi sesudah backup tetap menulis")
    r = await migrasi_cs("x", "x", "Rani", dry_run=False, log_dir=tmp_log, backup=False, _raw_rows_override=RAW_BARU)
    assert_eq(r["backup_file"], None, "backup=False -> tidak ada backup")
    print("-> backup: LOLOS\n")

    print("=== TEST 12: pagar root & peringatan izin file kunci ===")
    asli = getattr(m.os, "geteuid", None)
    try:
        m.os.geteuid = lambda: 0
        gagal = False
        try:
            m._tolak_jika_root(False)
        except SystemExit as e:
            gagal = "root" in str(e)
        assert_eq(gagal, True, "dijalankan sebagai root -> DITOLAK dengan pesan jelas")
        assert_eq(m._tolak_jika_root(True), None, "--izinkan-root meloloskan")
        m.os.geteuid = lambda: 1000
        assert_eq(m._tolak_jika_root(False), None, "user biasa -> lolos")
    finally:
        if asli is not None:
            m.os.geteuid = asli
    if os.name == "posix":
        kunci = os.path.join(tmp_log, "kunci_palsu.json")
        open(kunci, "w").write("{}")
        for izin, harapan in [(0o644, True), (0o640, False), (0o600, False), (0o604, True)]:
            os.chmod(kunci, izin)
            assert_eq(m._izin_kunci_terlalu_longgar(kunci), harapan, f"izin {oct(izin)} -> terlalu longgar={harapan}")
    print("-> pagar root & izin kunci: LOLOS\n")

    import shutil
    shutil.rmtree(tmp_log, ignore_errors=True)
    os.remove(TEST_DB)
    print("=== SEMUA TEST LOLOS ===")


if __name__ == "__main__":
    asyncio.run(main())
