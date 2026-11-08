# Enterprise AI — deployment AWS

Paket deployment mencakup container aplikasi, konfigurasi EKS, private service TLS,
IAM, security journal Lambda/DynamoDB, backup, rollback, dan recovery.
Panduan ini menjelaskan konfigurasi environment serta alur rilis dan operasi.

| ID | Implementasi | Operasi |
| --- | --- | --- |
| FE-ENV-01 | Container frontend–API satu origin, private service TLS, config/secret renderer | Konfigurasi dan pemasangan environment |
| FE-AUTH-01 | SPA auth enabled pada build; Cognito/BFF yang ada dipasang pada host sama; query logging off | Callback Cognito dan penggunaan login/logout/switch |
| OPS-01 | Dockerfiles, ECR/journal/backup CloudFormation, cluster/IRSA policy, pipeline manual | Review resource/biaya, provision dan deploy |
| OPS-02 | Probes/limits, replicas1–2, rolling/recreate, PDB, serial workers/parser, rollback | Konfigurasi rollout dan kapasitas |
| JOURNAL-01 | Authoritative triggers/outbox; intent sebelum commit, result sesudah commit; independent DynamoDB chain/head/replay | Konfigurasi journal dan IAM |
| OPS-03 | Backup DB/file versions/config; isolated held restore, journal replay/tombstones, release guard | Backup/restore dan rekonsiliasi pada target terisolasi |
| OPS-04 | Runbook rilis/rollback/recovery dan lembar target operasi | Konfigurasi target operasi |

Provider model dipilih melalui konfigurasi environment dan diaktifkan secara eksplisit.

## Konfigurasi dalam satu tempat

- `target.example.json` → private `deployment/target.json`: account/cluster,
  registry/image SHA, ACM certificate, public origin, tiga IRSA ARN, parser digest.
- `env/platform.env.example` → private platform env: scoped DB runtime, Cognito,
  existing private bucket, interservice signing/trust dan journal ARN/ID.
- `env/reference.env.example` → private service env dengan DB/credential berbeda.
  Service credential, dispatcher dan recovery credential harus berbeda.
- Worker env: gabungkan platform env dan `env/worker.extra.env.example`, lalu
  credential enrollment. Semua executor aktif harus terdaftar pada map worker.
- `recovery.example.json` → encrypted operator workspace. Migration URLs tidak
  masuk pod API/worker, build argument, image, Git atau chat.

Isi seluruh `REPLACE_`. Gunakan dotenv → `secret_manifest.py` untuk PEM/JSON
multiline; jangan memasang dotenv multiline mentah lewat `--from-env-file` kubectl.

## Deployment — dijalankan operator

1. Review resource cluster, NLB, node, Lambda, DynamoDB, ECR dan backup storage.
   Tetapkan anggaran environment dan ambil provider snapshot sebelum upgrade.
2. Isi `infra/aws/cluster.yaml`: supported EKS version, operator/VPN CIDR,
   secrets-encryption KMS untuk target. Jalankan `eksctl create cluster -f
   infra/aws/cluster.yaml`. Pasang Load Balancer Controller dengan IRSA mengikuti
   dokumentasi primer di bawah. Aktifkan VPC CNI policy **strict** sebelum parser.
3. ZIP `infra/journal/handler.py` sebagai `handler.py` di root ZIP, upload ke
   artifact bucket private. Review change set `deployment-foundation.json` sebelum
   deploy. Pilih JournalId tetap; jangan mengganti journal dari snapshot platform.
   Simpan output writer ARN platform/reference dan reader ARN.
4. Isi policy `infra/aws/iam/*.example.json` dengan resource ARN exact. Buat role
   IRSA untuk service accounts platform/reference/worker di namespace enterprise-ai.
   App hanya invoke writer sendiri; reader untuk recovery. App tidak mendapat
   akses langsung tabel journal atau database migration/restore.
5. Pasang platform schema **41** dan service schema **7**. Platform:
   `enterprise-ai-migrate upgrade --env-file <private-file>`. Service: set private
   REIMBURSEMENT_MIGRATION_DATABASE_URL dan REIMBURSEMENT_DATABASE_URL, lalu
   `reimbursement-migrate --upgrade`. Jangan jalankan command verify/test.
   Provision scoped runtime role serta tenant/key/caller sesuai AUTH/POL/FILE.
6. Buat JSON daftar tenant UUID yang sudah terdaftar. Jalankan
   `python deployment/job_credentials.py --tenants <file> --expires-at
   <explicit-future-UTC> --out <new-private-directory>`. Apply `enroll.sql`
   sebagai operator dan merge `worker.env`. Nonaktifkan obsolete callers sendiri
   atau sertakan seluruh executor aktif; daemon menolak map yang tidak lengkap.
7. Workflow manual `.github/workflows/deployment.yml` membangun image serial,
   tanpa tes/inference. Configure GitHub Environment `aws-deployment`, variables
   AWS_RELEASE_ROLE_ARN, KUBECTL_VERSION, DEPLOYMENT_CONFIG_JSON. Default hanya
   build/push; pilih input deploy sendiri. Tag commit SHA immutable; parser dipin
   ke digest image hasil build.
8. Buat namespace enterprise-ai sebelum secret. Jalankan `private_tls.py --out
   <new-private-directory> --days 30`, lalu apply hanya `tls-manifest.json`.
   Simpan CA private key offline. Jalankan `secret_manifest.py` untuk platform,
   reference dan worker, lalu apply manifest privat yang dihasilkan.
9. Render `python deployment/render.py --config deployment/target.json --out
   deployment/generated/release.json`. Apply melalui `activate.ps1` atau pipeline.
   Worker default **0**, model default **false**.
10. DNS domain diarahkan ke NLB. Callback Cognito harus exact
    `https://<public-host>/api/v1/auth/callback`; PUBLIC_ORIGIN harus sama.
    Frontend memakai `/api` pada origin ini. `/internal` tidak diproxy publik.
11. Periksa koneksi aplikasi. Aktifkan satu worker setelah registrasi/konfigurasi
    cocok. Eksekusi model memerlukan pilihan eksplisit EAI_ENABLE_MODEL_EXECUTION
    serta konfigurasi provider pada template environment, beserta persetujuan biaya/retensi/routing.
    Endpoint Singapore tidak mengubah Nova Pro APAC menjadi Singapore-only.
    Privacy tetap false sampai policy serta owning physical evidence lengkap.

## Operasi dan rollback

- Atur replicas sesuai kebutuhan environment. Konfigurasi menyediakan replicas1–2,
  requests/limits, health probes, dan kebijakan rollout.
- Worker tunggal memakai Recreate; owning consumers mempertahankan heartbeat,
  lease/generation yang ada. Tidak ada daemon otomatis dari import API.
- Parser pod per file, quota1/CPU1/memory512Mi, read-only filesystem, dropped caps,
  UID65534, no token/env, network deny, admission image pin, pids32 pada program.
  Konfigurasi kubelet podPidsLimit32 menambah batas cgroup pada AMI pilihan.
  Input/output melalui exec, bukan container-main logs. Tidak ada Docker socket.
- `rollback.ps1` menahan worker dan melakukan deployment rollback aplikasi.
  Schema database tidak diturunkan. Simpan SHA image sebelumnya.
- TLS verification tetap aktif. Private CA hanya menambah trust interservice.
  Jangan log payload, cookie, token, prompt atau query callback.
- Isi `operations-targets.example.json` dengan target availability, RPO/RTO,
  queue lag, budget, retention, dan rotation untuk environment yang digunakan.

## Backup → restore → replay → release

Pakai PostgreSQL client yang kompatibel dan backend Python environment. Semua
file privat berada pada encrypted operator workspace di luar Git/OneDrive.

1. Blok traffic dan stop **seluruh producer/worker**, tunggu in-flight selesai.
   Set `producers_quiesced=true`. Snapshot dibatasi release41/service7; backup
   legacy sebelum upgrade memakai fasilitas provider.
2. `python deployment/recovery.py backup --config <private-json> --out <new-dir>`
   menulis DB dumps, exact file-version copies, config secrets terenkripsi dan
   manifest. Watermark independen diambil **sebelum** snapshots. Journal DynamoDB
   tidak disalin sebagai bagian backup platform.
3. Siapkan database baru pada **host berbeda**, nama `eai_restore_*`, serta bucket
   versioned `eai-restore-*`. Schema/managed runtime roles harus belum ada.
4. `recovery.py restore --config ... --manifest ... --out <new-dir>` membuat fresh
   runtime credentials, restore/rebind registry OIDs, memeriksa restored byte
   digest, lalu rebind physical object version IDs. Secret snapshot tidak
   diaktifkan otomatis. Target held dengan marker manifest; jobs lama ditahan.
5. `recovery.py replay ...` memverifikasi current independent epoch/head/chain.
   Missing sequence, stale challenge, bad chain atau changing head tetap held.
   Old sessions/login flows dan trust material dicabut; membership/principal dinonaktifkan; document/policy
   ditutup; claim tenant dikarantina. Conversation deletion direplay ke salinan
   konten dengan tombstone. Financial dependency ambigu tetap menahan scope.
6. Rekonsiliasi action/pin/service ledger melalui owning recovery authority dan
   terminal proof FLOW. Jangan menghapus ledger/pin atau menandai transaksi
   committed untuk meloloskan gate. Tidak ada blind financial replay.
7. `recovery.py release --config ... --manifest ... --out <new-dir>` drain
   committed outbox idempotently, lalu memeriksa applied denies, terminal actions
   dan current watermark. Jika journal maju, replay tail dan release lagi.
   Release tidak otomatis start pod. Claim tenant quarantine tetap dipertahankan.
8. Replay menonaktifkan key/caller/job credentials lama dan mengosongkan login/JWKS cache. Enroll fresh keys/callers setelah replay final; rotate/approve current secrets dan roles/policy. Setelah pemeriksaan recovery,
   arahkan host ke target dan mulai consumer baru. Old jobs tidak otomatis hidup.

## Rujukan primer

- [AWS Load Balancer Controller](https://docs.aws.amazon.com/eks/latest/userguide/lbc-helm.html)
- [Atomic DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html)
- [Arsitektur](../ARCHITECTURE.md) dan [kontrak data/API](../DATA_AND_API_CONTRACTS.md).
