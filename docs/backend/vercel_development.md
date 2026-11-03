# Vercel development API

Pengguna memilih Vercel sebagai pengganti laptop + ngrok pada 3 November 2026. Tujuannya backend development berjalan di cloud tanpa proses lokal terus menyala. Ini jalur development/SPIKE-01, bukan penyelesaian OPS-01 atau perubahan target deployment AWS enterprise. Cognito tetap provider login; Neon tetap database. Tidak ada endpoint OIDC yang tersedia sebelum probe/implementasi terkait dibuat.

## Import dan deployment pertama

1. Di Vercel, import private GitHub repository `williamlo90/enterprise-ai-agent-platform` yang sudah ada. Gunakan project baru `enterprise-ai-platform-dev`, bukan project deployment aplikasi lain.
2. Set **Root Directory = backend** dan **Framework Preset = FastAPI**. Manifest/lockfile ada di direktori itu. Gunakan dependency installation bawaan dengan uv.lock; tidak memakai npm build, CLI enterprise-ai-api atau perintah uvicorn sebagai Build Command. Tidak ada Output Directory statis.
3. `pyproject.toml` memilih Python >=3.13,<3.14 dan entrypoint `asgi:app`. `asgi.py` membuat app dari environment platform, tidak membaca .env lokal dan tidak menjalankan Uvicorn sendiri. `vercel.json` menetapkan region Singapore `sin1` dan maxDuration 30 detik. Periksa region/build log aktual setelah deploy; konfigurasi source bukan bukti region terpasang.
4. Masukkan environment variables berikut sebelum build/deploy pada environment target development ini. Deployment yang disebut Production oleh Vercel tetap aplikasi development kita, sehingga gunakan config produksi yang menutup docs publik:

| Variable | Nilai bootstrap |
| --- | --- |
| APP_ENV | production |
| APP_HOST | 127.0.0.1 |
| APP_PORT | 8000 |
| APP_DATABASE_ENABLED | false |
| APP_MAX_REQUEST_BODY_BYTES | 1048576 |
| APP_REQUEST_BODY_TIMEOUT_SECONDS | 10 |

Host/port diperlukan validasi konfigurasi, bukan public URL atau perintah bind server di Vercel. Deploy pertama hanya membuktikan health/bootstrap. Jangan memasukkan DATABASE_DIRECT_URL, file .env, AWS access keys atau Cognito client secret untuk bootstrap. Nilai auth belum menjadi Settings executable. Untuk uji DB berikutnya, tambahkan **DATABASE_URL restricted runtime** sebagai secret platform dan APP_DATABASE_ENABLED=true; verifikasi restricted login/TLS/readiness/pool actual sebelum mengklaim DB cloud lulus. Migration dijalankan dari jalur administratif terpisah, bukan API startup/build.

5. Deploy dan ambil **domain project tetap** yang diberikan Vercel (bukan URL unik commit/preview yang berganti). Periksa GET /health/live dan /health/ready → 200, /docs → 404, callback yang belum tersedia → 404. Jangan mengklaim login lulus berdasarkan health. Build/health aktual masih perlu dibuktikan; belum ada deployment cloud dari perubahan source saja.
6. Pastikan aturan Deployment Protection pada domain pengujian mengizinkan browser pengguna/Cognito callback mencapai aplikasi. Jika ada login Vercel/interstitial, selesaikan pengaturan akses domain test secara sengaja; jangan taruh bypass token dalam callback URL. Restriksi environment lain tetap sesuai kebutuhan.
7. Setelah domain tetap terkonfirmasi, ubah Allowed callback URLs Cognito app client ke **https://DOMAIN-VERCEL-YANG-AKTUAL/api/v1/auth/callback**, exact dan tanpa wildcard, menggantikan URL ngrok. PUBLIC_ORIGIN target = origin yang sama. Itu pola instruksi, bukan URL siap didaftarkan. Jangan memasukkan domain contoh atau preview berubah-ubah. Pembaruan callback memerlukan domain actual dan penyesuaian konfigurasi probe SPIKE-01; belum membuat endpoint berfungsi otomatis.
8. Sesudah health cloud terbukti, terminal Uvicorn dan ngrok lokal dapat dihentikan (Ctrl+C pada masing-masing terminal). Menutup tunnel tidak mematikan Cognito atau database. Simpan konfigurasi lokal untuk pengembangan sesuai kebutuhan; tidak menghapus resource otomatis.

## Batas dan verifikasi lanjutan

Vercel mendukung lifespan FastAPI, tetapi shutdown cleanup dibatasi 500ms setelah SIGTERM. DB adapter sekarang memakai cleanup deadline hingga 5 detik; batas cloud itu perlu diuji saat DB diaktifkan, termasuk freeze/restart/concurrency dan aggregate connections lintas instances. Jangan menghapus cleanup/cancellation safeguards lokal untuk menyesuaikan platform tanpa bukti. Transaksi login/session/limiter yang harus bertahan lintas instance harus durable di store bersama; memory satu function tidak menjadi sumber kebenaran. SPIKE-01 mendemonstrasikan integrasi provider secara terpisah dari klaim session production lengkap AUTH-02.

Request/response function dibatasi 4.5 MB menurut dokumentasi platform. Login/health cukup kecil; target upload 10 MiB FILE-01 perlu jalur upload langsung ke private storage atau keputusan hosting lain sebelum diterapkan. Tidak mengurangi batas produk diam-diam. Worker/job/checkpoint di task plan belum dideploy; pemilihan runtime eksekusi panjang/durable perlu desain dan uji tersendiri.

Callback memuat kode sensitif pada query. Sebelum actual login SPIKE-01, periksa konfigurasi request logging/observability Vercel dan pastikan query callback, headers cookie/authorization, body token exchange dan raw exception tidak disimpan/ditampilkan. Redaksi log aplikasi saja tidak membuktikan redaksi log platform. Session/cookie Secure, issuer/audience, nonce/state/PKCE dan batas rate tetap mengikuti [identity protocol](identity_protocol.md).

Harga/fitur akun harus dicek pada Vercel saat setup; source ini tidak mengaktifkan paket berbayar atau menjanjikan biaya nol. Untuk acceptance, catat build/runtime Python, revision deployed, region, URL actual, HTTP health, DB jika diaktifkan, cold/warm behavior dan keterbatasan tersanitasi. Jangan log secret atau salin environment pengguna ke artifact.

Referensi: [FastAPI di Vercel](https://vercel.com/docs/frameworks/backend/fastapi), [Python runtime](https://vercel.com/docs/functions/runtimes/python), [function limits](https://vercel.com/docs/functions/limitations).

## Deployment bootstrap aktual — 3 November 2026

Project development `enterprise-ai-platform-dev` telah dideploy dari source `ea4dff0f1fb14aaa9090a092d3dfa7172a2dad60`, Root Directory backend, FastAPI, dashboard mencatat build 20 detik (alur import melaporkan 22 detik). Domain project tetap: https://enterprise-ai-platform-dev.vercel.app . Ini environment Production menurut Vercel, dengan scope aplikasi development/bootstrap.

Main melakukan HTTP eksternal tanpa login Vercel: /health/live 200 process, /health/ready 200 bootstrap, /docs 404 envelope, /api/v1/auth/callback 404 envelope, dan pengulangan health 200. Routing header menunjukkan region sin1. APP_DATABASE_ENABLED=false; tidak memasukkan credential DB/auth. Belum menguji cloud DB, forced cold start/restart/load, transaction persistence, OIDC atau logging callback provider. Target callback baru adalah https://enterprise-ai-platform-dev.vercel.app/api/v1/auth/callback ; **belum didaftarkan ulang ke Cognito melalui pekerjaan deploy ini**. Konfigurasi callback ngrok lama perlu diganti sebelum login test.

Setelah bootstrap cloud terverifikasi, pengguna dapat menghentikan terminal backend/ngrok lokal. Tidak ada proses lokal dihentikan otomatis agar proses proyek lain tidak terpengaruh. **QA independen PASS bootstrap cloud**: mereproduksi seluruh HTTP checks di atas tanpa interstitial login Vercel, region sin1, tanpa blocker bootstrap. Identitas revision mengikuti source pada dashboard deployment; HTTP publik tidak membuktikan commit. Bukti screenshot tersanitasi disimpan lokal pada local/vercel/deployment-ready.png dan diabaikan Git.
