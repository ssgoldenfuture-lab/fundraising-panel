/**
 * Rekap CS Otomatis — Google Apps Script
 * Deploy sebagai Web App: Execute as "Me", Access "Anyone"
 *
 * Fungsi:
 * 1. Terima POST dari server Python berisi data rekap + foto base64
 * 2. Append row ke tab CS di spreadsheet yang sesuai (per CS)
 * 3. Upload foto ke Google Drive folder khusus
 * 4. Return URL Drive foto
 */

// ── KONFIGURASI ──────────────────────────────────────────────────────────────

// Spreadsheet Nabila (sheet existing)
const SHEET_NABILA  = "1O8yISLxVeznRY7iBDH14eQIb8zbG5f3vNQwTpqiw4u4";

// Spreadsheet CS lain — untuk saat ini pakai sheet yang sama
const SHEET_OTHERS  = "1O8yISLxVeznRY7iBDH14eQIb8zbG5f3vNQwTpqiw4u4";

// Mapping CS → Spreadsheet ID
// CS yang tidak ada di sini → pakai SHEET_OTHERS
const CS_SHEET_MAP  = {
  "Nabila": SHEET_NABILA,
};

// Mapping CS → nama tab khusus (override prefix Auto_)
const CS_TAB_MAP = {
  "Nabila": "PaidNabila",
};

// Prefix tab default untuk CS yang tidak ada di CS_TAB_MAP
const TAB_PREFIX = "Auto_";

// Folder Google Drive untuk menyimpan foto bukti TF
const DRIVE_FOLDER_NAME = "Bukti TF Rekap CS";

// ── KOLOM (samain dengan struktur sheet manual) ───────────────────────────────
// A=No | B=Tanggal | C=Nama Donatur | D=No HP | E=IG Username
// F=Nominal | G=Kode Program | H=Asal Donasi | I=Foto (Drive URL)
// J=Bank Asal | K=Keterangan | L=Bulan | M=CS | N=Platform | O=Submit At

// ── Helper: cari spreadsheet ID untuk CS tertentu ────────────────────────────
function getSheetId(csName) {
  return CS_SHEET_MAP[csName] || SHEET_OTHERS;
}


// ── MAIN: Handle POST request ─────────────────────────────────────────────────
function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);

    const cs_name      = (body.cs_name     || "").trim();
    const tanggal      = (body.tanggal     || "").trim();   // YYYY-MM-DD
    const nama_donatur = (body.nama_donatur|| "").trim();
    const nomor_hp     = (body.nomor_hp    || "").trim();
    const username_ig  = (body.username_ig || "").trim();
    const nominal      = parseInt(body.nominal || 0);
    const kode_program = (body.kode_program|| "").trim();
    const asal_donasi  = (body.asal_donasi || "").trim();
    const bank_asal    = (body.bank_asal   || "").trim();
    const keterangan   = (body.keterangan  || "").trim();
    const bulan        = (body.bulan       || "").trim();   // JANUARI, dst
    const foto_b64     = (body.foto_base64 || "").trim();
    const foto_mime    = (body.foto_mime   || "image/jpeg").trim();
    const foto_name    = (body.foto_name   || `bukti_${Date.now()}.jpg`).trim();
    const submit_at    = new Date().toISOString();

    if (!cs_name || !tanggal || !nama_donatur || nominal <= 0) {
      return jsonResponse({status: "error", message: "Field wajib tidak lengkap"});
    }

    // 1. Upload foto ke Google Drive
    let drive_url = "";
    if (foto_b64) {
      drive_url = uploadToDrive(foto_b64, foto_mime, foto_name, cs_name);
    }

    // 2. Append row ke sheet
    appendToSheet(cs_name, [
      tanggal, nama_donatur, nomor_hp, username_ig,
      nominal, kode_program, asal_donasi, drive_url,
      bank_asal, keterangan, bulan, cs_name, "Web Form", submit_at
    ]);

    return jsonResponse({status: "ok", drive_url: drive_url, message: "Berhasil disimpan"});

  } catch (err) {
    console.error("doPost error:", err.toString());
    return jsonResponse({status: "error", message: err.toString()});
  }
}

// ── Upload foto ke Google Drive ───────────────────────────────────────────────
function uploadToDrive(b64data, mimeType, fileName, csName) {
  // Cari/buat folder utama
  let rootFolder = getOrCreateFolder(DRIVE_FOLDER_NAME, DriveApp.getRootFolder());
  // Sub-folder per CS
  let csFolder   = getOrCreateFolder(csName, rootFolder);

  const blob = Utilities.newBlob(
    Utilities.base64Decode(b64data), mimeType, fileName
  );
  const file = csFolder.createFile(blob);
  file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);

  // Return direct view URL
  return `https://drive.google.com/file/d/${file.getId()}/view`;
}

function getOrCreateFolder(name, parent) {
  const folders = parent.getFoldersByName(name);
  if (folders.hasNext()) return folders.next();
  return parent.createFolder(name);
}

// ── Append row ke tab CS di spreadsheet yang sesuai ──────────────────────────
function appendToSheet(csName, rowData) {
  const sheetId = getSheetId(csName);               // ← routing per CS
  const ss      = SpreadsheetApp.openById(sheetId);
  const tabName = CS_TAB_MAP[csName] || (TAB_PREFIX + csName);  // Nabila → "PaidNabila", lain → "Auto_Xxx"

  let sheet = ss.getSheetByName(tabName);

  if (!sheet) {
    // Buat tab baru otomatis
    sheet = ss.insertSheet(tabName);
    // Header
    sheet.appendRow([
      "No", "Tanggal", "Nama Donatur", "No HP", "IG Username",
      "Nominal", "Kode Program", "Asal Donasi", "Foto Bukti TF",
      "Bank Asal", "Keterangan", "Bulan", "CS", "Platform", "Submit At"
    ]);
    sheet.getRange(1, 1, 1, 15).setFontWeight("bold").setBackground("#2F6B4F").setFontColor("#FFFFFF");
    sheet.setFrozenRows(1);
    // Lebar kolom
    sheet.setColumnWidth(2, 95);   // Tanggal
    sheet.setColumnWidth(3, 160);  // Nama
    sheet.setColumnWidth(6, 100);  // Nominal
    sheet.setColumnWidth(9, 200);  // Link foto
  }

  // Nomor urut
  const lastRow = sheet.getLastRow();
  const no      = lastRow;  // baris 1 = header, jadi no = lastRow

  sheet.appendRow([no, ...rowData]);
}


// ── Helper: JSON response ─────────────────────────────────────────────────────
function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ── TEST: GET request untuk cek deployment ────────────────────────────────────
function doGet(e) {
  return jsonResponse({
    status:       "ok",
    message:      "Rekap CS Apps Script aktif",
    sheet_nabila: SHEET_NABILA,
    sheet_others: SHEET_OTHERS !== "GANTI_DENGAN_ID_SPREADSHEET_BARU"
                  ? "configured" : "BELUM DIISI",
    cs_routing:   CS_SHEET_MAP,
    tab_prefix:   TAB_PREFIX,
    drive_folder: DRIVE_FOLDER_NAME,
  });
}

