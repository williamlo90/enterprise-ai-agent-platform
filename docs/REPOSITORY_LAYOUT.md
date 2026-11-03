# Repository layout

Status: struktur repository telah direview QA. BE-01 menambahkan package API, config, manifest/lockfile dan tes bootstrap; BE-02 menambahkan health/readiness, HTTP boundary dan shared schemas. Folder lain tetap menyiapkan implementasi. Belum ada fitur bisnis, migration, atau deployment executable.

Empat dokumen kontrak tetap di root. Struktur mengikuti [ARCHITECTURE.md](../ARCHITECTURE.md) dan tidak mengubah scope produk.

```text
enterprise-ai-agent-platform/
├── README.md
├── PRODUCT_CONTRACT.md
├── ARCHITECTURE.md
├── DATA_AND_API_CONTRACTS.md
├── TEST_PLAN.md
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   ├── features/
│   │   │   ├── auth/
│   │   │   ├── tenants/
│   │   │   ├── knowledge/
│   │   │   ├── chat/
│   │   │   ├── reimbursements/
│   │   │   ├── approvals/
│   │   │   └── admin/
│   │   ├── components/
│   │   ├── api/
│   │   └── locales/
│   ├── public/
│   └── tests/
│       ├── unit/
│       └── integration/
├── backend/
│   ├── src/enterprise_ai_platform/
│   │   ├── api/
│   │   │   ├── public/
│   │   │   └── internal/
│   │   ├── modules/
│   │   │   ├── identity/
│   │   │   ├── tenancy/
│   │   │   ├── knowledge/
│   │   │   ├── conversations/
│   │   │   ├── agent/
│   │   │   ├── policies/
│   │   │   ├── drafts/
│   │   │   ├── actions/
│   │   │   ├── files/
│   │   │   └── audit/
│   │   ├── integrations/
│   │   │   ├── identity/
│   │   │   ├── models/
│   │   │   └── reimbursement/
│   │   └── infrastructure/
│   │       ├── database/
│   │       ├── checkpoints/
│   │       ├── jobs/
│   │       ├── storage/
│   │       ├── observability/
│   │       └── security_journal/
│   ├── migrations/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       └── contracts/
├── reference_service/
│   ├── src/reimbursement_service/
│   │   ├── api/
│   │   ├── modules/
│   │   │   ├── access/
│   │   │   ├── trips/
│   │   │   ├── policies/
│   │   │   ├── claims/
│   │   │   ├── operations/
│   │   │   └── audit/
│   │   ├── integrations/platform/
│   │   └── infrastructure/database/
│   ├── migrations/
│   ├── fixtures/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       └── contracts/
├── contracts/
│   ├── openapi/
│   └── schemas/
├── tests/
│   ├── e2e/
│   ├── security/
│   └── resilience/
├── evaluations/
│   ├── datasets/
│   │   ├── development/
│   │   └── acceptance/
│   ├── rubrics/
│   ├── runners/
│   └── manifests/
├── scripts/
│   ├── development/
│   ├── checks/
│   └── operations/
├── deploy/
│   ├── containers/
│   ├── compose/
│   ├── kubernetes/
│   └── infrastructure/
├── docs/
│   ├── REPOSITORY_LAYOUT.md
│   ├── backend/
│   │   ├── architecture.md
│   │   ├── database_schema.md
│   │   └── task_plan.md
│   ├── decisions/
│   └── runbooks/
└── .github/workflows/
```

## Batas modul dan dependency

- API dan worker platform memakai package `enterprise_ai_platform` yang sama. Entry point API/worker, config, composition root, manifest dan lockfile dibuat pada tahap bootstrap, bukan diisi placeholder seolah executable.
- Route menangani transport/validasi dan memanggil service modul. Domain/service/ports diletakkan dalam modul sesuai kebutuhan; adapter SQL/provider memasok implementasi port. Domain tidak mengimpor FastAPI, SDK provider atau koneksi DB. Modul lain mengakses public service/port, bukan tabel atau repository internal modul.
- `modules/actions` memiliki consent/human action receipt, action identity, snapshot, state dan orkestrasi dispatch; `infrastructure/jobs` menyediakan claim/lease/outbox runner. State bisnis claim serta keputusan final milik service referensi. `modules/drafts` mengelola isi draf dan versinya; `modules/policies` mengelola paket aturan/eligibility. Detail transaksi consent+draft freeze+action/outbox dikoordinasikan oleh service aplikasi dan unit of work platform, tidak oleh route/model.
- `modules/files` mengelola metadata/pin/akses/lifecycle file; adapter bytes privat di `infrastructure/storage`. Knowledge mengurus parsing/index dan memanggil file service. Agent menggunakan layanan bisnis yang sama dengan formulir manual, bukan SQL atau SDK bebas.
- Identity membership/tenant policy aplikasi berbeda dari adapter OIDC di `integrations/identity`. Signed delegation, actor/eligibility/receipt API platform berada pada route internal dengan layanan domain terkait.
- Reference service merupakan package dan unit build/deployment terpisah. Tidak mengimpor source backend, berbagi runtime model ORM, migration atau credential DB. `integrations/platform` memanggil HTTP API actor, eligibility dan receipt; `modules/access` mengendalikan izin service dan unduh receipt melalui relasi claim. `modules/operations` memiliki idempotency ledger/seal; `modules/claims` memiliki stage, revisions, keputusan dan expense reservations.
- Schema publik antarlayanan berada di `contracts/` ketika OpenAPI/schema sudah dibakukan. Kode generated client mengikuti schema, tidak menjadi package domain bersama. Dokumen Markdown saat ini adalah sumber kontrak desain; versi executable ditambahkan dengan validasi drift, bukan duplikasi manual tanpa review.
- Migration platform dan service terpisah. `infrastructure/security_journal` adalah adapter ke journal durabel independen, bukan penyimpanan jurnal pada DB snapshot yang sama. Deployment storage independennya berada pada infrastructure config saat milestone terkait.

## Pengujian, evaluasi, dan artefak

- Tes yang hanya memeriksa satu package berada pada tests package tersebut. Root `tests/` khusus lintas web/platform/service: E2E, security boundary dan resilience; tidak menggandakan semua unit test.
- Fixture operasional service bersifat fiktif dan terpisah dari benchmark. Dataset development dan acceptance memiliki manifest, gold, versi dan kontrol perubahan; tidak dipakai sebagai data seed demo.
- Pemisahan folder acceptance tidak membuat isinya rahasia dari implementer. Holdout independen dikelola QA melalui proses terpisah/akses terbatas bila diperlukan, dan kasus yang sudah dipakai tuning dinyatakan regression sesuai TEST_PLAN.
- `evaluations/manifests` menyimpan konfigurasi dan ringkasan hasil yang telah disanitasi; raw trace, receipt, dokumen klien, dump DB, backup, dan hasil eksperimen lokal berada di `local/` atau `artifacts/` yang diabaikan Git.
- `deploy/compose` untuk dependency development/integration minimum; Kubernetes/IaC memiliki milestone pembuktian sendiri. Tidak ada manifest deployment palsu, CI kosong yang terlihat lulus, dependency terpasang atau klaim aplikasi dapat dijalankan pada tahap struktur.
- Folder kosong ditandai `.gitkeep` agar tersimpan di Git. File ini dihapus saat folder memiliki implementasi nyata. Tidak membuat file source/config/migration kosong yang menyiratkan fitur selesai.

## Kelanjutan bootstrap

Platform API mendapatkan manifest/lockfile, konfigurasi tervalidasi, entry point dan instruksi run/test pada BE-01; composition root dan dependency wiring diperinci pada BE-03. Unit worker/reference service mengikuti task masing-masing. Secret menggunakan environment atau secret manager; `.env.example` BE-01 hanya memuat tiga setting bootstrap tanpa credential. Struktur ini tidak otomatis mengizinkan pembuatan resource cloud atau pemakaian data sensitif.
