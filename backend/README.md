# Backend platform

BE-01 menyediakan paket FastAPI yang dapat diinstal dan dijalankan. BE-02 menambahkan health/readiness bootstrap, error envelope, request ID dan batas input. BE-03 memasang composition root, health service/ports dan dependency wiring. DB-01 menyediakan koneksi PostgreSQL opsional dan tes offline; penerimaan database nyata menunggu credential lokal. Belum ada schema bisnis, RLS, autentikasi, RAG, worker, atau endpoint reimbursement.

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
- `GET /health/ready` → 200 `{"status":"ready","scope":"bootstrap"}` setelah startup. Sebelum siap/saat ditutup, respons 503 envelope. Saat database diaktifkan, memeriksa koneksi aktual; outage menghasilkan 503. Model, OIDC, storage dan schema/RLS belum diperiksa.
- Setiap request HTTP yang mencapai app mendapat UUID baru dari server pada `X-Request-ID`; input header dari klien diabaikan. Error memakai ID yang sama di body dan header. Rejected HTTP pada layer parser/server sebelum ASGI berada di luar envelope aplikasi.
- Error berbentuk `{"error":{"code":"not_found","message":"Resource not found.","request_id":"...","retryable":false}}`. Detail exception, field values, body/query/header privat tidak ditampilkan. HTTPException memakai pesan aman terdaftar, bukan detail bebas.
- Malformed JSON → 400; input validation → 422; body melebihi limit → 413; menerima body melewati deadline → 408; unhandled failure sebelum response start → 500. Setelah headers terkirim, respons tidak diganti JSON kedua; koneksi/stream gagal dengan exception tersanitasi.
- `RequestModel` untuk model input berikutnya menolak field tambahan, memakai tipe strict dan string maksimum 4096 karakter. List/numeric/domain limits harus ditetapkan per endpoint saat dibangun; ini bukan klaim seluruh model masa depan otomatis aman.
- Body dibuffer hingga limit sebelum route dijalankan. Upload file belum tersedia; FILE-01 harus menetapkan batas upload yang sesuai (satu file maksimum 10 MiB) dan menguji limit transport serta pemeriksaan file, bukan diam-diam menonaktifkan batas ini.

## Pengujian dan build

```powershell
python -m uv run --locked --project backend pytest backend/tests
python -m uv build --project backend --out-dir local/be01-dist
python scripts/checks/check_architecture.py
```

`uv.lock` mengunci dependency aplikasi dan development. Build backend `uv_build` juga dipin pada pyproject. Perubahan dependency harus memperbarui lock dan menjalankan checks relevan. `.gitkeep` masih menandai direktori fitur yang belum diimplementasikan.

Verifikasi BE-01 menghasilkan **20 tes lulus**, termasuk konfigurasi, isolasi app, CLI failure dan startup HTTP aktual. Instalasi wheel non-editable dari salinan bersih dan reproduksi QA independen juga lulus; bukti serta batasnya tercatat pada [task plan](../docs/backend/task_plan.md#11-hasil-pelaksanaan-be-01). Tes bootstrap tidak meluluskan API/DB/security seluruh produk.

BE-02 memperluas suite menjadi **58 tes lulus**, termasuk batas body/chunked, error sanitisation, request ID, health/readiness, dan failure setelah headers. Instalasi wheel bersih serta reproduksi QA independen lulus. Bukti dan batas kelulusannya ada pada [hasil BE-02](../docs/backend/task_plan.md#12-hasil-pelaksanaan-be-02).

## Composition dan batas dependency BE-03

`main.create_app(settings, services=...)` membuat resources baru untuk setiap app dan menerima substitusi ApplicationServices untuk pengujian. `composition.py` memasang HealthService pada RuntimeReadiness melalui port. Lifespan mengaktifkan readiness dan selalu menonaktifkannya saat keluar. Route memakai FastAPI Depends ke HealthServicePort, tidak membaca resource adapter secara langsung. Container service immutable; readiness mutable hanya dimiliki satu aplikasi.

Modul system memakai dataclass dan Protocol stdlib. Domain/ports tidak mengimpor transport, SDK, config atau DB; service menggunakan domain/ports dan public service/port modul lain. API hanya memakai transport serta public module service/port. Adapters memakai domain/ports dan tidak mengimpor route atau composition. Config terpisah dari application wiring. Reference service tidak mengimpor source platform, begitu pula sebaliknya.

`scripts/checks/check_architecture.py` memeriksa import AST source aplikasi termasuk relative imports, package imports, cross-module private imports dan mekanisme dynamic import. Driver database stdlib (`sqlite3`, `_sqlite3`, `dbm`, `shelve`) ditolak pada API/domain/ports/service/config sebelum whitelist stdlib diterapkan; adapter tetap boleh memakainya. Importlib/builtins, wildcard import serta eval/exec/__import__ tidak diizinkan dalam source yang diperiksa. Guard merupakan regression check arsitektur, bukan sandbox atau bukti bahwa seluruh bentuk introspeksi Python mustahil. Perubahan layer/adapter memerlukan review aturan guard; tidak mematikan check untuk meloloskan dependency baru.

Worker dan repository bisnis belum tersedia. DB-01 menambahkan lifecycle dan transaction context database; public service bisnis mengikuti task terkait.

BE-03 ditutup dengan **91 tes lulus**, termasuk build/install wheel bersih dan reproduksi QA independen. Temuan guard stdlib database sudah diperbaiki dan lulus recheck; catatan hasil ada pada [task plan BE-03](../docs/backend/task_plan.md#13-hasil-pelaksanaan-be-03).

## Koneksi database DB-01

Status: implementasi dan tes offline; **penerimaan database nyata menunggu credential**. Isi `.env` lokal pada root repository dengan URI dari database/branch pengujian khusus, memakai data fiktif:

| Nama | Penggunaan |
| --- | --- |
| `APP_DATABASE_ENABLED` | Default `false`. Ubah ke `true` untuk API dengan dependency DB wajib; konfigurasi/koneksi gagal menggagalkan startup. |
| `DATABASE_URL` | URI runtime pooled. Neon: endpoint `-pooler`. |
| `DATABASE_DIRECT_URL` | URI direct untuk probe/migration saja. Neon: endpoint tanpa `-pooler`, database/endpoint dasar sama. API tidak memuat field credential direct. |

Jangan menaruh URI bercredential di command line, Git atau chat. User runtime/direct boleh berbeda; least privilege dan RLS merupakan DB-03. Parser menerima PostgreSQL hostname tunggal, username/password/dbname, dengan query hanya `sslmode=require`/`verify-full` dan `channel_binding=prefer`/`require`. Koneksi selalu memakai **verify-full**, CA certifi dan pemeriksaan hostname. Opsi pengalihan koneksi/pelemahan TLS ditolak. Pairing mendukung endpoint sama atau bentuk Neon `-pooler`; provider dengan hostname/port pooling berbeda membutuhkan aturan pairing tersendiri.

Pool runtime dibuka/ditutup oleh lifespan, min 0/maks 2 koneksi per aplikasi, maksimal 8 penunggu. Startup menguji koneksi dan TLS sungguhan; membuka pool kosong saja tidak dianggap siap. Prepared statements dinonaktifkan untuk transaction pooling.

`RuntimeDatabase.transaction()` menyediakan commit saat sukses dan rollback saat exception. Checkout termasuk connection check dibatasi 5 detik; seluruh transaksi 15 detik; commit/rollback/cleanup masing-masing dibatasi 5 detik. Driver cancellation dan cleanup dapat menambah waktu setelah deadline kerja. Timeout/cancel/error SQL membuang koneksi yang mungkin tidak aman, lalu pool menggantinya. Statement timeout 5 detik, lock timeout 2 detik dan idle-in-transaction 10 detik dipasang transaction-local. Actor/tenant context belum ada. Jangan menunggu model/HTTP call di dalam transaksi.

Validasi konfigurasi saja, tanpa koneksi atau SQL:

```powershell
python -m uv run --locked --project backend enterprise-ai-db-check --env-file .env
```

Setelah credential siap, uji nyata dengan `--live` di bawah. Probe membuat tabel unik `public.db01_probe_*`, memberi SELECT/INSERT ke runtime user, lalu menghapusnya pada cleanup. Direct user harus dapat CREATE/GRANT/DROP di database pengujian itu. Kegagalan cleanup menggagalkan probe; periksa tabel berprefix tersebut secara lokal sebelum mengulang.

```powershell
python -m uv run --locked --project backend enterprise-ai-db-check --env-file .env --live
```

Probe memeriksa TLS runtime/direct, commit yang terlihat melalui direct, rollback, SQL error lalu reuse, cancellation, deadline, shutdown dan penghapusan tabel. Output hanya status tersanitasi, tanpa URI/SQL input/exception driver mentah. `APP_DATABASE_ENABLED` boleh tetap `false` untuk probe. Kelulusannya tidak membuktikan tenant isolation, migration framework atau performance produksi.

Suite biasa selalu memakai konfigurasi fiktif dan **melewati live test**. Untuk memasukkan live test ke pytest setelah `.env` siap:

```powershell
$env:RUN_DATABASE_TESTS = '1'
python -m uv run --locked --project backend pytest backend/tests
Remove-Item Env:RUN_DATABASE_TESTS
```

CLI API/probe memilih SelectorEventLoop pada Windows karena psycopg async tidak mendukung Proactor. Host ASGI lain harus menggunakan loop kompatibel. Liveness tetap process-only; readiness melakukan ping ketika DB aktif. Migration tidak berjalan otomatis pada startup API. Credential migration tidak menjadi resource runtime.

Rujukan teknis: [psycopg async/Windows](https://www.psycopg.org/psycopg3/docs/advanced/async.html), [psycopg pool lifecycle](https://www.psycopg.org/psycopg3/docs/advanced/pool.html), dan [PostgreSQL TLS verification](https://www.postgresql.org/docs/current/libpq-ssl.html). TLS diperiksa pada koneksi client melalui libpq; statistik koneksi backend di balik pooler tidak dipakai sebagai bukti TLS client.
