# Changelog

## v0.4.1 — 2026-10-03
- SINKRON: memasukkan ke repo fitur yang sudah live di server tapi belum pernah di-commit (tidak ada perubahan perilaku — isi file sama persis dengan production)
- /home: filter bulan, perbandingan bulan dipilih vs bulan sebelumnya, grafik per minggu (dengan angka nominal di atas batang), capaian donasi web per campaign (MySQL berdonasi), kalender konten 30 hari
- /rekap: rekap CS backup (login nama + PIN, OCR Gemini bukti transfer, simpan ke MySQL `rekap_cs`, kirim ke Apps Script untuk Drive + Sheet)
- rekap_cs_appscript.js: kode Apps Script yang sedang ter-deploy (routing Nabila -> tab PaidNabila, CS lain -> tab Auto_<Nama>)
- requirements: `requests` dicatat eksplisit; .env.example dilengkapi; .gitignore: seed_users.py, knowledge.json, file kunci Service Account

## v0.4.0 — 2026-10-01
- Database Donatur: halaman Lihat & Filter Data (`/database/data`), read-only. Dropdown bertingkat Nomor CS -> Label, tabel terpaginasi 50 baris/halaman
- Label gabungan ("A ~ B") dipecah per label satuan di dropdown
- AKSES: seluruh area `/database` sekarang hanya untuk role admin atau username di env `DB_VIEWERS` (default aman: kosong = hanya admin). Sebelumnya cukup login
- Perlu tindakan saat deploy: isi `DB_VIEWERS` di `.env` server (mis. `DB_VIEWERS=mifipsb,fitri`) kalau akun non-admin perlu akses

## v0.3.0 — 2026-09-25
- Database Donatur (Fase 1a): input massal via paste (format `no_hp,apa saja`), satu No HP CS + Divisi berlaku per-batch
- Dedup exact-match otomatis (niru formula COUNTIF manual) — db bentrok masuk antrian review, tidak menimpa data lama otomatis
- Antrian review: pilih pertahankan data lama / pakai data baru / lewati
- Tabel baru: `db_donatur` (gabungan DB MASTER + per-CS lama), `db_duplikat_antrian`
- Catatan: migrasi data ASLI dari Google Sheets ke tabel ini belum jalan (Fase 1b, menyusul terpisah) — tabel masih kosong sampai itu selesai

## v0.2.0 — 2026-09-16
- Tambah 2 kategori FAQ: Broadcast WhatsApp, Database Donatur
- Sidebar /faq: smooth scroll ke section
- Footer /faq: penanda visual lebih jelas

## v0.1.0 — 2026-09-16
- FAQ & Kotak Tanya: submit publik, triase staff, tampilan publik (PR #1)
- Kotak Tanya lengkap: field topik/saran, tahap konfirmasi, Kelola FAQ (PR #2)
- Fix conflict marker git yang bikin app tidak bisa start (PR #3)
- Review FAQ: kategori jadi field bersama, tab status, field catatan/konteks
