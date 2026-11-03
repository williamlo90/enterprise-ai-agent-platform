# Backend platform

BE-01 menyediakan paket FastAPI yang dapat diinstal dan dijalankan. Health/error contracts mengikuti BE-02; composition dan modul bisnis mengikuti BE-03 serta tugas berikutnya. Belum ada DB, autentikasi, RAG, worker, atau endpoint reimbursement.

## Persiapan

Gunakan Python 3.13 dan uv 0.11.26. Versi Python lain belum masuk rentang kompatibilitas proyek. Jika uv belum tersedia, pasang pada environment tooling lokal dari root repository:

```powershell
python -m venv local/tooling
.\local\tooling\Scripts\python.exe -m pip install uv==0.11.26
```

Selanjutnya gunakan `.\local\tooling\Scripts\uv.exe` sebagai pengganti `python -m uv` pada perintah di bawah jika uv tidak tersedia pada interpreter utama. Tooling dan virtualenv diabaikan Git.

## Instalasi dan menjalankan

Dari root repository:

```powershell
python -m uv sync --locked --project backend
python -m uv run --locked --project backend enterprise-ai-api --env-file .env.example --check-config
python -m uv run --locked --project backend enterprise-ai-api --env-file .env.example
```

Buka `http://127.0.0.1:8000/docs`; metadata OpenAPI ada di `/openapi.json`. Endpoint bisnis dan `/health/live` belum tersedia. `/` mengembalikan 404. Hentikan server dengan Ctrl+C. Swagger UI memuat asset dokumentasinya melalui CDN; `/openapi.json` dapat diperiksa langsung tanpa UI tersebut.

Untuk konfigurasi lokal, salin `.env.example` menjadi `.env` jika file tersebut belum ada, kemudian jalankan tanpa `--env-file`. File `.env` lokal tidak masuk Git. `.env` hanya dicari di working directory saat menjalankan command; tidak berjalan naik ke parent atau bergantung pada letak package.

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
python -m uv run --locked --project backend enterprise-ai-api
```

## Konfigurasi

Ketiga variabel wajib tersedia dari file terpilih atau environment proses. Environment proses menang atas file, termasuk nilai kosong yang kemudian ditolak. `--no-env-file` menonaktifkan pembacaan dotenv; `--env-file PATH` eksplisit wajib menunjuk file yang dapat dibaca. Keduanya tidak dapat dipakai bersamaan.

| Nama | Nilai |
| --- | --- |
| APP_ENV | `development`, `test`, atau `production` |
| APP_HOST | IP literal IPv4/IPv6 tanpa scope suffix; default contoh loopback. URL dan hostname seperti `localhost` ditolak. |
| APP_PORT | Integer 1–65535; tidak menerima boolean atau angka pecahan. |

Tidak ada default diam-diam untuk variabel wajib. Kesalahan konfigurasi mengeluarkan exit 2 dengan nama field, tanpa nilai input/file contents. Mode test/production mematikan docs/OpenAPI; debug selalu false. Ini bootstrap, bukan deployment production yang sudah diamankan. Access log bawaan Uvicorn dimatikan karena path/query dapat berisi data privat; request ID dan logging terstruktur mengikuti BE-02.

## Pengujian dan build

```powershell
python -m uv run --locked --project backend pytest backend/tests
python -m uv build --project backend --out-dir local/be01-dist
```

`uv.lock` mengunci dependency aplikasi dan development. Build backend `uv_build` juga dipin pada pyproject. Perubahan dependency harus memperbarui lock dan menjalankan checks relevan. `.gitkeep` masih menandai direktori fitur yang belum diimplementasikan.

Verifikasi BE-01 menghasilkan **20 tes lulus**, termasuk konfigurasi, isolasi app, CLI failure dan startup HTTP aktual. Instalasi wheel non-editable dari salinan bersih dan reproduksi QA independen juga lulus; bukti serta batasnya tercatat pada [task plan](../docs/backend/task_plan.md#11-hasil-pelaksanaan-be-01). Tes bootstrap tidak meluluskan API/DB/security seluruh produk.
