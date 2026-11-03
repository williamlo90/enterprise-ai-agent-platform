# Backend platform

BE-01 menyediakan paket FastAPI yang dapat diinstal dan dijalankan. BE-02 menambahkan health/readiness bootstrap, error envelope, request ID dan batas input. BE-03 memasang composition root, health service/ports dan dependency wiring. DB-01 menyediakan koneksi PostgreSQL opsional dan telah lulus tes offline/live serta QA independen. DB-02 menyediakan foundation identity/config migration yang sudah terpasang. DB-03 memasang role runtime terbatas, context transaksi dan RLS fondasi. Schema fitur bisnis, autentikasi, RAG, worker, dan endpoint reimbursement belum tersedia.

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

`scripts/checks/check_architecture.py` memeriksa import AST source aplikasi termasuk relative imports, package imports, cross-module private imports dan mekanisme dynamic import. Driver database stdlib (`sqlite3`, `_sqlite3`, `dbm`, `shelve`) ditolak pada API/domain/ports/service/config sebelum whitelist stdlib diterapkan; adapter tetap boleh memakainya. Dynamic importlib/builtins, wildcard import serta eval/exec/__import__ tidak diizinkan. Pengecualian sempit `from importlib.resources import files` hanya pada infrastructure untuk membaca SQL paket; tidak membuka dynamic import. Guard merupakan regression check arsitektur, bukan sandbox atau bukti bahwa seluruh bentuk introspeksi Python mustahil. Perubahan layer/adapter memerlukan review aturan guard; tidak mematikan check untuk meloloskan dependency baru.

Worker dan repository bisnis belum tersedia. DB-01 menambahkan lifecycle dan transaction context database; public service bisnis mengikuti task terkait.

BE-03 ditutup dengan **91 tes lulus**, termasuk build/install wheel bersih dan reproduksi QA independen. Temuan guard stdlib database sudah diperbaiki dan lulus recheck; catatan hasil ada pada [task plan BE-03](../docs/backend/task_plan.md#13-hasil-pelaksanaan-be-03).

## Koneksi database DB-01

Status: **selesai dan lulus QA**, dengan 139 tes offline dan 1 tes integrasi live Neon lulus. Bukti ada pada [hasil DB-01](../docs/backend/task_plan.md#14-hasil-pelaksanaan-db-01). Isi `.env` lokal pada root repository dengan URI dari database/branch pengujian khusus, memakai data fiktif:

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

## Migration dan foundation schema DB-02

Status: **selesai dan lulus QA**, 166 tes termasuk dua integrasi live lulus; DB-02 diterima pada revision 0002; head aplikasi kini 0003 melalui DB-03. Wheel bersih dan 40 pemeriksaan migration live juga lulus. Bukti pada [hasil DB-02](../docs/backend/task_plan.md#15-hasil-pelaksanaan-db-02). Migration tidak dijalankan saat API startup. Gunakan Python/uv terkunci dari root repository; command hanya memuat `DATABASE_DIRECT_URL` secara internal, tanpa membutuhkan runtime URL.

```powershell
python -m uv run --locked --project backend enterprise-ai-migrate status --env-file .env
python -m uv run --locked --project backend enterprise-ai-migrate verify --env-file .env
python -m uv run --locked --project backend enterprise-ai-migrate upgrade --env-file .env
```

- `status` read-only: memeriksa revision/checksum dan melaporkan current/head, tanpa membuat schema atau ledger.
- `verify` menguji migration dan constraint pada schema acak `db02_probe_*` milik run tersebut, kemudian membersihkannya. Tidak menerapkan migration pada schema aplikasi. Pakai database pengujian khusus; kegagalan cleanup menggagalkan verifikasi.
- `upgrade` menerapkan seluruh revision tertunda pada **schema `platform`**, melalui direct connection dengan TLS. Menjalankan ulang pada head tidak mengubah data. Tidak ada endpoint publik untuk menjalankan migration.

Runner memakai SQL berversi di package `infrastructure/database/migrations`, bukan file SQL yang ditemukan dari working directory. Revision 0001 membuat principals, tenants, memberships, membership_roles, sessions, tenant_configs, security_policy_configs dan timezone_catalog; 0002 menambahkan indeks identity/session/grant. Ledger `platform.schema_migrations` menyimpan revision, nama, SHA256 dan waktu. Hash memakai SQL UTF-8 dengan newline LF agar source/wheel Windows/Linux konsisten.

Upgrade mengambil advisory transaction lock sebelum membaca history atau membuat objek. Schema, seluruh pending DDL dan ledger commit bersama; SQL gagal membuat seluruh batch rollback. History harus menjadi prefix persis dari revision paket; checksum/nama/revision asing atau berlubang ditolak. Schema existing tanpa ledger atau dengan ledger kosong ditolak; tidak diadopsi otomatis. Bootstrap CREATE tanpa IF NOT EXISTS menggagalkan race pembuatan namespace asing. Lock timeout 5 detik, statement/idle timeout 20 detik, keseluruhan koneksi+pekerjaan 60 detik, serta commit/rollback/close dengan budget cleanup terpisah 5 detik. SQL revision merupakan kode admin yang direview; hanya transactional SQL, tanpa pengendalian transaksi sendiri. Jangan menunggu API/model di dalam migration.

Timezone allowlist versi 1: `Asia/Jakarta`, `Asia/Singapore`, `UTC`, ditegakkan FK. Locale snapshot: `id`, `en`, atau keduanya tanpa duplikat. Config `schema_version=1`, nilai scalar penting dibatasi, dan snapshot menolak UPDATE/DELETE/TRUNCATE. Config baru dibuat sebagai snapshot baru lalu pointer tenant dipindahkan pada transaksi yang sama. FK pointer deferred menjaga initial tenant+snapshot sah, dan snapshot hilang/salah tenant ditolak saat commit. Identitas issuer/subject dibatasi byte agar unique index PostgreSQL tetap dapat menyimpan nilai yang sah.

Role memiliki allowlist enam nilai kontrak. `scope_kind`/`scope_ref` sementara hanya penyimpanan bounded; vocabulary/otorisasi scope wajib dikunci sebelum AUTH-03. Scope tak dikenal tidak boleh memberi izin. Hash token/CSRF disimpan sebagai teks bounded, tanpa memilih algoritme/lifetime sebelum AUTH-01. Tabel security policy menyimpan snapshot/draft dan struktur dasar JSON; kategori, validasi semantik processing policy, residency/retention lengkap dan activation gate tetap DATA-01. Foreign key sesi membuktikan relasi membership, bukan membership masih aktif; login/expiry/revoke checks tetap tugas AUTH.

**DB-02 belum meluluskan RLS, least privilege runtime, actor/tenant context, self-escalation rules, login atau fitur bisnis.** Itu DB-03/AUTH dan tugas fitur berikutnya. Jangan memakai schema fondasi untuk data sensitif sebelum kontrol tersebut selesai. Panduan menambah revision ada di [migration sources](migrations/README.md).

## Runtime identity, context dan RLS DB-03

Status: **selesai, terpasang pada revision 0003 dan lulus QA**. Suite lengkap **189 tes lulus**, termasuk tiga integrasi live. Wheel bersih memuat tiga revision SQL; 186 tes offline serta 40 migration checks dan 55 security checks live lulus. Bukti pada [hasil DB-03](../docs/backend/task_plan.md#16-hasil-pelaksanaan-db-03). PostgreSQL **16 atau lebih baru** diperlukan untuk membership `INHERIT TRUE, SET FALSE, ADMIN FALSE`.

Revision 0003 membuat access role NOLOGIN tanpa kepemilikan objek, registry role private (name+OID), serta ENABLE/FORCE RLS pada tabel identity/config private dan ledger. Policy runtime ditujukan ke access role khusus, bukan PUBLIC. Migration identity memiliki maintenance policy eksplisit; ia merupakan trusted administrator, bukan credential API. Helper SECURITY DEFINER hanya mengembalikan boolean, memakai qualified objects/fixed pg_catalog search_path, dan EXECUTE default PUBLIC dicabut. Runtime tidak dapat menjalankan private privilege-preflight helper.

Runtime LOGIN tidak mempunyai SUPERUSER/BYPASSRLS/CREATEROLE/CREATEDB/REPLICATION atau kepemilikan objek. Satu-satunya membership adalah access role yang direkam, dengan INHERIT TRUE dan SET/ADMIN FALSE; NOINHERIT pada atribut login tetap dipertahankan. Guard memeriksa hak efektif tabel **dan kolom**, grant options, fungsi, sequences, schema/database CREATE, ownership serta jalur membership. Akses ke sessions, security policies, ledger dan registry tidak diberikan. Database CONNECT diberikan eksplisit; default PostgreSQL PUBLIC TEMP dibiarkan tanpa perubahan database-wide. Objek temp tidak menjadi jalur ke helper karena seluruh referensinya qualified. Guard menolak ownership objek runtime/access role; aplikasi tidak membuat objek temporary/persistent sendiri.

| Data | Hak runtime foundation |
| --- | --- |
| principals | SELECT identitas actor sendiri ketika context/membership/principal/tenant aktif |
| tenants, tenant_configs | SELECT tenant dalam context yang sah |
| memberships, membership_roles | SELECT membership/role actor sendiri dalam tenant context |
| timezone_catalog | SELECT allowlist global yang tidak berisi data pengguna |
| sessions, security_policy_configs, schema_migrations, registry | Tidak ada akses tabel umum |
| Mutasi foundation, TRUNCATE, policy/helper/schema DDL | Tidak diberikan; policy runtime foundation juga SELECT-only |

Menambahkan ACL write saja tidak membuka perubahan akun/role: AUTH harus menyediakan operasi scoped dan policy yang direview. Probe memakai tabel fitur sementara dengan `USING/WITH CHECK` untuk menguji writes tenant/owner; grant itu hanya berada di schema uji, bukan tabel foundation aplikasi.

`TenantActorContext(tenant_id: UUID, actor_id: UUID)` berasal dari backend identity/policy yang tepercaya. `RuntimeDatabase.transaction(context)` memasang kedua UUID menggunakan transaction-local `set_config(..., true)`; tanpa context keduanya disetel kosong. Context tidak disimpan pada resource/global dan tidak diambil dari header/body publik. RLS memeriksa principal, tenant, dan membership masih aktif. Missing/invalid/partial context fail closed. Context hilang pada commit/rollback; failure/cancel/deadline membuang koneksi tidak pasti, dan checkout berikutnya memasang context baru atau kosong.

**Context bukan autentikasi.** Pemegang credential SQL runtime masih bisa mengirim GUC sendiri; AUTH harus membuktikan actor dan tenant sebelum adapter dipanggil. Model/agent/browser tidak memperoleh credential atau SQL tool. Ini fondasi isolasi DB, bukan kelulusan login, seluruh object policy, atau keamanan produk penuh.

Perintah dari root repository:

```powershell
# Probe live terisolasi: membuat role/schema acak dan data tenant fiktif, lalu membersihkan semuanya.
python -m uv run --locked --project backend enterprise-ai-security verify --env-file .env
# Setelah migration 0003 diterima dan diterapkan, buat serta verifikasi LOGIN terbatas.
python -m uv run --locked --project backend enterprise-ai-security provision --env-file .env
# Periksa credential runtime lokal melalui login sungguhan dan guard privilege.
python -m uv run --locked --project backend enterprise-ai-security check --env-file .env
```

Provisioning hanya memakai direct credential secara internal. Nama/OID existing yang tidak cocok registry tidak diadopsi. Guard privilege managed role dijalankan sebelum password atau grant lama diubah. Pengulangan normal mempertahankan password yang masih valid; tidak merotasi otomatis. LOGIN baru diverifikasi sungguhan sebelum hanya `DATABASE_URL` di `.env` ditulis melalui atomic replacement. `DATABASE_DIRECT_URL` dan pilihan `APP_DATABASE_ENABLED` tetap utuh. DATABASE_URL dari process environment dan perubahan file bersamaan menahan provisioning agar tidak menimpa konfigurasi pengguna.

DB dan file lokal bukan satu transaksi. Jika pembuatan role berhasil tetapi penyimpanan lokal gagal, setelah memperbaiki akses file gunakan recovery **eksplisit** berikut. Ia memvalidasi registry serta privilege terlebih dahulu, kemudian merotasi hanya LOGIN milik proyek dan menyimpan credential lokal yang baru:

```powershell
python -m uv run --locked --project backend enterprise-ai-security provision --rotate --env-file .env
```

Jangan gunakan recovery untuk role dengan privilege tak aman; preflight menolak sebelum credential diubah. Provision bukan bagian startup atau perintah rutin deploy. Neon memerlukan input password plaintext melalui SQL; input dikirim hanya lewat TLS verify-full, tidak dikembalikan lewat CLI/log/chat. Role dibuat melalui SQL agar tidak otomatis mendapat neon_superuser. Rujukan: [Neon role management](https://neon.com/docs/manage/roles), [PostgreSQL role membership](https://www.postgresql.org/docs/16/role-membership.html), [row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html).
