# Product Contract — Enterprise AI Agent Platform

Status: brief masalah dan pengguna, skenario dan alur layanan, kebutuhan dan batas MVP, aturan bisnis/data/kewenangan, serta rencana pengujian dan ukuran keberhasilan telah dibahas dan disepakati. Tahap perencanaan; belum merupakan hasil implementasi.

Dokumen ini mencatat arah produk dan keputusan perencanaan. Rencana pengujian terdapat pada bagian 5. Kontrak teknis, stack, komposisi kasus terperinci, dan ambang penerimaan final ditetapkan sebelum evaluasi penerimaan.

Dokumen turunan: [ARCHITECTURE.md](ARCHITECTURE.md) menjelaskan komponen, tanggung jawab, aliran data, dan deployment; [DATA_AND_API_CONTRACTS.md](DATA_AND_API_CONTRACTS.md) merinci model data, API, akses, dan perubahan state; [TEST_PLAN.md](TEST_PLAN.md) menurunkan kebutuhan menjadi pemeriksaan dan bukti kelulusan. Dokumen turunan memperinci kontrak ini tanpa mengubah batas MVP. Nilai operasional/provider yang masih terbuka dikunci melalui spike dan acceptance manifest sebelum tahap terkait dinyatakan lulus.

## 1. Brief masalah dan pengguna

### Definisi produk

Enterprise AI Agent Platform adalah platform untuk membangun dan mengelola agent AI bagi beberapa perusahaan, dengan pengetahuan, akses, integrasi sistem, dan aturan yang terpisah untuk setiap klien.

Platform mencari pengetahuan internal dan menjalankan workflow bisnis melalui API, konfirmasi pengguna, serta persetujuan pihak yang berwenang. MVP berfokus pada kebijakan perjalanan dan pengajuan reimbursement untuk dua tenant. RAG menjadi checkpoint implementasi pertama, kemudian dilengkapi workflow sebelum MVP dinyatakan selesai.

Satu perusahaan klien menjadi satu tenant. Setiap tenant memiliki sumber pengetahuan, pengguna, konfigurasi, dan integrasinya sendiri.

### Brief masalah

Tim pengembang dan implementasi AI perlu membangun agent untuk berbagai perusahaan dan proses bisnis. Kebutuhan tersebut memiliki fondasi serupa: orkestrasi langkah agent, pencarian pengetahuan, integrasi API, pemeriksaan akses, dan penerapan aturan bisnis.

Jika setiap solusi dibangun terpisah, pekerjaan fondasi tersebut berulang. Integrasi dan perilaku agent menjadi sulit dijaga konsistensinya, sementara perubahan perlu diterapkan pada beberapa implementasi. Setiap perusahaan juga membutuhkan dokumen, sistem, serta aturan yang berbeda.

Platform menyediakan fondasi bersama yang dapat digunakan ulang dan dikonfigurasi untuk setiap tenant. Isolasi tenant menjadi kebutuhan inti agar penggunaan infrastruktur bersama tidak membuka akses ke data perusahaan lain.

Di sisi pengguna akhir, masalah yang ingin dibantu adalah kesulitan menemukan informasi internal yang relevan dan memahami langkah untuk menyelesaikan kebutuhan bisnis. Pengetahuan dan prosedur dapat tersebar di berbagai dokumen dan sistem, sehingga pengguna harus mencari sendiri atau bertanya kepada tim terkait.

#### Contoh konkret

Perusahaan A dan B sama-sama membutuhkan assistant untuk membantu pengajuan pembelian perangkat. Keduanya memerlukan pencarian kebijakan, klarifikasi kebutuhan, pembacaan katalog melalui API, konfirmasi pengajuan, dan persetujuan.

Namun, dokumen kebijakan, sistem procurement, serta aturan persetujuan mereka berbeda. Sebagai ilustrasi, A meminta persetujuan kepala divisi untuk pembelian di atas Rp15 juta, sedangkan B meminta persetujuan Finance untuk pembelian di atas Rp10 juta.

Tim implementasi menggunakan fondasi platform yang sama, lalu mengatur sumber pengetahuan, konektor, dan aturan masing-masing tenant. Konektor untuk sistem baru tetap membutuhkan implementasi; konfigurasi tidak otomatis menggantikan seluruh pekerjaan integrasi.

Contoh procurement ini menjelaskan konsep penggunaan ulang platform dan berada di luar MVP. Workflow MVP yang dipilih adalah pengajuan reimbursement.

### Pengguna dan peran

| Kelompok | Peran dan kebutuhan |
| --- | --- |
| Tim pengembang / implementasi AI | Pengguna utama platform. Membuat dan mengonfigurasi agent, menghubungkan sumber pengetahuan serta API, dan memeriksa perilakunya tanpa mengulang seluruh fondasi. |
| Administrator perusahaan klien | Mengelola pengguna, hak akses, sumber pengetahuan, dan konfigurasi dalam tenant yang menjadi kewenangannya. |
| Pemilik proses bisnis / pengelola pengetahuan | Menentukan kebijakan, menjaga sumber informasi, dan menetapkan aturan serta pihak pemberi persetujuan. |
| Karyawan perusahaan klien | Pengguna akhir untuk arah layanan awal. Mengajukan pertanyaan, memperoleh jawaban bersumber, dan memahami langkah berikutnya sesuai aksesnya. |
| Approver / petugas pelaksana | Memeriksa, menyetujui, menolak, atau meminta kelengkapan pengajuan reimbursement sesuai kewenangannya. |
| Operator platform | Mengoperasikan layanan bersama, memantau gangguan, dan mengelola lifecycle tenant. Akses administratif operasional tidak otomatis memberi hak membaca seluruh isi tenant. |

Perusahaan klien merupakan organisasi pelanggan platform. Satu orang dapat menjalankan beberapa peran sesuai kewenangannya. Pelanggan eksternal perusahaan dapat menjadi pengguna agent pada pengembangan berikutnya; dukungannya belum ditetapkan sebagai scope awal.

### Dasar masalah dan riset

Deskripsi lowongan menjadi acuan kemampuan: reusable agent orchestration, knowledge retrieval, API integrations, business rules, dan tenant-level configurations. Dari acuan tersebut, masalah pekerjaan fondasi yang berulang menjadi interpretasi produk yang dipilih dalam pembahasan ini.

Riset awal mendukung masalah pada sisi pengguna akhir dan koordinasi layanan internal:

- [Handbook GitLab — Glean End User Guide](https://handbook.gitlab.com/handbook/eta/ai/tools/glean/): GitLab menjelaskan pengetahuan yang tersebar, waktu pencarian, jawaban yang bertentangan, dan hambatan menemukan informasi saat onboarding. Ini dokumentasi langsung perusahaan.
- [Pengalaman pekerja HR — New Hire Onboarding Process](https://www.reddit.com/r/humanresources/comments/135jx2e/new_hire_onboarding_process/): penulis menceritakan kebutuhan mengejar manajer untuk melengkapi spreadsheet akses sebelum tiket IT dapat dibuat. Ini laporan pengalaman individu.
- [Pengalaman pengelola onboarding IT](https://www.reddit.com/r/ITManagers/comments/1djddp2/upper_management_asked_to_create_an_it_onboarding/): praktisi menjelaskan permintaan yang melewati beberapa tim sehingga informasi dapat hilang atau berubah. Ini laporan pengalaman individu.
- [Pengalaman pemeliharaan knowledge base](https://www.reddit.com/r/sysadmin/comments/1ikvmm9/keeping_a_knowledge_base_up_to_date_is_a_nightmare/): pengelola tim melaporkan wiki yang jarang diperbarui dan pertanyaan berulang melalui Slack. Ini laporan pengalaman individu.
- [Implementasi internal Atlassian](https://www.atlassian.com/blog/jira-service-management/using-jira-service-management-to-improve-hr): penggunaan integrasi Workday, Jira Service Management, dan Workato menunjukkan penerapan otomasi lintas sistem untuk onboarding. Kasus ini juga menunjukkan peran otomasi konvensional dalam menyelesaikan proses yang terstruktur.

Sumber tersebut mendukung keberadaan masalah informasi dan koordinasi. Sumber tersebut belum membuktikan permintaan pasar atau kesediaan membayar untuk platform kita, maupun besarnya penghematan dari penggunaan ulang komponen oleh developer. Manfaat platform akan diperiksa melalui penerapan konkret dan evaluasi.

### Hasil yang dituju

- Tim implementasi dapat menggunakan fondasi yang sama untuk beberapa tenant, dengan perbedaan konfigurasi dan konektor yang jelas.
- Perusahaan klien memiliki assistant yang memakai pengetahuan serta aturan miliknya dan menghormati hak akses pengguna.
- Karyawan mendapat jawaban yang dapat ditelusuri sumbernya dan arahan yang relevan untuk kebutuhannya.
- Pengguna dapat mengetahui apakah permintaan masih berupa draf, menunggu persetujuan, memerlukan informasi tambahan, sedang diproses, berhasil, atau gagal; status berasal dari sistem yang menangani tindakan.

Kriteria selesai MVP dicatat pada bagian 3 dan rencana pengujiannya pada bagian 5. Nama provider model dan detail stack belum ditetapkan. Arah deployment cloud dan pembuktian model dicatat pada bagian 3, subbagian Tahap pembuktian wajib setelah MVP.

## 2. Skenario penggunaan dan alur layanan

Lima skenario berikut menjelaskan perjalanan dari penyiapan assistant untuk perusahaan sampai pengguna mendapat jawaban atau menyelesaikan permintaan. RAG menjadi checkpoint pertama; workflow reimbursement merupakan bagian wajib MVP.

### Skenario 1 — Menyiapkan assistant untuk perusahaan baru

**Aktor:** tim implementasi, administrator perusahaan, dan pengelola pengetahuan.

**Kebutuhan:** perusahaan A ingin assistant yang menjawab pertanyaan kebijakan internal.

**Alur:**

1. Tim implementasi membuat tenant perusahaan A.
2. Administrator mengatur pengguna dan hak akses.
3. Pengelola pengetahuan memasukkan dokumen dan menetapkan siapa yang boleh mengaksesnya.
4. Tim memeriksa hasil retrieval, jawaban, serta pembatasan akses.
5. Assistant diaktifkan untuk karyawan setelah pemeriksaan tersebut lulus.

Sebagai contoh, kebijakan umum tersedia untuk seluruh karyawan, sedangkan prosedur administrasi payroll hanya tersedia untuk tim HR.

**Hasil:** perusahaan memiliki assistant yang memakai sumber pengetahuannya sendiri dengan akses yang sudah diperiksa.

### Skenario 2 — Karyawan mencari jawaban sesuai situasinya

**Aktor:** karyawan dan pengelola kebijakan jika diperlukan.

**Contoh pertanyaan:** “Gue kerja remote dari Bandung. Kalau datang ke kantor Jakarta untuk meeting, transportasinya bisa direimburse nggak?”

**Alur:**

1. Assistant mencari kebijakan yang relevan dan boleh diakses pengguna.
2. Assistant meminta klarifikasi bila informasi situasi belum cukup, misalnya jenis penugasan.
3. Assistant menjelaskan ketentuan yang relevan serta menunjukkan sumber dan bagian dokumennya.
4. Jika dokumen belum menjawab kondisi pengguna atau aturan bertentangan, assistant menjelaskan bagian yang belum pasti dan mengarahkan pertanyaan kepada pengelola kebijakan.

**Hasil:** pengguna mendapat jawaban bersumber atau mengetahui pihak yang perlu memberikan keputusan.

### Skenario 3 — Pertanyaan berlanjut menjadi pengajuan

**Aktor:** karyawan, assistant, dan sistem bisnis tujuan.

**Contoh permintaan:** “Oke, bantu ajukan reimbursement perjalanan kemarin.”

**Alur:**

1. Assistant meminta informasi dan bukti yang diperlukan.
2. Assistant membaca data perjalanan terkait melalui API service referensi sesuai hak akses pengguna.
3. Sistem memeriksa kelengkapan berdasarkan aturan perusahaan.
4. Assistant menampilkan ringkasan pengajuan beserta versi draf, aturan, lampiran, dan tujuan persetujuan agar pengguna dapat mengoreksi atau mengonfirmasinya.
5. Setelah konfirmasi, sistem memvalidasi ulang akses serta aturan dan mencatat pengajuan melalui API. Perubahan penting memerlukan ringkasan dan konfirmasi baru sebelum pencatatan; service tujuan menegakkan validasi dan jalur approval.
6. Pengguna menerima nomor pengajuan serta status sebenarnya. Jika koneksi ke sistem tujuan bermasalah, status dan langkah berikutnya ditampilkan sesuai hasil yang dapat dibuktikan.

**Hasil:** pengajuan tercatat setelah konfirmasi, dengan tujuan dan status yang jelas. Pernyataan assistant tidak menggantikan bukti pencatatan dari sistem tujuan.

### Skenario 4 — Approver menangani permintaan dan karyawan memantau hasil

**Aktor:** approver, karyawan, dan petugas pelaksana jika diperlukan.

**Alur:**

1. Approver menerima pengajuan sesuai kewenangannya.
2. Approver melihat ringkasan, bukti, serta ketentuan terkait.
3. Approver menyetujui, menolak dengan alasan, atau meminta informasi tambahan.
4. Jika perlu informasi tambahan, karyawan menerima permintaan yang spesifik, melengkapi pengajuan, lalu mengonfirmasi versi baru untuk mengembalikannya ke antrean persetujuan. Keputusan selalu terikat versi pengajuan yang diperiksa.
5. Karyawan dapat menanyakan perkembangan pengajuan. Assistant membaca status dari sistem pengajuan dan menjelaskan siapa yang sedang perlu bertindak.

**Contoh pertanyaan status:** “Reimbursement gue sekarang sampai mana?”

**Hasil:** keputusan dilakukan oleh pihak berwenang dan pengguna dapat mengikuti perkembangan permintaannya. Persetujuan tidak otomatis berarti pekerjaan lanjutan, seperti pembayaran, telah selesai.

### Skenario 5 — Platform digunakan perusahaan lain dengan konfigurasi berbeda

**Aktor:** tim implementasi, administrator, pengelola pengetahuan, dan pengguna dari dua tenant.

**Kebutuhan:** perusahaan B membutuhkan assistant dengan kemampuan serupa, tetapi memiliki kebijakan, sistem, dan aturan sendiri.

**Alur:**

1. Tim implementasi menyiapkan tenant B menggunakan fondasi platform yang sama.
2. Sumber pengetahuan, konektor API service reimbursement, serta aturan perusahaan B dikonfigurasi secara terpisah.
3. Pertanyaan serupa dijawab berdasarkan sumber dan aturan tenant pengguna. Sebagai ilustrasi, A mengizinkan pengajuan maksimal 30 hari setelah perjalanan, sedangkan B menetapkan 14 hari.
4. Tim memeriksa bahwa pengguna A hanya mengakses data A dan pengguna B hanya mengakses data B, termasuk melalui jawaban dan retrieval.
5. Pembatasan dalam satu tenant tetap diperiksa; seorang karyawan tidak otomatis boleh melihat pengajuan rekan kerjanya.

**Hasil:** fondasi platform digunakan ulang, sementara pengetahuan, aturan, serta akses mengikuti perusahaan masing-masing. Pada putaran RAG, pembuktian skenario ini berfokus pada pengetahuan, konfigurasi, dan akses; konektor serta tindakan diuji saat workflow ditambahkan.

### Urutan pengerjaan yang disepakati

- **Milestone 1 — checkpoint fondasi RAG, skenario 1, 2, dan 5:** menyiapkan assistant untuk dua tenant, lalu membuktikan jawaban bersumber, perbedaan konfigurasi, serta pembatasan akses antar-tenant dan di dalam tenant.
- **Milestone 2 — penyelesaian MVP agent platform, skenario 3, 4, dan perluasan 5:** melengkapi fondasi RAG dengan workflow reimbursement melalui API, aturan per tenant, konfirmasi, persetujuan, pelacakan status, state tersimpan, dan pemulihan kegagalan.
- **Milestone 3 — pembuktian delivery:** deployment cloud dan pengujian operasional sebagaimana bagian 3.
- **Milestone 4 — pembuktian model:** deployment model open-source dan perbandingan dengan commercial API sebagaimana bagian 3.

RAG dua tenant merupakan checkpoint pertama. MVP baru selesai setelah workflow reimbursement lengkap bekerja dan lulus pengujian. Procurement pada bagian 1 tetap merupakan ilustrasi di luar MVP.

## 3. Kebutuhan produk dan batas MVP

### Tujuan MVP

Membuktikan bahwa fondasi platform yang sama dapat melayani dua perusahaan dengan pengetahuan, konfigurasi, hak akses, serta aturan bisnis berbeda. Produk MVP adalah assistant kebijakan perjalanan dan pengajuan reimbursement yang dapat digunakan melalui aplikasi web sampai keputusan persetujuan dan pelacakan status.

**Alur utama:** pertanyaan kebijakan → jawaban bersumber → permintaan pengajuan → pengumpulan detail dan pembacaan data perjalanan melalui API → draf → konfirmasi karyawan → pencatatan pengajuan → keputusan approver → pemantauan status.

### Kebutuhan produk wajib

| Kebutuhan | Perilaku yang harus tersedia |
| --- | --- |
| Tenant dan identitas pengguna | Pengguna login dan masuk ke perusahaan yang menjadi kewenangannya. Sistem menentukan akses berdasarkan identitas tersebut. |
| Peran dan akses | Admin mengelola tenantnya, pengelola pengetahuan mengurus dokumen, karyawan bertanya dan mengajukan kebutuhan, serta approver memutuskan pengajuan sesuai kewenangan. Satu orang dapat menjalankan beberapa peran. |
| Pengelolaan pengetahuan | Pengelola memasukkan dokumen, menetapkan akses, melihat status pemrosesan, lalu menerbitkan sumber yang siap dipakai. |
| Pembaruan sumber | Dokumen dapat diganti atau ditarik. Versi baru digunakan setelah siap dan diterbitkan. Penarikan sumber atau pencabutan akses langsung memblokir pembacaan dan penggunaan ulang sumber tersebut pada pemeriksaan akses berikutnya, tanpa menunggu pembaruan indeks. |
| Percakapan RAG | Karyawan bertanya, memberikan klarifikasi, dan melanjutkan pertanyaan dengan konteks percakapan yang relevan. |
| Jawaban bersumber | Jawaban menyertakan rujukan ke bagian dokumen yang mendukungnya dan boleh dibuka pengguna. |
| Informasi tidak cukup | Assistant meminta klarifikasi, menyatakan informasi belum ditemukan, atau menunjukkan konflik sumber. Pengguna mendapat arahan kepada pengelola terkait. |
| Konfigurasi per tenant | Nama assistant, sumber pengetahuan, kontak bantuan, instruksi layanan, koneksi API, dan aturan bisnis dapat berbeda. Konfigurasi tunduk pada pembatasan akses platform. |
| Orkestrasi dan tool calling | Agent menjalankan langkah RAG serta workflow melalui tool yang diizinkan; otorisasi dan validasi tindakan ditegakkan sistem. Mekanisme yang sama digunakan kedua tenant. |
| Integrasi API | Membaca data perjalanan, membuat pengajuan, dan mengambil status melalui service reimbursement referensi sesuai akses pengguna. |
| Draf dan konfirmasi | Pengguna melihat ringkasan dan mengonfirmasi versi tertentu sebelum pengajuan dicatat. Perubahan isi penting, aturan, lampiran, atau tujuan persetujuan memerlukan konfirmasi baru. |
| Aturan bisnis dan persetujuan | Batas waktu pengajuan dan pihak pemberi persetujuan dapat berbeda per tenant dan benar-benar ditegakkan sistem. Approver dapat menyetujui, menolak dengan alasan, atau meminta informasi tambahan. |
| State dan pemulihan | Proses tersimpan, dapat menunggu persetujuan dan dilanjutkan setelah terputus. Retry tidak menggandakan pengajuan; hasil yang belum diketahui diperiksa sebelum tindakan diulang. |
| Riwayat dan observability | Pengguna membuka percakapannya sendiri. Tim berwenang dapat menelusuri langkah agent, sumber, pemanggilan tool, kegagalan, latency, dan penggunaan model sesuai aksesnya. |
| Evaluasi otomatis | Menguji kualitas retrieval dan jawaban, keberhasilan tugas, kepatuhan aturan, akses, latency, serta biaya per tugas. |
| Keamanan AI | Menguji isolasi tenant, pembatasan akses dalam tenant, prompt injection, kebocoran data, dan pemanggilan tool tanpa izin. |
| Umpan balik | Pengguna dapat menandai jawaban yang tidak membantu agar masalahnya dapat diperiksa. |

### Batas MVP yang disepakati

- **Dua tenant demonstrasi:** aplikasi dan codebase yang sama, tanpa cabang kode khusus untuk setiap perusahaan. Pengetahuan, konfigurasi, data, serta credential masing-masing terpisah secara akses.
- **Satu bidang pengetahuan:** kebijakan perjalanan dinas dan reimbursement, dengan aturan berbeda untuk kedua tenant.
- **Satu workflow lengkap:** pengajuan reimbursement dari draf dan konfirmasi sampai keputusan persetujuan dan pelacakan status. Pembayaran uang kepada karyawan berada di luar MVP; status disetujui tidak berarti sudah dibayar.
- **Satu integrasi service referensi:** service perjalanan/reimbursement dibangun dan dijalankan terpisah, dengan HTTP API, penyimpanan nyata, serta pengujian kegagalan. Penggunaan service referensi dijelaskan dalam dokumentasi; integrasi ERP tertentu hanya diklaim jika benar-benar diuji.
- **Satu kanal:** aplikasi web untuk admin, pengelola pengetahuan, karyawan, dan approver.
- **Format dokumen:** PDF berbasis teks dan teks/Markdown. PDF hasil scan, OCR, serta pembacaan tabel kompleks ditunda.
- **Bahasa:** Indonesia dan Inggris, dengan contoh pertanyaan serta sumber yang diuji pada keduanya.
- **Model:** satu provider komersial terlebih dahulu. Nama provider dan model dipilih pada perancangan teknis; deployment open-source dan perbandingan model menjadi milestone wajib setelah MVP.
- **Penyiapan tenant:** dibantu operator platform. Pendaftaran mandiri, pembayaran langganan, dan billing belum masuk MVP.

### Fitur yang ditunda dari MVP

| Fitur | Posisi setelah MVP |
| --- | --- |
| Integrasi langsung Google Drive, SharePoint, CRM, atau ERP komersial | Konektor tambahan dipilih berdasarkan kebutuhan; integrasi HTTP API service reimbursement sudah wajib dalam MVP. |
| Visual workflow builder dan marketplace agent | Dipertimbangkan setelah pola konfigurasi yang diperlukan terlihat. |
| Deployment model open-source / private inference | Milestone 4 yang wajib, dengan pembuktian inference nyata dan evaluasi. |
| Fine-tuning | Dilakukan jika kebutuhan dapat dibuktikan melalui evaluasi; eksperimen mencatat hasil sebelum dan sesudah. |
| WhatsApp, voice, kiosk, aplikasi mobile, OCR, dan tender analysis | Pengembangan terpisah setelah alur web dan workflow inti selesai. |
| Pembayaran reimbursement, billing, dan pendaftaran tenant mandiri | Di luar MVP. |

### Kriteria selesai MVP

MVP dinyatakan selesai setelah demonstrasi melalui aplikasi dan pengujian membuktikan hal berikut:

1. Dua tenant disiapkan dan menggunakan fondasi yang sama, dengan dokumen, konfigurasi, serta aturan berbeda.
2. Pertanyaan serupa menghasilkan jawaban sesuai kebijakan masing-masing tenant, lengkap dengan sumber yang boleh diakses.
3. Pengguna tidak dapat membaca sumber, percakapan, atau pengajuan tenant lain maupun data terbatas dalam tenantnya.
4. Pembaruan dan penarikan dokumen benar-benar mengubah sumber yang dipakai assistant.
5. Pertanyaan yang tidak terjawab atau sumber yang bertentangan ditangani dengan jelas.
6. Karyawan menyusun, mengoreksi, dan mengonfirmasi pengajuan; pencatatan dilakukan melalui API dan menghasilkan referensi dari sistem tujuan.
7. Aturan per tenant ditegakkan pada tindakan. Approver yang berwenang dapat memutuskan atau meminta informasi tambahan, dan pengguna dapat memantau status dari sistem pengajuan.
8. Proses dapat dilanjutkan setelah terputus, termasuk ketika menunggu persetujuan. Retry dan kegagalan API tidak menghasilkan pengajuan ganda atau klaim sukses tanpa bukti.
9. Pengujian prompt injection, kebocoran data, dan tool tanpa izin membuktikan pembatasan akses serta kewenangan tetap berlaku.
10. Alur utama dapat dijalankan melalui UI. Evaluasi otomatis serta tracing menghasilkan bukti kualitas retrieval/jawaban, task completion, latency, dan biaya per tugas.
11. Perubahan aturan antara draf, konfirmasi, dan commit memerlukan validasi serta konfirmasi yang sesuai; pengajuan tercatat mempertahankan paket aturan yang dikunci.
12. Pencabutan akses berlaku pada riwayat, sumber, lampiran, konteks/checkpoint, tool, serta keputusan; data turunan tidak membuka akses kembali.
13. Endpoint service menolak tenant/owner/status yang tidak sah, self-approval, versi kedaluwarsa, konflik keputusan bersamaan, dan penyalahgunaan idempotency key.
14. Lampiran hanya dipakai setelah siap, dapat dibuka approver yang berwenang, dan terikat versi konfirmasi. Pemeriksaan manusia dibedakan dari verifikasi otomatis yang tidak tersedia.
15. Konfigurasi/role tidak memberi jalan eskalasi hak sendiri, routing aktif tidak berubah diam-diam, dan trace/retensi memenuhi kontrak data yang ditetapkan sebelum penggunaan data sensitif.

Dataset, metode, kondisi pengujian, serta ambang kualitas, latency, dan biaya dirinci sebelum evaluasi penerimaan. Pendekatan retrieval vector dibandingkan dengan hybrid search/reranking untuk menentukan manfaatnya berdasarkan hasil evaluasi, bukan mewajibkan seluruh pendekatan dipakai bersamaan.

Status kesepakatan dalam dokumen ini merupakan keputusan perencanaan, bukan klaim bahwa implementasi atau pengujiannya telah selesai. Lulus MVP tidak otomatis berarti pembuktian cloud, model open-source, atau kesiapan produksi keseluruhan telah selesai.

### Tahap pembuktian wajib setelah MVP

#### Milestone 3 — Delivery dan operasi cloud

- Deployment cloud dengan arah AWS, menggunakan Docker, CI/CD, serta infrastructure as code. Detail layanan dan kapasitas dipilih pada perancangan teknis.
- Konfigurasi akses, secret, jaringan, enkripsi, logging, dan monitoring sesuai kebutuhan deployment.
- Pengujian beban, penanganan kegagalan, backup–restore, serta pemulihan dengan hasil yang dicatat.
- Kubernetes mendapat tahap pengujian deployment tersendiri, termasuk mekanisme kesiapan layanan, pemulihan proses, dan rollout/rollback. Memiliki manifest saja tidak dihitung sebagai pembuktian operasional.
- Target kapasitas, availability, dan pemulihan dinyatakan berdasarkan pengukuran pada lingkungan yang diuji.

#### Milestone 4 — Model open-source dan perbandingan inference

- Menjalankan model open-source pada infrastruktur yang sesuai dan membuktikan inference nyata.
- Membandingkannya dengan commercial API menggunakan dataset dan tugas yang sebanding, termasuk retrieval, tool calling, serta kepatuhan aturan dan akses.
- Mengukur kualitas, latency, kebutuhan resource, serta biaya dengan kondisi pengujian yang jelas.
- Mengonfigurasi model dan inference untuk kebutuhan aplikasi serta mendokumentasikan tradeoff commercial API, self-hosted, dan private deployment. Lokasi hosting saja tidak membuktikan seluruh kontrol privasi terpenuhi.
- Fine-tuning seperti LoRA/QLoRA dilakukan bila evaluasi menunjukkan kebutuhan; jika dilakukan, catat data, metode, serta hasil sebelum dan sesudah.

#### Keterkaitan dengan lowongan acuan

| Kemampuan dalam lowongan | Bukti yang ditargetkan proyek |
| --- | --- |
| Reusable orchestration dan tenant-level configurations | Dua tenant menggunakan fondasi yang sama, dengan konfigurasi dan aturan berbeda. |
| Knowledge retrieval dan tenant-isolated knowledge | RAG bersumber, lifecycle dokumen, evaluasi retrieval, dan pengujian akses. |
| Tool calling, API execution, state persistence, approvals | Workflow reimbursement lengkap, integrasi service referensi, state tersimpan, serta pemulihan dan retry yang diuji. |
| AI security, evaluation, observability, cost | Uji akses dan prompt injection, evaluasi otomatis, tracing, task completion, latency, dan biaya per tugas. |
| Cloud, Docker, CI/CD, IaC, Kubernetes | Deployment serta pembuktian operasional pada milestone 3. |
| Open-source deployment, customisation, inference optimisation | Deployment nyata, konfigurasi dan optimasi inference, serta perbandingan model pada milestone 4; fine-tuning bersyarat pada hasil evaluasi. |

Proyek ini menargetkan bukti kemampuan teknis yang relevan. Voice, document intelligence yang luas, serta seluruh jenis integrasi dalam roadmap lowongan tidak dijadikan kewajiban MVP proyek 2.

## 4. Aturan bisnis, data, dan kewenangan agent

### Sumber data yang berwenang

| Data | Acuan dan batas penggunaan |
| --- | --- |
| Identitas, tenant, membership, dan hak akses | Sistem identitas; konteks aktor berasal dari sesi dan komunikasi antarlayanan yang terverifikasi. Nilai tenant atau peran dalam chat/payload bebas tidak memberikan kewenangan. |
| Penjelasan kebijakan | Dokumen terbit yang berlaku dan boleh diakses pengguna, dengan versi dan rujukan sumber. |
| Validasi dan jalur persetujuan | Konfigurasi aturan terstruktur yang disetujui dan ditegakkan sistem; bukan instruksi bebas yang ditafsirkan model. |
| Perjalanan | API service referensi; akses ke referensi perjalanan tetap memerlukan pemeriksaan kepemilikan atau kewenangan. |
| Pengajuan tercatat, keputusan, dan status | Service reimbursement menjadi sumber hasil bisnis; platform menyimpan draf dan riwayat eksekusi serta menyelaraskan hasil melalui API. |
| Rincian biaya dan keterangan kebutuhan | Pernyataan karyawan sampai diperiksa melalui proses bisnis. Pernyataan bahwa atasan sudah menyetujui tidak menggantikan keputusan approver dalam sistem. |
| Ringkasan dan jawaban agent | Hasil bantuan AI yang harus merujuk sumber di atas; tidak dapat menetapkan status atau kewenangan sendiri. |

Data inti mencakup identitas dan membership, dokumen beserta versi/akses, konfigurasi aturan, percakapan dan provenance sumber, draf, referensi perjalanan, rincian biaya, lampiran, pengajuan, keputusan, serta audit tindakan.

### Aturan per tenant dan perubahan versi

- Konfigurasi mencakup batas waktu pengajuan, jenis biaya yang diperbolehkan, bukti wajib, batas nominal bila diperlukan, serta urutan dan pihak persetujuan. Angka 30/14 hari pada skenario merupakan ilustrasi, bukan nilai yang sudah dikunci.
- Untuk MVP, versi kebijakan dipilih berdasarkan tanggal selesai perjalanan dalam timezone tenant. Paket kebijakan memiliki rentang tanggal berlaku yang tidak tumpang tindih; pemetaan versi tersebut dipertahankan agar pengajuan berikutnya untuk perjalanan lama tidak diam-diam memakai aturan baru.
- Batas waktu dihitung dalam hari kalender setelah tanggal selesai perjalanan, hingga akhir hari terakhir pada timezone tenant. Timestamp penerimaan submission di service menentukan ketepatan waktu; tanggal biaya tetap dicatat untuk memeriksa kelayakan jenis/rentang pengeluaran. Nilai batas dan timezone setiap tenant ditetapkan pada konfigurasi demo sebelum pengujian.
- Dokumen dan konfigurasi aturan diterbitkan sebagai paket versi yang telah diperiksa pengelola berwenang. Dokumen menjelaskan kebijakan; backend menjalankan aturan terstrukturnya. Deteksi konflik tidak boleh sepenuhnya bergantung pada AI. Konflik yang diketahui menahan tindakan terkait dan diarahkan kepada pengelola.
- Konfirmasi terikat versi draf, paket aturan, rincian biaya, mata uang, referensi perjalanan, versi lampiran, dan tujuan/jalur persetujuan yang ditampilkan. Sistem memeriksa ulang akses, kelengkapan, batas waktu, versi, serta kelayakan sebelum commit.
- Perubahan yang memengaruhi isi, kelayakan, atau tujuan persetujuan membatalkan konfirmasi. Ringkasan baru dan konfirmasi baru diperlukan; draf lama tidak dipindahkan ke aturan lain secara diam-diam.
- Pengajuan yang sudah tercatat mengunci versi paket aturan dan jalur persetujuannya. Pelengkapan menggunakan paket yang sama dan tidak dianggap submission baru untuk menghitung deadline. Hak akses serta kewenangan approver tetap diperiksa berdasarkan keadaan terkini.
- Migrasi aturan retroaktif dan perubahan jalur pada pengajuan aktif berada di luar MVP. Jika approver tidak lagi tersedia atau paket yang dikunci diketahui bermasalah, keputusan ditahan, alasannya ditampilkan, dan admin diberi arahan tindak lanjut. Sistem tidak otomatis mengganti approver atau memberikan hak kepada admin.

### Kewenangan agent dan manusia

Agent boleh mencari serta merangkum sumber yang diizinkan, meminta klarifikasi, membaca data perjalanan/status melalui tool yang berwenang, menyiapkan dan memperbaiki draf, serta mengirim pengajuan setelah validasi dan konfirmasi. Agent dapat menunjukkan kontak bantuan yang dikonfigurasi tenant.

Agent tidak boleh menyetujui atau menolak pengajuan atas nama approver, mengubah aturan/role/izin, memilih identitas dari chat, membuka data tanpa hak, melakukan pembayaran, atau menyatakan keberhasilan tanpa hasil terverifikasi. Instruksi dari dokumen, receipt, hasil tool, atau percakapan tidak dapat mengubah batas ini.

Keputusan manusia mempunyai catatan aksi immutable yang mengikat aktor/sesi, claim, expected version, revision, stage, pilihan keputusan, alasan dan identitas tindakan. Mengubah keputusan atau alasan tidak boleh memakai bukti konfirmasi/key lama. Mekanisme ini berlaku tanpa mengharuskan keputusan approver memiliki draf submission.

| Peran | Tanggung jawab dan pembatasan |
| --- | --- |
| Karyawan | Memberikan informasi, memeriksa/mengonfirmasi draf, dan melengkapi pengajuan miliknya. |
| Approver | Memutuskan dalam scope dan tahap yang ditugaskan, berdasarkan versi pengajuan yang diperiksa. Penolakan dan permintaan informasi menyertakan alasan. |
| Pengelola pengetahuan | Menjaga dokumen, akses, versi, dan masa berlaku; menerbitkan sesuai haknya. Hak ini tidak otomatis memberikan kewenangan mengubah aturan bisnis. |
| Pemilik proses yang berwenang | Memeriksa dan menerbitkan paket aturan bisnis serta routing yang diizinkan. Perubahan hanya berlaku untuk scope dan tanggal yang ditetapkan. |
| Administrator tenant | Mengelola akun serta konfigurasi dalam scope. Tidak otomatis menjadi approver, pembaca seluruh data pribadi, atau penerbit seluruh aturan. |
| Operator platform | Menyiapkan tenant dan menjaga layanan. Akses operasional tidak otomatis memberi akses isi dokumen, lampiran, dan percakapan tenant. |

Self-approval dilarang berdasarkan identitas orang, termasuk ketika orang tersebut memiliki beberapa role. Konfigurasi tenant tidak dapat mematikan larangan ini maupun isolasi akses. Tindakan approval hanya diterima dari aksi eksplisit manusia yang terautentikasi, bukan teks model.

Pemberian role sensitif dan kewenangan penerbitan dilakukan oleh aktor berwenang yang berbeda dari penerima hak. Penetapan awal dapat dilakukan operator melalui provisioning yang diaudit; admin tidak dapat memberi dirinya sendiri kewenangan tambahan. Perubahan konfigurasi tidak memperluas akses atau mengubah routing pengajuan aktif secara otomatis.

Konektor memakai tujuan API yang didaftarkan dan diizinkan operator; admin tenant memilih konektor yang tersedia dan credential dalam scope, tanpa mengarahkan tool ke URL sembarang. Credential disimpan backend dan tidak dikirim ke konteks model, respons tool, log, atau trace. Instruksi tenant tidak dapat mengubah izin tool atau sumber identitas.

### Penegakan pada service referensi

Service reimbursement memverifikasi pemanggil dan konteks aktor yang dipercaya. Service menegakkan tenant, kepemilikan, aturan versi yang digunakan, transisi status, kewenangan approver, dan larangan self-approval, termasuk jika endpoint dipanggil tanpa melalui agent.

Service memiliki salinan paket aturan terstruktur yang diterbitkan melalui jalur terotorisasi dan memverifikasi versi pada submission. Payload bebas tidak boleh menetapkan owner, tenant, approver, atau status akhir tanpa validasi. Penolakan akses dan aturan tidak boleh berubah menjadi sukses karena platform mengulang request.

Service juga memeriksa kelayakan operasional paket terkini sebelum submission, pelengkapan, dan keputusan melalui pemeriksaan tepercaya platform. Penarikan/konflik paket menahan tindakan terkait termasuk claim yang sudah mengunci versi; digest immutable dan ack publikasi tidak menggantikan pemeriksaan ini. Paket tetap tersedia untuk audit. Detail protokol terdapat pada DATA_AND_API_CONTRACTS.md.

Platform bukan penentu keputusan final. Draf serta execution state berada di platform; pengajuan tercatat dan keputusan final mengikuti service. Integrasi tidak membutuhkan database bersama atau klaim transaksi atomik lintas service.

### Status bisnis dan persetujuan

| Status/transisi | Aktor dan ketentuan |
| --- | --- |
| Draf → menunggu persetujuan | Karyawan mengonfirmasi versi draf; sistem dan service memvalidasi lalu mencatat pengajuan. Draf berada di platform sampai submission berhasil dibuktikan. |
| Menunggu persetujuan → perlu informasi | Approver yang masih berwenang meminta informasi spesifik beserta alasan. |
| Perlu informasi → menunggu persetujuan | Karyawan melengkapi dan mengonfirmasi versi baru; pemeriksaan aturan dilakukan ulang dengan paket yang dikunci. Persetujuan tahap sebelumnya atas isi lama tidak diteruskan; jalur persetujuan dimulai ulang. |
| Menunggu persetujuan → tahap persetujuan berikutnya | Approver tahap saat ini menyetujui versi yang diperiksa. Status tetap menunggu sampai seluruh tahap wajib selesai. |
| Menunggu persetujuan → disetujui/ditolak | Disetujui setelah seluruh tahap wajib lulus; ditolak oleh approver tahap yang berwenang dengan alasan. |

Keputusan, perpindahan tahap, dan pelengkapan atomik terhadap versi pengajuan di service. Request dengan versi lama ditolak; keputusan bersamaan tidak saling menimpa. Perubahan isi hanya melalui alur perlu informasi, bukan saat keputusan sedang diproses. Status disetujui/ditolak terminal untuk MVP; pembukaan ulang, pembatalan setelah submission, serta pembayaran tidak disediakan.

Status teknis seperti sedang dikirim, hasil belum diketahui, atau gagal dikirim dipisahkan dari status bisnis. Setelah timeout, UI tidak menyatakan ditolak atau berhasil tanpa hasil service. Jika proses ditahan karena approver tidak tersedia, pengguna melihat alasan dan pihak yang perlu menindaklanjuti.

### Konfirmasi, idempotency, dan recovery

- Identitas tindakan disimpan sebelum request dikirim. Retry setelah restart atau timeout memakai identitas/key yang sama.
- Service mengikat key pada tenant, jenis operasi, aktor, serta payload dan versi yang relevan. Key sama dengan isi berbeda ditolak; key dan isi sama mengembalikan hasil sebelumnya.
- Submission, pelengkapan, dan keputusan approval memiliki proteksi pengulangan. Hasil dapat diperiksa berdasarkan identitas tindakan oleh pemilik atau aktor berwenang.
- Selama hasil belum dapat dipastikan, tampilkan hasil belum diketahui dan rekonsiliasi dengan service. Jangan membuat submission pengganti atau key baru untuk tindakan yang sama.
- Draf yang telah tercatat memiliki referensi hasil tetap. Memperbarui halaman atau mengonfirmasi ulang tidak menciptakan pengajuan baru.
- Terpisah dari retry, aturan duplikasi bisnis MVP melarang item biaya dengan referensi item perjalanan yang sama berada pada lebih dari satu pengajuan menunggu persetujuan, perlu informasi, atau disetujui. Service menerapkan pemeriksaan atomik. Setelah penolakan, pengajuan baru boleh dibuat dengan referensi ke pengajuan sebelumnya dan tetap mengikuti deadline. Kemiripan isi receipt tidak dianggap terverifikasi otomatis.

### Isolasi, revocation, dan konteks tersimpan

Prinsip isolasi berlaku untuk dokumen, hasil retrieval, file, percakapan, konfigurasi, credential, cache, checkpoint, konteks model, dan riwayat eksekusi. Setiap data turunan memiliki provenance yang cukup untuk menentukan sumber serta hak penggunaannya.

Provenance juga mencakup trip, claim, receipt dan hasil tool, bukan hanya dokumen. Perpindahan tahap approval dapat menghilangkan akses reviewer meskipun membership tetap aktif; data turunannya diperiksa ulang pada riwayat/konteks/output. Memiliki percakapan tidak berarti berhak membaca semua data service yang pernah masuk ke percakapan itu.

Kewenangan terkini diperiksa sebelum membaca data, menampilkan sumber/lampiran/riwayat, memasukkan konteks ke model, menjalankan tool, melanjutkan checkpoint, mengirim keluaran agent, dan menerima keputusan. Jika akses dicabut, konteks yang bergantung pada sumber tersebut disaring atau dibangun ulang; salinan lama bukan jalan untuk memperoleh akses kembali.

Pencabutan izin dan penarikan sumber memblokir penggunaan berikutnya tanpa menunggu reindex. Jawaban lama yang memuat kutipan atau data turunan sumber terbatas disembunyikan atau disaring ketika riwayat dibuka tanpa izin terkini. Ini tidak menghapus informasi yang telah dilihat pengguna sebelumnya, tetapi mencegah pengungkapan ulang dari aplikasi.

Pembaruan dokumen berbeda dari pencabutan akses: versi pengganti harus siap sebelum diterbitkan, dengan status pemrosesan yang jelas. Riwayat yang tetap boleh dibuka mempertahankan rujukan versi lama dan penanda bahwa sumber sudah diganti; sumber yang ditarik tidak dipakai untuk jawaban baru.

### Lampiran dan rincian biaya

- Receipt diterima sebagai PDF, JPEG, atau PNG, maksimal 10 MiB per file dan 10 file per pengajuan. PDF di sini merupakan bukti yang dibuka manusia, terpisah dari dokumen knowledge yang harus berbasis teks.
- File melewati pemeriksaan tipe/ukuran, validitas, dan keamanan sebelum berstatus siap. File rusak, terenkripsi, tidak didukung, atau gagal pemeriksaan ditolak; file yang masih diperiksa belum memenuhi bukti wajib.
- Penyimpanan dan unduh privat memeriksa akses terkini. Approver harus dapat membuka versi bukti yang ditampilkan saat konfirmasi.
- Lampiran yang sudah dikonfirmasi tidak diganti isinya pada ID/versi yang sama. Perubahan lampiran menghasilkan versi baru, validasi ulang, dan konfirmasi ulang.
- Nominal, mata uang, tanggal, serta rincian biaya diisi secara terstruktur oleh karyawan. OCR dan verifikasi otomatis isi receipt berada di luar MVP; agent tidak boleh menyatakan nominal sudah cocok dengan receipt.
- Approver memeriksa kecocokan bukti dan rincian; ketidaksesuaian ditangani melalui permintaan informasi atau penolakan. Mata uang harus sesuai konfigurasi tenant; konversi kurs otomatis berada di luar MVP.

### Audit, retensi, dan privasi

Audit mencatat aktor, tenant, waktu, tindakan, versi draf/pengajuan/paket aturan, identitas tindakan, serta hasil yang diperlukan untuk penelusuran. Audit tidak otomatis menyimpan seluruh konten sensitif. Trace operasional menggunakan redaksi dan akses sesuai peran; operator tidak otomatis dapat membaca prompt, receipt, atau respons tool lengkap.

Sebelum data sensitif digunakan, kontrak data harus menetapkan masa simpan dan akses per kategori: dokumen, percakapan, draf/checkpoint, receipt, pengajuan/keputusan, audit/trace, serta backup. Retensi, lokasi dan pengiriman data ke provider model, penghapusan salinan turunan, dan perlakuan backup menjadi prasyarat penggunaan data tersebut. Pengujian awal menggunakan data fiktif.

Penghapusan memperhitungkan indeks, cache, file, checkpoint, dan trace terkait. Bukti audit minimum yang perlu dipertahankan dipisahkan dari isi yang dihapus. Restore backup harus menerapkan kembali pencabutan dan penghapusan sebelum layanan membuka akses. Angka retensi dan prosedur teknis dirinci sebelum schema/API dibakukan untuk penggunaan data sensitif; ini kebutuhan desain, bukan klaim sertifikasi.

Pembukaan kembali pascarestore memerlukan bukti kelengkapan security recovery journal dari sumber durabel independen di luar snapshot lama, dengan watermark yang dapat diverifikasi. Revocation/deletion/perubahan akses sesudah backup harus dapat diterapkan kembali; scope dengan journal hilang atau tidak lengkap tetap ditahan. Rincian intent dan rekonsiliasi diturunkan pada arsitektur serta kontrak data; ini tidak menjanjikan zero-RPO untuk semua data bisnis.

## 5. Rencana pengujian dan ukuran keberhasilan

### Tujuan dan keterkaitan dengan lowongan

Pengujian membuktikan kebenaran, keamanan, keandalan, manfaat penggunaan, serta biaya platform. Jumlah kasus merupakan pilihan perencanaan proyek, bukan ketentuan lowongan. Evaluasi mengikuti kemampuan teknis yang dicari tanpa mewajibkan seluruh roadmap atau vendor masuk MVP.

| Bidang | Pembuktian yang ditargetkan |
| --- | --- |
| Arsitektur enterprise dan reuse | Dua tenant menjalankan fondasi dan orkestrasi yang sama dengan konfigurasi berbeda, tanpa cabang kode khusus perusahaan. |
| Agent, workflow, dan API | Pengajuan dari klarifikasi sampai approval/status, dengan bukti payload serta keadaan akhir service. |
| RAG dan pengetahuan | Retrieval, jawaban bersumber, lifecycle dokumen, klarifikasi, dan penanganan sumber tidak cukup atau konflik. |
| Keamanan dan aturan bisnis | Isolasi antar/dalam tenant, pencegahan prompt injection, tindakan tanpa izin, self-approval, serta perubahan aturan yang tidak sah. |
| Keandalan | Timeout, restart, respons hilang, retry, dan concurrency ditangani tanpa transaksi ganda atau keputusan saling menimpa. |
| Evaluasi, observability, dan biaya | Evaluasi otomatis, tracing tersanitasi, task completion, latency, biaya, dan pilot manual–assisted. |
| Delivery dan pilihan model | Deployment serta pemulihan cloud pada milestone 3; inference open-source nyata dan perbandingan model pada milestone 4. |

### Paket evaluasi awal

- **80 kasus RAG:** empat kombinasi tenant × bahasa (A/Indonesia, A/Inggris, B/Indonesia, B/Inggris), masing-masing 20 kasus. Setiap kombinasi mencakup pertanyaan answerable, klarifikasi, serta informasi tidak tersedia/konflik. Jumlah tiap kategori dan sumber acuan dikunci pada test plan sebelum acceptance.
- **24 skenario workflow:** mencakup pengajuan valid, penolakan aturan, approval bertahap, pelengkapan, dan pemulihan gangguan pada kedua tenant. Variasi bahasa diuji pada interaksi yang relevan; tes backend yang sama tidak perlu digandakan hanya karena bahasa. Matriks skenario menetapkan cakupan tenant, bahasa, aktor, expected behavior, dan hasil service.
- **Suite keamanan dan integritas tersendiri:** jumlah mengikuti seluruh invariant pada bagian 4. Semua kasus wajib tetap diuji meskipun memerlukan tambahan di luar paket 80/24.

Paket ini adalah titik awal, bukan batas maksimal atau alasan untuk melewatkan risiko. Kasus ditambah berdasarkan celah cakupan dan pola kegagalan pilot. Kasus dengan sumber, aktor, atau aturan yang berbeda tidak dianggap setara hanya karena pertanyaannya serupa.

Fixtures menggunakan data fiktif: dua tenant, matriks peran/akses, kebijakan berbeda, dokumen umum/terbatas/lama/ditarik/konflik, perjalanan, rincian biaya, serta lampiran. Manusia menyiapkan gold berupa unit bukti yang diperlukan beserta alternatif setara, expected behavior, dan status bisnis yang benar. Jawaban model tidak menjadi satu-satunya acuan kebenaran.

### Cara pengujian

- **Unit:** aturan, deadline/timezone, perhitungan biaya, versi, dan transisi status.
- **Integrasi dan kontrak API:** database, penyimpanan file, service HTTP, autentikasi/otorisasi, model, dan format hasil/error.
- **End-to-end melalui UI:** pengelolaan dokumen, tanya jawab, draf, konfirmasi, keputusan manusia, pelengkapan, dan status.
- **Gangguan dan concurrency:** kegagalan sebelum/sesudah commit, restart, klik ganda, keputusan bersamaan, serta rekonsiliasi hasil belum diketahui.
- **Evaluasi AI:** retrieval dan jawaban, klarifikasi sampai kelanjutan, abstain/konflik, pemilihan tool, serta penyelesaian tugas.
- **Keamanan dan privasi:** akses objek/data turunan, prompt injection, batas konektor, file, trace, retensi, dan penghapusan.

Mock digunakan untuk unit dan simulasi terkontrol, bukan bukti integrasi provider nyata. Koneksi service/provider nyata dan fault injection dilaporkan terpisah. Gangguan HTTP dapat disuntikkan melalui server/proxy terkontrol untuk menguji integrasi sebenarnya.

### Pengujian kritis wajib

- Akses silang tenant dan akses data karyawan lain yang tidak berwenang, termasuk pemanggilan endpoint service langsung.
- Pencabutan izin atau penarikan sumber setelah data masuk ke riwayat, cache, konteks, atau checkpoint, termasuk pemeriksaan sebelum keluaran dikirim.
- Perubahan dokumen/config antara draf, konfirmasi, dan commit; paket aturan tidak tersedia/konflik; pengajuan tercatat tetap memakai versi yang dikunci.
- Tepat sebelum, pada, dan setelah deadline menurut timezone; retry setelah deadline untuk tindakan yang sebenarnya sudah berhasil dicatat.
- Self-approval, urutan approval dilewati, approver dicabut, keputusan bersamaan, versi lama, dan approval yang harus dimulai ulang setelah pelengkapan.
- Respons submission/approval/pelengkapan hilang setelah commit; restart dengan key sama; key sama untuk payload berbeda; duplikasi bisnis dengan key berbeda secara bersamaan.
- Nominal nol/negatif, presisi dan total tidak cocok, mata uang atau referensi perjalanan tidak sah, serta pemalsuan status/owner/tenant.
- Instruksi berbahaya pada chat/dokumen/hasil tool, eskalasi role/config, tujuan konektor tidak diizinkan, serta kebocoran credential dan konten sensitif melalui trace.
- Lampiran belum siap, berubah setelah konfirmasi, bukan milik pengaju, rusak/terenkripsi, tipe aktual tidak sesuai, serta tepat pada/melewati batas ukuran/jumlah. Agent tidak mengklaim verifikasi receipt otomatis.
- Penghapusan salinan turunan dan, pada pengujian restore, pencegahan data yang telah dicabut/dihapus menjadi terbuka kembali.

### Ukuran keberhasilan dan aturan penilaian

| Ukuran | Cara menilai |
| --- | --- |
| Retrieval | Proporsi unit bukti yang diperlukan dan ditemukan pada lima hasil akhir retrieval teratas; dinilai pada seluruh kasus answerable sesuai akses/waktu. Potongan duplikat tidak menambah nilai. |
| Jawaban | Lulus bila akurat, cukup lengkap, didukung bukti, dan citation benar serta dapat diakses. Denominator seluruh kasus answerable; abstain yang keliru, error, dan timeout tetap gagal. |
| Klarifikasi | Pertanyaan klarifikasi relevan dan cukup, lalu proses dapat dilanjutkan setelah informasi diberikan; bukan terus meminta klarifikasi tanpa kemajuan. |
| Abstain/konflik | Memilih respons dan arahan yang sesuai ketika sumber tidak cukup atau bertentangan, tanpa mengarang keputusan. Dinilai terpisah dari klarifikasi dan answerable. |
| Workflow valid | Mencapai hasil service yang ditentukan, dengan payload, jalur approval, dan versi benar. Semua skenario valid terjadwal termasuk dalam denominator, termasuk error/timeout. |
| Penolakan dan gangguan | Menghasilkan expected behavior masing-masing. Penolakan sah atau status hasil belum diketahui bukan keberhasilan submission, tetapi dapat menjadi hasil aman yang diharapkan. |
| Kasus kritis | Seluruh kasus wajib menghasilkan perilaku yang benar. Satu kesalahan kritis teramati memblokir kelulusan milestone/release terkait. |

Target 90% untuk retrieval dan 95% untuk jawaban/klarifikasi/abstain/workflow yang pernah dibahas merupakan usulan awal, bukan acceptance gate yang sudah dikunci. Dengan paket lebih kecil, laporan mengutamakan jumlah benar/total per kategori, tenant, bahasa, dan run; persentase menyertakan denominator. Target final ditetapkan setelah kalibrasi dan cakupan ditinjau, sebelum acceptance. Pada kelompok kecil, target tinggi dapat berarti semua kasus harus lulus.

Kesalahan kritis mencakup kebocoran data, tindakan tanpa izin, self-approval, pengajuan ganda, credential terungkap, klaim sukses palsu, serta informasi bisnis material yang salah tentang kelayakan, deadline, nominal/mata uang, approver, atau status. Skor rata-rata tidak dapat menutupi kesalahan ini. Nol pelanggaran pada suite berarti tidak ditemukan pada pengujian tersebut, bukan jaminan universal.

Pengujian AI diulang minimal tiga run dengan kondisi yang dicatat. Target berlaku pada setiap run dan kelompok yang ditetapkan sebelum acceptance; run terbaik tidak dipilih untuk mewakili semua hasil. Pengulangan kasus yang sama tidak dihitung sebagai kasus independen tambahan. Penilai manusia memakai rubrik dan menyelesaikan kasus meragukan; model judge dapat membantu, tetapi bukan satu-satunya penentu kelulusan kritis.

### Pilot manual dibandingkan dengan bantuan AI

Jumlah kasus mengukur cakupan/kebenaran sistem. Pilot manual–assisted memeriksa manfaat bagi pengguna; keduanya tidak saling menggantikan.

**Manual:** pengguna mencari kebijakan, menentukan persyaratan, lalu mengisi formulir pengajuan yang menuju service yang sama. **Assisted:** pengguna bertanya kepada assistant, memeriksa sumber dan draf, lalu mengonfirmasi. Keduanya memakai hak akses, kebijakan, serta kondisi tugas yang sebanding.

Pilot menggunakan tugas setara dan urutan bergantian agar peserta tidak selalu mengerjakan kondisi assisted setelah mengetahui jawabannya. Ukur waktu sampai jawaban/pengajuan yang benar, kelengkapan dan kesalahan, jumlah koreksi/bantuan tambahan, serta pemahaman sumber dan status. Waktu memeriksa dan memperbaiki keluaran AI termasuk dalam pengukuran; kegagalan dan tugas tidak selesai ikut dilaporkan. Waktu tunggu manusia dipisahkan dari waktu pengerjaan aktif.

Jumlah peserta, tugas, protokol, dan kriteria keberhasilan pilot ditetapkan sebelum pelaksanaan. Laporkan hasil per kondisi, jumlah tugas benar/selesai, waktu, dan bantuan yang diperlukan. Jika peserta representatif belum tersedia, hasil disebut walkthrough skenario, bukan validasi pengguna. Pilot kecil tidak menjadi dasar klaim peningkatan produktivitas yang luas; manfaat AI tidak diasumsikan sebelum diukur.

### Performa, biaya, dan bukti tahap lanjutan

Latency MVP diukur terpisah untuk jawaban RAG, submission sampai hasil terbukti, pembacaan status, dan rekonsiliasi setelah gangguan. Titik mulai/akhir serta timeout ditentukan; waktu menunggu keputusan manusia tidak masuk latency sistem. Laporkan p50/p95 bersama jumlah timeout/error agar request gagal tidak menghilang dari laporan.

Biaya per tugas berhasil dihitung dari seluruh biaya percobaan dalam cohort, termasuk kegagalan dan retry, dibagi jumlah tugas berhasil. Komponen model dan infrastruktur serta basis alokasinya dilaporkan terpisah. Jika tidak ada tugas berhasil, biaya per keberhasilan tidak terdefinisi.

Target latency dan biaya MVP dikalibrasi lalu dikunci sebelum acceptance, bersama lingkungan, ukuran/jumlah dokumen, panjang konteks, campuran tugas, beban/concurrency, durasi, dan kondisi cache/cold start. Target tidak dilonggarkan diam-diam setelah kegagalan.

- **Checkpoint RAG:** sumber, jawaban, lifecycle dokumen, serta akses dua tenant.
- **MVP lengkap:** workflow, approval, integrasi API, recovery, keamanan, latency, dan biaya.
- **Milestone 3:** clean deploy, Docker/CI/CD/IaC, pengujian beban, readiness, restart, rollout/rollback, dan restore nyata pada lingkungan terisolasi. Kapasitas, availability, RPO/RTO dan kondisi pengukuran dikunci sebelum penerimaan tahap ini. Pemeriksaan relevan diulang pada deployment akhir.
- **Milestone 4:** commercial API dibandingkan dengan model open-source yang benar-benar dijalankan, memakai tugas dan kondisi layanan sebanding. Kualitas, keselamatan, tool completion, latency, resource/GPU, serta biaya diukur; hardware dan basis biaya masing-masing dicatat. Model yang gagal gate keselamatan dapat menjadi hasil eksperimen, tetapi tidak dipromosikan menjadi backend aplikasi. Jika fine-tuning dilakukan, data train/validation/test dipisahkan.

### Pengelolaan evaluasi dan bukti kelulusan

Dataset pengembangan/kalibrasi dipisahkan dari acceptance. Sebelum acceptance, kunci versi dataset, gold, rubrik, ambang, model/prompt/config, aturan tenant, dan kondisi pengujian. Kasus gagal tetap disimpan sebagai regression; tidak dihapus untuk memperbaiki skor. Koreksi gold yang terbukti salah harus memiliki alasan dan catatan perubahan.

Jika hasil acceptance digunakan untuk mendiagnosis dan memperbaiki kandidat, dataset itu tidak lagi sepenuhnya held-out bagi revisi berikutnya. Jalankan regression yang relevan dan gunakan tambahan holdout yang belum dilihat untuk memeriksa generalisasi. Setiap kandidat dan perubahan dataset dilacak.

Bukti per requirement mencakup ID kasus/run, input dan expected/actual, versi kode/dataset/model/config, waktu dan lingkungan, sumber, trace tersanitasi, serta hasil service. QA memeriksa dan mereproduksi pemeriksaan kritis secara independen; Lead memeriksa dasar temuannya. Panduan menjalankan, runbook, dan checkpoint pembelajaran membantu pengguna menjelaskan, mereproduksi, serta memperbaiki hasil proyek.

Kelulusan spesifikasi berbeda dari kelulusan runtime. Rencana ini mendukung pembuktian kemampuan teknis dalam lowongan; belum menyatakan pengujian telah dilakukan, kesiapan produksi keseluruhan, atau terpenuhinya syarat tahun pengalaman kerja.

## 6. Rancangan teknis awal dan rencana pengerjaan

### Arah arsitektur

Satu repository berisi aplikasi web, backend platform modular, worker, dan service perjalanan/reimbursement referensi. API dan worker platform menggunakan modul bisnis yang sama, tetapi dijalankan sebagai proses terpisah. Service referensi memiliki API, otorisasi, migration, dan penyimpanan bisnis sendiri; platform tidak membaca tabel service secara langsung.

```text
Web karyawan / approver / admin / pengelola pengetahuan
                         |
                   Platform API
      identitas, tenant, akses, dokumen, percakapan,
       draf, konfigurasi, orkestrasi, audit, evaluasi
               |              |              |
         Platform DB     File privat     Worker platform
         + vector index                  ingest / agent jobs /
         + checkpoint                    retry / rekonsiliasi
                                             |
                              Adapter model dan tool terotorisasi
                                  |                    |
                             Model API         Service referensi API
                                                + database sendiri
```

Backend modular dipilih agar transaksi internal, pengujian, dan operasi tetap terkendali. Batas modul tetap eksplisit; pemisahan menjadi microservice tambahan memerlukan kebutuhan yang dibuktikan melalui beban atau isolasi kegagalan. Service referensi dipisahkan untuk membuktikan integrasi lintas sistem, bukan untuk memecah setiap modul menjadi layanan.

### Stack awal dan alasan

| Bagian | Pilihan awal | Alasan dan batas keputusan |
| --- | --- | --- |
| Web | React + TypeScript | Satu aplikasi dengan tampilan sesuai peran, mendukung ID/EN dan alur manual maupun assisted. |
| Backend | Python + FastAPI | API tervalidasi, service bisnis, integrasi AI, dan kemampuan backend yang selaras lowongan. |
| Orkestrasi | LangGraph dengan checkpoint persisten | Mengatur klarifikasi, tool, serta pause/resume. Checkpoint tidak menggantikan otorisasi, idempotency, atau state bisnis service. |
| Data platform | PostgreSQL + pgvector | Data relasional, aturan/versi, draf, audit, job, checkpoint, dan pencarian vector. Full-text search menjadi kandidat hybrid; reranking dipilih dari evaluasi. |
| Database hosted | PostgreSQL terkelola; Neon kandidat awal | Mengurangi kebutuhan database lokal. Provider belum dikunci; koneksi, pgvector, biaya, region, backup, dan kompatibilitas checkpoint harus diuji. |
| Identity | Login OIDC melalui provider identitas | Identitas eksternal diverifikasi backend; membership dan kewenangan tenant tetap milik aplikasi. Provider, sesi browser, logout, dan revocation dirinci sebelum endpoint privat dibangun. |
| File | Object storage privat melalui adapter; arah AWS S3 | Dokumen dan receipt dipisahkan dari database. Pengunduhan melewati pemeriksaan akses backend agar revocation tidak bergantung pada URL publik. |
| Job dan recovery | Tabel job/outbox PostgreSQL + worker dengan lease | Ingest dan eksekusi tidak bergantung pada umur request HTTP. Redis/broker tambahan belum diperlukan sampai kebutuhan terukur. |
| Model dan embedding | Satu provider komersial melalui adapter | Pilih model chat/tool dan embedding ID/EN berdasarkan evaluasi, akses akun, kebijakan data, latency, dan biaya. Nama provider/model belum dikunci. |
| Service referensi | Python/FastAPI + PostgreSQL terpisah secara akses | Menyimpan perjalanan dan pengajuan dengan state machine serta aturan yang ditegakkan service. Dapat memakai server DB yang sama pada demo, tetapi database/credential/migration terpisah dan tidak saling diakses. |
| Pengujian | pytest, HTTP integration, dan Playwright | Aturan, kontrak, gangguan, evaluasi otomatis, serta alur UI. Tool evaluasi tambahan dipilih jika diperlukan. |
| Delivery | Docker, CI/CD, dan infrastructure as code | Build yang dapat direproduksi; cloud diarahkan ke AWS. Versi dependency dikunci setelah compatibility check. |

Dokumentasi resmi yang menjadi acuan kemampuan framework: [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) menjelaskan checkpoint per thread; [FastAPI containers](https://fastapi.tiangolo.com/deployment/docker/) menjelaskan deployment container; [Neon connection pooling](https://neon.com/docs/connect/connection-pooling) menjelaskan pooling dan batas kompatibilitasnya. Pemakaian library tidak otomatis meluluskan kebutuhan bisnis atau keamanan proyek.

### Modul dan kepemilikan data

- **Identity dan akses:** verifikasi login, sesi, membership, peran, tenant context, dan kebijakan akses objek. Tenant aktif hanya dipilih dari membership sah; ID pada URL/chat bukan bukti kewenangan.
- **Knowledge:** file/version, pemeriksaan dokumen, chunk, embedding, ACL, publikasi, penarikan, serta provenance hasil retrieval.
- **Agent dan conversations:** percakapan, konteks minimum, checkpoint, tool registry, batas langkah/token/waktu, dan referensi eksekusi. Run ID terikat tenant, pengguna, dan percakapan; pengetahuan run ID tidak memberi akses resume.
- **Business configuration dan drafts:** paket aturan immutable, draf versioned, ringkasan konfirmasi, serta catatan consent dan identitas tindakan.
- **Integration dan jobs:** adapter model, storage, service API, outbox, lease, retry terbatas, dan rekonsiliasi. Error otorisasi/validasi tidak diperlakukan sebagai gangguan yang bisa diatasi dengan retry.
- **Audit dan evaluation:** metadata tindakan, tracing tersanitasi, metrik kualitas/biaya, serta runner dataset yang dipisahkan dari data operasional.
- **Service referensi:** perjalanan/item biaya, salinan paket aturan, pengajuan, jalur/tahap approval, keputusan versioned, bukti idempotency, dan referensi receipt yang immutable. Akses bukti tetap memerlukan pemeriksaan aktor terkini oleh jalur unduh yang dipercaya.

Database platform memakai tenant_id pada data tenant, relasi yang tidak mengizinkan referensi lintas tenant, serta row-level security sebagai lapisan tambahan. Role runtime dibatasi dan dipisahkan dari role migration. Koneksi pool menetapkan konteks dalam transaksi dan tidak membawa tenant sebelumnya. RLS, checkpoint adapter, worker, dan pooled/direct connections diuji bersama; jangan mengandalkan state sesi koneksi atau thread_id saja.

Adapter checkpoint baru diterima setelah spike membuktikan isolasi read/write/list/history/resume serta penyimpanan perantara yang digunakannya. Runtime tanpa konteks tenant/aktor yang sah ditolak. Setup/migration menggunakan jalur terpisah; kompatibilitas adapter tidak boleh diatasi dengan memberi role owner atau BYPASSRLS kepada runtime. Adapter yang gagal gate ini diganti atau dibungkus melalui repository berotorisasi sebelum dipakai pada data privat.

### Alur teknis utama

**Dokumen:** upload privat → pemeriksaan file → parsing PDF teks/Markdown → chunk dan embedding → validasi indeks/ACL → siap → terbitkan versi. Pekerjaan menyimpan identitas tenant dan versi. Versi tidak terbit sebagian; ingest gagal dapat diperiksa dan diulang tanpa menggandakan versi aktif. Penarikan/ACL dicatat pada sumber otorisasi terlebih dahulu sehingga indeks lama tidak membuka akses.

**Pertanyaan:** autentikasi → tentukan akses terkini → retrieval dengan filter tenant/ACL/versi → verifikasi ulang kandidat sumber → bentuk konteks berprovenance → panggil model → periksa rujukan dan akses sebelum keluaran → simpan riwayat dan sumber. Hasil parsial sensitif tidak dikirim sebelum pemeriksaan akhir. Model menerima konten minimum; dokumen diperlakukan sebagai data, bukan pemberi instruksi tool.

**Submission:** draf → validasi → ringkasan → konfirmasi eksplisit → transaksi platform menyimpan consent, identitas tindakan, dan outbox → worker memanggil service dengan key tetap → service memvalidasi aktor, paket aturan, deadline, dan bukti lalu mencatat hasil atomik → platform merekonsiliasi referensi/status. Identitas serta bukti konfirmasi dari backend diteruskan melalui komunikasi terautentikasi; model tidak membuat bukti otorisasi.

Transaksi consent/outbox membekukan snapshot payload, versi aturan, dan versi/digest lampiran. Selama antrean pengiriman, pengiriman berlangsung, atau hasil belum diketahui, draf tidak dapat diedit atau diajukan ulang. UI menjelaskan bahwa waktu penerimaan service menentukan deadline; konfirmasi sebelum deadline belum menjamin submission tepat waktu bila antrean terlambat. Koreksi baru dibuka setelah terbukti tidak ada commit dan tindakan lama tidak dapat dikirim lagi. Jika telah tercatat, perubahan mengikuti alur perlu informasi di service. Worker memeriksa kewenangan terkini sebelum pengiriman baru; hasil ambigu diperiksa melalui lookup sebelum retry tanpa mengubah payload/key.

API service tidak memegang transaksi database platform. Timeout lintas jaringan menghasilkan status belum diketahui sampai lookup hasil membuktikannya. Service menyediakan lookup identitas tindakan, expected version, dan idempotency untuk submission, pelengkapan, serta keputusan. Masa simpan key mencakup lifetime retry/recovery yang didukung dan tidak didaur ulang selama hasil bisnis terkait masih aktif.

Claim, perpanjangan lease, penulisan checkpoint, publikasi dokumen, dan penyelesaian job memeriksa generasi kepemilikan yang masih berlaku. Worker yang kehilangan lease tidak boleh memajukan state. Hanya satu eksekusi boleh memajukan state percakapan/run pada satu waktu; request resume bersamaan ditolak atau diserialkan melalui version check. Penegakan ini mencakup adapter persistence, bukan hanya tabel job. Side effect eksternal tetap memakai identitas tindakan tetap dan idempotency service; checkpoint tidak membuktikan bahwa side effect belum terjadi. Uji lease habis lalu worker lama kembali dan dua resume bersamaan wajib lulus.

**Receipt lintas service:** sebelum commit, service memverifikasi tenant, pemilik, versi/digest, dan status siap melalui API metadata internal terautentikasi pada platform. Metadata payload bebas bukan bukti validitas file. Saat unduh, platform memeriksa hubungan aktor dengan pengajuan dan tahap approval melalui service, selain akses file/tenant terkini; role approver umum tidak cukup. Bila pemeriksaan tidak tersedia, akses ditahan. Versi receipt yang menjadi bukti pengajuan dipertahankan sesuai kebijakan retensi dan tidak dihapus oleh pembersihan draf sementara. Rincian hak unduh per tahap ditetapkan pada kontrak API dengan prinsip akses minimum.

**Approval dan resume:** aksi manusia diterima API terautentikasi → otorisasi dan versi diperiksa service → keputusan/tahap disimpan atomik → UI/platform membaca status. UI memakai polling terotorisasi pada MVP. Agent tidak perlu menahan request HTTP selama menunggu manusia; resume memeriksa akses dan state service terkini sebelum memakai checkpoint.

**Paket aturan:** pengelola berwenang memeriksa pasangan dokumen/config → paket immutable disiapkan pada service → pengakuan versi diverifikasi → platform mengaktifkan versi untuk rentang tanggal yang sah. Kegagalan sinkronisasi menahan submission terkait, bukan memakai versi berbeda diam-diam. Perubahan retroaktif tetap di luar MVP.

### Batas operasi, risiko, dan uji kelayakan awal

Sebelum fitur bergantung pada provider, lakukan percobaan kecil dengan data fiktif: login OIDC dan revocation, koneksi DB/pgvector/checkpoint, upload–download privat dan pemeriksaan file, model tool calling/embedding ID/EN, serta API service yang commit lalu kehilangan respons. Persetujuan akun, credential, batas kuota, region/data handling, dan biaya dicatat tanpa menampilkan secret. Provider yang tidak memenuhi kebutuhan diganti melalui adapter sebelum implementasi besar.

Ingest, run agent, serta job memiliki timeout, ukuran input, jumlah langkah, retry, dan anggaran yang dibatasi. Kuota per tenant dan batas worker mencegah satu klien menghabiskan kapasitas bersama; nilainya ditetapkan dari kalibrasi. Status gagal/ditahan dapat diperiksa manusia; fallback tidak boleh mengganti aturan, melonggarkan akses, atau mengirim ulang aksi dengan key baru.

Komponen biaya awal: model chat, embedding/reranking bila dipakai, database, file storage/transfer, logging, compute aplikasi, serta GPU pada milestone open-source. Estimasi dibuat untuk workload demo dan beban uji, memakai harga provider yang diverifikasi saat pemilihan. Belum ada nilai biaya atau paket server yang diklaim telah disetujui; pengadaan resource berbayar dibahas sebelum dilakukan.

Pengembangan lokal memakai layanan minimum yang diperlukan; cluster Kubernetes dan GPU tidak wajib menyala sepanjang pengerjaan. Milestone cloud memakai compute Linux AWS dengan layanan database/storage yang memenuhi kebutuhan; bentuk EC2/K3s atau alternatif dipilih melalui estimasi resource, akses, region, dan biaya. Single node tidak diklaim high availability lintas mesin. Backup platform, service, file, dan versi konfigurasi direstore bersama ke lingkungan terisolasi dan direkonsiliasi sebelum akses dibuka.

Restore tidak mengasumsikan snapshot antarstorage diambil pada waktu yang sama. Worker dan pengiriman eksternal ditahan selama pemulihan. Rekonsiliasi memeriksa identitas tindakan terhadap hasil service, versi file/receipt yang tersedia, dan paket aturan; deletion/revocation diterapkan kembali sebelum akses dan worker dibuka. Ketidaksesuaian yang belum terselesaikan ditahan tanpa mengganti key atau membuat pengajuan baru. Pengujian milestone 3 mencakup backup platform yang mendahului commit service dan database yang merujuk file belum pulih.

### Urutan pengerjaan dan checkpoint

| Tahap | Pekerjaan dan prasyarat | Bukti selesai / checkpoint |
| --- | --- | --- |
| Fondasi kontrak | Rinci arsitektur, schema, kontrak API/aktor, state machine, retensi, serta test plan dari bagian 1–5. | Keputusan konsisten; temuan QA material ditutup sebelum kontrak implementasi dibakukan. |
| Uji provider dan bootstrap | Spike kelayakan kecil, dependency lock, konfigurasi tersanitasi, health/error/request ID, serta instruksi instalasi. | Clean setup dan pemeriksaan koneksi aktual yang relevan; credential hanya dimuat proses aplikasi/testing. |
| Identitas, data, dan tenant | Migration, runtime least privilege, OIDC/sesi, membership, RLS/ACL, dua tenant dan role fixtures. | Akses silang tenant/peran ditolak; revocation dan pool reuse diuji sebelum endpoint data privat dilanjutkan. Seluruh jalur persistence checkpoint lulus spike isolasi sebelum adapter dipakai. |
| Knowledge dan worker | Job/outbox/lease, file privat, ingestion, lifecycle versi, ACL, publikasi, serta penarikan. | Upload sampai terbit dan cabut bekerja; gagal/retry tidak membuka dokumen yang belum siap. |
| RAG dan web pertama | Retrieval, adapter model, konteks/checkpoint berprovenance, jawaban bersumber, UI admin/karyawan, dan manual search. | **Milestone 1:** alur dua tenant bekerja melalui UI; kalibrasi/evaluasi RAG dan pemeriksaan akses lulus. Checkpoint pembelajaran: pengguna menjelaskan sumber jawaban dan mencoba kasus akses ditolak. |
| Service referensi dan aturan | Dapat disiapkan setelah kontrak API/identity siap; perjalanan/item, paket aturan, state machine, key hasil, serta API approval. | Endpoint nyata menolak bypass aktor/tenant/aturan; concurrency dan hasil commit dapat dibuktikan dari storage service. |
| Workflow dan web approval | Setelah service dan RAG siap: draf/consent, tool, outbox, submission, pelengkapan, keputusan manusia, status/resume, formulir manual pembanding. | Alur lengkap termasuk multi-stage dan needs-information; snapshot draf beku, metadata/akses receipt lintas service, lease kedaluwarsa, dan resume bersamaan diuji. Restart, timeout, dan klik ganda tidak menggandakan tindakan. |
| Penerimaan MVP | Freeze dataset/rubrik/ambang, paket 80 RAG/24 workflow dan suite kritis; pilot manual–assisted bila peserta tersedia. | **Milestone 2:** seluruh kriteria bagian 3–5 lulus dengan bukti. Walkthrough dibedakan dari validasi pengguna. Pengguna mencoba menjalankan dan menjelaskan satu recovery sendiri. |
| Cloud dan operasi | Setelah baseline MVP tersedia: Docker, CI/CD/IaC, AWS deployment, Kubernetes operasional, monitoring, beban, rollout/rollback, backup–restore. | **Milestone 3:** target kapasitas/pemulihan yang dikunci diuji pada lingkungan akhir, termasuk rekonsiliasi backup berbeda waktu sebelum worker/akses dibuka; bukan sekadar tersedianya manifest. |
| Open-source inference | Setelah dataset dan baseline commercial tersedia: pilih model/resource, deploy inference, konfigurasi dan optimasi, bandingkan; fine-tuning jika dibutuhkan. | **Milestone 4:** inference nyata serta hasil tradeoff sesuai bagian 3 dan 5; model tidak dipakai aplikasi bila gagal gate keselamatan. |

Frontend dikerjakan per alur setelah kontrak API yang bersangkutan siap; tidak menunggu seluruh backend selesai. Uji keamanan dan recovery mengikuti tiap tahap, bukan ditunda sampai acceptance. Detail tugas kecil, ID, dependency, dan perintah verifikasi diturunkan pada task plan implementasi berikutnya.

Dokumen ini adalah rancangan awal. Pemilihan versi library, provider, paket cloud, schema/API final, dan angka kapasitas mengikuti hasil percobaan serta review; tidak ada klaim fitur telah berjalan atau production-ready hanya karena rancangan ini ditulis.
