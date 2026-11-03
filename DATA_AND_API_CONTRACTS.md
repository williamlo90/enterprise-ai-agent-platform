# Data and API Contracts — Enterprise AI Agent Platform

Status: kontrak desain. Fondasi koneksi DB-01 telah lulus QA; implementasi migration identity/config DB-02 dan status verifikasinya dicatat pada docs/backend/task_plan.md. API/fitur lain tetap rancangan sampai tugas terkait diterima. Turunan [PRODUCT_CONTRACT.md](PRODUCT_CONTRACT.md) §3–6; komponen pada [ARCHITECTURE.md](ARCHITECTURE.md), kasus uji pada [TEST_PLAN.md](TEST_PLAN.md). Aturan produk mengungguli detail teknis; perubahan perilaku produk memerlukan penyelarasan keempat file.

## 1. Konvensi dan invariant

ID UUID opaque bukan bukti akses. Platform dan service memiliki database/credential/migration terpisah; referensi lintas sistem berupa UUID eksternal, tidak memakai FK/SQL lintas database. Di dalam tiap database, tabel tenant menggunakan PK `(tenant_id,id)` atau unique pasangan tersebut; semua FK terkait tenant menyertakan tenant_id. Kolom audit aktor/waktu/version wajib pada mutasi penting.

Tipe dasar: UUID, TEXT/enum allowlist, BOOLEAN, INTEGER/BIGINT, TIMESTAMPTZ UTC, DATE bisnis, JSONB tervalidasi. Tenant menyimpan timezone IANA. Uang berupa `amount_minor BIGINT > 0` dan currency allowlist beserta exponent; tidak memakai float. Total dihitung backend/service dengan overflow checks. Mata uang per claim tunggal sesuai konfigurasi tenant; FX di luar MVP. Lampiran receipt maksimum 10 MiB/file dan 10/claim; knowledge upload memiliki batas terkonfigurasi yang dikunci sebelum endpoint diterima.

Versi sumber/paket aturan immutable, versi draf/claim/run monotonik. Input unknown field ditolak pada kontrak mutasi. Payload yang dikendalikan server (tenant, owner, role, final status, approver) tidak dapat disuntikkan melalui body publik. Semua daftar, cursor, count, error, lookup dan download terotorisasi; objek tenant lain memberi not-found generik. 403 hanya untuk tindakan yang tidak diizinkan pada scope yang boleh diketahui.

## 2. Model data platform

Semua baris berisi tenant_id kecuali principals eksternal, sessions dan registry operator yang memiliki scope eksplisit. Tabel turunan menyimpan owner/source refs untuk pemeriksaan akses, bukan hanya nama role.

| Entitas | Kolom inti dan constraint | Relasi/kepemilikan |
| --- | --- | --- |
| principals | id PK, issuer, subject, display_name, active; UQ(issuer,subject) | Identitas eksternal; tidak otomatis mendapat membership |
| tenants | id PK, slug UQ, name, timezone, active, config_version | Perusahaan pelanggan |
| memberships | id, principal_id, active, access_revision; UQ tenant+principal | N:M principal–tenant; roles terpisah agar gabungan diperbolehkan |
| membership_roles | membership_id, role, scope; composite PK | Scope dan role allowlist, grantor dan audit; larangan eskalasi diri |
| sessions | id PK, token_hash UQ, principal_id, active_tenant_id?, csrf_hash, expiry, revoked_at | Aktif tenant hanya membership sah; token mentah hanya cookie |
| tenant_configs | id, version, assistant_name, locale settings, help_contact, connector_ref, model_policy_ref | Instruction text tidak mengubah tool permissions |
| policy_packages | id, version, digest, document_version_refs, structured_rules, valid_trip_end_from/to, timezone, publication_state, service_ack_digest | Range aktif tidak overlap; draft/prepared/active/withdrawn; publisher berwenang |
| files | id, owner_principal_id, purpose, object_version, digest, size, detected_type, scan_state, deletion_state | Object key server-side; quarantine/ready/rejected; immutable bytes/version |
| file_pins | file_id/version, operation_id, candidate_claim_id, pin_state | UQ operation+file version; held/finalised/released, prevents unsafe deletion |
| documents | id, title, active_version_id?, access_revision, withdrawn_at? | Pengelola dan ACL terpisah |
| document_versions | id, document_id, file_id/version, content_digest, parse_state, index_state, publication_state, effective metadata | Immutable; sumber aktif setelah seluruh pipeline siap |
| document_acl | document_id, grantee_kind/id, permission, revision | Subject/role scope, deny tanpa grant; publication dan ACL diperiksa bersama |
| chunks | id, document_version_id, location/page/offset, text_ref, embedding, embedding_model/version | Same-tenant FK, hanya published eligible untuk retrieval |
| conversations | id, owner_principal_id, language, version, active_run_id? | Riwayat milik pemilik; tidak diwarisi admin umum |
| messages | id, conversation_id, author, body_ref, sequence, client_message_id | UQ conversation+client_message_id; ordered events; provenance wajib pada output berbasis sumber |
| source_dependencies | output/run/checkpoint_ref, source_version_id, source_acl_revision, location | Melacak data turunan untuk revocation dan rebuild |
| runs | id, conversation_id, actor_id, state, version, lease_generation, model/config refs, usage, current_action_ref? | queued/running/waiting/completed/failed/held; satu active writer |
| checkpoints | id, run_id, namespace, checkpoint_version, generation, state_ref, dependency refs | Semua read/write/history/intermediate lewat adapter terotorisasi/fenced |
| drafts | id, owner, conversation_id?, claim_ref?, kind, version, state, payload, payload_hash, policy_ref/digest, receipt_versions, approval_route | kind submission/information_update; state editable/frozen/committed/not_committed |
| consents | id, draft_id/version, actor, session_ref, payload_hash, policy_digest, receipt digests, destination, time | Immutable, eksplisit manusia; stale tidak berlaku untuk versi baru |
| human_action_receipts | id, actor, session_ref, operation_id, action_kind, canonical_payload_hash, bound_object/version, human_action_time | Immutable; mengikat consent draft atau keputusan manusia tanpa mewajibkan draft pada decision |
| actions | id, draft_id/version nullable untuk decision, human_action_receipt_id, kind, key UQ tenant+kind+actor+key, request_hash, snapshot, state, result_ref, last_error | queued/sending/unknown/committed/not_committed; identitas sebelum send |
| outbox_jobs | id, action/run/source_ref, job_type, expected_version, generation, lease_until, next_attempt_at, attempts, state | Unique business job identity; dispatch/complete conditional pada generation |
| audit_events | id, actor, action/object/version, outcome, timestamp, correlation, metadata_redacted | Append-only melalui runtime; akses scoped |
| feedback | id, message_id, actor_id, rating, reason? | Pemilik percakapan; bukan akses membaca seluruh dataset |

Retention jobs tidak menghapus files yang masih dipin claim/action. Migration penuh akan merinci indeks, enum/check/FK dan storage layout adapter; tabel logis di atas adalah kewajiban perilaku, bukan izin menyimpan JSONB tanpa validasi.

`source_dependencies` memakai tagged resource references: source_system platform/service, resource_type document_version/trip/claim/receipt, tenant, resource_id/version, required_scope/relation dan last_checked_access_revision. Output tool dan ringkasan turunannya ikut dicatat. Reader memeriksa akses resource terkini melalui service bila relevan; ownership percakapan tidak mempertahankan akses setelah reviewer berpindah stage. History/cache/checkpoint/output disaring atau ditahan jika verifikasi tidak tersedia.

`policy_packages` memiliki operational eligibility terpisah dari digest konten immutable: eligibility_revision, allowed_for_new, allowed_for_locked_claims, blocked_reason, changed_at. Withdraw/known-conflict menutup keduanya; supersession dapat menutup new use tanpa membatalkan locked claim yang tetap sah. Verifikasi mempertimbangkan rentang trip date dan claim binding, bukan mempercayai boolean dari caller.

## 3. Model service referensi

| Entitas | Kolom inti dan constraint | Acuan |
| --- | --- | --- |
| service_policy_packages | id/version, digest, structured_rules, dates/timezone, state | Salinan immutable dari jalur publikasi terotorisasi |
| trips | id, owner_principal_ref, start/end_date, employee_ref, state | Owner trusted identity mapping; data fiktif pada demo |
| trip_expense_items | id, trip_id, date, category, declared amount/currency, owner_ref | Referensi item biaya tepercaya; tidak bebas memakai item orang lain |
| claims | id, public_reference UQ tenant, owner_ref, trip_id, policy_id/version/digest, state, version, active_stage, submitted_at | pending_approval/needs_information/approved/rejected |
| claim_revisions | id, claim_id, revision_no, payload_hash, items, total_minor/currency, receipt_version_refs, consent_ref | Immutable setiap submission/pelengkapan |
| claim_stages | id, claim_id/revision, ordinal, approver_principal_ref, scope_ref, status | Tidak self-approval, urut; approval lama reset setelah revision baru |
| decisions | id, claim_id/revision, stage, actor, decision, reason?, operation_id, created_at | expected version dan actor terkini; reject/info reason wajib |
| expense_reservations | item_id, claim_id, state | UQ item untuk claim pending/needs_information/approved; transaksi menjaga race key berbeda |
| service_operations | id, caller/actor, kind, key, request_hash, outcome, result_ref, result_version, checked_at, created_at | tenant+kind+actor+key UQ; committed/rejected/sealed_no_commit; ledger durabel |
| service_audit | actor, tenant, object, operation/version, outcome, time | State bisnis dan event audit atomik |

Owner/approver adalah trusted principal refs, bukan FK ke database platform. Service memverifikasi pemanggil dan aktor terkini melalui identity-verification API; caller yang sekadar tahu UUID tidak berhak mengakses. Klaim/paket/rincian finansial ditegakkan oleh service, tidak tergantung keyakinan model.

## 4. Otorisasi per objek

| Aktor | Dapat mengakses | Batas |
| --- | --- | --- |
| Karyawan | Dokumen terbit sesuai ACL; percakapan/draf/trip/claim miliknya | Tidak seluruh dokumen/claim tenant |
| Approver | Claim pada stage aktif yang ditugaskan dan receipt revision terkait | Tidak self-approve atau melihat seluruh claim hanya karena role |
| Pengelola pengetahuan | Dokumen/ACL pada scope pengelolaan | Tidak otomatis business-rule publisher/approver |
| Pemilik proses/publisher | Paket aturan dan rentang berlaku pada scope penerbitan | Tidak mengubah klaim aktif; grant sensitif oleh orang lain |
| Admin tenant | Akun/config dalam scope, job metadata yang diperlukan | Tidak memberi role sensitif kepada diri sendiri atau membaca konten privat otomatis |
| Operator | Provisioning/health/config operasional yang diizinkan | Tidak akses konten tenant; akses khusus scoped diaudit |
| Worker/service caller | Operasi tertentu dengan delegasi aktor atau job authority terbatas | Bukan tenant superuser; agent tidak memiliki grant keputusan manusia |

Sesudah keputusan tahap, reviewer tidak mempertahankan hak receipt hanya dari role. Kebijakan minimum MVP: pemilik tetap dapat membaca claim/receipt miliknya; reviewer hanya selama active stage; kebutuhan akses audit terpisah dan diberikan eksplisit. Ketidaktersediaan verifikasi aktor/claim/file menahan baca/mutasi; UI menampilkan error sementara, tidak beralih ke izin cache lama.

## 5. Protokol HTTP umum

Public prefix `/api/v1`; service internal `/service/v1` dan platform internal `/internal/v1`. Body JSON UTF-8, timestamp ISO UTC, dates YYYY-MM-DD, UUID; locale id/en. Mutasi versioned wajib `expected_version`; side effects wajib `Idempotency-Key`. Server membuat request_id; output error `{error:{code,message,request_id,retryable}}` tanpa credential/input sensitif. Pagination `{items,next_cursor}` terikat tenant/filter/actor; cursor tidak membuka scope baru.

Status utama: 200/201 hasil terverifikasi, 202 pekerjaan diterima, 400 payload invalid, 401 sesi/caller tidak sah, 403 izin scope diketahui tidak cukup, 404 objek tidak tersedia, 409 version/idempotency/state conflict, 413 limit ukuran, 415 tipe tidak didukung, 422 aturan bisnis tidak terpenuhi, 429 quota, 503 dependency sementara. 202 tidak berarti pengajuan tercatat. Response mutations memuat operation_id, state dan result_ref bila terbukti; hasil bisnis dipisahkan dari status teknis.

Canonical request hash mencakup jenis operasi, tenant/actor yang telah diverifikasi, versi draf/claim, policy digest, trip/item refs, integer amount/currency, versi/digest receipt, route dan destination. Canonical serialization deterministik dirinci sebelum implementasi; reorder item list dinormalisasi menurut ID. Key sama dengan hash berbeda 409; key sama/hash sama mengembalikan hasil yang sama setelah reotorisasi. Query hasil tidak mengubah aturan yang dikunci.

### Baseline HTTP yang diimplementasikan pada BE-02

Health berada di luar prefix bisnis: `GET /health/live` memberi 200 `{status:ok,scope:process}`; `GET /health/ready` memberi 200 `{status:ready,scope:bootstrap}` setelah lifecycle startup dan 503 envelope saat belum siap. Kesiapan bootstrap belum menguji DB/model/identity/storage; dependency minimum ditambahkan saat tersedia.

Server menerbitkan UUID request baru dan mengirim `X-Request-ID`, mengabaikan input ID klien; body error memakai ID yang sama. Error memakai pesan/code aman tanpa input atau detail exception bebas. Malformed JSON 400, validasi input 422, oversize 413, deadline body 408, unhandled failure 500. `retryable` true hanya untuk 408/429/503 pada baseline; flag bukan izin mengulang tindakan bisnis dengan key baru. Respons HTTP yang ditolak parser/server sebelum ASGI di luar envelope aplikasi.

Body default 1 MiB, config maksimum 16 MiB, dihitung dari byte aktual termasuk request chunked/tanpa Content-Length. Deadline menerima body default 10 detik, config 1–60. Content-Length invalid/duplikat/mismatch ditolak di boundary aplikasi bila mencapai ASGI. Input model berikutnya memakai RequestModel strict/extra-forbid/string≤4096 ditambah batas khusus endpoint. Upload FILE-01 harus memilih batas transport yang memadai untuk file maksimum 10 MiB dan menguji ulang; belum tersedia pada BE-02.

Hash didefinisikan per operation kind. Submission mengikat snapshot draf/consent beserta seluruh field di atas. Information update juga mengikat claim, expected_version dan revision yang diganti. Decision wajib mengikat claim_id, expected_version, revision, stage, decision (`approve/reject/request_information`) dan reason persis yang dikirim, selain tenant/actor/operation ID. Human_action_receipt dan signed delegation mengikat canonical hash yang sama. Perubahan decision/reason/stage/revision dengan key atau grant lama ditolak; replay identik tidak mengambil keputusan baru. Normalisasi string/whitespace dan urutan field ditetapkan konsisten pada canonical serialization, bukan menghapus alasan dari hash.

### API browser — identity dan tenant

| Endpoint | Kontrak |
| --- | --- |
| GET /auth/login, GET /auth/callback | OIDC state/nonce/PKCE dan redirect allowlist; callback gagal tidak membuat sesi |
| POST /auth/logout | CSRF, revoke server session dan cookie |
| GET /me | Principal, membership/role minimum, active tenant dan revision; tanpa credential |
| POST /session/tenant | tenant_ref dari membership aktif; rotate/bind context, tidak mengadopsi run lama |
| POST /operator/tenants | Operator scoped provisioning, key/idempotency dan audit |
| GET/POST /admin/memberships; PATCH /admin/memberships/{id} | Scope admin, version/key untuk mutasi; sensitivity/anti-self-escalation; revoke langsung mengubah access_revision |
| GET/PATCH /admin/config | Config versioned allowlist; connector_ref bukan URL bebas; mutasi diaudit |

### API browser — knowledge dan percakapan

| Endpoint | Kontrak |
| --- | --- |
| POST /files | purpose knowledge/receipt; upload terbatas dan quarantine; owner server-side, 202 file_id+scan_state |
| GET /files/{id}/status | Pemilik/pengelola berwenang, status tanpa object key |
| GET /files/{id}/content | Download proxy, current ACL/claim relationship, no public URL; generic not-found jika scope tidak boleh diketahui |
| POST /knowledge/documents | Pengelola, title+file_ref, membuat draft/version belum terbit |
| POST /knowledge/documents/{id}/versions | expected_version, file baru immutable; proses ingest; versi lama aktif sampai switch sah |
| GET /knowledge/documents/{id}/versions/{version}/status | Pengelola scoped, parse/index/scan failure sanitized |
| POST /knowledge/versions/{id}/publish | expected_version/key, ready+ACL+index, pointer aktif atomik |
| POST /knowledge/documents/{id}/withdraw | expected_version/key, deny penggunaan baru sebelum cleanup indeks |
| PATCH /knowledge/documents/{id}/acl | Version/key, access_revision naik; data turunan harus diperiksa ulang |
| GET /knowledge/search | Query dan language, scoped filter; dipakai manual baseline dan retrieval, tidak menampilkan sumber ditolak |
| GET /knowledge/sources/{version}/content | Current access, citation location; status superseded jika tetap boleh dibaca |
| POST /conversations | Owner server-side, language id/en |
| GET /conversations; GET /conversations/{id}/messages | Owner saja, ordered/paginated, provenance filtering pada riwayat |
| POST /conversations/{id}/messages | text+client_message_id, idempotent enqueue run; user text bukan confirmation |
| GET /runs/{id}; POST /runs/{id}/resume | Owner+current membership, expected run version, single writer; resume tidak memberi hak tool baru |
| POST /messages/{id}/feedback | Pemilik, rating+reason opsional, idempotent |

Run output schema: `{kind:answer|clarification|insufficient_evidence|source_conflict|service_unavailable, text, citations:[{source_version_id,location}], run_id}`. Citation harus supported/current-access; tidak mengembalikan konten yang terlarang meski model menyebutnya. Tool result berupa field allowlist; tidak meneruskan credential/raw traceback/prompt injection sebagai system instruction.

### API browser — aturan dan workflow

| Endpoint | Kontrak |
| --- | --- |
| POST /policies/packages; POST /policies/packages/{id}/prepare | Publisher scoped, document/config digest immutable dan rentang tanggal; prepare menyinkronkan service |
| POST /policies/packages/{id}/activate | expected_version/key, ack digest service, ACL/dokumen sah dan rentang nonoverlap |
| POST /policies/packages/{id}/withdraw | Publisher, audit dan hold tindakan terkait; tidak migrasi claim lama |
| GET /trips; GET /trips/{id} | Proxy service, verified owner/current grants |
| POST /drafts | kind submission/information_update, trip_ref/claim_ref dan item refs; owner/status/route ditentukan backend |
| GET/PATCH /drafts/{id} | Owner; patch hanya editable + expected_version; hash/version berubah |
| POST /drafts/{id}/preview | Validasi terkini, summary, draft_version/hash, policy_digest, receipt_versions dan route; belum consent |
| POST /drafts/{id}/confirm | Sesi manusia+CSRF, expected_version/hash+key, validasi lalu snapshot/consent/action/outbox atomik, 202 operation_id |
| GET /actions/{operation_id} | Owner/scoped actor; queued/sending/unknown/committed/not_committed, ref/version/error aman |
| GET /claims; GET /claims/{id} | Proxy service, owner atau reviewer active stage, current grants, no trusted status dari model |
| POST /claims/{id}/decisions | Human action/session/CSRF+expected_version+key, approve/reject/request_information, reason bila wajib; delegasi spesifik keputusan, bukan tool agent |

Formulir manual dan assistant memakai draft/preview/confirm/service yang sama. Decision juga dicatat sebagai action/outbox durabel sebelum dispatch dan hasilnya direkonsiliasi; user double-click tidak menerbitkan keputusan kedua. Information update menggunakan draft/consent dan endpoint service khusus; bukan PATCH langsung nominal claim aktif.

### API internal — identitas dan receipt

| Endpoint | Caller dan kontrak |
| --- | --- |
| POST /internal/v1/actors/verify | Service bercredential, signed delegation; validasi audience/expiry/action/object/actor/current membership+role revision+human grant bila relevan; return allow/deny dan revision/waktu. Tidak menerima role bebas dari browser. |
| POST /internal/v1/policy-packages/{id}/eligibility | Service caller; version/digest, operation_kind, trip_date dan claim_ref bila terkunci; platform memverifikasi current eligibility/range/claim binding yang disahkan saat submission. Return allow/deny, eligibility_revision dan checked_at; unavailable berarti hold. Endpoint tidak callback ke service. |
| POST /internal/v1/receipt-pins | Service; operation_id+candidate_claim_id dan receipt version refs; verify owner/tenant/hash/consent/ready, pin idempotent; response attestation refs/digests, tidak mengambil claim status via service balik |
| POST /internal/v1/receipt-pins/{operation_id}/finalise | Service; committed claim ref atau sealed-no-commit proof, update pin retention atau release; delivery retry aman |
| POST /service/v1/claims/{id}/access-check | Platform caller+actor; service memeriksa owner atau active stage dengan current identity; response allow/deny dan allowed receipt revision refs, tidak menerima role approver umum sebagai bukti |

Pin tetap dipegang jika respons finalise hilang sampai rekonsiliasi service_operations memastikan hasil. Tidak melepaskan hanya karena TTL upload habis. Internal file metadata tidak memanggil access-check service sehingga tidak ada callback recursion. Semua jawaban internal terikat audience/correlation dan hanya diakses caller minimum.

### API service referensi

Semua endpoint memverifikasi caller dan konteks aktor/scope terkini; tidak menerima public cookie browser. Operator seed memakai jalur provisioning terpisah, tidak membuka endpoint mutasi trip untuk model.

Service memverifikasi policy eligibility terkini pada submission, information update dan decision selain digest salinan lokal. Untuk locked claim, delegasi dan claim binding tersimpan membuktikan hubungan paket tanpa menerima klaim ownership dari field bebas. Paket withdrawn/known-conflict menahan mutasi, termasuk keputusan claim lama. Status bisnis tidak berubah menjadi rejected hanya karena hold teknis; pembacaan/audit tetap mengikuti izin. Replay hasil yang sudah committed tidak membuat mutasi baru dan tetap memerlukan reotorisasi akses.

| Endpoint | Kontrak |
| --- | --- |
| GET /service/v1/trips; GET /service/v1/trips/{id} | Trips/items milik aktor, tenant dan relasi yang diverifikasi |
| PUT /service/v1/policy-packages/{id}/versions/{version} | Publisher delegation; immutable digest+rules+dates/timezone, idem key; conflicting digest 409, ack digest |
| POST /service/v1/claims | operation_id/key, frozen payload+consent proof+policy+trip/item/receipts; validate+pin receipts, lalu claim/stage/reservation/ledger/audit satu transaksi |
| POST /service/v1/claims/{id}/information | operation_id/key, expected_version, frozen revision+consent; only needs_information, locked policy, reset approval stages atomik |
| POST /service/v1/claims/{id}/decisions | operation/key+expected_version+revision/stage, human-decision delegation; current actor != owner, stage/order valid; decision/state/ledger/audit atomik |
| GET /service/v1/claims; GET /service/v1/claims/{id} | Owner/current active reviewer; state/version/stage/next actor, locked policy refs dan declared money |
| GET /service/v1/operations/{operation_id} | Reotorisasi caller/actor; ledger result committed/rejected/sealed_no_commit atau 404 unknown belum terbukti no-commit |
| POST /service/v1/operations/{operation_id}/seal-if-absent | Recovery caller scoped; transaksi mengunci identitas action. Jika hasil committed kembalikan hasil; jika absent tulis tombstone terminal no-commit. Future dispatch ID/key sama ditolak. |

Claim response minimum: `{id,reference,state,version,revision,active_stage,policy_ref,trip_ref,items,total_minor,currency,receipt_versions,last_updated_at,next_actor,next_action}` sesuai akses. Amount adalah deklarasi karyawan sampai diperiksa manusia; service tidak mengklaim OCR verification. Client mengirim expected_version, bukan status akhir.

## 6. Transisi dan transaksi

| Objek | Transisi yang diperbolehkan | Gate |
| --- | --- | --- |
| File | quarantine → ready/rejected; ready → retained/deletion_pending | Scan, immutable version/digest, pins/retention; revocation akses terpisah dari deletion |
| Knowledge | draft → processing → ready → published → superseded/withdrawn; processing → failed → retry | Publication pointer dan generation job sah; ACL sebelum output, source withdrawn tidak di-republish diam-diam |
| Policy | draft → prepared → active → withdrawn | Ack service digest, publisher scope, date mapping; withdrawal hold, bukan overwrite locked package |
| Draft/action | editable → frozen/queued → sending → committed atau unknown → committed/not_committed | Snapshot+consent+outbox satu transaksi; no edit saat frozen; unknown hanya resolve bukti/atomic seal |
| Claim | submission → pending_approval | Service validation, deadline dan reservations pada commit |
| Claim | pending_approval → needs_information/rejected; approve stage → pending next/approved | Current human actor/stage, expected_version, no self approval, alasan wajib |
| Claim | needs_information → pending_approval | Owner confirms new revision, locked rules dan route, reset stage approvals; deadline submission asli tidak dihitung ulang |
| Claim | approved/rejected → tidak ada mutasi bisnis lanjutan MVP | No reopening/cancellation/payment; idempotent replay hasil lama tetap boleh |

Concurrency per action/claim menggunakan row lock atau compare-and-swap version yang setara dan constraint UQ; release item reservation setelah rejection atomik. Resubmission item rejected memakai action baru+link previous claim dan deadline normal. Item sama tidak berada dalam lebih dari satu pending/needs-info/approved claim. Service memeriksa penambahan/pengurangan item pada information update dan menjaga reservation atomik.

Deadline dihitung akhir hari lokal N hari kalender setelah selesai perjalanan, kemudian dibanding timestamp penerimaan service yang tersimpan. Lookup/replay hasil committed memakai submitted_at asli; retry tidak menjadikan submission lama terlambat. Jika action belum commit, penerimaan pertama yang sah harus masih tepat waktu. Paket aturan berdasarkan tanggal selesai perjalanan; tidak tersedia/konflik/withdrawn/ack hilang berarti hold, tidak menebak paket terdekat.

Ledger key+hash, claim/reservation/stages dan audit outcome ditulis satu transaksi service. Panggilan eksternal pin/actor check dilakukan sebelum transaksi singkat, dengan final verification dan expected versions sebelum commit; pin tidak mengirim keputusan. Retry/reconcile finalise mengatasi tidak adanya transaksi lintas database.

Lease generation dibuktikan pada setiap write job/run/checkpoint/publication. External request stale boleh tiba, tetapi action snapshot/key tetap sama dan service memastikan satu hasil. Seal-if-absent dan commit memakai lock identitas yang sama sehingga worker lama tidak dapat commit setelah tombstone. Session/membership revocation memblokir dispatch/resume baru; lookup hasil tetap terotorisasi dan tidak melanjutkan side effect.

## 7. Retensi, residency, dan data sensitif

| Kategori | Default akses | Kebijakan yang wajib dikunci sebelum data sensitif |
| --- | --- | --- |
| Knowledge/chunks/embedding | ACL tenant+published/version | Masa simpan versi, expiry, region dan penghapusan indeks |
| Percakapan/checkpoint/source dependencies | Owner+current source access | TTL, history filtering, cleanup cache/checkpoints |
| Draf/consent/actions/outbox | Owner dan worker scoped | TTL editable, frozen/unknown tidak dihapus sebelum reconcile; bukti action dijaga selama hasil/restore terkait masih ada |
| Receipt dan pins | Owner atau active reviewer verified | Retention bukti claim, pin orphan reconcile, delete bytes/versions |
| Claims/decisions/service ledger | Owner/active reviewer, audit scope eksplisit | Retention financial records dan safe idempotency tombstones; angka diputuskan pemilik data |
| Audit/trace | Metadata operational scoped, konten diagnostik eksplisit | Redaksi, TTL, akses, erase/export, minimum evidence yang dipertahankan |
| Backup/config/deletion journal | Operator recovery scoped | Region, retention, encryption, restore hold/replay deletion dan revocation |
| Model/embedding processing | Egress minimal dari authorized source | Provider/region/retention/training-use/telemetry yang diterima pemilik data; secret tidak dikirim |

Nilai numerik belum disetujui untuk data sensitif. Registry konfigurasi retensi/residency harus lengkap dan lolos SEC-08 sebelum penggunaan tersebut; development/evaluation menggunakan data fiktif. Penghapusan bersifat terorkestrasi, mencakup turunan dan backup handling, tidak menghapus claim evidence/ledger secara tidak konsisten. Kontrak ini bukan klaim kepatuhan hukum/sertifikasi.

Recovery journal memiliki tenant_id, sequence, event_id, affected typed resource/principal, action deny/delete/access-change/package-withdrawal, intent/result, timestamp dan integrity metadata. Sumber append-only durabel berada di failure domain terpisah dari backup DB. Snapshot mencatat journal watermark; latest complete watermark diperoleh dari sumber independen saat recovery. Intent dicatat sebelum revoke/delete/perubahan akses dinyatakan berhasil; hasil ambigu direplay sebagai deny konservatif. Gap sequence atau watermark yang tidak dapat dibuktikan menahan scope terkait, bukan menganggap replay selesai. Uji meliputi peristiwa sesudah backup, bukan hanya isi journal yang sudah ada dalam snapshot. Detail media/replication dan retention journal dikunci sebelum deployment recovery M3 diterima.

## 8. Penutupan kontrak implementasi

Sebelum implementasi fitur privat, turunkan schema migration/OpenAPI dan tetapkan invariant, trust boundary, canonical hashing/delegation, scope mapping serta batas resource awal. Spike/prototype memakai data fiktif dan konfigurasi sementara yang aman; nilai provider/session/rotation/retry diselesaikan sebelum fitur bergantung padanya, dan retensi/residency sebelum data sensitif dipakai. Workload, benchmark gates dan manifest final dikalibrasi lewat prototype, kemudian dibekukan sebelum acceptance sesuai TEST_PLAN §5. Tidak ada kewajiban mengunci hasil kalibrasi sebelum prototype yang diperlukan untuk mengukurnya. Perubahan lintas dokumen dicatat dengan requirement/test ID, alasan, dampak dan review QA.
