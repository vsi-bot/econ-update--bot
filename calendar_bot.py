#!/usr/bin/env python3
"""
Calendar Bot: TradingView Economic Calendar -> analisis dampak ke Indonesia -> Discord
-------------------------------------------------------------------------------------
Yang dilakukan setiap kali jalan (idealnya tiap 10-15 menit via GitHub Actions):
  1. Ambil kalender ekonomi TradingView (3 hari ke belakang s/d 100 hari ke depan).
  2. Kategorikan dampak ke market & ekonomi Indonesia: TINGGI / SEDANG / RENDAH.
  3. Jadwal hanya dipantau (tidak dikirim). Opsional: SEND_PREVIEW=1 / SEND_REMINDER=1.
  4. Begitu angka actual keluar: kirim Actual vs Forecast (konsensus) vs Previous,
     bacaan sinyal makro (Indonesia, AS, global; pendek dan panjang),
     dan jadwal rilis berikutnya.

Env vars:
  DISCORD_WEBHOOK_URL  (wajib)  Server Settings -> Integrations -> Webhooks
  ANTHROPIC_API_KEY    (opsional) kalau diisi, analisis ditulis Claude; kalau kosong pakai aturan
  CLAUDE_MODEL         (opsional) default claude-haiku-5-5 (paling murah)
  COUNTRIES            (opsional) default ID,US,CN,EU,JP
  MIN_ALERT            (opsional) TINGGI / SEDANG / RENDAH, default SEDANG
  PREVIEW_HOUR_WIB     (opsional) default 7
  DRY_RUN=1            (opsional) print ke layar, tidak kirim ke Discord
  SHEETS_WEBHOOK_URL   (opsional) URL Web app Google Apps Script -> data masuk Google Sheets
  SHEETS_TOKEN         (opsional) kata sandi yang sama dengan TOKEN di Apps Script
"""
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Warna tema (merah ala sekuritas). Ganti hex di sini kalau punya kode warna resmi brand.
BRAND_RED = "#C8102E"     # garis Actual, aksen utama
BRAND_DARK = "#3A3A3A"    # garis Forecast, teks
BRAND_SOFT = "#F7E1E4"    # area lembut di bawah garis Actual
BRAND_GRID = "#E6E6E6"

# ---------------------------------------------------------------- konfigurasi
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "calendar_state.json"

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
COUNTRIES = os.getenv("COUNTRIES", "ID,US,CN,EU,JP")
MIN_ALERT = os.getenv("MIN_ALERT", "SEDANG").upper()
PREVIEW_HOUR = int(os.getenv("PREVIEW_HOUR_WIB", "7"))
DRY_RUN = os.getenv("DRY_RUN") == "1"
# Default: kalender hanya dipantau, yang dikirim ke Discord cuma hasil rilis.
SEND_PREVIEW = os.getenv("SEND_PREVIEW", "0") == "1"    # jadwal harian
SEND_REMINDER = os.getenv("SEND_REMINDER", "0") == "1"  # pengingat sebelum rilis
TEST_SEND = os.getenv("TEST_SEND", "0") == "1"  # kirim 1 rilis terakhir sebagai tes, lalu berhenti
TEST_MATCH = os.getenv("TEST_MATCH", "").strip()  # kalau diisi, tes hanya rilis yang judulnya cocok
SHEETS_URL = os.getenv("SHEETS_WEBHOOK_URL", "")
SHEETS_TOKEN = os.getenv("SHEETS_TOKEN", "")
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "1830"))  # ±5 tahun riwayat untuk Google Sheets
BACKFILL_VERSION = "v3-cadangan"  # ganti nilai ini = paksa isi ulang riwayat Sheets
SHEETS_BUDGET = int(os.getenv("SHEETS_BUDGET_SECONDS", "150"))  # waktu maksimal cicil riwayat per run  # riwayat awal saat Sheets pertama tersambung

WIB = ZoneInfo("Asia/Jakarta")
TV_URL = "https://economic-calendar.tradingview.com/events"
TV_HEADERS = {
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}
LEVEL_RANK = {"RENDAH": 0, "SEDANG": 1, "TINGGI": 2}
LEVEL_STYLE = {  # emoji, warna embed Discord
    "TINGGI": ("🔴", 0xE74C3C),
    "SEDANG": ("🟠", 0xF39C12),
    "RENDAH": ("⚪", 0x95A5A6),
}
FLAGS = {"ID": "🇮🇩", "US": "🇺🇸", "CN": "🇨🇳", "EU": "🇪🇺", "JP": "🇯🇵",
         "GB": "🇬🇧", "KR": "🇰🇷", "SG": "🇸🇬", "AU": "🇦🇺", "IN": "🇮🇳"}
HARI = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("calbot")


# ---------------------------------------------------------------- kategori dampak
# Kata kunci dicocokkan ke judul event (huruf kecil). Urutan: yang pertama cocok menang.
RULES = {
    "ID": [
        (["interest rate", "rate decision", "bi rate", "7-day reverse repo"], "TINGGI"),
        (["inflation", "cpi"], "TINGGI"),
        (["gdp"], "TINGGI"),
        (["trade balance", "exports", "imports"], "TINGGI"),
        (["foreign exchange reserves", "fx reserves"], "SEDANG"),
        (["retail sales", "consumer confidence", "pmi", "loan growth", "m2"], "SEDANG"),
    ],
    "US": [
        (["fed interest rate", "fomc", "fed funds"], "TINGGI"),
        (["non farm", "nonfarm", "non-farm"], "TINGGI"),
        (["cpi", "inflation rate", "pce price"], "TINGGI"),
        (["gdp growth"], "TINGGI"),
        (["unemployment rate", "fed chair"], "TINGGI"),
        (["ism", "retail sales", "ppi", "jobless claims", "jolts", "michigan",
          "fomc minutes", "adp", "average hourly earnings"], "SEDANG"),
    ],
    "CN": [
        (["gdp", "trade balance", "loan prime rate"], "SEDANG"),
        (["pmi", "cpi", "inflation", "industrial production", "retail sales"], "SEDANG"),
    ],
    "*": [  # negara lain
        (["interest rate decision", "rate decision"], "SEDANG"),
    ],
}


def classify(ev):
    """Return (level, alasan) dampak ke Indonesia."""
    title = (ev.get("title") or "").lower()
    c = ev.get("country", "")
    tv_imp = ev.get("importance", -1)
    for keys, level in RULES.get(c, []) + (RULES["*"] if c not in RULES else []):
        if any(k in title for k in keys):
            return level, "indikator kunci"
    # fallback: pakai importance TradingView, diturunkan untuk negara yang dampaknya ke RI lebih kecil
    if c == "ID":
        return {1: "TINGGI", 0: "SEDANG"}.get(tv_imp, "RENDAH"), "importance TradingView"
    if c == "US":
        return {1: "SEDANG"}.get(tv_imp, "RENDAH"), "importance TradingView"
    if c == "CN" and tv_imp == 1:
        return "SEDANG", "importance TradingView"
    return "RENDAH", "importance TradingView"


# ---------------------------------------------------------------- analisis
def indicator_type(title):
    t = title.lower()
    if any(k in t for k in ["rate decision", "interest rate", "loan prime", "fed funds", "reverse repo"]):
        return "rate"
    if any(k in t for k in ["unemployment", "jobless"]):
        return "unemployment"  # naik = ekonomi melemah
    if any(k in t for k in ["cpi", "inflation", "pce", "ppi", "price index", "hourly earnings"]):
        return "inflation"
    if any(k in t for k in ["payroll", "employment change", "adp", "jolts"]):
        return "jobs"
    if "gdp" in t:
        return "growth"
    if any(k in t for k in ["trade balance", "balance of trade", "exports", "imports"]):
        return "trade"
    if "reserves" in t:
        return "reserves"
    if any(k in t for k in ("minutes", "fed chair", "press conference", "speech")):
        return "guidance"
    return "activity"


def surprise(ev):
    a, f, p = ev.get("actual"), ev.get("forecast"), ev.get("previous")
    base, label = (f, "forecast") if f is not None else (p, "previous")
    if a is None or base is None:
        return 0, None, label
    diff = a - base
    if abs(diff) < 1e-9:
        return 0, 0.0, label
    return (1 if diff > 0 else -1), diff, label


# ---------------------------------------------------------------- bacaan makro
# Rilis dibaca dalam bahasa sehari-hari, lalu dipetakan ke tiga pasar
# (Indonesia, AS, global) dan dua horizon (hari ini, beberapa bulan ke depan).
MARKET_KEYS = ("id_pendek", "id_panjang", "us_pendek", "us_panjang",
               "global_pendek", "global_panjang")


def _clip(text, limit):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0]
    return (cut or text[:limit - 1]).rstrip(".,;:") + "…"


def _origin(country):
    return country if country in ("US", "ID", "CN", "JP", "EU") else "OTHER"


def _place(country):
    return {
        "ID": "Indonesia", "US": "AS", "CN": "China", "EU": "eurozone", "JP": "Jepang",
        "GB": "Inggris", "KR": "Korea", "AU": "Australia", "SG": "Singapura", "IN": "India",
    }.get(country or "", country or "negara ini")


def _actor(country):
    return {
        "ID": "BI", "US": "The Fed", "CN": "PBoC", "EU": "ECB", "JP": "BoJ",
        "GB": "BoE", "KR": "BoK", "AU": "RBA", "IN": "RBI",
    }.get(country or "", "bank sentralnya")


def _family(typ):
    return {
        "rate": "rate", "inflation": "prices", "unemployment": "labor", "jobs": "labor",
        "growth": "growth", "activity": "activity", "trade": "trade",
        "reserves": "reserves", "guidance": "guidance",
    }.get(typ, "activity")


def _heat(typ, direction):
    """hot = ekonomi atau kebijakan lebih kuat. Pengangguran dibalik: naik berarti lebih lemah."""
    if typ == "unemployment":
        return "cold" if direction > 0 else "hot"
    return "hot" if direction > 0 else "cold"


def _facts(ev):
    title = ev.get("title") or "Rilis"
    typ = indicator_type(title)
    direction, diff, base = surprise(ev)
    if diff is None:
        kind, paren = "unknown", "tanpa perkiraan maupun previous"
    elif direction == 0:
        kind, paren = "inline", f"sesuai {base}"
    else:
        kind, paren = _heat(typ, direction), f"{diff:+.4g} vs {base}"
    country = ev.get("country") or ""
    return {
        "title": title, "actual": fmt_val(ev, "actual"), "paren": paren, "typ": typ,
        "kind": kind, "origin": _origin(country), "place": _place(country),
        "actor": _actor(country), "family": _family(typ),
    }


def _compact_title(title):
    return "".join((title or "").lower().replace("-", " ").split())


def _note(title):
    """Satu kalimat tambahan, hanya kalau judulnya mudah salah baca."""
    t = (title or "").lower()
    if any(k in t for k in ("hourly", "wage", "earnings")):
        return "Ini upah pekerja, bukan harga barang di toko."
    if "ppi" in t or "producer price" in t:
        return "Ini harga di pabrik. Harga di toko sering menyusul."
    if "core" in t or "inti" in t:
        return "Angka ini tidak menghitung pangan dan bensin, yang sering naik turun sendiri."
    if "adp" in t:
        return "Ini hitungan perusahaan swasta, belum angka resmi pemerintah."
    if "jobless" in t or "claims" in t:
        return "Ini data mingguan, bukan tingkat pengangguran sebulan."
    if any(k in t for k in ("nonfarm", "non-farm", "non farm", "payroll")):
        return "Ini jumlah orang yang dapat kerja baru, bukan persentase pengangguran."
    if "minutes" in t or "notulen" in t:
        return "Ini catatan rapat, bukan angka ekonomi yang baru."
    if "qoq" in t and "yoy" not in t:
        return "Ini perubahan satu kuartal, bukan setahun penuh."
    return ""


def _dir_phrase(f):
    """Arah kejutan dalam kata sehari-hari, tanpa istilah analis."""
    t = f["title"].lower()
    kind, family = f["kind"], f["family"]
    if kind == "unknown":
        return "Belum ada perkiraan untuk dibandingkan"
    if kind == "inline":
        return "Angkanya pas dengan perkiraan"
    up = kind == "hot"
    if family == "rate":
        return "Bunga lebih tinggi dari perkiraan" if up else "Bunga lebih rendah dari perkiraan"
    if family == "prices":
        if any(k in t for k in ("hourly", "wage", "earnings")):
            return "Upah naik lebih cepat dari perkiraan" if up else "Upah naik lebih pelan dari perkiraan"
        return "Harga naik lebih cepat dari perkiraan" if up else "Harga naik lebih pelan dari perkiraan"
    if family == "labor":
        if "jobless" in t or "claims" in t:
            return ("Lebih sedikit orang menganggur dari perkiraan" if up else
                    "Lebih banyak orang menganggur dari perkiraan")
        if any(k in t for k in ("payroll", "nonfarm", "non-farm", "non farm", "employment", "adp")):
            return ("Perusahaan menambah pekerja lebih banyak dari perkiraan" if up else
                    "Perusahaan menambah pekerja lebih sedikit dari perkiraan")
        return ("Pengangguran lebih rendah dari perkiraan" if up else
                "Pengangguran lebih tinggi dari perkiraan")
    if family == "growth":
        return ("Ekonomi tumbuh lebih kencang dari perkiraan" if up else
                "Ekonomi tumbuh lebih pelan dari perkiraan")
    if family == "activity":
        return ("Survei bulan ini lebih kuat dari perkiraan" if up else
                "Survei bulan ini lebih lemah dari perkiraan")
    if family == "trade":
        return ("Ekspor lebih unggul dari perkiraan" if up else
                "Ekspor kalah dari impor lebih besar dari perkiraan")
    if family == "reserves":
        return "Cadangan dolar bertambah" if up else "Cadangan dolar berkurang"
    if family == "guidance":
        return ("Bank sentral terdengar lebih condong menahan bunga tinggi" if up else
                "Bank sentral terdengar lebih condong menurunkan bunga")
    return "Lebih kuat dari perkiraan" if up else "Lebih lemah dari perkiraan"


def _everyday(f):
    phrase = _dir_phrase(f)
    place = f["place"]
    if f["kind"] in ("unknown", "inline"):
        return f"{phrase}."
    if f["family"] == "activity":
        return f"{phrase}. Ini gambaran satu bulan di {place}, bukan ukuran ekonomi setahun."
    if f["family"] == "labor" and any(k in f["title"].lower() for k in ("payroll", "nonfarm", "non-farm", "non farm")):
        return f"{phrase}."
    if f["family"] == "rate":
        return f"{phrase}."
    return f"{phrase}."


def _today_limit(f):
    """Dua kalimat: apa yang biasanya terjadi hari ini, dan apa yang belum diputuskan."""
    origin, family, kind = f["origin"], f["family"], f["kind"]
    actor, place = f["actor"], f["place"]
    if kind == "unknown":
        return ("Hari ini rupiah tidak dapat arah baru.",
                "BI dan The Fed tidak mengubah bunga sebelum ada angka pembanding.")
    if kind == "inline":
        return ("Hari ini tidak ada kejutan, jadi rupiah tidak dapat dorongan baru.",
                "Bunga tidak berubah hanya karena angkanya sesuai perkiraan.")
    up = kind == "hot"
    if origin == "US" and family in ("prices", "labor", "rate", "guidance"):
        today = ("Hari ini dolar biasanya menguat, jadi rupiah bisa melemah." if up else
                 "Hari ini dolar biasanya melemah, jadi rupiah bisa sedikit naik.")
        limit = ("The Fed dan BI tidak mengubah bunga hanya karena satu laporan." if up else
                 "Bunga baru diturunkan jika angka seperti ini berulang, bukan karena satu kali.")
        return today, limit
    if origin == "US" and family == "activity":
        return (("Hari ini dolar bisa menguat sebentar, dan rupiah ikut tertekan." if up else
                 "Hari ini dolar bisa sedikit melemah, dan rupiah bisa sedikit lega."),
                "Satu survei belum membuat The Fed atau BI mengubah bunga.")
    if origin == "US" and family == "growth":
        return (("Hari ini rupiah biasanya ikut dolar, yang bisa tetap kuat." if up else
                 "Hari ini orang lebih hati-hati, dan rupiah bisa melemah dulu."),
                ("Ekonomi yang lebih kuat bagus untuk bisnis, tapi The Fed kurang alasan menurunkan bunga." if up else
                 "Kalau ekonomi AS memang melambat, The Fed baru punya alasan menurunkan bunga."))
    if origin == "US" and family == "trade":
        return ("Efeknya ke rupiah kecil pada hari ini.",
                "The Fed tidak mengubah bunga karena neraca dagang.")
    if origin == "US" and family == "reserves":
        return ("Cadangan devisa AS tidak menggerakkan rupiah hari ini.",
                "The Fed tidak memakai angka cadangan untuk menentukan bunga.")
    if origin == "ID" and family == "rate":
        return (("Bunga lebih tinggi menahan rupiah hari ini, tapi pinjaman jadi lebih mahal." if up else
                 "Bunga lebih rendah membuat pinjaman lebih murah, tapi rupiah bisa melemah."),
                "The Fed tidak ikut berubah. Yang berubah ada di Indonesia.")
    if origin == "ID" and family == "prices":
        return (("Orang mengira BI akan menahan bunga. Harga surat utang bisa turun hari ini." if up else
                 "Orang mengira BI bisa menurunkan bunga. Harga surat utang bisa naik hari ini."),
                ("Daya beli menyempit selama harga tetap naik cepat." if up else
                 "Itu baru terjadi jika harga yang tenang ini bukan cuma sekali."))
    if origin == "ID" and family == "labor":
        return (("Lebih banyak orang kerja menopang belanja di dalam negeri. Rupiah tidak otomatis menguat." if up else
                 "Lebih susah dapat kerja menekan belanja di dalam negeri. Rupiah bisa ikut lemah."),
                ("BI kurang alasan menurunkan bunga jika lowongan tetap banyak." if up else
                 "BI baru menurunkan bunga jika ini berulang, dan tetap menjaga rupiah."))
    if origin == "ID" and family == "growth":
        return (("Ekonomi yang lebih kuat menolong saham Indonesia dan kepercayaan ke rupiah hari ini." if up else
                 "Ekonomi yang lebih pelan menekan saham Indonesia, dan rupiah jadi lebih rentan hari ini."),
                ("BI kurang terdesak menurunkan bunga jika ekonomi sudah kuat." if up else
                 "Dorongan agar BI menurunkan bunga naik, tapi penurunan yang terlalu besar bisa menekan rupiah lagi."))
    if origin == "ID" and family == "activity":
        return ("Ini cuma survei satu bulan. BI tidak mengubah bunga hari ini.",
                "BI baru mengubah bunga jika survei yang searah ini berulang beberapa bulan.")
    if origin == "ID" and family == "trade":
        return (("Uang dari luar lebih banyak masuk, jadi rupiah dapat dukungan hari ini." if up else
                 "Uang dari luar lebih sedikit masuk, jadi rupiah lebih rentan hari ini."),
                "BI tidak otomatis mengubah bunga. Angka ini tidak membedakan harga barang ekspor dan jumlah barang.")
    if origin == "ID" and family == "reserves":
        return (("BI punya lebih banyak dolar untuk menjaga rupiah. Ini bukan tanda ekonomi tumbuh." if up else
                 "Cadangan yang turun sering berarti BI sedang menahan rupiah, atau ada uang yang keluar."),
                ("Cadangan yang lebih tebal membuat BI lebih mampu menahan gejolak." if up else
                 "Kalau cadangan terus turun, BI lebih sulit menjaga rupiah tanpa menaikkan bunga."))
    if origin == "ID" and family == "guidance":
        return (("BI terdengar lebih condong menahan bunga tinggi. Rupiah dapat dukungan hari ini." if up else
                 "BI terdengar lebih condong menurunkan bunga. Rupiah bisa melemah hari ini."),
                "Bunga baru benar-benar berubah jika keputusan berikutnya mengikuti kata-kata ini.")
    if origin == "CN" and family in ("growth", "activity"):
        return (("China yang lebih kuat biasanya mengangkat harga batu bara, nikel, dan sawit." if up else
                 "China yang lebih pelan biasanya menekan harga batu bara, nikel, dan sawit."),
                "BI tidak mengubah bunga karena data China. Ekspor baru benar-benar berubah jika ini bertahan.")
    if origin == "CN" and family == "rate":
        return (("Bunga China yang lebih tinggi mengurangi harapan bantuan pemerintah di sana." if up else
                 "Bunga China yang lebih rendah memunculkan harapan permintaan, belum berarti barangnya sudah terbeli."),
                "Ekspor Indonesia baru ikut berubah jika pabrik dan impor China benar-benar bergerak setelah ini.")
    if origin == "CN" and family == "trade":
        return (("China mungkin membeli lebih sedikit dari luar. Itu kurang baik untuk batu bara, nikel, dan sawit." if up else
                 "China mungkin membeli lebih banyak dari luar, dan itu baik untuk barang ekspor Indonesia. Bisa juga ekspor China yang melemah."),
                "Angka ini tidak memisahkan keduanya. Efeknya baru jelas setelah beberapa bulan.")
    if origin == "CN":
        return ("Efek ke Indonesia, kalau ada, lewat permintaan China, bukan lewat keputusan BI hari ini.",
                "The Fed tidak mengubah bunga karena data China.")
    if origin == "JP" and family == "rate":
        return (("Yen bisa menguat, dan orang yang minjam yen murah bisa menjual asetnya. Saham Indonesia bisa turun hari ini." if up else
                 "Yen bisa melemah, dan pinjaman yen murah bisa hidup lagi. Saham Indonesia bisa dapat dukungan hari ini."),
                ("BI tidak mengubah bunga karena Jepang. Efeknya baru besar jika Jepang terus menaikkan bunga." if up else
                 "Dukungan itu hilang jika yen terlalu lemah dan Jepang terpaksa menaikkan bunga lagi."))
    if origin == "EU" and family == "rate":
        return (("Bunga Eropa yang lebih tinggi mengangkat euro. Rupiah hanya kena sedikit." if up else
                 "Bunga Eropa yang lebih rendah menekan euro. Rupiah dapat ruang tipis, bukan karena BI."),
                "BI tetap lebih mengikuti The Fed daripada bank sentral Eropa.")
    if family in ("prices", "labor", "rate", "guidance"):
        return ((f"Bunga di {place} yang lebih tinggi bisa menekan rupiah sedikit hari ini." if up else
                 f"Bunga di {place} yang lebih rendah bisa memberi rupiah ruang tipis hari ini."),
                f"Efeknya lebih kecil daripada data AS, karena yang mengubah dolar besar adalah The Fed, bukan {actor}.")
    if family in ("growth", "activity"):
        return (f"Efek ke rupiah kecil pada hari ini.",
                f"BI tidak mengubah bunga karena permintaan di {place}.")
    return (f"Efek ke rupiah kecil pada hari rilis.",
            f"Satu angka dari {place} belum mengubah bunga BI atau The Fed.")


def rule_analysis(ev):
    f = _facts(ev)
    today, limit = _today_limit(f)
    parts = [
        _everyday(f),
        f"{f['title']} {f['place']} {f['actual']} ({f['paren']}).",
    ]
    note = _note(f["title"])
    if note:
        parts.append(note)
    parts.extend((today, limit))
    return _clip(" ".join(parts), 900)


def _say(f, text):
    return f"{f['title']} {f['place']}: {text}"


def _pack(id_pendek, id_panjang, us_pendek, us_panjang, global_pendek, global_panjang):
    return {
        "id_pendek": id_pendek, "id_panjang": id_panjang, "us_pendek": us_pendek,
        "us_panjang": us_panjang, "global_pendek": global_pendek, "global_panjang": global_panjang,
    }


def _market_lines(f):
    phrase = _dir_phrase(f)
    origin, family, kind = f["origin"], f["family"], f["kind"]
    actor, place = f["actor"], f["place"]
    if kind == "unknown":
        return _pack(
            _say(f, "belum ada perkiraan, jadi rupiah tidak dapat arah baru hari ini."),
            _say(f, "BI tidak mengubah bunga sebelum ada angka pembanding."),
            _say(f, "pasar AS tidak mengubah harga selama kejutannya belum terukur."),
            _say(f, "The Fed juga belum dapat arah baru."),
            _say(f, "pasar dunia tidak bergerak besar tanpa kejutan yang jelas."),
            _say(f, "satu angka tanpa pembanding belum mengubah apa-apa."),
        )
    if kind == "inline":
        return _pack(
            _say(f, "pas dengan perkiraan, jadi rupiah tidak dapat dorongan baru hari ini."),
            _say(f, "bunga tidak berubah hanya karena angkanya sesuai."),
            _say(f, "sudah diduga, jadi saham dan surat utang AS tidak perlu diubah hari ini."),
            _say(f, "The Fed tidak dapat alasan baru untuk mengubah bunga."),
            _say(f, "tidak menggerakkan dolar hari ini."),
            _say(f, "yang tetap berlaku adalah angkanya sendiri, bukan kejutan."),
        )
    up = kind == "hot"
    if origin == "US" and family == "labor":
        return _pack(
            _say(f, f"{phrase}. Dolar biasanya {'naik' if up else 'turun'} hari ini, jadi rupiah bisa {'melemah' if up else 'sedikit naik'}."),
            _say(f, "BI tidak mengubah bunga karena satu laporan kerja AS." if up else
                  "BI baru punya alasan menurunkan bunga jika laporan kerja AS yang lemah ini berulang."),
            _say(f, f"{phrase}. Bunga pinjaman jangka pendek AS bisa {'naik' if up else 'turun'} hari ini."),
            _say(f, "The Fed tidak menaikkan bunga hanya karena satu bulan orang lebih mudah dapat kerja." if up else
                  "The Fed baru mempertimbangkan bunga lebih rendah jika orang makin susah dapat kerja beberapa bulan."),
            _say(f, f"{phrase}. Dolar biasanya {'menguat' if up else 'melemah'} terhadap mata uang lain hari ini."),
            _say(f, "Perdagangan dunia tidak berubah dari satu laporan kerja." if up else
                  "Satu laporan yang lemah belum berarti ekonomi dunia melambat."),
        )
    if origin == "US" and family == "prices":
        return _pack(
            _say(f, f"{phrase}. Dolar biasanya {'naik' if up else 'turun'} hari ini, jadi rupiah bisa {'melemah' if up else 'sedikit naik'}."),
            _say(f, "BI kehilangan ruang menurunkan bunga selama harga di AS tetap naik cepat." if up else
                  "BI dapat ruang menurunkan bunga jika harga di AS terus lebih tenang."),
            _say(f, f"bunga pinjaman jangka pendek AS bisa {'naik' if up else 'turun'} hari ini. Saham yang sensitif bunga paling terasa."),
            _say(f, "The Fed lebih sulit menurunkan bunga selama harga belum mendingin." if up else
                  "The Fed baru menurunkan bunga jika harga yang lebih tenang ini bertahan, bukan karena satu bulan."),
            _say(f, f"dolar biasanya {'menguat' if up else 'melemah'} hari ini."),
            _say(f, "kalau harga AS tetap naik cepat, orang lebih suka menaruh uang di aset dolar." if up else
                  "kalau harga AS terus mendingin, biaya pinjaman di luar AS bisa ikut lebih murah."),
        )
    if origin == "US" and family == "rate":
        return _pack(
            _say(f, f"{phrase}. Rupiah {'tertekan' if up else 'bisa sedikit lega'} hari ini karena dolar {'menguat' if up else 'melemah'}."),
            _say(f, "BI sulit menurunkan bunga lebih dulu tanpa menekan rupiah." if up else
                  "BI dapat ruang menurunkan bunga, selama rupiah tidak melemah karena hal lain."),
            _say(f, "bunga pinjaman jangka pendek AS langsung berubah pada hari keputusan."),
            _say(f, "ini perubahan harga uang sekarang, bukan angka yang masih menunggu perbaikan data."),
            _say(f, f"dolar {'menguat' if up else 'melemah'} dan pinjaman global jadi {'lebih mahal' if up else 'lebih murah'} hari ini."),
            _say(f, "efeknya bertahan jika The Fed tidak membatalkan keputusan ini pada rapat berikut."),
        )
    if origin == "US" and family == "guidance":
        return _pack(
            _say(f, f"{phrase}. Dolar bisa {'naik' if up else 'turun'} hari ini, dan rupiah ikut."),
            _say(f, "BI tidak mengubah bunga hanya karena kata-kata The Fed. Data harga dan kerja berikutnya yang menentukan."),
            _say(f, "bunga pinjaman jangka pendek AS bisa bergerak hari ini karena kalimat itu."),
            _say(f, "satu pernyataan belum mengubah bunga. Angka harga dan lapangan kerja tetap yang menguji."),
            _say(f, f"dolar bisa {'menguat' if up else 'melemah'} hari ini karena orang membaca nada The Fed."),
            _say(f, "pasar dunia baru menganggapnya serius jika The Fed mengulanginya, bukan dari satu teks."),
        )
    if origin == "US" and family == "growth":
        return _pack(
            _say(f, f"{phrase}. Rupiah hari ini biasanya ikut dolar, yang bisa {'tetap kuat' if up else 'melemah'}."),
            _say(f, "ekspor Indonesia ke AS lebih tertolong, tapi biaya pinjaman global tidak cepat turun." if up else
                  "pasar ekspor ke AS menyusut, sementara ruang BI menurunkan bunga baru terbuka kalau dolar benar-benar turun."),
            _say(f, "saham bisnis harian AS dapat dukungan, sementara bunga jangka pendek bisa naik." if up else
                  "saham bisnis harian AS kehilangan dukungan, sementara bunga jangka pendek bisa turun."),
            _say(f, "satu kuartal masih bisa diperbaiki. The Fed kurang alasan menurunkan bunga jika pertumbuhan bertahan." if up else
                  "kalau ini berlanjut, ekonomi lebih pelan dan bunga bisa lebih rendah. Keduanya perlu kuartal berikutnya."),
            _say(f, "minat beli di pasar dunia naik, tapi dolar tidak otomatis melemah." if up else
                  "minat beli di pasar dunia turun pada hari rilis."),
            _say(f, "perdagangan dunia lebih tertolong oleh permintaan AS yang kuat." if up else
                  "perdagangan dunia kehilangan permintaan AS. Bunga The Fed yang lebih rendah, kalau ada, baru terasa belakangan."),
        )
    if origin == "US" and family == "activity":
        return _pack(
            _say(f, f"{phrase}. Ini satu bulan, bukan ukuran ekonomi setahun. Rupiah bisa {'tertekan' if up else 'sedikit lega'} hari ini."),
            _say(f, "The Fed dan BI baru mengubah bunga jika survei yang searah ini bertahan beberapa bulan."),
            _say(f, "saham bisnis harian AS bisa bergerak hari ini, lalu mudah balik bulan depan."),
            _say(f, "The Fed tidak mengubah bunga karena satu bulan. Yang dilihat adalah harga dan lapangan kerja."),
            _say(f, "pasar dunia bergerak tipis. Ini bukan keputusan bunga."),
            _say(f, "perdagangan dunia tidak berubah sebelum angka ini terlihat di pertumbuhan dan harga."),
        )
    if origin == "US" and family == "trade":
        return _pack(
            _say(f, f"{phrase}. Efeknya ke rupiah kecil."),
            _say(f, "neraca dagang AS tidak mengubah ruang BI."),
            _say(f, "The Fed tidak mengubah bunga karena neraca dagang. Saham dan surat utang AS tidak punya alasan besar untuk berubah."),
            _say(f, "angka ini mencerminkan perdagangan yang sudah terjadi, bukan keputusan bunga yang baru."),
            _say(f, "arus dagang tidak berbalik pada hari rilis."),
            _say(f, "angka ini tidak memberitahu apakah yang berubah harga barang atau jumlah barang."),
        )
    if origin == "US" and family == "reserves":
        return _pack(
            _say(f, "cadangan devisa AS tidak menggerakkan rupiah hari ini."),
            _say(f, "angka ini tidak mengubah ruang BI dan bukan permintaan untuk ekspor Indonesia."),
            _say(f, "bunga AS tidak berubah karena cadangan devisa."),
            _say(f, "The Fed tidak memakai angka cadangan untuk menentukan bunga."),
            _say(f, "dolar tidak berbalik karena perubahan cadangan AS."),
            _say(f, "uang beredar di dunia tidak berubah hanya karena satu catatan cadangan."),
        )
    if origin == "ID":
        return _id_lines(f, phrase, up)
    if origin == "CN":
        return _cn_lines(f, phrase, up)
    if origin == "JP" and family == "rate":
        return _pack(
            _say(f, f"{phrase}. Yen bisa {'menguat' if up else 'melemah'}, dan saham Indonesia bisa {'turun' if up else 'dapat dukungan'} hari ini."),
            _say(f, "BI tidak mengubah bunga karena Jepang." if up else
                  "dukungan itu hilang jika yen terlalu lemah dan Jepang terpaksa menaikkan bunga lagi."),
            _say(f, "The Fed tidak ikut mengubah bunga. Surat utang AS paling kena lewat orang yang menutup pinjaman yen."),
            _say(f, "efek ke AS kecil. Yang panjang adalah apakah pinjaman yen murah terus menyempit." if up else
                  "jalur bunga AS tidak mengikuti Jepang."),
            _say(f, f"yen {'menguat' if up else 'melemah'} pada hari keputusan."),
            _say(f, "efeknya baru besar jika Jepang mengulang keputusan ini, bukan pada satu rapat."),
        )
    if origin == "EU" and family == "rate":
        return _pack(
            _say(f, f"{phrase}. Rupiah hanya kena sedikit, jauh lebih kecil daripada keputusan The Fed."),
            _say(f, "BI tetap lebih mengikuti The Fed daripada bank sentral Eropa."),
            _say(f, "The Fed tidak mengubah bunga hanya karena Eropa."),
            _say(f, "yang terasa di AS cuma bunga surat utang yang bergerak bareng, dan itu kecil."),
            _say(f, f"euro {'menguat' if up else 'melemah'} pada hari keputusan."),
            _say(f, "ini belum berarti bunga di seluruh negara maju bergerak bareng, selama The Fed belum ikut."),
        )
    return _other_lines(f, phrase, up)


def _id_lines(f, phrase, up):
    family = f["family"]
    if family == "rate":
        return _pack(
            _say(f, f"{phrase}. Rupiah {'dapat dukungan' if up else 'bisa melemah'} hari ini, dan pinjaman jadi {'lebih mahal' if up else 'lebih murah'}."),
            _say(f, "kredit lebih berat selama bunga tetap tinggi." if up else
                  "BI tidak bisa menurunkan bunga jauh di bawah The Fed tanpa menekan rupiah."),
            _say(f, "pasar AS tidak mengubah bunga karena keputusan BI."),
            _say(f, "The Fed tidak memakai angka Indonesia."),
            _say(f, "mata uang lain tidak otomatis ikut."),
            _say(f, "efeknya ada di Indonesia, bukan di dolar dunia."),
        )
    if family == "prices":
        return _pack(
            _say(f, f"{phrase}. Harga surat utang Indonesia bisa {'turun' if up else 'naik'} hari ini."),
            _say(f, "daya beli menyempit selama harga tetap naik cepat." if up else
                  "ruang BI menurunkan bunga melebar jika ini bukan cuma sekali."),
            _say(f, "harga Indonesia tidak mengubah bunga The Fed."),
            _say(f, "pasar AS tidak merevisi bunga karena inflasi Indonesia."),
            _say(f, "harga dunia tidak berpindah karena angka ini."),
            _say(f, "ini cerita dalam negeri, bukan harga barang dunia."),
        )
    if family == "labor":
        return _pack(
            _say(f, f"{phrase}. Belanja di dalam negeri {'lebih tertolong' if up else 'bisa melemah'}."),
            _say(f, "BI kurang alasan menurunkan bunga jika lowongan tetap banyak." if up else
                  "BI baru menurunkan bunga jika ini berulang, dan tetap menjaga rupiah."),
            _say(f, "lapangan kerja Indonesia tidak mengubah laporan kerja AS."),
            _say(f, "The Fed tidak memakai angka ini."),
            _say(f, "dolar tidak berbalik karena pengangguran Indonesia."),
            _say(f, "yang berubah adalah belanja di dalam negeri, bukan perdagangan dunia."),
        )
    if family == "growth":
        return _pack(
            _say(f, f"{phrase}. Saham Indonesia dan rupiah {'dapat dukungan' if up else 'lebih rentan'} hari ini."),
            _say(f, "BI kurang terdesak menurunkan bunga jika ekonomi sudah kuat." if up else
                  "dorongan agar BI menurunkan bunga naik, tapi penurunan yang terlalu besar bisa menekan rupiah lagi."),
            _say(f, "pertumbuhan Indonesia tidak mengubah saham atau surat utang AS hari ini."),
            _say(f, "The Fed tidak merevisi bunga karena GDP Indonesia."),
            _say(f, "pasar dunia hampir tidak bergerak."),
            _say(f, "kontribusinya ke perdagangan dunia baru terasa jika ini bertahan beberapa kuartal."),
        )
    if family == "activity":
        return _pack(
            _say(f, f"{phrase}. Ini satu bulan, bukan ukuran ekonomi setahun. BI tidak mengubah bunga hari ini."),
            _say(f, "BI baru mengubah bunga jika survei yang searah ini berulang."),
            _say(f, "survei Indonesia tidak masuk harga bunga AS."),
            _say(f, "The Fed tidak memakai survei ini."),
            _say(f, "satu survei Indonesia tidak menggerakkan harga dunia."),
            _say(f, "baru berarti di luar negeri jika kemudian terlihat di pertumbuhan dan impor."),
        )
    if family == "trade":
        return _pack(
            _say(f, f"{phrase}. Rupiah {'dapat dukungan' if up else 'lebih rentan'} hari ini."),
            _say(f, "BI tidak otomatis mengubah bunga. Angka ini tidak membedakan harga dan jumlah barang."),
            _say(f, "neraca dagang Indonesia tidak mengubah The Fed."),
            _say(f, "pasar AS tidak merevisi bunga karena angka ini."),
            _say(f, "harga batu bara, nikel, dan sawit tidak ditentukan oleh satu neraca Indonesia."),
            _say(f, "kalau yang berubah cuma harga barang ekspor, bukan jumlah barang, itu tidak tertulis di angka ini."),
        )
    if family == "reserves":
        return _pack(
            _say(f, f"{phrase}. {'BI lebih mampu menjaga rupiah.' if up else 'BI sedang menahan rupiah, atau ada uang yang keluar.'}"),
            _say(f, "ini simpanan dolar, bukan tanda ekonomi tumbuh." if up else
                  "kalau cadangan terus turun, BI lebih sulit menjaga rupiah tanpa menaikkan bunga."),
            _say(f, "cadangan Indonesia tidak mengubah The Fed."),
            _say(f, "pasar AS tidak merevisi bunga karena angka ini."),
            _say(f, "pasar dunia tidak diubah. Yang berubah adalah kemampuan BI menjaga rupiah."),
            _say(f, "dolar dunia tidak berbalik karena cadangan Indonesia."),
        )
    if family == "guidance":
        return _pack(
            _say(f, f"{phrase}. Rupiah {'dapat dukungan' if up else 'bisa melemah'} hari ini."),
            _say(f, "bunga baru benar-benar berubah jika keputusan berikutnya mengikuti kata-kata ini."),
            _say(f, "kata-kata BI tidak mengubah bunga The Fed."),
            _say(f, "pasar AS tidak merevisi bunga karena pernyataan BI."),
            _say(f, "dolar dunia tidak berbalik karena nada BI."),
            _say(f, "satu pernyataan dalam negeri tidak mengubah pinjaman dunia."),
        )
    return _pack(
        _say(f, f"{phrase}. Efek utamanya di Indonesia, lewat BI dan rupiah."),
        _say(f, "BI baru mengubah bunga jika pola ini bertahan, bukan karena satu rilis."),
        _say(f, "pasar AS tidak mengubah bunga karena data Indonesia."),
        _say(f, "The Fed tidak memakai angka ini."),
        _say(f, "dolar dunia tidak berbalik karena rilis Indonesia."),
        _say(f, "perdagangan dunia tidak ditentukan oleh satu data Indonesia."),
    )


def _cn_lines(f, phrase, up):
    family = f["family"]
    if family in ("growth", "activity"):
        return _pack(
            _say(f, f"{phrase}. Harga batu bara, nikel, dan sawit biasanya {'naik' if up else 'turun'}."),
            _say(f, "BI tidak mengubah bunga. Ekspor Indonesia baru benar-benar berubah jika permintaan China bertahan."),
            _say(f, "saham AS yang jual ke Asia bisa bergerak tipis. Bunga The Fed tidak berubah."),
            _say(f, "The Fed tidak mengubah bunga karena China."),
            _say(f, f"harga logam dan energi {'dapat dukungan' if up else 'kehilangan dukungan'} hari ini."),
            _say(f, "pabrik dunia baru benar-benar terasa efeknya jika data China yang searah ini berulang."),
        )
    if family == "rate":
        return _pack(
            _say(f, f"{phrase}. Harapan permintaan barang ekspor Indonesia {'meredup' if up else 'naik'}, belum berarti barangnya sudah terbeli."),
            _say(f, "ekspor Indonesia baru ikut jika pabrik dan impor China benar-benar bergerak setelah ini."),
            _say(f, "The Fed tidak ikut mengubah bunga."),
            _say(f, "bunga AS tidak mengikuti bank sentral China."),
            _say(f, f"logam dan energi {'kehilangan' if up else 'mendapat'} dukungan hari ini."),
            _say(f, "satu keputusan belum mengubah perdagangan barang dunia."),
        )
    if family == "prices":
        return _pack(
            _say(f, f"{phrase}. Bank sentral China {'lebih hati-hati memberi bantuan' if up else 'punya ruang memberi bantuan'}."),
            _say(f, "ekspor Indonesia baru tertolong jika pabrik di China benar-benar lebih ramai."),
            _say(f, "harga China bukan alasan The Fed mengubah bunga."),
            _say(f, "pasar AS tidak merevisi bunga karena angka ini."),
            _say(f, "bantuan pemerintah China jadi kurang pasti hari ini." if up else
                  "harapan permintaan barang dunia, terutama logam dan energi, naik tipis."),
            _say(f, "efek ke harga barang negara lain baru terasa belakangan, kalau pola ini bertahan."),
        )
    if family == "trade":
        return _pack(
            _say(f, f"{phrase}. {'China mungkin membeli lebih sedikit dari luar, dan itu kurang baik untuk barang ekspor Indonesia.' if up else 'China mungkin membeli lebih banyak, dan itu baik untuk barang ekspor Indonesia. Bisa juga ekspor China yang melemah.'}"),
            _say(f, "angka ini tidak memisahkan keduanya. Efeknya baru jelas setelah beberapa bulan."),
            _say(f, "The Fed tidak bergerak karena neraca China."),
            _say(f, "bunga AS tidak berubah. Efek ke harga barang impor AS, kalau ada, lambat."),
            _say(f, "pasar barang dunia belum dapat arah yang bersih hari ini."),
            _say(f, "pangsa dagang baru bergeser jika pola ini bertahan, bukan karena satu bulan."),
        )
    if family == "labor":
        return _pack(
            _say(f, f"{phrase}. Efek ke barang ekspor Indonesia lebih kecil daripada data pabrik atau pertumbuhan."),
            _say(f, "yang panjang adalah daya beli orang China untuk barang impor."),
            _say(f, "lapangan kerja China tidak mengubah laporan kerja AS."),
            _say(f, "The Fed tidak memakai angka ini."),
            _say(f, "pasar dunia bergerak kecil pada satu rilis."),
            _say(f, "perdagangan dunia tidak berbalik karena pengangguran China semata."),
        )
    if family == "reserves":
        return _pack(
            _say(f, f"{phrase}. Ini bukan tanda China membeli lebih banyak barang ekspor."),
            _say(f, "cadangan China tidak otomatis menambah ekspor Indonesia."),
            _say(f, "The Fed tidak bergerak karena cadangan China."),
            _say(f, "bunga AS tidak berubah."),
            _say(f, "uang beredar dunia tidak berbalik karena satu catatan simpanan."),
            _say(f, "simpanan dolar bukan pengganti permintaan impor China."),
        )
    if family == "guidance":
        return _pack(
            _say(f, f"{phrase}. Harga barang ekspor Indonesia bisa {'turun' if up else 'naik'} karena harapan, belum karena barang yang sudah terbeli."),
            _say(f, "ekspor Indonesia baru berubah jika pinjaman dan impor China mengikuti kata-kata ini."),
            _say(f, "The Fed tidak mengubah bunga karena pernyataan China."),
            _say(f, "bunga AS tidak mengikuti nada China."),
            _say(f, f"logam {'kehilangan' if up else 'mendapat'} dukungan harapan hari ini."),
            _say(f, "satu pernyataan belum menjadi perubahan kebijakan yang menetap."),
        )
    return _pack(
        _say(f, f"{phrase}. Efek ke Indonesia, kalau ada, lewat permintaan China, bukan lewat BI."),
        _say(f, "efeknya baru berarti jika pola ini bertahan beberapa rilis."),
        _say(f, "The Fed tidak mengubah bunga karena data China."),
        _say(f, "bunga AS tidak mengikuti China."),
        _say(f, "harga dunia tidak otomatis berbalik pada satu rilis."),
        _say(f, "perdagangan barang baru berubah jika data China yang searah berulang."),
    )


def _other_lines(f, phrase, up):
    family, place, actor = f["family"], f["place"], f["actor"]
    if family in ("prices", "labor", "rate", "guidance"):
        return _pack(
            _say(f, f"{phrase}. Rupiah bisa {'tertekan' if up else 'sedikit lega'} hari ini, tapi efeknya lebih kecil daripada data AS."),
            _say(f, f"BI tidak mengubah bunga hanya karena {actor}."),
            _say(f, f"The Fed tidak mengubah bunga karena {actor}."),
            _say(f, "bunga AS tetap ditentukan data AS."),
            _say(f, f"mata uang {place} bergerak pada hari rilis."),
            _say(f, "efeknya baru bertahan jika pola ini berulang, bukan pada satu rilis."),
        )
    if family in ("growth", "activity"):
        return _pack(
            _say(f, f"{phrase}. Efek ke rupiah kecil pada hari ini."),
            _say(f, f"BI tidak mengubah bunga karena permintaan di {place}."),
            _say(f, f"pertumbuhan {place} tidak mengubah bunga The Fed hari ini."),
            _say(f, "bunga AS tetap ditentukan data AS."),
            _say(f, "pasar dunia bergerak tipis."),
            _say(f, f"perdagangan dunia tidak berbalik sebelum data {place} yang searah berulang."),
        )
    if family == "trade":
        return _pack(
            _say(f, f"{phrase}. Neraca {place} bukan uang yang masuk ke Indonesia, jadi rupiah hampir tidak bergerak."),
            _say(f, "BI tidak dapat informasi baru tentang ekspor Indonesia dari neraca negara lain."),
            _say(f, f"The Fed tidak mengubah bunga karena neraca {place}."),
            _say(f, "bunga AS tidak mengikuti neraca negara lain."),
            _say(f, "arus dagang tidak berbalik pada satu rilis."),
            _say(f, "pangsa dagang baru bergeser jika pola ini bertahan beberapa bulan."),
        )
    return _pack(
        _say(f, f"{phrase}. Tidak ada alasan rupiah bergerak besar hari ini."),
        _say(f, f"BI tidak mengubah bunga karena angka {place} ini."),
        _say(f, "pasar AS tidak diubah oleh rilis ini."),
        _say(f, "The Fed tidak mengubah bunga."),
        _say(f, "pasar dunia tidak mendapat arah baru."),
        _say(f, "satu rilis tidak mengubah pinjaman dunia."),
    )


def market_views(ev):
    f = _facts(ev)
    lines = _market_lines(f)
    return {k: _clip(lines.get(k), 420) for k in MARKET_KEYS}

def build_macro_read(ev):
    read = market_views(ev)
    read["ringkasan"] = rule_analysis(ev)
    return read


def _parse_llm_json(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    ring = data.get("ringkasan")
    if not isinstance(ring, str) or not ring.strip():
        return None
    out = {"ringkasan": " ".join(ring.split())}
    for key in MARKET_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = " ".join(val.split())
    return out


def _merge_read(rule, llm):
    if not isinstance(llm, dict):
        return rule
    out = dict(rule)
    for key in ("ringkasan",) + MARKET_KEYS:
        val = llm.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = " ".join(val.split())
    return out


def llm_analysis(ev, level, read, next_txt):
    """Minta Claude menulis sinyal makro. None kalau tidak ada key atau balasan gagal."""
    if not ANTHROPIC_KEY:
        return None
    payload = {k: ev.get(k) for k in ("title", "country", "period", "actual", "forecast",
                                       "previous", "unit", "scale", "source")}
    peta = {k: read.get(k) for k in ("ringkasan",) + MARKET_KEYS}
    prompt = (
        "Kamu menjelaskan rilis ekonomi ke orang yang bukan ekonom, dalam bahasa Indonesia yang pendek dan biasa.\n"
        "Jangan pakai kata stance, siklus, kanal, momentum, disinflasi, durasi, atau rezim. "
        "Kalau perlu menyebut bunga, dolar, rupiah, saham, atau harga barang, pakai kata itu saja.\n"
        "Gunakan HANYA angka yang diberikan. Jangan mengarang data lain. Jangan menyuruh beli atau jual.\n"
        "Balas HANYA JSON valid, tanpa markdown, dengan kunci:\n"
        "ringkasan: 2 atau 3 kalimat pendek. Sebut nama indikator, angkanya, dan artinya dalam hidup sehari-hari "
        "(lebih banyak orang dapat kerja, harga naik lebih cepat, bunga lebih tinggi, dan sejenisnya). "
        "Sebut juga apa yang satu laporan ini belum putuskan.\n"
        "id_pendek, id_panjang, us_pendek, us_panjang, global_pendek, global_panjang: masing-masing SATU kalimat pendek, "
        "dan keenamnya harus berbeda. Pasar Indonesia, pasar Amerika, dan global. "
        "Jangka pendek artinya hari rilis. Jangka panjang artinya beberapa bulan ke depan. "
        "Tiap kalimat menyebut nama indikator dan apakah angkanya lebih kuat atau lebih lemah dari perkiraan. "
        "Kalau efeknya kecil, katakan kecil dan kenapa.\n\n"
        f"Data: {json.dumps(payload, ensure_ascii=False)}\n"
        f"Kategori dampak ke Indonesia: {level}\n"
        f"Rilis berikutnya: {next_txt}\n"
        "Peta berikut hanya supaya efek yang penting tidak terlewat. Jangan disalin kata per kata.\n"
        f"Peta: {json.dumps(peta, ensure_ascii=False)}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 900,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        parsed = _parse_llm_json(text)
        if not parsed:
            log.warning("Claude tidak mengembalikan JSON lengkap, pakai analisis aturan")
        return parsed
    except Exception as e:
        log.warning("Claude API gagal, pakai analisis aturan: %s", e)
        return None


def _market_fields(read):
    rows = (
        ("Pasar Indonesia", "id_pendek", "id_panjang"),
        ("Pasar Amerika", "us_pendek", "us_panjang"),
        ("Global", "global_pendek", "global_panjang"),
    )
    return [{
        "name": name,
        "value": f"Jangka pendek: {read[a]}\nJangka panjang: {read[b]}",
        "inline": False,
    } for name, a, b in rows]


def _embed_weight(embed):
    total = len(embed.get("title") or "") + len(embed.get("description") or "")
    total += len((embed.get("footer") or {}).get("text") or "")
    for field in embed.get("fields") or []:
        total += len(field.get("name") or "") + len(str(field.get("value") or ""))
    return total


def _fit_embed(embed, limit=5600):
    while _embed_weight(embed) > limit and len(embed.get("description") or "") > 240:
        embed["description"] = _clip(embed["description"], max(240, len(embed["description"]) - 180))
    return embed


# ---------------------------------------------------------------- utilitas
def parse_dt(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_wib(d):
    w = d.astimezone(WIB)
    return f"{HARI[w.weekday()]}, {w.day:02d} {BULAN[w.month - 1]} {w.year} {w:%H:%M} WIB"


def _short_wib(d):
    """Tanggal singkat supaya kolom embed Discord tetap sejajar."""
    w = d.astimezone(WIB)
    return f"{w.day:02d} {BULAN[w.month - 1]} · {w:%H:%M}"


def fmt_val(ev, key):
    v = ev.get(key)
    if v is None:
        return "–"
    s = f"{v:,.2f}".rstrip("0").rstrip(".") + (ev.get("scale") or "")
    unit = ev.get("unit") or ""
    return s + "%" if unit == "%" else (f"{s} {unit}".strip())


def series_key(ev):
    return ev.get("ticker") or f"{ev.get('country')}|{ev.get('indicator') or ev.get('title')}"


_FUTURE = None


def future_events(now):
    """Kalender 100 hari ke depan, diambil SEKALI per run dan hanya kalau ada rilis yang dikirim."""
    global _FUTURE
    if _FUTURE is None:
        try:
            _FUTURE = fetch_events(now, back_days=0, fwd_days=100)
        except Exception as e:
            log.warning("Gagal ambil kalender jangka panjang: %s", e)
            _FUTURE = []
    return _FUTURE


def next_release(ev, events, now):
    k = series_key(ev)
    events = events + future_events(now)
    future = sorted((parse_dt(e["date"]) for e in events
                     if series_key(e) == k and e["id"] != ev["id"] and parse_dt(e["date"]) > now))
    return fmt_wib(future[0]) if future else "belum ada di kalender TradingView (cek berkala)"


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st):
    DATA.mkdir(exist_ok=True)
    for k in ("posted", "reminded"):  # batasi ukuran
        st[k] = st.get(k, [])[-3000:]
    STATE_FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def discord(embeds, content=None, png=None):
    for i in range(0, len(embeds), 10):  # max 10 embed per pesan
        body = {"embeds": embeds[i:i + 10], "username": "Macro Calendar Bot"}
        if content and i == 0:
            body["content"] = content
        if DRY_RUN or not WEBHOOK:
            print(json.dumps(body, indent=2, ensure_ascii=False))
            if png:
                Path("/tmp/chart_preview.png").write_bytes(png)
            continue
        for _ in range(3):
            if png and i == 0:  # kirim gambar grafik sebagai lampiran
                r = requests.post(WEBHOOK, timeout=60,
                                  data={"payload_json": json.dumps(body)},
                                  files={"files[0]": ("chart.png", png, "image/png")})
            else:
                r = requests.post(WEBHOOK, json=body, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            if r.status_code >= 400:
                log.warning("Discord menolak pesan: HTTP %s", r.status_code)
            r.raise_for_status()
            log.info("Discord menerima pesan (HTTP %s)", r.status_code)
            break
        time.sleep(1)


# ---------------------------------------------------------------- ambil data
def fetch_events(now, back_days=2, fwd_days=2):
    """Default rentang pendek (±2 hari) supaya tiap run ringan."""
    params = {
        "from": (now - dt.timedelta(days=back_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "to": (now + dt.timedelta(days=fwd_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "countries": COUNTRIES,
    }
    r = requests.get(TV_URL, params=params, headers=TV_HEADERS, timeout=30)
    r.raise_for_status()
    j = r.json()
    events = j.get("result", j) if isinstance(j, dict) else j
    for e in events:
        e["id"] = str(e.get("id"))
    return events


# ---------------------------------------------------------------- pesan
_HISTORY = None


def history_events(now):
    """Riwayat 400 hari semua negara, diambil sekali per run (hanya kalau ada rilis)."""
    global _HISTORY
    if _HISTORY is None:
        try:
            _HISTORY = fetch_events(now, back_days=400, fwd_days=0)
        except Exception as e:
            log.warning("Riwayat untuk grafik gagal diambil: %s", e)
            _HISTORY = []
    return _HISTORY


def _smooth_curve(xs, ys, n=200):
    """Kurva mulus yang tetap lewat tiap actual. PCHIP, jadi tidak menyimpang di puncak/dasar."""
    import numpy as np
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    keep = np.concatenate([[True], np.diff(xs) > 0])
    xs, ys = xs[keep], ys[keep]
    if len(xs) < 3:
        return xs, ys
    h = np.diff(xs)
    delta = np.diff(ys) / h
    m = np.zeros(len(ys))
    m[0] = delta[0]
    m[-1] = delta[-1]
    for i in range(1, len(ys) - 1):
        if delta[i - 1] == 0 or delta[i] == 0 or delta[i - 1] * delta[i] < 0:
            m[i] = 0.0
        else:
            w1 = 2 * h[i] + h[i - 1]
            w2 = h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])
    for i in range(len(delta)):
        if delta[i] == 0:
            m[i] = 0.0
            m[i + 1] = 0.0
            continue
        a = m[i] / delta[i]
        b = m[i + 1] / delta[i]
        hyp = a * a + b * b
        if hyp > 9:
            scale = 3 / np.sqrt(hyp)
            m[i] = scale * a * delta[i]
            m[i + 1] = scale * b * delta[i]
    grid = np.linspace(xs[0], xs[-1], n)
    seg = np.clip(np.searchsorted(xs, grid, side="right") - 1, 0, len(ys) - 2)
    out = np.empty_like(grid)
    for i in range(len(ys) - 1):
        mask = seg == i
        if not np.any(mask):
            continue
        t = (grid[mask] - xs[i]) / h[i]
        t2, t3 = t * t, t * t * t
        out[mask] = ((2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h[i] * m[i]
                     + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h[i] * m[i + 1])
    return grid, out


def _extreme_index(vals, when, want_max):
    best = 0
    for i in range(1, len(vals)):
        higher = vals[i] > vals[best] if want_max else vals[i] < vals[best]
        later_tie = vals[i] == vals[best] and when[i] > when[best]
        if higher or later_tie:
            best = i
    return best


def _overlap(a, b, pad=3):
    return not (a.x1 + pad < b.x0 or b.x1 + pad < a.x0 or a.y1 + pad < b.y0 or b.y1 + pad < a.y0)


def _place_note(ax, fig, xy, text, offsets, occupied, **style):
    """Coba beberapa posisi sampai label tidak menimpa label lain. Kalau sempit, tetap tempel."""
    renderer = fig.canvas.get_renderer()
    fallback = None
    for dx, dy in offsets:
        note = ax.annotate(text, xy, xytext=(dx, dy), textcoords="offset points", **style)
        fig.canvas.draw()
        box = note.get_window_extent(renderer).expanded(1.04, 1.12)
        if fallback is None:
            fallback = (note, box)
        if any(_overlap(box, old) for old in occupied):
            if fallback[0] is not note:
                note.remove()
            continue
        if fallback[0] is not note:
            fallback[0].remove()
        occupied.append(box)
        return note
    if fallback:
        occupied.append(fallback[1])
        return fallback[0]
    return None


def chart_points(ev, now, months=12):
    """Satu angka per bulan. Kalender TradingView dipotong 2000 event, jadi diambil per potongan."""
    k = series_key(ev)
    by_month = {}
    end = now
    for _ in range(14):
        batch = fetch_events(end, back_days=45, fwd_days=0)
        if not batch:
            break
        for e in batch:
            if series_key(e) != k or e.get("actual") is None:
                continue
            when = parse_dt(e["date"]).astimezone(WIB)
            key = (when.year, when.month)
            prev = by_month.get(key)
            if prev is None or e["date"] > prev["date"]:
                by_month[key] = e
        oldest = min(parse_dt(e["date"]) for e in batch)
        nxt = oldest - dt.timedelta(hours=1)
        if nxt >= end or (now - nxt).days > 420 or len(by_month) >= months:
            break
        end = nxt
    when = parse_dt(ev["date"]).astimezone(WIB)
    by_month[(when.year, when.month)] = ev
    return sorted(by_month.values(), key=lambda e: e["date"])[-months:]


def make_chart(ev, now):
    """Garis actual yang halus, satu titik berlabel per bulan, plus puncak dan dasar."""
    try:
        import io as _io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        log.warning("matplotlib belum terpasang, grafik dilewati")
        return None

    pts = chart_points(ev, now, months=12)
    if len(pts) < 3:
        return None
    x = [parse_dt(e["date"]).astimezone(WIB) for e in pts]
    act = [float(e["actual"]) for e in pts]
    fc = [e.get("forecast") for e in pts]
    suffix = (ev.get("scale") or "") + ("%" if ev.get("unit") == "%" else "")

    fig, ax = plt.subplots(figsize=(11.4, 5.6), dpi=140)
    fig.patch.set_facecolor("white")
    xs = mdates.date2num(x)
    smooth_x, smooth_y = _smooth_curve(xs, act, n=480)
    smooth_when = mdates.num2date(smooth_x, tz=WIB)
    floor = min(act + [v for v in fc if v is not None])
    ax.fill_between(smooth_when, smooth_y, floor, color=BRAND_SOFT, alpha=0.55, zorder=1)
    ax.plot(smooth_when, smooth_y, color=BRAND_RED, lw=2.8, zorder=3, label="Actual",
            solid_capstyle="round", solid_joinstyle="round")
    ax.plot(x, act, linestyle="none", marker="o", ms=7, color=BRAND_RED,
            markeredgecolor="white", markeredgewidth=1.4, zorder=5)
    fx = [(a, b) for a, b in zip(x, fc) if b is not None]
    if len(fx) >= 2:
        fxs = mdates.date2num([a for a, _ in fx])
        fys = [b for _, b in fx]
        sg, sy = _smooth_curve(fxs, fys, n=360)
        ax.plot(mdates.num2date(sg, tz=WIB), sy, color="#4A4A4A", lw=1.5,
                ls=(0, (5, 3)), zorder=2, label="Forecast")
    span = max(act) - min(act) or 1.0
    ax.set_ylim(min(act) - span * 0.42, max(act) + span * 0.55)
    ax.set_xlim(xs[0] - 12, xs[-1] + 12)

    peak_i = _extreme_index(act, x, True)
    trough_i = _extreme_index(act, x, False)
    show_marks = act[peak_i] != act[trough_i]
    occupied = []
    fig.canvas.draw()
    value_style = dict(
        ha="center", fontsize=8, color="#7A1020", fontweight="bold", zorder=7, clip_on=False,
        bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.92},
    )
    for i, (when, val) in enumerate(zip(x, act)):
        above = True
        if 0 < i < len(act) - 1:
            above = val >= act[i - 1]
        offsets = []
        for dy in ((14, 28, 42, -16, -30, -44) if above else (-16, -30, -44, 14, 28, 42)):
            for dx in (0, 16, -16, 32, -32):
                offsets.append((dx, dy))
        _place_note(ax, fig, (when, val), f"{val:.2f}{suffix}", offsets, occupied, **value_style)
    if show_marks:
        for idx, kind, prefer in ((peak_i, "Puncak", 1), (trough_i, "Dasar", -1)):
            when = x[idx]
            label = f"{kind} {BULAN[when.month - 1]} {when.year}"
            dx0 = 36 if idx < len(act) - 2 else -46
            offsets = [(dx0, 18 * prefer), (-dx0, 18 * prefer), (dx0, 34 * prefer),
                       (0, 36 * prefer), (dx0, -22 * prefer)]
            _place_note(
                ax, fig, (when, act[idx]), label, offsets, occupied,
                ha="left" if dx0 > 0 else "right", fontsize=8.5, color=BRAND_DARK,
                fontweight="bold", zorder=8, clip_on=False,
                bbox={"boxstyle": "round,pad=0.25", "fc": "#FFF8F8" if kind == "Puncak" else "#F4F7FB",
                      "ec": "#E4C6CB" if kind == "Puncak" else "#D5DEE8", "lw": 0.6},
            )

    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#BDBDBD")
    ax.grid(axis="y", color=BRAND_GRID, lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{BULAN[d.month - 1]}\n{d.year}" for d in x], fontsize=8, color="#555555")
    ax.tick_params(axis="x", length=0, pad=4)
    ax.tick_params(axis="y", colors="#666666", labelsize=9, length=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.2f}{suffix}"))
    fig.text(0.06, 0.955, ev["title"], fontsize=15, fontweight="bold", color=BRAND_DARK)
    fig.text(0.06, 0.915, f"{ev.get('country')} · {len(pts)} bulan terakhir · sumber: TradingView",
             fontsize=9, color="#777777")
    ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=BRAND_DARK)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.84, bottom=0.16)
    buf = _io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def release_embed(ev, events, now):
    level, _ = classify(ev)
    emoji, color = LEVEL_STYLE[level]
    nxt = next_release(ev, events, now)
    rule = build_macro_read(ev)
    read = _merge_read(rule, llm_analysis(ev, level, rule, nxt))
    d, diff, base = surprise(ev)
    surp = "–" if diff is None else ("sesuai" if d == 0 else f"{round(diff, 4):+g} vs {base}")
    embed = {
        "title": f"{emoji} {FLAGS.get(ev['country'], ev['country'])} {ev['title']}"
                 + (f" ({ev['period']})" if ev.get("period") else ""),
        "color": color,
        "description": _clip(read["ringkasan"], 1500),
        "fields": [
            {"name": "Actual", "value": f"**{fmt_val(ev, 'actual')}**", "inline": True},
            {"name": "Forecast", "value": fmt_val(ev, "forecast"), "inline": True},
            {"name": "Previous", "value": fmt_val(ev, "previous"), "inline": True},
            {"name": "Selisih", "value": surp, "inline": True},
            {"name": "Dampak", "value": level, "inline": True},
            {"name": "Rilis", "value": _short_wib(parse_dt(ev["date"])), "inline": True},
            {"name": "Next update", "value": nxt, "inline": False},
            *_market_fields(read),
        ],
        "footer": {"text": " · ".join(x for x in ["Sumber: TradingView", ev.get("source"),
                                                     "bukan rekomendasi investasi"] if x)},
    }
    return _fit_embed(embed)


def preview_embed(evs, title, color):
    lines = []
    for ev in evs:
        level, _ = classify(ev)
        t = parse_dt(ev["date"]).astimezone(WIB)
        lines.append(f"`{t:%H:%M}` {LEVEL_STYLE[level][0]} {FLAGS.get(ev['country'], ev['country'])} "
                     f"**{ev['title']}** — F: {fmt_val(ev, 'forecast')} | P: {fmt_val(ev, 'previous')}")
    return {"title": title, "color": color,
            "description": "\n".join(lines)[:4000] or "Tidak ada rilis penting.",
            "footer": {"text": "🔴 Tinggi · 🟠 Sedang · F = Forecast/Konsensus · P = Previous"}}


# ---------------------------------------------------------------- Google Sheets
def sheet_name(ev):
    name = f"{ev.get('country')} - {ev.get('title')}"
    for ch in '[]:*?/\\':
        name = name.replace(ch, " ")
    return name[:95]


MONTHS_EN = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def data_month(ev):
    """Bulan periode data (bukan tanggal rilis), format YYYY-MM-01."""
    ref = ev.get("referenceDate")
    if ref:
        try:
            d = parse_dt(ref)
            return f"{d.year:04d}-{d.month:02d}-01"
        except ValueError:
            pass
    rel = parse_dt(ev["date"]).astimezone(WIB)
    per = (ev.get("period") or "").strip().lower()
    month = MONTHS_EN.get(per[:3])
    if per.startswith("q") and per[1:2].isdigit():
        month = int(per[1]) * 3                       # Q2 -> Jun
    if month:
        year = rel.year - 1 if month > rel.month else rel.year
        return f"{year:04d}-{month:02d}-01"
    return f"{rel.year:04d}-{rel.month:02d}-01"     # fallback: bulan rilis


def sheet_row(ev):
    _, diff, _ = surprise(ev)
    unit = (ev.get("scale") or "") + (ev.get("unit") or "")
    return {
        "sheet": sheet_name(ev), "id": ev["id"], "month": data_month(ev), "source": "TradingView",
        "date": parse_dt(ev["date"]).astimezone(WIB).isoformat(),
        "period": ev.get("period") or "", "actual": ev.get("actual"),
        "forecast": ev.get("forecast"), "previous": ev.get("previous"),
        "surprise": None if diff is None else round(diff, 4), "unit": unit,
        "impact": classify(ev)[0], "country": ev.get("country"), "title": ev.get("title"),
    }


CHART_SMOOTH = 3  # rata-rata bergerak 3 rilis: garis Sheets yang lebih halus


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _month_key(row):
    month = row.get("month") or ""
    return month[:7] if len(month) >= 7 else ""


def _dedupe_months(rows):
    """Satu actual per bulan. Kalau bentrok, tanggal yang lebih baru menang."""
    best = {}
    for row in rows:
        key = _month_key(row)
        if not key or not _is_num(row.get("actual")):
            continue
        prev = best.get(key)
        if prev is None or (row.get("date") or "") >= (prev.get("date") or ""):
            best[key] = row
    return [best[k] for k in sorted(best)]


def _fmt_marker_num(value):
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _extreme_label(kind, month, value, series):
    if not month or value is None:
        return ""
    unit = ""
    for row in series:
        if _month_key(row) == month:
            unit = row.get("unit") or ""
            break
    year, mon = int(month[:4]), int(month[5:7])
    return f"{kind} {BULAN[mon - 1]} {year} ({_fmt_marker_num(value)}{unit})"


def _chart_stats(series):
    months, vals = [], []
    for row in series:
        months.append(_month_key(row))
        vals.append(float(row["actual"]))
    smooth = {}
    for i, month in enumerate(months):
        if i + 1 >= CHART_SMOOTH:
            window = vals[i + 1 - CHART_SMOOTH:i + 1]
            smooth[month] = round(sum(window) / CHART_SMOOTH, 4)
    peak_month = trough_month = None
    peak_value = trough_value = None
    if len(vals) >= CHART_SMOOTH and max(vals) != min(vals):
        peak_i = trough_i = 0
        for i in range(1, len(vals)):
            if vals[i] > vals[peak_i] or (vals[i] == vals[peak_i] and months[i] > months[peak_i]):
                peak_i = i
            if vals[i] < vals[trough_i] or (vals[i] == vals[trough_i] and months[i] > months[trough_i]):
                trough_i = i
        peak_month, peak_value = months[peak_i], vals[peak_i]
        trough_month, trough_value = months[trough_i], vals[trough_i]
    return {
        "smooth": smooth,
        "peak_month": peak_month, "peak_value": peak_value,
        "trough_month": trough_month, "trough_value": trough_value,
        "peak_label": _extreme_label("Puncak", peak_month, peak_value, series),
        "trough_label": _extreme_label("Dasar", trough_month, trough_value, series),
    }


def _history_rows_for_sheet(sheet, now):
    out = []
    try:
        events = history_events(now)
    except Exception as e:
        log.warning("Riwayat grafik Sheets gagal: %s", e)
        return out
    for ev in events:
        if ev.get("actual") is None:
            continue
        try:
            if sheet_name(ev) != sheet:
                continue
            out.append(sheet_row(ev))
        except Exception:
            continue
    return out


def _chart_series(sheet, batch, now):
    months = {_month_key(row) for row in batch if _is_num(row.get("actual")) and _month_key(row)}
    # Backfill panjang sudah berisi seri lengkap. Satu rilis baru butuh riwayat.
    if len(months) >= CHART_SMOOTH or now is None:
        return _dedupe_months(batch)
    return _dedupe_months(_history_rows_for_sheet(sheet, now) + batch)


def _stamp_chart(row, stats):
    month = _month_key(row)
    row["actual_smooth"] = stats["smooth"].get(month)
    is_peak = bool(month and month == stats["peak_month"])
    is_trough = bool(month and month == stats["trough_month"])
    if is_peak and is_trough:
        is_peak = is_trough = False
    row["is_peak"] = is_peak
    row["is_trough"] = is_trough
    row["peak_month"] = stats["peak_month"] or ""
    row["peak_value"] = stats["peak_value"]
    row["trough_month"] = stats["trough_month"] or ""
    row["trough_value"] = stats["trough_value"]
    row["peak_label"] = stats["peak_label"]
    row["trough_label"] = stats["trough_label"]
    row["marker_label"] = stats["peak_label"] if is_peak else (stats["trough_label"] if is_trough else "")


def annotate_chart_rows(rows, now=None):
    """Kolom untuk grafik Google Sheets. Chart-nya sendiri digambar Apps Script di luar repo.

    actual_smooth = rata-rata bergerak 3 rilis (kosong sebelum ada 3 titik).
    is_peak / is_trough menandai bulan tertinggi / terendah pada seri yang terlihat
    (minimal 3 bulan, dan nilainya tidak datar). peak_label dan trough_label ikut
    di setiap baris supaya rilis baru bisa menandai bulan lama yang sudah ada di Sheet.
    """
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(row.get("sheet") or "", []).append(i)
    for sheet, idxs in groups.items():
        batch = [rows[i] for i in idxs]
        stats = _chart_stats(_chart_series(sheet, batch, now))
        for i in idxs:
            _stamp_chart(rows[i], stats)
    return rows


def push_sheets(rows, now=None):
    """Kirim baris ke Apps Script (per 150 baris). Return True kalau semua sukses."""
    if not rows:
        return True
    rows = [dict(row) for row in rows]
    try:
        annotate_chart_rows(rows, now)
    except Exception as e:
        log.warning("Anotasi grafik Sheets gagal, data tetap dikirim: %s", e)
    if DRY_RUN:
        print(f"[SHEETS] {len(rows)} baris, contoh:", json.dumps(rows[0], ensure_ascii=False))
        return True
    for i in range(0, len(rows), 150):
        try:
            r = requests.post(SHEETS_URL, json={"token": SHEETS_TOKEN, "rows": rows[i:i + 150]},
                              timeout=150)
            res = r.json()
            if not res.get("ok"):
                log.warning("Google Sheets menolak: %s", res.get("error"))
                return False
            log.info("Google Sheets: +%s baris", res.get("added"))
        except Exception as e:
            log.warning("Google Sheets gagal: %s", e)
            return False
    return True


def fetch_history(now, days):
    """Ambil riwayat panjang dalam potongan 60 hari (lebih aman untuk endpoint)."""
    out, seen = [], set()
    for start in range(days, 0, -60):
        end_ago = max(start - 60, 0)
        chunk_now = now - dt.timedelta(days=end_ago)
        try:
            for e in fetch_events(chunk_now, back_days=start - end_ago, fwd_days=0):
                if e["id"] not in seen:
                    seen.add(e["id"])
                    out.append(e)
        except Exception as e:
            log.warning("Riwayat %d-%d hari lalu gagal: %s", start, end_ago, e)
        time.sleep(0.4)
    return sorted(out, key=lambda e: e["date"])


# ---------------------------------------------------------------- sumber cadangan
# Dipakai HANYA untuk mengisi bulan yang tidak ada di TradingView (tanpa API key).
# (negara, kata wajib di judul, kata terlarang, sumber, kode seri, transformasi, judul sheet bila belum ada)
FALLBACKS = [
    ("US", ["fed interest rate decision"], [], "FRED", "DFEDTARU", "last", "Fed Interest Rate Decision"),
    ("US", ["inflation rate", "yoy"], ["core"], "FRED", "CPIAUCSL", "yoy", "Inflation Rate YoY"),
    ("US", ["inflation rate", "mom"], ["core"], "FRED", "CPIAUCSL", "mom", "Inflation Rate MoM"),
    ("US", ["core inflation rate", "yoy"], [], "FRED", "CPILFESL", "yoy", "Core Inflation Rate YoY"),
    ("US", ["core inflation rate", "mom"], [], "FRED", "CPILFESL", "mom", "Core Inflation Rate MoM"),
    ("US", ["unemployment rate"], [], "FRED", "UNRATE", "last", "Unemployment Rate"),
    ("US", ["non farm payrolls"], [], "FRED", "PAYEMS", "diff", "Non Farm Payrolls"),
    ("US", ["pce price index", "yoy"], ["core"], "FRED", "PCEPI", "yoy", "PCE Price Index YoY"),
    ("US", ["core pce price index", "yoy"], [], "FRED", "PCEPILFE", "yoy", "Core PCE Price Index YoY"),
    ("US", ["retail sales", "mom"], ["ex "], "FRED", "RSAFS", "mom", "Retail Sales MoM"),
    ("US", ["gdp growth rate qoq"], [], "FRED", "A191RL1Q225SBEA", "quarter", "GDP Growth Rate QoQ"),
    ("ID", ["interest rate decision"], [], "BIS", "ID", "last", "Interest Rate Decision"),
    ("JP", ["boj interest rate decision"], [], "BIS", "JP", "last", "BoJ Interest Rate Decision"),
]
FALLBACK_HEADERS = {"User-Agent": "Mozilla/5.0 (EconOutlookBot/1.0; personal research)"}


def _csv_rows(url, params=None):
    import csv
    r = requests.get(url, params=params, headers=FALLBACK_HEADERS, timeout=60)
    r.raise_for_status()
    return list(csv.reader(r.text.splitlines()))


def fetch_fred_monthly(sid):
    """FRED CSV publik -> {YYYY-MM: nilai terakhir bulan itu}."""
    rows = _csv_rows("https://fred.stlouisfed.org/graph/fredgraph.csv", {"id": sid})
    out = {}
    for row in rows[1:]:
        try:
            out[row[0][:7]] = float(row[1])   # data harian: nilai terakhir di bulan itu menang
        except (ValueError, IndexError):
            continue
    return out


def fetch_bis_monthly(cc, start):
    """Suku bunga kebijakan BIS (WS_CBPOL) -> {YYYY-MM: nilai}."""
    urls = [f"https://stats.bis.org/api/v1/data/WS_CBPOL/M.{cc}/all",
            f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/M.{cc}"]
    for url in urls:
        try:
            rows = _csv_rows(url, {"format": "csv", "startPeriod": start})
            head = rows[0]
            ti, vi = head.index("TIME_PERIOD"), head.index("OBS_VALUE")
            return {r[ti][:7]: float(r[vi]) for r in rows[1:] if len(r) > vi and r[vi]}
        except Exception as e:
            log.warning("BIS %s gagal di %s: %s", cc, url, e)
    return {}


def _transform(levels, how):
    keys = sorted(levels)
    out = {}
    for i, k in enumerate(keys):
        v = levels[k]
        if how in ("last", "quarter"):
            out[k] = round(v, 2)
        elif how == "diff" and i >= 1:
            out[k] = round(v - levels[keys[i - 1]], 0)
        elif how == "mom" and i >= 1:
            out[k] = round((v / levels[keys[i - 1]] - 1) * 100, 1)
        elif how == "yoy" and i >= 12:
            out[k] = round((v / levels[keys[i - 12]] - 1) * 100, 1)
    if how == "quarter":  # FRED kuartal ditulis bulan awal (Apr) -> samakan dgn TradingView (Jun)
        shifted = {}
        for k, v in out.items():
            y, m = int(k[:4]), int(k[5:7]) + 2
            y, m = (y + 1, m - 12) if m > 12 else (y, m)
            shifted[f"{y:04d}-{m:02d}"] = v
        out = shifted
    return out


def add_fallbacks(groups, now, min_rank):
    """Lengkapi bulan yang kosong di TradingView dari FRED / BIS."""
    start = (now - dt.timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m")
    end = now.astimezone(WIB).strftime("%Y-%m")
    cache = {}
    for country, need, avoid, src, sid, how, label in FALLBACKS:
        level = classify({"title": label, "country": country, "importance": 1})[0]
        if LEVEL_RANK[level] < min_rank:
            continue
        target = None
        for name, rows in groups.items():
            t = (rows[0].get("title") or "").lower()
            if (rows[0].get("country") == country and all(w in t for w in need)
                    and not any(w in t for w in avoid)):
                target = name
                break
        target = target or sheet_name({"country": country, "title": label})
        rows = groups.setdefault(target, [])
        have = {r["month"][:7] for r in rows}
        try:
            key = (src, sid)
            if key not in cache:
                cache[key] = fetch_fred_monthly(sid) if src == "FRED" else fetch_bis_monthly(sid, start)
            series = _transform(cache[key], how)
        except Exception as e:
            log.warning("Cadangan %s %s gagal: %s", src, sid, e)
            continue
        added = 0
        for month, val in series.items():
            if month < start or month > end or month in have:
                continue
            rows.append({
                "sheet": target, "id": f"{src}:{sid}:{month}", "month": f"{month}-01",
                "date": f"{month}-01T00:00:00+07:00", "period": "", "actual": val,
                "forecast": None, "previous": None, "surprise": None,
                "unit": "K" if how == "diff" else "%", "impact": level, "country": country,
                "title": rows[0]["title"] if rows and rows[0].get("title") else label,
                "source": f"{src} ({sid})",
            })
            added += 1
        if added:
            log.info("Cadangan %s %s: +%d bulan untuk %s", src, sid, added, target)


def backfill_sheets(st, now, min_rank, budget_s):
    """Isi riwayat dicicil per sheet sampai batas waktu, lanjut di run berikutnya."""
    t0 = time.time()
    hist = fetch_history(now, BACKFILL_DAYS)
    groups = {}
    for e in hist:
        if (e.get("actual") is not None and parse_dt(e["date"]) <= now
                and LEVEL_RANK[classify(e)[0]] >= min_rank):
            row = sheet_row(e)
            groups.setdefault(row["sheet"], []).append(row)
    add_fallbacks(groups, now, min_rank)
    done = set(st.get("backfill_done", []))
    todo = sorted(g for g in groups if g not in done and groups[g])
    log.info("Riwayat Google Sheets: %d sheet selesai, %d tersisa", len(done), len(todo))
    for name in todo:
        if time.time() - t0 > budget_s:
            break
        if not push_sheets(groups[name]):
            break
        done.add(name)
        st["backfill_done"] = sorted(done)
        save_state(st)  # simpan progres tiap sheet, aman kalau run terhenti
    if all(g in done for g in groups if groups[g]):
        st["sheets_backfilled"] = True
        st.pop("backfill_done", None)
        log.info("Riwayat Google Sheets lengkap")


# ---------------------------------------------------------------- mode tes
def _pick_test_event(done):
    """Rilis terakhir, atau yang judulnya cocok dengan TEST_MATCH. None kalau tidak ada."""
    if not TEST_MATCH:
        return done[-1]
    needle = _compact_title(TEST_MATCH)
    matched = [e for e in done if needle in _compact_title(e.get("title"))]
    if not matched:
        log.warning("TES: tidak ada rilis yang cocok dengan %s", TEST_MATCH)
        return None
    headline = [e for e in matched if "private" not in _compact_title(e.get("title"))]
    pool = headline or matched
    # Tanggal terbaru menang. Kalau sama, judul yang lebih pendek (headline, bukan "Private").
    pool.sort(key=lambda e: (e.get("date") or "", -len(e.get("title") or "")))
    return pool[-1]


def test_send(now):
    """Kirim satu rilis berdampak SEDANG/TINGGI. State tidak diubah.
    TEST_MATCH membatasi judul, supaya tes tidak mengirim rilis lain."""
    min_rank = LEVEL_RANK.get(MIN_ALERT, 1)
    evs = fetch_events(now, back_days=7, fwd_days=0)
    done = sorted((e for e in evs if e.get("actual") is not None and parse_dt(e["date"]) <= now
                   and LEVEL_RANK[classify(e)[0]] >= min_rank), key=lambda e: e["date"])
    if not done:
        log.warning("TES: tidak ada rilis berdampak sedang/tinggi dalam 7 hari terakhir")
        return
    ev = _pick_test_event(done)
    if ev is None:
        return
    log.info("TES: mengirim %s %s (%s)", ev.get("country"), ev.get("title"), ev.get("date"))
    embed = release_embed(ev, evs, now)
    embed["title"] = "🧪 TES · " + embed["title"]
    log.info("TES ringkasan: %s", embed.get("description"))
    for field in embed.get("fields") or []:
        if field.get("name") in ("Pasar Indonesia", "Pasar Amerika", "Global"):
            log.info("TES %s: %s", field["name"], (field.get("value") or "").replace("\n", " | "))
    png = make_chart(ev, now)
    if png:
        embed["image"] = {"url": "attachment://chart.png"}
    discord([embed], png=png)
    if SHEETS_URL or DRY_RUN:
        push_sheets([sheet_row(ev)], now)  # aman: baris yang sama tidak akan tercatat dua kali


# ---------------------------------------------------------------- main
def main():
    now = dt.datetime.now(dt.timezone.utc)
    if TEST_SEND:
        test_send(now)
        return
    st = load_state()
    first_run = "posted" not in st
    if st.get("backfill_version") != BACKFILL_VERSION:  # format Sheets baru -> isi ulang riwayat
        st["sheets_backfilled"] = False
        st.pop("backfill_done", None)
        st["backfill_version"] = BACKFILL_VERSION
    st.setdefault("posted", [])
    st.setdefault("reminded", [])

    events = fetch_events(now, back_days=2, fwd_days=7 if SEND_PREVIEW else 2)
    log.info("Dapat %d event dari TradingView", len(events))
    min_rank = LEVEL_RANK.get(MIN_ALERT, 1)
    posted, reminded = set(st["posted"]), set(st["reminded"])

    # 1) jadwal harian
    today = now.astimezone(WIB).date()
    if SEND_PREVIEW and now.astimezone(WIB).hour >= PREVIEW_HOUR and st.get("preview_date") != str(today):
        todays = sorted((e for e in events
                         if parse_dt(e["date"]).astimezone(WIB).date() == today
                         and LEVEL_RANK[classify(e)[0]] >= 1), key=lambda e: e["date"])
        embeds = [preview_embed(todays, f"📅 Jadwal rilis hari ini · {fmt_wib(now).split(' ', 1)[1][:11]}",
                                0x3498DB)]
        if today.weekday() == 0:  # Senin: tambah ringkasan event TINGGI seminggu
            week = sorted((e for e in events
                           if 0 <= (parse_dt(e["date"]).astimezone(WIB).date() - today).days < 7
                           and classify(e)[0] == "TINGGI"), key=lambda e: e["date"])
            emb = preview_embed(week, "🗓️ Event berdampak TINGGI minggu ini", 0x9B59B6)
            emb["description"] = "\n".join(
                f"{fmt_wib(parse_dt(e['date']))[:10]} · " + line
                for e, line in zip(week, emb["description"].split("\n"))) if week else emb["description"]
            embeds.append(emb)
        discord(embeds)
        st["preview_date"] = str(today)

    # 2) pengingat ±30 menit sebelum event TINGGI
    for ev in events:
        t = parse_dt(ev["date"])
        if (SEND_REMINDER and ev["id"] not in reminded and ev.get("actual") is None
                and classify(ev)[0] == "TINGGI"
                and dt.timedelta(0) < t - now <= dt.timedelta(minutes=40)):
            mins = int((t - now).total_seconds() // 60)
            discord([{"title": f"⏰ {mins} menit lagi: {FLAGS.get(ev['country'], '')} {ev['title']}",
                      "color": 0xE74C3C,
                      "description": f"Forecast/Konsensus: **{fmt_val(ev, 'forecast')}** · "
                                     f"Previous: {fmt_val(ev, 'previous')}\nRilis: {fmt_wib(t)}"}])
            reminded.add(ev["id"])

    # 3) data yang baru rilis
    released = sorted((e for e in events if e.get("actual") is not None
                       and e["id"] not in posted and parse_dt(e["date"]) <= now),
                      key=lambda e: e["date"])
    if first_run:  # jangan spam data lama saat pertama kali dinyalakan
        posted.update(e["id"] for e in released)
        released = []
    sheet_rows = []
    for ev in released:
        if LEVEL_RANK[classify(ev)[0]] >= min_rank:
            embed = release_embed(ev, events, now)
            png = make_chart(ev, now)
            if png:
                embed["image"] = {"url": "attachment://chart.png"}
            discord([embed], png=png)
            sheet_rows.append(sheet_row(ev))
        posted.add(ev["id"])

    # simpan dulu catatan Discord, supaya aman walau langkah Sheets lama
    st["posted"], st["reminded"] = list(posted), list(reminded)
    save_state(st)

    # 4) Google Sheets: rilis baru dulu (cepat), lalu cicil riwayat awal
    if SHEETS_URL or DRY_RUN:
        pending = st.get("sheet_pending", []) + sheet_rows  # antrean kalau sebelumnya gagal
        st["sheet_pending"] = [] if push_sheets(pending, now) else pending[-500:]
        save_state(st)
        if not st.get("sheets_backfilled"):
            backfill_sheets(st, now, min_rank, SHEETS_BUDGET)
        save_state(st)
    log.info("Selesai: %d rilis baru diproses", len(released))


if __name__ == "__main__":
    main()
