# Arsitektur backend

Status: rancangan implementasi bertahap. BE-01/02 menyediakan bootstrap API/config dan HTTP boundary; BE-03 memasang composition/service/port health. DB-01 menambahkan adapter koneksi/transaksi PostgreSQL dan probe opt-in; pengujian DB nyata dan QA independen lulus. Komponen bisnis, schema/RLS, worker dan service referensi belum diimplementasikan. Acuan: [kontrak produk](../../PRODUCT_CONTRACT.md), [arsitektur sistem](../../ARCHITECTURE.md), [kontrak data/API](../../DATA_AND_API_CONTRACTS.md), [test plan](../../TEST_PLAN.md). Dokumen pendamping: [database schema](database_schema.md) dan [task plan](task_plan.md).

## 1. Unit executable dan tanggung jawab

| Unit | Package / entry point | Tanggung jawab |
| --- | --- | --- |
| Platform API | `backend/src/enterprise_ai_platform/main.py` | Lifecycle HTTP, public/internal routers, identity/CSRF, dependency injection, request limits, pemanggilan application services |
| Platform worker | `backend/src/enterprise_ai_platform/worker.py` | Claim jobs, ingest, agent execution, dispatch dan reconciliation; memakai application services platform yang sama |
| Reference API | `reference_service/src/reimbursement_service/main.py` | Trips, rule packages, service operations, claim state/stages/revisions dan keputusan; authoritative business store |
| Operator CLI/scripts | Package/entry point scoped pada unit terkait | Migration, seed fiktif, probes, recovery; tidak menjadi jalur admin tanpa audit |

Tidak menambah microservice untuk setiap modul. API/worker berbagi kode bisnis platform, sedangkan reference API tidak mengimpor source platform atau berbagi ORM/database credential. Manifest, lockfile, config dan migration masing-masing unit build disiapkan ketika bootstrap; pin versi dari compatibility check, bukan menjalankan semua package dari satu environment global.

## 2. Aturan dependency dan komposisi

Dalam modul, mulai dengan file domain/service/ports yang benar-benar diperlukan. Jangan membuat seluruh hierarki class kosong. Arah import:

```text
main / worker / composition
       -> api transport -> application service -> domain + ports
       -> infrastructure / integrations -> implementasi ports
```

Domain/ports tidak mengimpor FastAPI, SQL driver, LangGraph, provider SDK, atau module-global credential. Application service menerima port/repository/UoW; adapter memasok implementasi. Route memvalidasi bentuk request dan memanggil service, bukan memegang SQL atau retry. Infrastructure tidak memanggil route. Composition root memasang adapter, resource lifecycle, konfigurasi dan execution policies; aplikasi berbeda tidak berbagi mutable actor/session state.

`composition.py` adalah lokasi wiring API/worker; `config.py` memvalidasi config tersanitasi. Resources yang dibagi adalah pool/client/thread-safe immutable config, bukan transaksi/actor. Dependency request/job memberi ActorContext, tenant, correlation dan UoW scoped. Runtime dan migration mempunyai config serta credential berbeda; secret tidak ditampilkan lewat repr/error/log.

Modul lain hanya memanggil public service/port. Import repository/private domain atau tabel modul lain dilarang. Application coordinator dapat memakai beberapa public module ports dalam satu UoW platform, sehingga transaksi lintas modul internal tetap atomik tanpa route menyentuh tabel. Guard dependency diuji terhadap pelanggaran nyata, bukan sekadar mirror struktur direktori.

## 3. Modul platform dan public ports

| Modul | Kepemilikan | Public service/port dan batas |
| --- | --- | --- |
| identity | Principals, sessions, membership roles dan grant receipts | verify session/OIDC identity, revoke, current actor; tenancy memberi scope tenant, bukan role dari prompt |
| tenancy | Tenant registry dan config terbatas | resolve allowed tenant, config versions, quota policy; tidak otomatis pembaca konten |
| files | File metadata/versions/pins/readiness/deletion | upload status, pin/finalise, content authorization; bytes melalui storage port |
| knowledge | Document/versions/ACL/chunks/index pointer | ingest, publish, withdraw, source lookup/search; memanggil FilesService, bukan tabel file |
| conversations | Conversation/message sequencing dan feedback | owner-scoped read/append/filter; read memakai provenance access service |
| agent | Run dan state koordinasi/tool registry | start/resume/run lifecycle, bounded model operations; tidak memiliki authority approval |
| policies | Paket aturan, rentang tanggal dan eligibility | prepare/activate/withdraw/eligibility; dokumen+rules digabung immutable, eligibility mutable |
| drafts | Editable drafts dan immutable draft versions | prepare/patch/preview/freeze; owns item/receipt snapshot validation |
| actions | Consent, human_action_receipts, actions/outbox orchestration | confirm/record decision/dispatch/reconcile/seal; owns stable operation identity |
| audit | Append-only redacted event records | record/read scoped event; bukan tempat menyimpan receipt/prompt lengkap |

Modul fondasi `system` sudah diimplementasikan untuk health: ProbeResult dataclass, BootstrapReadiness/HealthServicePort Protocol, serta HealthService. RuntimeReadiness adapter dimiliki ApplicationResources per app; ApplicationServices memasang HealthService. Route health memakai dependency ke port. Invariant bisnis tenant/DB/worker dalam tabel lain tetap target tugas berikutnya, bukan ditutup oleh wiring health.

Guard AST pada `scripts/checks/check_architecture.py` menegakkan import layer termasuk relative/from-package forms dan menolak dynamic import/wildcards. Source API tidak mengimpor composition/adapters; config independen; cross-package platform/service ditolak. Guard ini pemeriksaan static dependency, bukan security sandbox untuk kode Python arbitrer. Unit negatif dan tes substitusi service mengecek batas tersebut; aturan berkembang dengan layer yang diotorisasi.

`integrations/identity` mengimplementasikan OIDC; `integrations/models` chat/tool/embedding/rerank ports; `integrations/reimbursement` HTTP client dan delegation; `infrastructure/database` pool/UoW/repository adapters; `infrastructure/checkpoints` persistent fenced adapter; `infrastructure/jobs` leasing scheduler; `infrastructure/storage` private bytes; `infrastructure/observability` request/trace/usage; `infrastructure/security_journal` external durable journal port. Penempatan repository implementation per modul dapat memakai subpackage adapter modul, tetapi wiring dan boundary yang sama wajib dipertahankan dan layout diperbarui bila ditambah.

## 4. Reference service

| Modul | Tanggung jawab |
| --- | --- |
| access | Trusted caller/delegation/current actor check; claim active-stage/owner relation; tidak mempercayai browser cookie |
| trips | Trip dan expense item source; seed fiktif scoped, tanpa free model write |
| policies | Immutable copied package/digest; current eligibility melalui platform port |
| claims | Validasi nominal/item/deadline, revisions, receipt bindings, stage transitions, expense reservations |
| operations | Idempotency ledger, canonical hash, commit/seal identity lock, result lookup |
| audit | Audit state/decision atomik bersama hasil bisnis |

HTTP `integrations/platform` memakai scope berbeda untuk actor verification, policy eligibility, pin/finalise receipt. Tidak ada callback recursion: metadata/pin/eligibility platform tidak memanggil claim access-check balik. Download user melalui platform boleh memakai claim access-check service yang memverifikasi identity ke platform. Kedua jalur memakai endpoint berbeda dan budget timeout terbatas.

## 5. Identity, otorisasi, dan trust boundary

ActorContext berasal dari sesi server-side atau credential service+delegasi tervalidasi. Isi: principal, active tenant/membership, access_revision, operation scope, human-action receipt bila diperlukan, correlation. Semua diambil/diverifikasi server; model tidak dapat memilih tenant/role atau menghasilkan human grant.

OIDC code+PKCE, issuer/audience/signature/expiry/state/nonce dan redirect allowlist diperiksa; cookie opaque Secure/HttpOnly/SameSite, CSRF pada mutasi. Callback/nonce/session lifetime dikunci pada AUTH-01. Tenant switch hanya membership aktif dan tidak mengadopsi run. Logout/revocation tidak menunggu token model atau restart API.

Signed delegation mengikat audience/caller, actor/tenant, action/object/version, request hash, operation ID, jti dan expiry. Key rotation serta replay checks dibangun pada AUTH-04. Current membership/policy/file/claim relationship diperiksa lagi pada boundary yang relevan; signed snapshot bukan izin abadi. API approval hanya menerima aksi manusia terautentikasi; tool registry tidak memasang grant keputusan. Hanya identity service yang berwenang menyatakan current grants, tetapi service reimbursement tetap menegakkan own object/state/anti-self-approval.

RLS runtime tenant+actor-context bukan owner/BYPASSRLS; worker job membawa context scoped dan harus revalidate, bukan trusted tenant dari job payload bebas. DB tenant FK melengkapi application authorization. Principal/session global tables dibatasi repository/role khusus; tidak memberi runtime akses seluruh identity contents tanpa scope.

## 6. Transaksi, idempotency, dan concurrency

### Transaksi platform

ActionCoordinator memakai UoW untuk mengunci draft/version, cek policy/akses/bukti, merekam consent+human_action_receipt, membekukan draft version, membuat action snapshot dan outbox lalu audit. Commit hanya setelah semuanya konsisten. DecisionCoordinator membuat human receipt+decision action/outbox tanpa draft_id palsu. Form manual dan assistant memakai coordinator yang sama; chat tidak menjadi consent.

Request_hash per jenis operasi mencakup semua field material. Untuk keputusan mencakup decision, reason, claim, expected_version, revision, stage. Receipt dan delegation memakai hash sama. Draft beku tidak diedit/resubmit selama queued/sending/unknown. Worker memeriksa izin terkini sebelum send baru. Lock DB tidak ditahan selama memanggil model/network.

### Transaksi reference service

Operation identity dibuat/dikunci dengan key+hash. Actor/policy/receipt checks melalui HTTP dilakukan di luar transaksi panjang, lalu final checks/version di boundary sebelum transaksi commit singkat. Claim/revision/stage/item-reservations+operation result+audit dicatat bersama. Idempotent replay hasil lama tidak membuat mutasi baru dan tetap memerlukan reotorisasi akses hasil.

Commit dan seal-if-absent menggunakan serialization identity yang sama. Lookup 404 bukan bukti final: request lama mungkin masih berjalan. Seal menulis tombstone bila belum commit atau mengembalikan committed result. Request lama sesudah tombstone ditolak. Setelah definitive no-commit, platform fences outbox attempt lama sebelum membuka koreksi. Tidak menyebut jaringan exactly-once; ledger+constraints menjamin satu hasil bisnis pada tindakan yang diuji.

Claim mutations memakai expected_version+active revision/stage; satu keputusan menang. Needs-info revision reset seluruh approval route pada paket terkunci. Expense reservation item unik melindungi duplicate business request dengan key berbeda. Paket withdrawn/known-conflict menahan submit/info/decision melalui live policy eligibility; tidak mengubah status claim menjadi rejected secara otomatis.

### Lease dan run writer

Job claim atomik dengan generation monotonik, lease_until, heartbeat dan attempt budget. Semua checkpoint/intermediate write/publication/job completion memeriksa generation yang masih sah. Satu run/conversation state writer; concurrent resume diserialkan atau 409. Lease hilang menutup writes worker lama; in-flight HTTP tetap memakai snapshot/key yang sama dan dilindungi service. Race test wajib mencakup worker lama kembali setelah takeover, bukan hanya happy-path heartbeat.

## 7. File, receipt, rules, dan provenance

File immutable per version/digest; quarantine/parser/scan dibatasi resource dan tidak memiliki egress bebas. Ingestion idempotent per source version dan index version. Publish pointer/ACL atomik hanya setelah index siap; withdrawal/revocation menutup pembacaan/pemakaian berikutnya sebelum cleanup cache/index.

PinReceiptCoordinator memverifikasi owner/tenant/consent/readiness/digest dan membuat pin idempotent operation+candidate claim. Pin dipertahankan saat unknown dan difinalisasi setelah committed atau sealed-no-commit proof. Penghapusan tidak mengandalkan TTL untuk file yang dipakai claim. Content download mengecek owner atau current active reviewer via service, file ACL/state, dan visibility revision; approver role umum tidak cukup. Tidak ada OCR receipt otomatis pada MVP.

Policy publication mengunci rentang tenant dan memasangkan doc version/rules digest → service ack → activate eligible mapping. Konten immutable; operational eligibility revision berbeda. Tanggal selesai perjalanan menentukan paket, deadline memakai timezone dan waktu penerimaan service. Penarikan eligibility tersimpan dan diverifikasi, bukan menganggap cache package selamanya sah. Tidak ada migration retroaktif claim pada MVP.

Provenance bertipe document/trip/claim/receipt/tool result dengan source system/resource/version/required relation. Model input, history, cache, checkpoint resume dan buffered output memeriksa izin terkini, termasuk hilangnya reviewer access karena stage bergerak. Source inaccessible atau verifier down menyebabkan filtering/hold, bukan penggunaan snapshot lama. Snapshot jawaban lama yang tetap sah menandai sumber superseded, tidak mengganti citation menjadi versi baru palsu.

## 8. Resource lifecycle dan pengoperasian

API dan worker lifecycle membuka/menutup pool dan HTTP client; startup fail safe pada konfigurasi/dependency wajib, health liveness process dipisahkan readiness dependency. DB UoW memakai transaction-local context; success/error/cancel/timeout membersihkan context. Migration memakai direct/privileged role terpisah dan tidak berjalan bersamaan pada semua replica. Compatibility tests pooled/direct/checkpoint menjadi gate sebelum adapter dipakai.

Job parsing/model/dispatch mempunyai input/time/step/token/retry limits, fair tenant quotas, backoff/jitter, dan dead/held state yang dapat diperiksa. Tidak retry validation/permission denial sebagai error sementara. Unknown commit direkonsiliasi dengan key tetap; provider fallback tidak melakukan mutasi baru atau memperluas hak. Rate/cost budgets tercatat per tenant/run; nilai awal aman sebelum fitur dan nilai final sebelum acceptance.

Trace default metadata/correlation/source/action/version/usage/error tersanitasi. Tidak log credential, cookie, receipt body, raw user prompt atau respons tool sensitif tanpa diagnostik scoped eksplisit. Retention/residency config wajib sebelum data sensitif; fixtures awal fiktif. Backup/recovery mengikuti root architecture: worker/outbound hold, reconcile ledger/file/policy, external security-journal watermark dan post-backup intents, reapply revocation/deletion, lalu buka scope yang terbukti lengkap. Journal independen bukan tabel pada snapshot yang sama.

## 9. Bentuk kode dan pengujian

- Package API tests: route/schema/access, error/request ID/body limit; domain unit rules tidak memerlukan provider.
- Repository integration: PostgreSQL/runtime role nyata, migration upgrade/constraints/RLS, action/claim concurrent transactions.
- HTTP integration: service nyata dan fault-injection server/proxy terpisah; actual provider evidence tidak digantikan mock.
- Checkpoint adapter tests: read/write/list/history/resume/intermediate, provenance filtering dan fencing.
- Root tests: lintas web/platform/service security, workflow dan resilience; evaluations terpisah dari seed operasional.

Schema rinci dan fase migration ada pada database_schema.md; task_plan.md memberikan ID/dependency/kriteria selesai dan test family yang mengikatnya. Dokumentasi teknis adalah target perilaku; tidak ada fitur atau hasil runtime dinyatakan lulus dari desain ini.
