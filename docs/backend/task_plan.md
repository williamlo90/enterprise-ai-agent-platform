# Backend task plan

Status: **BE-01, BE-02, dan BE-03 selesai dan lulus QA — 3 November 2026**. Tugas implementasi lain **belum mulai**. Skeleton dan dokumen bukan bukti bahwa fitur lain dalam tabel telah selesai. Acuan [backend architecture](architecture.md), [database schema](database_schema.md), [PRODUCT_CONTRACT.md](../../PRODUCT_CONTRACT.md), [DATA_AND_API_CONTRACTS.md](../../DATA_AND_API_CONTRACTS.md), dan [TEST_PLAN.md](../../TEST_PLAN.md).

## 1. Cara menggunakan rencana

Setiap tugas mempunyai ID, prasyarat, hasil yang dapat diperiksa dan bukti. Status: belum mulai → dikerjakan → menunggu review → selesai; terhambat hanya jika ada ketergantungan nyata. Implementasi ditutup setelah checks relevan, temuan QA material dan recheck, dokumentasi sesuai hasil aktual, serta diff/secret review. Commit/push mengikuti cakupan kerja yang diotorisasi; tidak menambahkan resource berbayar atau membaca secret ke chat.

Feature tasks mencakup migration/constraint/index/RLS yang diperlukan, input/error contracts, integration tests, dan konfigurasi/run instructions yang relevan. Tidak menunggu akhir untuk menguji keamanan. Fitur dependent tidak diklaim lulus jika provider integration yang diperlukan masih mock/skipped. Spike memakai data fiktif dan safe temporary values; final acceptance thresholds dibekukan setelah calibration, bukan sebelum prototype.

Test families merujuk ID pada TEST_PLAN; per-case IDs dibuat saat implementasi dengan expected/actual dan evidence manifest. Hasil aktual dicatat pada catatan pelaksanaan tugas; kriteria tabel merupakan target, bukan hasil tes.

## 2. Fondasi executable dan kelayakan

| ID | Prasyarat | Pekerjaan | Kriteria selesai dan bukti |
| --- | --- | --- | --- |
| BE-01 | Dokumen backend lulus QA | Bootstrap paket Python/FastAPI platform, manifest+lockfile, config tervalidasi, CLI/entry point, `.env.example` placeholder dan README backend | Clean installation/build wheel dan startup aktual; invalid config aman, tanpa DB/business claims. OPS-01. |
| BE-02 | BE-01 | Process health/readiness baseline, error schema, request ID, body/field limits | HTTP sungguhan memberi format konsisten; malformed/oversize/error tidak membocorkan input/secret. Readiness dependency ditambah saat dependency ada. |
| BE-03 | BE-02 | Composition root, domain/application ports, dependency injection dan guard import | Route memakai service; instance tidak berbagi actor state; contoh violation import ditolak. ARC-01. |
| SPIKE-01 | BE-01 | Verifikasi akses OIDC provider, callback/PKCE/key handling dan dev setup | Actual provider login/callback proof atau status blocked jelas; provider test terkontrol tidak disebut integrasi provider live. Pilih provider sebelum AUTH-02. |
| SPIKE-02 | BE-01 | Akses model chat/tool dan embedding ID/EN, rate/schema/timeout/usage | Actual API request terotorisasi, usage/cost tersanitasi, provider constraints dan model/dimension ditetapkan; MODEL-01. |
| SPIKE-03 | BE-01 | Pilih private storage+scanner/parser strategy, upload/download, byte limits dan URL access | Probes aktual dengan file fiktif, region/cost/timeout/scan capability tercatat. DOC-01/SEC-08; belum file feature lengkap. |

Provider/spike dapat berjalan independen setelah BE-01, namun task lain hanya memakai hasil yang benar-benar lulus. Pengguna memasukkan credential lokal; aplikasi/probe boleh memuatnya secara internal tanpa mengembalikan nilainya ke model/chat. Sambil credential belum tersedia, unit/prototype lain dapat dilanjutkan dengan batas bukti yang jelas.

## 3. Database, identity, dan akses

| ID | Prasyarat | Pekerjaan | Kriteria selesai dan bukti |
| --- | --- | --- | --- |
| DB-01 | BE-03 | Konfigurasi pooled runtime/direct migration, pool lifecycle, UoW, deadlines/cancel cleanup | DB aktual: TLS, commit/rollback, reuse setelah error dan pool shutdown; script tidak membocorkan credential. Belum schema bisnis/RLS. |
| DB-02 | DB-01 | Migration framework dan foundation tenants/principals/memberships/roles/sessions/config | Empty+upgrade migration, tenant FK, unique/null checks dan wheel resources terbukti. Circular tenant/config pointer deferred diuji: header+snapshot awal sah, missing/wrong-tenant snapshot ditolak saat commit. Feature tables menyusul task terkait. |
| DB-03 | DB-02 | Runtime least privilege, transaction-local actor/tenant, RLS dan pool safety | Real runtime role menolak cross tenant/missing context; success/error/cancel/timeout reuse aman; bukan owner/BYPASSRLS. SEC-05. |
| AUTH-01 | DB-03, SPIKE-01 | Kunci identity protocol/settings: issuer/audience/key refresh/state/nonce/PKCE, sesi/CSRF/lifetime/revoke | Contract fixtures dan negative cases siap, session/redirect limits diputuskan sebelum endpoint private login. SEC-06. |
| AUTH-02 | AUTH-01, BE-03 | Implementasi login/callback/session/logout/tenant switch | Actual login integration, callback failures, CSRF/logout/revocation ditolak benar, tenant switch tidak mengambil run lama. SEC-06. |
| AUTH-03 | AUTH-02 | Shared actor/object policy, role gabungan, grant sensitif dan anti-self-escalation | Matrix fixture A/B dan privileged scope tests; admin/operator tidak otomatis pembaca/approver. Endpoint fitur menerapkan policy saat dibangun. SEC-01/02/03. |
| AUTH-04 | AUTH-03 | Internal caller auth, signed delegation, canonical human-action hash contract, actor verification | Signature/audience/expiry/rotation/hash/operation contracts dan endpoint actor verification tersedia diuji. Human-action fixture terbatas, belum penerbitan/penyimpanan receipt nyata; jalur penuh ditutup ACT-01/FLOW-01/SEC-09. Tidak menutup seluruh SEC-07 dari fondasi ini. |
| DB-04 | DB-03 | Pola feature migration, cyclic FK, expand/contract dan failed-upgrade rollback | Template/procedure dan upgrade filled fixtures diuji; schema berikutnya tidak melemahkan RLS/constraints. OPS-01. |

## 4. Jobs, file, knowledge, dan percakapan

| ID | Prasyarat | Pekerjaan | Kriteria selesai dan bukti |
| --- | --- | --- | --- |
| JOB-01 | AUTH-03, DB-04 | Job/outbox storage, claim/lease/generation, heartbeat, bounded retry/fair tenant scheduling | Stale worker/takeover/concurrent claim tidak menulis state job; quota A tidak menghabiskan B. JOB-01/02. Fencing checkpoint/publication diuji lagi saat adapter tersedia. |
| FILE-01 | SPIKE-03, AUTH-03, DB-04 | File schema/private upload/quarantine/readiness/metadata; scanner/parser resource limits | Type/size/count/malformed/encrypted/security checks aktual; file unready tidak jadi knowledge/bukti; owner/ACL reads scoped. DOC-01, SEC-01. |
| KNOW-01 | FILE-01, JOB-01, SPIKE-02 | Dokumen/versions/ACL, parsing/chunk/embedding/index ingestion | Retry/partial failures tidak publish sebagian; schema model/dimension dan generation source jelas. DOC-02. |
| KNOW-02 | KNOW-01 | Publish/replace/withdraw/access_revision, owner-scoped search/source API | Atomic pointer+ACL, revoke tidak menunggu reindex, superseded/withdrawn behavior benar. DOC-02/03, SEC-03. |
| CONV-01 | AUTH-03, DB-04 | Conversation ownership, ordered messages/client-message dedup, feedback, typed provenance stores | User tidak membaca percakapan lain; integritas message→run exact conversation diuji dengan fixture run (lifecycle run penuh CHK-01). Provenance doc/service berbentuk port/fixture, bukan bukti live service filtering; document integration RAG-02 dan service integration SEC-09 menutup sisanya. |
| CHK-01 | CONV-01, JOB-01 | Spike/checkpoint adapter persistent+RLS+generation di semua jalur | Read/write/list/history/resume/intermediate tanpa/salah tenant ditolak; worker lama dan concurrent resume ditahan. Adapter belum dipakai production path sebelum lulus. SEC-05, JOB-01. |
| RAG-01 | KNOW-02, CONV-01, CHK-01, SPIKE-02 | Retrieval pipeline, context builder/provenance, citations, clarify/abstain/conflict, output buffering | Pertanyaan ID/EN bersumber; current access dicek sebelum model dan keluaran. Vector baseline vs hybrid/rerank diuji, bukan otomatis semua dipakai. RAG-01–06, SEC-04. |
| RAG-02 | RAG-01 | Batas token/langkah/waktu, usage/tracing tersanitasi, reconnect/history rechecks | No raw secrets/receipt trace; revoked documents tidak masuk history/checkpoint; model failure jelas. PERF-01/02, SEC-03/08. Data service rechecked setelah integration tersedia. |

## 5. Frontend pertama dan checkpoint RAG

| ID | Prasyarat | Pekerjaan | Kriteria selesai dan bukti |
| --- | --- | --- | --- |
| WEB-01 | AUTH-03, KNOW-02, CONV-01 | Web bootstrap manifest/lock, login/tenant, admin knowledge upload/status/publish, manual source search | Alur browser memakai API aktual, role/empty/error states jelas; public content tidak memuat credential. Bukan hanya mock UI. |
| WEB-02 | WEB-01, RAG-02 | Chat ID/EN, citations/source viewer, feedback dan riwayat yang difilter | User dapat mengikuti alur tanya/clarify/source tanpa akses tenant lain; UI E2E dan access regression lulus. |
| EVAL-01 | RAG-02, WEB-02 | Dataset development/gold, runner dan rubrik, calibration, manifest M1/gates | Paket acceptance 80 RAG terpisah dari development; three runs dan slice/denominator disepakati sebelum acceptance. EVAL-01/02. |
| GATE-01 | EVAL-01 | Penerimaan milestone 1 dan checkpoint ownership | RAG/knowledge/identity+applicable critical suite lulus, p95/cost gates M1 terukur; belum kelulusan workflow MVP. Pengguna menjalankan dan menjelaskan satu source/access case. |

Frontend dibangun ketika kontrak API alurnya siap. Tidak menunggu seluruh backend. WEB-01 dapat dimulai setelah API prerequisites walaupun RAG belum selesai; batas pekerjaan tetap per alur yang benar-benar bisa diuji.

## 6. Service referensi dan aturan

| ID | Prasyarat | Pekerjaan | Kriteria selesai dan bukti |
| --- | --- | --- | --- |
| REF-01 | BE-03 | Package/build/config/migration service terpisah, DB connection/least privilege/RLS, health/errors | Clean install+DB integration milik service, tidak import platform source/ORM atau login DB yang sama. OPS-01, SEC-05. |
| REF-02 | REF-01, AUTH-04 | Platform HTTP clients untuk actor/policy/file, caller/delegation authorization dan typed error handling | Actual actor-verification integration tersedia dan caller negatives lulus. Policy/file client diuji contract/fault HTTP fixture sampai endpoint lengkap tersedia; live eligibility ditutup POL-01 dan receipt/access-check FILE-02/FLOW-01. Bukan full SEC-07 atau full policy/file integration pass. |
| REF-03 | REF-02 | Service operations ledger/idempotency/commit-seal serialization dan result lookup | Race absent row/old request vs seal aman; same key/hash replay, mismatch reject. Claim link ditambahkan saat CLAIM-01 tersedia. WF-07/08. |
| REF-04 | REF-02, DB-04 | Service trips/items fiktif dan immutable rule-package receiving API | Same tenant owner enforced, package digest mismatch 409, data sources tersimpan sendiri; bukan benchmark seed. WF-01/02. |
| POL-01 | KNOW-02, AUTH-04, REF-04 | Platform policy packages/doc pairing, prepared ack/activation range, live eligibility verifier | Overlap activation race ditolak, known-conflict/withdraw fail closed, locked package retained; shared tenant config. WF-01. |
| CLAIM-01 | REF-03, REF-04, POL-01 | Service claim/revision/items/stages/decisions/reservations schema+state rules | Positive/negative constraints, locked policy/deadline/money validated; stage version/concurrent business duplicate tests. WF-01/02/05/07. Receipt integration masih prerequisite sebelum submission dinyatakan lengkap. |
| FILE-02 | FILE-01, AUTH-04, REF-03, CLAIM-01 | Internal receipt pin/finalise/proof, service claim access-check dan current file download relation | Owner/digest/ready/human consent verified, pin tidak lepas saat unknown, active reviewer vs general role dibedakan. WF-04, DOC-01. |

REF tasks dapat disiapkan setelah kontrak identity/API-nya tersedia, tanpa menunggu GATE-01; acceptance MVP tetap membutuhkan GATE-01. Perubahan source/trip bukan tool agent. Claim dan receipt access-check dapat dibangun dengan service fixtures sebelum action workflow disambungkan; fixtures tidak menjadi bukti full consent path.

## 7. Workflow, recovery, dan penerimaan MVP

| ID | Prasyarat | Pekerjaan | Kriteria selesai dan bukti |
| --- | --- | --- | --- |
| DRAFT-01 | POL-01, FILE-02, CONV-01, CLAIM-01 | Platform editable draft/revisions, structured item input, preview/hash/route/receipt refs | Owners/versions/amount/access validated; deferred header+version insert/update sah, missing/wrong-parent version ditolak saat commit. Preview tidak submit; source amounts adalah declared, bukan OCR verified. WF-02/03. |
| ACT-01 | DRAFT-01, JOB-01, AUTH-04 | Confirmation/decision receipts, atomic freeze+action+outbox+audit, stable key/hash | Receipt issuance/storage nyata menggantikan fixture AUTH-04. Chat bukan consent; material change reconfirm, frozen no edit, human decision binds exact decision/reason/stage/revision. Integrasi penuh grant→dispatch→service ditutup FLOW-01/SEC-09. |
| FLOW-01 | ACT-01, CLAIM-01, FILE-02, REF-03 | Worker dispatch service submit/information/decision, result lookup/reconcile/seal, pin finalise | Actual human receipt/delegation+live actor/eligibility/receipt/access-check→service state dibuktikan, termasuk endpoints yang sebelumnya fixture REF-02. Lost response/restart/old request no double effects; lookup 404 tidak langsung membuka draft. WF-04/07/08, JOB-01. |
| FLOW-02 | FLOW-01, RAG-02 | Agent read/prepare/submit/status tools, pause/resume, multi-stage approval dan needs-info | Explicit UI human confirm/decision, no model approval tool; revision reset approvals, current policy holds; payment/reopen di luar MVP. WF-05/06. |
| WEB-03 | FLOW-01, WEB-02 | Manual+assisted draft/form, summary/confirm, approver queue/decision, needs-info/status/unknown UI | Kedua jalur memakai services sama; queue delay deadline dan approved!=paid jelas; no fake success. UX-01, WF. |
| SEC-09 | FLOW-02, WEB-03 | Integrated critical regression seluruh data turunan/config/tool/API/file | Menutup seluruh integration gap AUTH-04/REF-02/CONV-01 dengan jalur nyata: stage change tanpa membership revoke menyaring claim history/cache/checkpoint/output, human receipt/grant invalid, live policy/file outage/withdrawal, role escalation, injection/duplicate races. SEC-01–08, WF, JOB. |
| EVAL-02 | SEC-09, EVAL-01 | Workflow24 + RAG80 acceptance pack, final M2 calibration/gates dan pilot manual–assisted | Development/regression/holdout dipisah; per run/slice counts, all attempts cost, latency dan failures dilaporkan; user pilot atau walkthrough dibedakan. |
| GATE-02 | EVAL-02, GATE-01 | Penerimaan MVP dan ownership checkpoint | Semua M2 requirements/root TEST_PLAN gates dipenuhi, QA critical reproduction dan temuan material ditutup; bukan klaim M3/M4 selesai. Pengguna mencoba satu recovery dan menjelaskan bukti status. |

## 8. Cloud, privacy, dan operasi

| ID | Prasyarat | Pekerjaan | Kriteria selesai dan bukti |
| --- | --- | --- | --- |
| DATA-01 | AUTH-03, FILE-01, CONV-01 | Registry retensi/residency/redaction/data category, delete/export orchestration | Prasyarat sebelum data sensitif dipakai; mencakup DB/index/file/cache/checkpoint/ledger/backup/pins. Demo tetap fiktif jika kebijakan belum lengkap. SEC-08. |
| OPS-01 | GATE-02 | Docker/CI build artifacts, infrastructure code, secret/network/IAM/TLS settings dan deployment AWS | Resource dan budget dipilih/disetujui sebelum provisioning; internal services/DB tidak public; relevant regressions di environment final. OPS-01/02. |
| OPS-02 | OPS-01 | Kubernetes probes/limits/replicas, graceful shutdown, rollout/rollback dan load | Actual cluster failure/restart/update drills dan p95/error/capacity/fairness gates; single node tidak disebut cross-machine HA. OPS-03/PERF. |
| JOURNAL-01 | AUTH-03, POL-01, FLOW-01, DATA-01 | Independent security journal adapter/storage, intent/result/watermark, revoke/delete/stage/policy producers | Intent durability sebelum acknowledgment, ambiguous deny, monotonic completeness dan gaps diuji. In-process mock bukan bukti M3 independent storage. SEC-08. |
| OPS-03 | OPS-01, JOURNAL-01 | Backup platform/service/file/config/ledger/pins, isolated restore worker hold, post-backup journal replay/reconcile | Skewed snapshots, commit lost result, missing files/journal, post-backup revokes/deletes diuji; scope incomplete tetap held. OPS-04. |
| OPS-04 | OPS-02, OPS-03, DATA-01 | Freeze M3 workload/availability/RPO/RTO/budget dan acceptance/runbooks | Actual drills memenuhi targets dengan timestamps dan sanitized evidence; review security controls tidak dianggap sertifikasi. |

DATA-01 dirinci sebelum fitur yang memproses data sensitif; tidak perlu menunggu GATE-02 untuk mengerjakannya. Journal interface dipertimbangkan sejak desain; producer/storage yang memenuhi recovery gate harus tersedia sebelum M3 diterima. M1/M2 memakai fixtures fiktif, bukan mengklaim penghapusan/restoration independent storage sudah terbukti.

## 9. Model open-source dan optimasi

| ID | Prasyarat | Pekerjaan | Kriteria selesai dan bukti |
| --- | --- | --- | --- |
| MODEL-01 | GATE-02, SPIKE-02 | Baseline commercial dan kriteria compare, model/license/resource/provider feasibility | Dataset/conditions/cost basis fixed; kebutuhan GPU/biaya dibahas sebelum dipakai, bukan memilih model dari popularitas saja. |
| MODEL-02 | MODEL-01 | Deploy open-source inference nyata, konfigurasi tool/ID/EN, batching/quantisation bila relevan | Actual endpoint/inference, hardware/resource/GPU/memory/quality/safety/latency/cost report; no production promotion jika safety gate gagal. MODEL-02 family. |
| MODEL-03 | MODEL-02 | Bandingkan RAG/prompt/rules vs fine-tuning bila ada gap terukur | Jika tune: train/val/test terpisah dan base/tuned comparison; jika tidak, alasan berbukti. Tidak membuat tuning hanya untuk checklist. |
| GATE-04 | MODEL-02, MODEL-03 | Tutup milestone4 dan technical decision record | Semua eksperimen/limits reproducible, pilihan backend model dijelaskan; bukan memaksa OSS mengalahkan API atau mengganti model yang lulus. |

Eksperimen M4 tidak memerlukan seluruh M3 selesai bila environment inference dan keamanan eksperimennya mandiri; penggunaan pada aplikasi cloud tetap membutuhkan gate deployment/data terkait. Milestone yang selesai dicatat terpisah, bukan disimpulkan dari nomor urut saja.

## 10. Bukti penutupan dan tugas pertama

Setiap penutupan mencatat ID/status, commit/config/dataset versions, test commands atau runner, hasil actual/expected, provider live vs stub/fault injection, sanitized artifacts, QA verdict dan limitation yang memengaruhi klaim. Tidak menambah riwayat eksperimen tak relevan ke README publik; raw hasil sensitif/local tetap diabaikan Git. Uji ulang terfokus setelah perubahan; tidak mengulang full suite tanpa alasan setelah checks relevan lulus.

**Tugas implementasi pertama adalah BE-01.** Outputnya aplikasi backend minimal yang bisa diinstal/dinyalakan, dependency terkunci, config aman, dan panduan run. DB, login, RAG, workflow serta frontend belum diklaim tersedia pada BE-01. Setelah itu BE-02/BE-03 dan spike sesuai dependency.

## 11. Hasil pelaksanaan BE-01

**Selesai — 3 November 2026.** Package FastAPI, CLI `enterprise-ai-api`/module entry point, config APP_ENV/APP_HOST/APP_PORT, manifest/lockfile, `.env.example` tanpa credential, serta [panduan backend](../../backend/README.md) tersedia.

- Windows, Python 3.13.6, uv 0.11.26; dependency terkunci termasuk FastAPI 0.162.0, Uvicorn 0.54.0, Pydantic 2.14.2, pydantic-settings 2.16.2. Versi lengkap dan hash pada backend/uv.lock.
- **20 tes lulus**, termasuk validasi dan precedence config, error tanpa nilai input/path privat, app isolation, mode docs, CLI, dan startup HTTP aktual.
- Salinan bersih file Git tanpa `.env`/virtualenv lokal berhasil melakukan locked dependency install, build sdist/wheel, install wheel non-editable, import dari site-packages, console config check, dan seluruh 20 tes.
- **QA lulus tanpa temuan material**, mereproduksi suite, instalasi bersih, wheel, console, serta empat probe config sintetis tersanitasi secara independen. QA tidak membaca `.env` pengguna.
- Batas bukti: bootstrap executable saja. Belum meluluskan DB, auth, health/readiness/error contracts BE-02, wiring BE-03, worker, model atau workflow bisnis. `/docs` dan `/openapi.json` hanya pada development; `/` serta endpoint bisnis masih 404.

Urutan setelah BE-01 adalah **BE-02**, sesuai dependency tabel. Provider spike boleh dijadwalkan terpisah setelah BE-01 tanpa mengklaim integrasi yang belum diuji.

## 12. Hasil pelaksanaan BE-02

**Selesai — 3 November 2026.** Health process/bootstrap-readiness, stable error envelope, server-owned request ID, request metadata logging, serta batas body/field dasar tersedia.

- `/health/live` memeriksa proses; `/health/ready` mengikuti lifecycle bootstrap (200/503). Dependency DB/model/OIDC/storage belum tersedia dan tidak diklaim sehat.
- Error 400/404/405/408/413/422/500 serta status HTTP lain yang didukung memakai envelope tanpa raw input/exception detail. Request ID UUID dari server sama pada header/body error, berbeda antarrequest bersamaan. Mode debug tetap false; raw access log tetap off.
- Limit body default 1 MiB, maksimum konfigurasi 16 MiB; aktual byte termasuk chunked diperiksa. Receive deadline default 10 detik (1–60). Input model dasar strict/extra-forbid/string≤4096, sedangkan batas domain/list mengikuti endpoint masa depan.
- **58 tes lulus**, termasuk HTTP nyata untuk health/error/request ID, exact/oversize/chunked body, timeout/disconnect, field validation, invalid output, concurrency, dan kegagalan setelah headers tanpa response kedua atau raw input di log.
- Clean staged-file export tanpa `.env`/virtualenv pengguna, locked dependency install, build sdist/wheel, non-editable wheel import, console config check, dan suite penuh berhasil: **58 passed**.
- **QA independen lulus tanpa temuan material**, mengulang 58 tes dari wheel non-editable, real HTTP, serta probe FastAPI StreamingResponse yang gagal setelah headers. Source dan kontrak telah diperiksa; tidak membaca `.env` pengguna.
- Batas bukti: HTTP boundary aplikasi saja. Parser/server dapat menolak request sebelum ASGI tanpa envelope aplikasi. Belum meluluskan wiring BE-03, auth, DB, upload file, agent/RAG, business workflow, atau production readiness keseluruhan. Dependency versions tetap menggunakan lockfile BE-01.

Berikutnya **BE-03: composition root, service/port interfaces, dependency wiring dan guard import**.

## 13. Hasil pelaksanaan BE-03

**Selesai — 3 November 2026.** Composition root memasang ApplicationServices dan ApplicationResources per aplikasi. Modul system menyediakan domain ProbeResult, BootstrapReadiness/HealthServicePort, serta HealthService yang digunakan route melalui dependency injection. Readiness adapter dimiliki lifecycle aplikasi, bukan singleton global.

- Factory service override dan FastAPI dependency override terbukti mencapai route aktual dan tidak bocor ke app lain; container services immutable, resources default terpisah dan lifecycle gagal tetap menonaktifkan readiness.
- Guard AST pada `scripts/checks/check_architecture.py` memeriksa import absolut/relatif, public port/service boundaries, config independence, cross-package source, dynamic imports/wildcards dan driver database stdlib pada layer yang dilarang. Negative fixtures dan positive adapter cases tersedia pada kedua package.
- **91 tes lulus**, termasuk regresi HTTP BE-01/02, wiring/lifecycle, serta guard positive/negative cases.
- Clean export dari file Git tanpa `.env`, dependency locked, sdist/wheel build, install wheel non-editable, composition import dari site-packages, guard CLI, console config check dan suite penuh berhasil: **91 passed**.
- **QA lulus setelah satu temuan P2 diperbaiki:** whitelist stdlib kini tidak mengizinkan sqlite3/_sqlite3/dbm/shelve pada route/domain/ports/service/config. Adapter tetap boleh memakainya. QA mengulang probe serta seluruh 91 tes dari wheel bersih dan menyatakan tidak ada temuan material tersisa.
- Batas bukti: fondasi wiring/guard saja; guard adalah static dependency regression, bukan sandbox. Tenant/business repositories, DB/UoW, auth, worker dan adapter provider belum diimplementasikan. Interface fitur masa depan tidak dianggap tersedia hanya karena health memakai Protocol.

Berikutnya **DB-01: konfigurasi koneksi runtime/migration dan pengujian transaksi database**, sesuai prasyarat. Provider spike tetap dapat dijadwalkan tersendiri.
