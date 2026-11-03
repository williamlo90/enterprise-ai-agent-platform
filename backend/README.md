# Backend platform

BE-01 menyediakan paket FastAPI yang dapat diinstal dan dijalankan. BE-02 menambahkan health/readiness bootstrap, error envelope, request ID dan batas input. Composition serta modul bisnis mengikuti BE-03 dan tugas berikutnya. Belum ada DB, autentikasi, RAG, worker, atau endpoint reimbursement.

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

Buka `http://127.0.0.1:8000/docs`; metadata OpenAPI ada di `/openapi.json`. Health tersedia di `/health/live` dan `/health/ready`. Endpoint bisnis belum tersedia; `/` mengembalikan 404 dengan error envelope. Hentikan server dengan Ctrl+C. Swagger UI memuat asset dokumentasinya melalui CDN; `/openapi.json` dapat diperiksa langsung tanpa UI tersebut.

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
| APP_MAX_REQUEST_BODY_BYTES | Opsional, default 1048576 (1 MiB), rentang 1–16777216 byte. Dihitung dari byte aktual termasuk chunked, bukan hanya header. |
| APP_REQUEST_BODY_TIMEOUT_SECONDS | Opsional, default 10, rentang 1–60 detik untuk menerima seluruh body. |

Tidak ada default diam-diam untuk tiga variabel wajib. Kesalahan konfigurasi mengeluarkan exit 2 dengan nama field, tanpa nilai input/file contents. Mode test/production mematikan docs/OpenAPI; debug selalu false. Ini fondasi API, bukan deployment production yang sudah diamankan. Access log bawaan Uvicorn dimatikan karena path/query dapat berisi data privat. CLI mengaktifkan log metadata request_id/status/durasi dan error tersanitasi; host ASGI lain bertanggung jawab mengatur logging-nya tanpa mengaktifkan raw access log.

## Kontrak HTTP BE-02

- `GET /health/live` → 200 `{"status":"ok","scope":"process"}`.
- `GET /health/ready` → 200 `{"status":"ready","scope":"bootstrap"}` setelah startup. Sebelum siap/saat ditutup, respons 503 envelope. Belum memeriksa database, model, OIDC atau storage; dependency checks ditambahkan ketika komponennya dibangun.
- Setiap request HTTP yang mencapai app mendapat UUID baru dari server pada `X-Request-ID`; input header dari klien diabaikan. Error memakai ID yang sama di body dan header. Rejected HTTP pada layer parser/server sebelum ASGI berada di luar envelope aplikasi.
- Error berbentuk `{"error":{"code":"not_found","message":"Resource not found.","request_id":"...","retryable":false}}`. Detail exception, field values, body/query/header privat tidak ditampilkan. HTTPException memakai pesan aman terdaftar, bukan detail bebas.
- Malformed JSON → 400; input validation → 422; body melebihi limit → 413; menerima body melewati deadline → 408; unhandled failure sebelum response start → 500. Setelah headers terkirim, respons tidak diganti JSON kedua; koneksi/stream gagal dengan exception tersanitasi.
- `RequestModel` untuk model input berikutnya menolak field tambahan, memakai tipe strict dan string maksimum 4096 karakter. List/numeric/domain limits harus ditetapkan per endpoint saat dibangun; ini bukan klaim seluruh model masa depan otomatis aman.
- Body dibuffer hingga limit sebelum route dijalankan. Upload file belum tersedia; FILE-01 harus menetapkan batas upload yang sesuai (satu file maksimum 10 MiB) dan menguji limit transport serta pemeriksaan file, bukan diam-diam menonaktifkan batas ini.

## Pengujian dan build

```powershell
python -m uv run --locked --project backend pytest backend/tests
python -m uv build --project backend --out-dir local/be01-dist
```

`uv.lock` mengunci dependency aplikasi dan development. Build backend `uv_build` juga dipin pada pyproject. Perubahan dependency harus memperbarui lock dan menjalankan checks relevan. `.gitkeep` masih menandai direktori fitur yang belum diimplementasikan.

Verifikasi BE-01 menghasilkan **20 tes lulus**, termasuk konfigurasi, isolasi app, CLI failure dan startup HTTP aktual. Instalasi wheel non-editable dari salinan bersih dan reproduksi QA independen juga lulus; bukti serta batasnya tercatat pada [task plan](../docs/backend/task_plan.md#11-hasil-pelaksanaan-be-01). Tes bootstrap tidak meluluskan API/DB/security seluruh produk.

BE-02 memperluas suite menjadi **58 tes lulus**, termasuk batas body/chunked, error sanitisation, request ID, health/readiness, dan failure setelah headers. Instalasi wheel bersih serta reproduksi QA independen lulus. Bukti dan batas kelulusannya ada pada [hasil BE-02](../docs/backend/task_plan.md#12-hasil-pelaksanaan-be-02).
