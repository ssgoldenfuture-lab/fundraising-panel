"""
Test integrasi route /database/* lewat Starlette TestClient (tanpa lifespan: tidak ada
scheduler, tidak ada koneksi MySQL/Google). Database SEMENTARA, data FIKTIF.

Yang diuji di sini adalah hal yang tidak tertangkap test unit: pagar akses di tiap route
(termasuk route TULIS), render template sungguhan lewat base.html, dan escaping HTML.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import aiosqlite

os.environ.setdefault("SECRET_KEY", "kunci-khusus-test")
sys.path.insert(0, os.path.dirname(__file__))

import main  # noqa: E402
from models import CREATE_SQL  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

TEST_DB = os.path.join(tempfile.gettempdir(), "test_db_donatur_route.db")
main.agg.DB_PATH = Path(TEST_DB)

XSS = "<script>alert(1)</script>"


def assert_true(cond, label):
    print(f"[{'OK  ' if cond else 'GAGAL'}] {label}")
    if not cond:
        raise AssertionError(label)


def client_sebagai(username=None, role="staff"):
    c = TestClient(main.app, follow_redirects=False)
    if username:
        c.cookies.set(main.COOKIE_NAME, main.make_session(username, role))
    return c


async def seed():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    async with aiosqlite.connect(TEST_DB) as db:
        await db.executescript(CREATE_SQL)
        n = 0

        async def add(cs, label, jumlah, nama_fmt="Donatur Fiktif {n}"):
            nonlocal n
            for _ in range(jumlah):
                n += 1
                await db.execute(
                    "INSERT INTO db_donatur (no_hp, nama_donatur, no_hp_cs, nama_label, divisi) VALUES (?,?,?,?,?)",
                    (f"628000{n:07d}", nama_fmt.format(n=n), cs, label, "SSU"),
                )

        await add("Rani 1 628111", "DB FIX BC (pernah donasi) ~ DB QURBAN", 3, "Rani Satu {n}")
        await add("Rani 1 628111", "Database", 2, "Rani Satu {n}")
        await add("Fitri 1 628333", "DB QURBAN", 2, "Fitri Satu {n}")
        await add("Rani 3 628444", "Database", 120, "Rani Tiga {n}")
        await add("Rani 1 628111", "Database", 1, XSS)  # nama berisi tag HTML
        await db.commit()


async def hitung(sql):
    async with aiosqlite.connect(TEST_DB) as db:
        async with db.execute(sql) as cur:
            return (await cur.fetchone())[0]


def main_test():
    asyncio.run(seed())
    os.environ.pop("DB_VIEWERS", None)
    total_awal = asyncio.run(hitung("SELECT COUNT(*) FROM db_donatur"))
    assert_true(total_awal == 128, "data uji siap (128 baris)")

    print("\n=== A. Belum login ===")
    anon = client_sebagai()
    for path in ["/database", "/database/data", "/database/antrian"]:
        r = anon.get(path)
        assert_true(r.status_code in (302, 307) and r.headers["location"] == "/login",
                    f"GET {path} tanpa login -> /login")

    print("\n=== B. Login tapi BUKAN admin & tidak di DB_VIEWERS -> ditolak, termasuk route TULIS ===")
    staff = client_sebagai("staff", "staff")
    for path in ["/database", "/database/data", "/database/antrian"]:
        r = staff.get(path)
        assert_true(r.status_code == 303 and r.headers["location"] == "/home", f"GET {path} akun biasa -> dialihkan ke /home")
    r = staff.post("/database/paste", data={"raw_text": "628111222333,Penyusup", "no_hp_cs": "X", "divisi": "Y"})
    assert_true(r.status_code == 303 and r.headers["location"] == "/home", "POST /database/paste akun biasa -> ditolak")
    assert_true(asyncio.run(hitung("SELECT COUNT(*) FROM db_donatur")) == total_awal,
                "PENTING: tidak ada data masuk dari akun yang ditolak")
    r = staff.post("/database/antrian/1/resolve", data={"action": "replace"})
    assert_true(r.status_code == 303 and r.headers["location"] == "/home", "POST resolve antrian akun biasa -> ditolak")

    print("\n=== C. Admin: halaman & filter ===")
    admin = client_sebagai("admin", "admin")
    r = admin.get("/database")
    assert_true(r.status_code == 200 and 'href="/database/data"' in r.text, "/database terbuka & ada tombol ke halaman filter")
    r = admin.get("/database/data")
    assert_true(r.status_code == 200 and "Lihat &amp; Filter Data" in r.text, "/database/data terbuka untuk admin")
    assert_true("Rani Satu" in r.text and "Fitri Satu" in r.text, "tanpa filter: data semua CS tampil")
    assert_true("Semua nomor CS (128)" in r.text, "dropdown CS menampilkan total semua")

    r = admin.get("/database/data", params={"cs": "Rani 1 628111", "label": "DB QURBAN"})
    assert_true("dari 3 db" in r.text, "filter CS + label -> 3 db")
    assert_true("Rani Satu" in r.text and "Fitri Satu" not in r.text and "Rani Tiga" not in r.text,
                "PENTING: hanya data CS terpilih yang tampil, data CS lain tidak bocor")
    assert_true('<option value="DB QURBAN" selected>' in r.text, "dropdown label menampilkan pilihan aktif")

    r = admin.get("/database/data", params={"cs": "Rani 3 628444"})
    assert_true("Halaman 1 dari 3" in r.text and "dari 120 db" in r.text, "paginasi: 120 baris = 3 halaman")
    assert_true("page=2" in r.text, "ada link ke halaman 2")
    r = admin.get("/database/data", params={"cs": "Rani 3 628444", "page": "3"})
    assert_true("Menampilkan 101–120 dari 120 db" in r.text, "halaman 3: rentang benar")
    r = admin.get("/database/data", params={"cs": "Rani 3 628444", "page": "abc"})
    assert_true(r.status_code == 200 and "Halaman 1 dari 3" in r.text, "page tidak valid -> halaman 1, bukan error 422/500")

    print("\n=== D. Keamanan tampilan: nama berisi tag HTML tidak boleh dieksekusi ===")
    r = admin.get("/database/data", params={"cs": "Rani 1 628111", "label": "Database"})
    assert_true(XSS not in r.text, "PENTING: <script> mentah TIDAK muncul di HTML")
    assert_true("&lt;script&gt;alert(1)&lt;/script&gt;" in r.text, "tag di-escape jadi teks biasa")

    print("\n=== E. DB_VIEWERS: akun non-admin yang diizinkan ===")
    os.environ["DB_VIEWERS"] = "mifipsb"
    r = client_sebagai("mifipsb", "staff").get("/database/data")
    assert_true(r.status_code == 200, "mifipsb (ada di DB_VIEWERS) boleh membuka")
    r = client_sebagai("staff", "staff").get("/database/data")
    assert_true(r.status_code == 303, "akun lain tetap ditolak walau DB_VIEWERS terisi")
    os.environ.pop("DB_VIEWERS", None)

    print("\n=== F. Fitur lama tidak rusak: admin masih bisa paste & dedup ===")
    r = admin.post("/database/paste", data={"raw_text": "628777000001,Uji Paste", "no_hp_cs": "Rani 1 628111", "divisi": "SSU"})
    assert_true(r.status_code == 303 and "/database?flash=" in r.headers["location"], "POST /database/paste oleh admin berjalan")
    assert_true(asyncio.run(hitung("SELECT COUNT(*) FROM db_donatur")) == total_awal + 1, "data dari admin masuk")

    os.remove(TEST_DB)
    print("\n=== SEMUA TEST ROUTE LOLOS ===")


if __name__ == "__main__":
    main_test()
