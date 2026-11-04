# Economic Outlook Bot

Bot yang otomatis memantau BI, BPS, dan The Fed tiap 3 jam lewat GitHub Actions,
menyimpan data terbaru ke `data/`, dan kirim notifikasi Telegram kalau ada rilis baru.

## Setup (sekali aja, ±15 menit)
1. Bikin repo baru di GitHub (boleh private), upload semua file ini.
2. Ambil API key gratis:
   - FRED: https://fred.stlouisfed.org/docs/api/api_key.html
   - BPS: https://webapi.bps.go.id/developer/ (daftar, lalu buat aplikasi, pakai App ID-nya)
3. Di repo: Settings → Secrets and variables → Actions → New repository secret:
   `FRED_API_KEY`, `BPS_API_KEY`, (opsional) `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Tab Actions → "Economic Outlook Bot" → Run workflow untuk tes pertama.
5. Lihat hasilnya di `data/OUTLOOK.md`.

## Telegram (opsional)
Chat @BotFather → /newbot → dapat token. Kirim pesan ke bot lu, lalu buka
`https://api.telegram.org/bot<TOKEN>/getUpdates` untuk lihat `chat.id`.

## Jalanin di laptop
pip install -r requirements.txt
FRED_API_KEY=xxx BPS_API_KEY=yyy python bot.py

## Nambah indikator
- FRED: tambah baris di `FRED_SERIES` (cari kode seri di fred.stlouisfed.org).
- BI: tambah URL halaman tabel di `BI_PAGES`.

---

# Calendar Bot (TradingView → Discord)

Pantau kalender ekonomi TradingView tiap 10 menit, kategorikan dampak ke Indonesia
(🔴 Tinggi / 🟠 Sedang / ⚪ Rendah), lalu kirim ke Discord:
- Jadwal kalender hanya dipantau di belakang layar, TIDAK dikirim
- Yang dikirim cuma saat data rilis: Actual vs Forecast/Konsensus vs Previous, analisis, dan jadwal rilis berikutnya

## Setup
1. Discord: Server Settings → Integrations → Webhooks → New Webhook → pilih channel → Copy Webhook URL.
2. GitHub: Settings → Secrets and variables → Actions → secret `DISCORD_WEBHOOK_URL` (tempel URL tadi).
   JANGAN taruh URL webhook di kode/chat publik; siapa pun yang punya URL itu bisa posting ke channel lu.
3. (Opsional) secret `ANTHROPIC_API_KEY` dari console.anthropic.com supaya analisis ditulis Claude.
   Tanpa ini, bot tetap jalan pakai analisis berbasis aturan.
4. Actions → "Calendar Bot" → Run workflow. Cek log di tab Actions; pesan Discord baru muncul saat ada data yang rilis.

## Atur sendiri (di calendar.yml)
- `COUNTRIES`: negara yang dipantau (ID,US,CN,EU,JP,GB,KR,...)
- `MIN_ALERT`: TINGGI = hanya yang paling penting, SEDANG = default, RENDAH = semua
- `SEND_PREVIEW: "1"` / `SEND_REMINDER: "1"` kalau suatu saat mau jadwal harian / pengingat lagi
- Aturan kategori dampak: dict `RULES` di calendar_bot.py

## Tes lokal tanpa kirim ke Discord
DRY_RUN=1 python calendar_bot.py

## Catatan kuota (mode hemat untuk free plan)
- Calendar bot jalan tiap 15 menit hanya di jam rilis, Senin-Jumat: ±1.230 menit/bulan.
- Bot data BI/BPS/Fed jalan 2x sehari Senin-Jumat: ±44 menit/bulan.
- Total ±1.300 menit, aman di bawah kuota 2.000 menit/bulan repo PRIVATE.
  Repo PUBLIC tidak dihitung kuota sama sekali.
- Tiap run cuma ambil kalender ±2 hari; kalender 100 hari ke depan hanya diambil
  saat ada rilis yang dikirim (untuk "next update").
- Analisis default berbasis aturan (gratis). ANTHROPIC_API_KEY opsional dan berbayar.
