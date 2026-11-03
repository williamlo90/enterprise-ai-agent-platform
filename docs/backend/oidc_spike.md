# SPIKE-01 — Cognito feasibility probe

Status: **persiapan implementasi lulus QA; belum lulus login/callback nyata**. Pengguna memilih Cognito dan menyelesaikan setup console melalui Guru. Client secret tersedia pada file lokal yang diabaikan Git; nilainya tidak dimuat ke chat. Vercel tetap menjalankan bootstrap API development.

## 1. Provider preflight

CLI terpisah membaca hanya file konfigurasi eksplisit, tidak memakai credential DB atau membuat sesi/principal/membership. Mode default memeriksa config tanpa request. Semua hasil berupa flags generik; exception/secret/token tidak ditampilkan.

```powershell
python -m uv run --locked --project backend enterprise-ai-oidc-check --env-file local/oidc/cognito.env
python -m uv run --locked --project backend enterprise-ai-oidc-check --env-file local/oidc/cognito.env --live --check-client
```

Hasil actual pada 3 November 2026: issuer/endpoints exact cocok discovery, response_type code dan RS256 tersedia, JWKS RSA/RS256 valid, dan token endpoint dengan client_secret_basic menerima request synthetic invalid code lalu mengembalikan invalid_grant. Ini bukti respons provider untuk probe negatif, **belum** bukti login pengguna, PKCE exchange sukses atau callback terdaftar benar. Tidak memanggil userinfo dan tidak menyimpan access/ID/refresh token. Evidence discovery/canary tersanitasi berada di local/oidc dan diabaikan Git.

## 2. Kelayakan host callback

Canary code/state sintetis pada callback 404 Vercel terbukti muncul utuh dalam tab Request → Search Params pada Runtime Logs. Stdout aplikasi hanya metadata request; redaksi aplikasi tidak mengubah penyimpanan query di platform. Ini sesuai [dokumentasi Vercel](https://vercel.com/docs/logs/runtime#log-details). Tidak ada credential/token/code pengguna nyata pada canary. Filter dashboard, menonaktifkan drain, atau redirect sesudah callback tidak membuktikan pencegahan penyimpanan query.

Pengguna secara eksplisit memilih mempertahankan kontrak no-query-login-logging dan menyiapkan probe pada cloud dengan logging yang dapat dikendalikan. Probe mandiri AWS Lambda Function URL dipersiapkan; actual canary CloudWatch wajib sebelum login. Cognito tetap sama. Provider response_mode=form_post tidak diasumsikan tersedia. Bootstrap Vercel tetap lulus QA, tetapi tidak menjadi host callback probe ini.

## 3. Scope probe AWS

- Satu Lambda Python 3.13/x86_64, 256 MB, timeout 25 detik, reserved concurrency 2; Function URL HTTPS publik agar redirect browser bisa mencapai callback. Launcher membutuhkan operator secret 256 bit + Origin exact + Fetch Metadata same-origin. Tidak membuka endpoint login pada API Vercel.
- DynamoDB single table khusus probe, encrypted at rest. State/nonce/browser binding disimpan sebagai domain-separated hash; PKCE verifier dienkripsi Fernet dengan key tersendiri. Transaksi maksimal 300 detik, diperiksa saat claim/result. Physical TTL cleanup DynamoDB dapat terlambat; batas expiry tidak bergantung pada cleanup. Ciphertext pending yang expired dapat masih tersimpan sampai TTL/cleanup stack; ini belum kebijakan retensi sesi produk AUTH-02.
- Deadline bersama diturunkan dari remaining-time Lambda, maksimum 24 detik dengan satu detik untuk respons; setiap SDK call memakai sisa budget, connect/read maksimal satu detik masing-masing tanpa retry. HTTP provider memakai sisa budget dengan cadangan tiga detik finalisasi (token maksimal 10 detik, JWKS maksimal 5 detik). Claim kondisional atomik memeriksa binding (partition key), state, expiry dan pending; menghapus verifier sambil mengembalikan old values. Penukaran kode hanya dilakukan setelah respons claim sukses. Ambiguous timeout/crash memerlukan login baru, tidak mencoba claim/exchange ulang. Login baru menghapus transaksi browser lama dalam transaksi yang sama dengan create.
- Budget **100 launch seumur stack**, rate global **10 operasi start/callback/result per fixed minute** dan concurrency 2. Rate ini lebih ketat daripada target per-IP produk, khusus probe operator terbatas. Rate/store failure menghasilkan 503 generik; tidak mengklaim error taxonomy/rate policy AUTH-02 lengkap. Maksimum 100 row transaksi + dua control rows; TTL bukan alat enforcement kapasitas. Batas ini **bukan batas tagihan AWS atau proteksi DDoS**; ingress publik tetap dapat menimbulkan usage.
- JWT memakai PyJWT/cryptography RS256 dan key RSA >=2048 bit. Header/key URL asing, duplicate JSON, wrong issuer/audience/azp/nonce/token_use, malformed NumericDate, expiry/iat/nbf ditolak. Cognito token_use wajib id. Fresh JWKS fetch hanya sesudah claim sah, bounded/no redirects; kebijakan cache/refresh lengkap AUTH-01 belum diimplementasikan pada probe.
- Hasil hanya boolean provider_verified dan application_session_created=false, terikat cookie browser Secure/HttpOnly/SameSite=Lax. Callback 303 ke halaman hasil tanpa query; no-store/no-referrer/CSP. Tidak mengembalikan identitas atau token. Tidak ada session cookie platform, CSRF aplikasi, tenant switch, membership grant atau role escalation endpoint.
- Secret Cognito/operator/enkripsi berada pada **satu Secrets Manager secret**, bukan CloudFormation parameters, artifact ZIP atau environment plaintext. Lambda environment hanya memuat ARN secret dan provider settings non-secret. IAM execution role dibatasi ke satu table, satu secret dan log group fungsi sendiri. Tidak memberi IAM role kepada browser.
- Handler tidak mencetak event/URL/query/headers/body/claims/exception. Transport logs disanitasi; tracing tidak diaktifkan oleh template. Pengaturan ingress/runtime dan tidak adanya data query pada CloudWatch harus dibuktikan actual, termasuk canary saat fungsi enabled; source saja bukan kelulusan logging.

## 4. Artifact dan persetujuan resource

Template: [infra/aws/oidc_spike.yaml](../../infra/aws/oidc_spike.yaml). Build Linux ZIP dari dependency terkunci tanpa membaca file credential:

```powershell
python scripts/spikes/build_oidc_lambda.py
```

Builder membuat direktori unik di ignored local/oidc, mengunduh wheels Linux Python 3.13 dengan hashes lockfile, memasukkan source package serta memeriksa ukuran uncompressed <250 MiB dan tidak ada .env. Import native Linux tetap perlu dibuktikan di Lambda; tes Windows bukan bukti startup Linux.

Provisioner [deploy_oidc_lambda.py](../../scripts/spikes/deploy_oidc_lambda.py) memerlukan --approve-provision setelah pengguna menyetujui resource dan budget. **Belum dijalankan.** Resource yang akan dibuat: satu bucket S3 artifact private/encrypted/TLS-only, satu CloudFormation stack, Lambda+Function URL dan invocation policies, IAM execution role terbatas, satu DynamoDB table PAY_PER_REQUEST, satu Secrets Manager secret, dan CloudWatch log group retensi satu hari. AWS CLI STS read-only terautentikasi, tetapi itu bukan persetujuan membuat resource berbayar/IAM. Tidak membuat AWS access key baru. Tidak menyentuh Neon atau resource proyek lain.

```powershell
# Jalankan hanya setelah persetujuan resource dan biaya; gunakan ZIP dari build terbaru.
python -m uv run --locked --project backend python scripts/spikes/deploy_oidc_lambda.py --env-file local/oidc/cognito.env --artifact PATH_ZIP_AKTUAL --approve-provision
```

Script memeriksa source ZIP masih identik, membuat manifest resource lokal sejak awal dan menolak rerun bila manifest sudah ada. Gagal/partial provisioning perlu rekonsiliasi manifest, tidak membuat stack kedua secara buta. Client secret dimuat internal dan hanya dikirim ke Secrets Manager AWS untuk probe ini. Operator key disimpan lokal pada local/oidc/aws-spike-operator.key, tidak ditampilkan. Function selalu tetap disabled sesudah provisioning/configure; tidak mengaktifkan login otomatis.

Setelah actual Function URL tersedia: uji ingress/query canary dan CloudWatch, daftarkan **Function URL origin + /api/v1/auth/callback** exact pada Cognito, konfigurasi PUBLIC_ORIGIN/redirect exact, lalu enable probe dengan review. Gunakan origin probe langsung di browser; jangan merelay code melalui Vercel. Pengguna sendiri memasukkan operator key/password/MFA pada browser. Login sukses harus menghasilkan provider_verified=true tanpa membuat sesi platform. Verifikasi parallel/replay/expiry/wrong binding/provider failure secara terkontrol dan store AWS nyata. Bersihkan stack/table/secret/log group serta exact S3 artifact/bucket setelah scope uji selesai; jangan melakukan delete prefix atau resource lain. Manifest exact stack ID/account/bucket/key harus direkonsiliasi sebelum cleanup.

## 5. Acceptance dan batas status

Preflight provider, unit tests dengan kunci RSA sungguhan dan fake HTTP/store, SDK Stubber, cfn-lint dan build artifact dicatat terpisah dari actual AWS/DynamoDB/Cognito evidence. Tests terkontrol tidak membuktikan atomicity database cloud atau integrasi login. SPIKE-01 hanya ditutup setelah actual provider login/callback proof dan QA recheck; AUTH-01 dibekukan sesudahnya, kemudian AUTH-02 mulai. Host/probe ini tidak menutup OPS-01 atau production security seluruh platform.

Referensi: [Cognito authorization](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html), [token endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html), [Lambda Function URLs](https://docs.aws.amazon.com/lambda/latest/dg/urls-invocation.html), [Lambda Python logging](https://docs.aws.amazon.com/lambda/latest/dg/python-logging.html), [DynamoDB UpdateItem](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_UpdateItem.html).

### Bukti persiapan — 3 November 2026

- QA independen **PASS persiapan** setelah temuan deadline gabungan, malformed input dan expiry yang terlewati selama store work diperbaiki dan direproduksi ulang. Tidak ada temuan material tersisa dalam scope source persiapan.
- Regresi offline dan clean non-editable export lulus **244 tests, 3 live DB skipped** sebelum dua regression cases expiry ditambahkan. Final source diuji terfokus dari wheel bersih: **58 tests passed**, termasuk dua cases expiry baru; tidak menjumlahkannya dengan suite penuh karena overlap. Architecture guard dan git diff checks lulus.
- CloudFormation cfn-lint lulus. ZIP Linux rebuilt mengikuti source terakhir pada ignored local/oidc/lambda-build-020762355f/oidc-spike.zip; hanya dependency Linux terkunci dan source package, tanpa dotenv atau credential. Artifact versi lama tidak dipakai untuk deploy; provisioner memeriksa byte source ZIP terhadap source working tree.
- Belum provision AWS atau mengimpor credential ke Vercel. Selanjutnya persetujuan resource/biaya/IAM, deploy disabled, canary actual, callback registration, enable, actual DynamoDB checks serta login manusia dan QA. SPIKE-01 tetap dikerjakan.
