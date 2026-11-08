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


# Naik pada jenis ini = kabar baik bagi ekonomi indikator itu. None = tidak ada arah baik/buruk.
_HIGHER_IS_GOOD = {
    "jobs": True,
    "growth": True,
    "unemployment": False,
    "inflation": False,
    "rate": False,
}
MOVE_GOOD = "#14804A"
MOVE_BAD = "#C8102E"
MOVE_NEUTRAL = "#5C6770"


def _news(typ, direction):
    """good, bad, atau neutral. Arah panah tidak sama dengan warna."""
    if direction == 0:
        return "neutral"
    higher = _HIGHER_IS_GOOD.get(typ)
    if higher is None:
        return "neutral"
    rose = direction > 0
    return "good" if (rose if higher else not rose) else "bad"


def _format_delta(ev, diff):
    """Selisih dalam satuan seri: K/nominal, persen, atau basis poin untuk suku bunga."""
    scale = ev.get("scale") or ""
    unit = (ev.get("unit") or "").strip()
    typ = indicator_type(ev.get("title") or "")
    low = unit.lower()
    if typ == "rate" and unit == "%":
        return f"{diff * 100:+,.2f} bp"
    if low in ("bp", "bps", "basis point", "basis points"):
        return f"{diff:+,.2f} bp"
    num = f"{diff:+,.2f}{scale}"
    if unit == "%":
        return num + "%"
    if unit:
        return f"{num} {unit}"
    return num


def move_badge(ev):
    """Panah mengikuti naik/turunnya angka. Warna mengikuti baik/buruknya bagi indikator.
    Teks memakai satuan seri, bukan persen perubahan relatif. None kalau tidak ada pembanding."""
    direction, diff, _label = surprise(ev)
    actual = ev.get("actual")
    base = ev.get("forecast") if ev.get("forecast") is not None else ev.get("previous")
    if diff is None or actual is None or base is None:
        return None
    typ = indicator_type(ev.get("title") or "")
    news = _news(typ, direction)
    color = {"good": MOVE_GOOD, "bad": MOVE_BAD, "neutral": MOVE_NEUTRAL}[news]
    if direction == 0 or abs(diff) < 1e-9:
        text = _format_delta(ev, 0.0)
        if text.startswith("+"):
            text = text[1:]
        return {"flat": True, "up": False, "text": text, "color": MOVE_NEUTRAL, "news": "neutral"}
    arrow = "▲" if direction > 0 else "▼"
    return {
        "flat": False,
        "up": direction > 0,
        "text": f"{arrow} {_format_delta(ev, diff)}",
        "color": color,
        "news": news,
    }


def _with_move(text, ev):
    """Sisipkan panah dan perubahan di awal narasi. Grafik tidak menggambar teks ini."""
    badge = move_badge(ev)
    if not badge:
        return text, None
    return f"{badge['text']}. {text}", badge


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
        return "Ini jumlah orang yang memperoleh pekerjaan baru, bukan tingkat pengangguran."
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
        return "Suku bunga kebijakan lebih tinggi daripada perkiraan" if up else "Suku bunga kebijakan lebih rendah daripada perkiraan"
    if family == "prices":
        if any(k in t for k in ("hourly", "wage", "earnings")):
            return "Upah naik lebih cepat daripada perkiraan" if up else "Upah naik lebih lambat daripada perkiraan"
        return "Harga konsumen naik lebih cepat daripada perkiraan" if up else "Harga konsumen naik lebih lambat daripada perkiraan"
    if family == "labor":
        if "unemployment" in t:
            return ("Tingkat pengangguran lebih rendah daripada perkiraan" if up else
                    "Tingkat pengangguran lebih tinggi daripada perkiraan")
        if "jobless" in t or "claims" in t:
            return ("Klaim pengangguran lebih sedikit daripada perkiraan" if up else
                    "Klaim pengangguran lebih banyak daripada perkiraan")
        if any(k in t for k in ("payroll", "nonfarm", "non-farm", "non farm", "employment change", "adp")):
            return ("Perusahaan menambah tenaga kerja lebih banyak daripada perkiraan" if up else
                    "Perusahaan menambah tenaga kerja lebih sedikit daripada perkiraan")
        return ("Tingkat pengangguran lebih rendah daripada perkiraan" if up else
                "Tingkat pengangguran lebih tinggi daripada perkiraan")
    if family == "growth":
        return ("Perekonomian tumbuh lebih cepat daripada perkiraan" if up else
                "Perekonomian tumbuh lebih lambat daripada perkiraan")
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


def rule_analysis(ev):
    """Fakta sekali. Akibatnya ada di enam baris pasar, bukan di sini."""
    f = _facts(ev)
    parts = [_everyday(f), f"{f['title']} {f['place']} {f['actual']} ({f['paren']})."]
    note = _note(f["title"])
    if note:
        parts.append(note)
    return _clip(" ".join(parts), 500)


def _pack(id_pendek, id_panjang, us_pendek, us_panjang, global_pendek, global_panjang):
    return {
        "id_pendek": id_pendek, "id_panjang": id_panjang, "us_pendek": us_pendek,
        "us_panjang": us_panjang, "global_pendek": global_pendek, "global_panjang": global_panjang,
    }


def _quiet():
    """Pasar yang tidak bergerak, tetap disebut lengkap, bukan satu kata."""
    return _pack(
        "Rupiah, harga SBN (surat utang negara), dan IHSG (saham Indonesia) tetap di tempat. BI tidak mengubah bunga.",
        "Bunga BI tetap. Harga SBN dan IHSG tidak mendapat arah baru.",
        "Bunga surat utang AS dan saham AS tetap. The Fed tidak mengubah bunga.",
        "Bunga The Fed tetap. Saham AS tidak dihargai ulang.",
        "Dolar tidak berubah, selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) tetap, dan perdagangan barang tidak bergeser.",
        "Dolar dan arus perdagangan barang tetap seperti sebelumnya. Selera risiko tidak berubah.",
    )


def _us_tighten(up, global_long_up, global_long_down):
    """Dolar dan bunga AS mengeras atau melunak. Dipakai data kerja dan harga AS."""
    if up:
        return _pack(
            "Rupiah melemah hari ini, harga SBN (surat utang negara) turun, dan IHSG (saham Indonesia) ikut turun. BI tidak mengubah bunga pada hari yang sama.",
            "BI menahan bunga selama dolar tetap kuat. Harga SBN dan IHSG baru pulih setelah dolar mereda.",
            "Bunga surat utang jangka pendek AS naik hari ini, dan saham yang butuh pinjaman murah turun. The Fed tidak mengubah bunga kebijakannya pada hari rilis.",
            "The Fed menahan bunga lebih lama. Saham yang butuh pinjaman murah tetap tertekan selama bunga surat utang itu tinggi.",
            "Dolar menguat terhadap euro dan yen, dan selera risiko (minat menaruh uang di aset yang untungnya tidak pasti) turun hari ini. Perdagangan barang antarnegara tidak berubah pada hari ini.",
            global_long_up,
        )
    return _pack(
        "Rupiah menguat hari ini, harga SBN (surat utang negara) naik, dan IHSG (saham Indonesia) ikut naik. BI tidak mengubah bunga pada hari yang sama.",
        "BI mendapat ruang menurunkan bunga setelah dolar mereda. Harga SBN dan IHSG naik bersama rupiah.",
        "Bunga surat utang jangka pendek AS turun hari ini, dan saham yang butuh pinjaman murah naik. The Fed tidak mengubah bunga kebijakannya pada hari rilis.",
        "The Fed menurunkan bunga. Saham yang butuh pinjaman murah lega selama bunga surat utang tetap lebih rendah.",
        "Dolar melemah terhadap euro dan yen, dan selera risiko (minat menaruh uang di aset yang untungnya tidak pasti) naik hari ini. Perdagangan barang antarnegara tidak berubah pada hari ini.",
        global_long_down,
    )


def _us_jobs(up):
    """Perekrutan: tambahan tenaga kerja, bukan tingkat pengangguran."""
    if up:
        return _pack(
            "Biaya dana dalam dolar tetap tinggi karena perusahaan di Amerika Serikat masih menambah tenaga kerja, sehingga rupiah melemah dan harga SBN (surat utang negara) turun. IHSG (saham Indonesia) tertekan oleh biaya dana itu, sedangkan BI tidak mengubah suku bunga pada hari yang sama.",
            "Selama tambahan tenaga kerja menahan penurunan bunga The Fed, BI mempertahankan suku bunga. Harga SBN dan IHSG baru pulih setelah biaya dana dolar mereda.",
            "Imbal hasil surat utang jangka pendek naik karena The Fed tidak memperoleh alasan menurunkan bunga ketika perekrutan masih kuat, dan saham yang bergantung pada pinjaman murah turun. Suku bunga kebijakan The Fed tidak berubah pada hari rilis.",
            "The Fed mempertahankan suku bunga lebih lama selama permintaan tenaga kerja belum melemah. Saham yang membutuhkan pinjaman murah tetap tertekan sampai imbal hasil surat utang jangka pendek turun.",
            "Dolar menguat terhadap euro dan yen karena imbal hasil Amerika Serikat tetap lebih menarik. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) menurun, sedangkan arus perdagangan barang tidak berubah pada hari rilis.",
            "Pinjaman di luar Amerika Serikat tetap mahal selama dolar kuat karena pasar tenaga kerja di sana belum longgar. Arus perdagangan barang tidak bergeser hanya oleh laporan perekrutan.",
        )
    return _pack(
        "Peluang penurunan bunga The Fed membuka ruang bagi dana asing ke aset rupiah, sehingga rupiah menguat dan harga SBN (surat utang negara) naik. IHSG (saham Indonesia) naik karena biaya dana luar negeri mereda, dan BI tidak mengubah suku bunga pada hari yang sama.",
        "BI memperoleh ruang menurunkan suku bunga setelah dolar mereda akibat perekrutan yang lebih lemah. Harga SBN dan IHSG menguat bersama rupiah.",
        "Imbal hasil surat utang jangka pendek turun karena pasar mulai memperhitungkan penurunan bunga The Fed. Saham yang membutuhkan pinjaman murah naik, sedangkan suku bunga kebijakan belum diubah pada hari rilis.",
        "The Fed menurunkan suku bunga apabila permintaan tenaga kerja tetap lemah. Saham yang bergantung pada pinjaman murah bertahan lebih tinggi selama imbal hasil surat utang rendah.",
        "Dolar melemah terhadap euro dan yen seiring turunnya imbal hasil Amerika Serikat. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat, dan perdagangan barang antarnegara tidak berubah pada hari rilis.",
        "Pinjaman di luar Amerika Serikat menjadi lebih murah mengikuti dolar yang lebih lemah. Arus barang antarnegara tidak bergeser.",
    )


def _us_unemployment(up):
    """Tingkat pengangguran dan klaim: pencari kerja, bukan jumlah perekrutan."""
    if up:
        return _pack(
            "Pencari kerja yang lebih sedikit membuat pasar tetap mengharapkan bunga The Fed tinggi, sehingga rupiah melemah terhadap dolar. Harga SBN (surat utang negara) turun karena imbal hasil acuan luar negeri tidak mereda, IHSG (saham Indonesia) melemah, dan BI tidak menyesuaikan suku bunga hari ini.",
            "BI menahan suku bunga selama pengangguran yang rendah di Amerika Serikat menunda pemotongan bunga The Fed. Harga SBN serta IHSG baru membaik setelah dolar tidak lagi menekan biaya dana.",
            "Imbal hasil surat utang jangka pendek lebih tinggi karena pengangguran yang rendah mengurangi alasan The Fed memotong bunga. Saham yang sensitif terhadap bunga kebijakan melemah, dan The Fed tidak mengubah suku bunga pada hari pengumuman.",
            "The Fed mempertahankan suku bunga dalam beberapa bulan ke depan apabila pencari kerja tetap sedikit. Saham yang bergantung pada biaya pinjaman tetap tertekan.",
            "Permintaan terhadap dolar naik terhadap euro dan yen karena imbal hasil Amerika Serikat tetap tinggi ketika pengangguran rendah, dan minat pada aset berisiko menurun. Perdagangan barang tidak berubah pada hari yang sama, sebab data ini tidak mengubah pesanan ekspor.",
            "Dolar yang tetap kuat membuat pinjaman di luar Amerika Serikat mahal selama pengangguran di sana rendah. Arus perdagangan barang tidak bergeser.",
        )
    return _pack(
        "Bertambahnya pencari kerja di Amerika Serikat membuka peluang bunga The Fed turun, sehingga rupiah menguat dan harga SBN (surat utang negara) naik. IHSG (saham Indonesia) naik karena biaya dana luar negeri mereda, sementara BI tidak mengubah suku bunga hari ini.",
        "Setelah dolar mereda, BI memperoleh ruang menurunkan suku bunga. Harga SBN dan IHSG menguat selama tekanan dana asing berkurang.",
        "Imbal hasil surat utang jangka pendek turun karena lebih banyak orang tanpa pekerjaan memberi alasan bagi The Fed untuk melonggarkan bunga. Saham yang sensitif terhadap biaya pinjaman naik, dan suku bunga kebijakan belum diubah hari ini.",
        "The Fed menurunkan suku bunga apabila tingkat pengangguran tetap lebih tinggi. Saham yang membutuhkan pinjaman murah bertahan naik.",
        "Dolar melemah terhadap euro dan yen seiring ekspektasi bunga Amerika Serikat yang lebih rendah. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat, sedangkan pesanan perdagangan barang tidak berubah pada hari rilis.",
        "Pinjaman di luar Amerika Serikat menjadi lebih murah. Arus barang antarnegara tidak berubah hanya karena data pengangguran.",
    )


def _us_inflation(up):
    """Harga konsumen: daya beli dan laju harga, bukan laporan tenaga kerja."""
    if up:
        return _pack(
            "Karena harga konsumen di Amerika Serikat naik lebih cepat, pasar menunda harapan pemotongan bunga dan rupiah melemah. Harga SBN (surat utang negara) turun, IHSG (saham Indonesia) tertekan biaya dana dolar, dan BI menahan suku bunga.",
            "BI mempertahankan suku bunga selama inflasi Amerika Serikat menahan The Fed. Harga SBN dan IHSG baru membaik setelah tekanan harga di sana mereda.",
            "Imbal hasil surat utang jangka pendek naik karena The Fed lebih sulit menurunkan bunga ketika harga konsumen masih naik. Saham yang sensitif terhadap daya beli melemah, dan suku bunga kebijakan tidak diubah pada hari rilis.",
            "The Fed menahan suku bunga lebih lama sampai laju harga mendekati sasaran. Saham yang bergantung pada pinjaman murah tetap tertekan selama imbal hasil itu tinggi.",
            "Dolar menguat terhadap euro dan yen karena imbal hasil mengikuti kenaikan harga konsumen, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) menurun. Volume perdagangan barang tidak berubah pada hari rilis.",
            "Harga barang di luar Amerika Serikat menghadapi tekanan naik. Pinjaman di luar Amerika Serikat tetap mahal selama dolar kuat karena inflasi di sana belum reda.",
        )
    return _pack(
        "Harga konsumen yang lebih tenang membuka peluang bunga The Fed turun, dan rupiah menguat. Harga SBN (surat utang negara) naik, IHSG (saham Indonesia) naik karena biaya dana mereda, dan BI memperoleh ruang menurunkan suku bunga.",
        "BI menurunkan suku bunga setelah dolar mereda karena inflasi Amerika Serikat lebih rendah. Harga SBN tetap lebih tinggi selama laju harga di sana tenang.",
        "Imbal hasil surat utang jangka pendek turun karena The Fed memperoleh ruang menurunkan bunga ketika harga konsumen melambat. Saham yang bergantung pada daya beli naik, dan suku bunga kebijakan belum diubah hari ini.",
        "The Fed menurunkan suku bunga. Saham yang membutuhkan pinjaman murah menguat selama laju harga tetap lebih rendah.",
        "Dolar melemah terhadap euro dan yen seiring imbal hasil yang turun karena harga lebih tenang, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat. Volume perdagangan barang tidak berubah pada hari rilis.",
        "Harga barang di luar Amerika Serikat menghadapi tekanan turun. Pinjaman di luar Amerika Serikat menjadi lebih murah mengikuti dolar yang lebih lemah.",
    )


def _us_rates(up):
    """Keputusan suku bunga: harga uang itu sendiri, dalam basis poin pada ringkasan."""
    if up:
        return _pack(
            "Kenaikan suku bunga kebijakan Amerika Serikat memperlebar selisih bunga terhadap Indonesia, sehingga rupiah melemah dan harga SBN (surat utang negara) turun. IHSG (saham Indonesia) turun karena biaya dana luar negeri naik, dan BI tidak mengubah suku bunga pada hari yang sama.",
            "BI menahan suku bunga karena bunga kebijakan Amerika Serikat lebih tinggi daripada bunga Indonesia. Harga SBN dan IHSG tetap tertekan selama selisih itu melebar.",
            "The Fed menaikkan suku bunga kebijakan, dan imbal hasil surat utang jangka pendek naik mengikuti keputusan itu. Saham turun karena biaya pinjaman perusahaan menjadi lebih mahal.",
            "Biaya utang perusahaan Amerika Serikat tetap lebih mahal. Saham baru pulih setelah The Fed menurunkan suku bunga kembali.",
            "Dolar menguat karena bunga kebijakan Amerika Serikat lebih tinggi, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) menurun. Perdagangan barang tidak berubah pada hari keputusan.",
            "Pinjaman di luar Amerika Serikat menjadi lebih mahal. Arus perdagangan barang tidak bergeser.",
        )
    return _pack(
        "Penurunan suku bunga kebijakan Amerika Serikat mempersempit selisih bunga, sehingga rupiah menguat dan harga SBN (surat utang negara) naik. IHSG (saham Indonesia) naik karena biaya dana luar negeri turun, dan BI tidak mengubah suku bunga pada hari yang sama.",
        "BI menurunkan suku bunga setelah dolar mereda akibat pelonggaran The Fed. Harga SBN dan IHSG menguat bersama rupiah.",
        "The Fed menurunkan suku bunga kebijakan. Imbal hasil surat utang jangka pendek turun, dan saham naik karena biaya pinjaman perusahaan lebih murah.",
        "Tagihan utang perusahaan menjadi lebih ringan. Saham yang membutuhkan pinjaman murah bertahan lebih tinggi.",
        "Dolar melemah karena bunga kebijakan Amerika Serikat lebih rendah, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat. Perdagangan barang tidak berubah pada hari keputusan.",
        "Pinjaman di luar Amerika Serikat menjadi lebih murah. Arus perdagangan barang tidak bergeser.",
    )


def _market_lines(f):
    origin, family, kind = f["origin"], f["family"], f["kind"]
    if kind in ("unknown", "inline"):
        return _quiet()
    up = kind == "hot"
    if origin == "US" and family == "labor":
        if f.get("typ") == "unemployment":
            return _us_unemployment(up)
        return _us_jobs(up)
    if origin == "US" and family == "prices":
        return _us_inflation(up)
    if origin == "US" and family == "rate":
        return _us_rates(up)
    if origin == "US" and family == "guidance":
        if up:
            return _pack(
                "Rupiah melemah, harga SBN turun, dan IHSG turun hari ini. BI tidak mengubah bunga pada hari pernyataan.",
                "BI tidak mengubah bunga sebelum rapat The Fed. Harga SBN dan IHSG mengikuti dolar sampai rapat itu.",
                "Bunga surat utang jangka pendek AS naik hari ini, dan saham turun. Bunga kebijakan The Fed belum berubah.",
                "Bunga The Fed baru berubah pada rapat berikutnya. Saham mengikuti bunga surat utang sampai rapat itu.",
                "Dolar menguat dan selera risiko turun hari ini. Perdagangan barang tidak berubah.",
                "Dolar tetap lebih kuat kalau The Fed menahan bunga di rapat berikut. Arus perdagangan barang tidak bergeser.",
            )
        return _pack(
            "Rupiah menguat, harga SBN naik, dan IHSG naik hari ini. BI tidak mengubah bunga pada hari pernyataan.",
            "BI belum menurunkan bunga sebelum rapat The Fed. Harga SBN dan IHSG mengikuti dolar sampai rapat itu.",
            "Bunga surat utang jangka pendek AS turun hari ini, dan saham naik. Bunga kebijakan The Fed belum berubah.",
            "Bunga The Fed baru berubah pada rapat berikutnya. Saham mengikuti bunga surat utang sampai rapat itu.",
            "Dolar melemah dan selera risiko naik hari ini. Perdagangan barang tidak berubah.",
            "Dolar tetap lebih lemah kalau The Fed menurunkan bunga di rapat berikut. Arus perdagangan barang tidak bergeser.",
        )
    if origin == "US" and family == "growth":
        if up:
            return _pack(
                "Rupiah melemah dan harga SBN turun hari ini, sementara saham ekspor di IHSG naik. BI tidak mengubah bunga pada hari yang sama.",
                "BI menahan bunga selama dolar kuat. Saham ekspor di IHSG tetap lebih diuntungkan daripada saham yang butuh pinjaman murah.",
                "Saham perusahaan yang menjual barang naik hari ini, dan bunga surat utang jangka pendek ikut naik. The Fed belum mengubah bunga kebijakannya.",
                "The Fed menahan bunga. Saham penjual barang tetap lebih kuat daripada saham yang butuh pinjaman murah.",
                "Dolar menguat, dan selera risiko naik untuk saham barang, bukan untuk utang. Perdagangan belum berubah hari ini.",
                "Pesanan barang dari AS bertambah. Dolar yang kuat tetap membuat pinjaman di luar AS mahal.",
            )
        return _pack(
            "Rupiah menguat dan harga SBN naik hari ini, sementara saham ekspor di IHSG turun. BI tidak mengubah bunga pada hari yang sama.",
            "BI menurunkan bunga setelah dolar mereda. Saham ekspor di IHSG tetap lebih lemah karena pesanan dari AS susut.",
            "Saham perusahaan yang menjual barang turun hari ini, dan bunga surat utang jangka pendek ikut turun. The Fed belum mengubah bunga kebijakannya.",
            "The Fed menurunkan bunga. Saham penjual barang tetap lebih lemah meskipun pinjaman lebih murah.",
            "Dolar melemah, dan selera risiko turun untuk saham barang. Perdagangan belum berubah hari ini.",
            "Pesanan barang dari AS berkurang. Pinjaman di luar AS jadi lebih murah mengikuti dolar yang lebih lemah.",
        )
    if origin == "US" and family == "activity":
        if up:
            return _pack(
                "Rupiah melemah dan harga SBN turun hari ini, dan IHSG ikut turun. BI tidak mengubah bunga.",
                "Bunga BI tetap. Harga SBN dan IHSG kembali kalau angka bulan depan berlawanan arah.",
                "Saham perusahaan AS naik hari ini, dan bunga surat utang jangka pendek ikut naik. The Fed tidak mengubah bunga.",
                "Bunga The Fed tetap. Saham AS mudah kembali ke posisi semula pada angka bulan depan.",
                "Dolar menguat dan selera risiko turun hari ini. Perdagangan barang tidak berubah.",
                "Dolar dan arus perdagangan barang kembali ke posisi semula kalau angka berikutnya berlawanan arah.",
            )
        return _pack(
            "Rupiah menguat dan harga SBN naik hari ini, dan IHSG ikut naik. BI tidak mengubah bunga.",
            "Bunga BI tetap. Harga SBN dan IHSG kembali kalau angka bulan depan berlawanan arah.",
            "Saham perusahaan AS turun hari ini, dan bunga surat utang jangka pendek ikut turun. The Fed tidak mengubah bunga.",
            "Bunga The Fed tetap. Saham AS mudah kembali ke posisi semula pada angka bulan depan.",
            "Dolar melemah dan selera risiko naik hari ini. Perdagangan barang tidak berubah.",
            "Dolar dan arus perdagangan barang kembali ke posisi semula kalau angka berikutnya berlawanan arah.",
        )
    if origin == "US" and family == "trade":
        lines = _quiet()
        lines["global_pendek"] = (
            "Dolar menguat hari ini. Selera risiko tetap, dan perdagangan barang tidak berubah pada hari rilis."
            if up else
            "Dolar melemah hari ini. Selera risiko tetap, dan perdagangan barang tidak berubah pada hari rilis."
        )
        lines["global_panjang"] = (
            "Dolar yang lebih kuat tidak mengubah arus barang. Selera risiko tetap."
            if up else
            "Dolar yang lebih lemah tidak mengubah arus barang. Selera risiko tetap."
        )
        return lines
    if origin == "US" and family == "reserves":
        return _quiet()
    if origin == "ID":
        return _id_lines(f, up)
    if origin == "CN":
        return _cn_lines(f, up)
    if origin == "JP" and family == "rate":
        if up:
            return _pack(
                "IHSG turun hari ini karena orang menjual aset yang dibiayai yen. Rupiah ikut melemah, harga SBN turun, dan BI tidak mengubah bunga.",
                "BI tidak mengubah bunga. IHSG dan harga SBN tetap lebih berat selama pinjaman yen mahal.",
                "Saham AS turun hari ini, dan bunga surat utang jangka panjang naik karena posisi yen ditutup. The Fed tidak mengubah bunga.",
                "Bunga The Fed tetap. Saham AS tetap lebih rendah selama pinjaman yen mahal.",
                "Yen menguat dan selera risiko turun hari ini. Perdagangan barang tidak berubah.",
                "Pinjaman dalam yen jadi lebih mahal. Arus perdagangan barang tidak bergeser.",
            )
        return _pack(
            "IHSG naik hari ini karena pinjaman yen murah hidup lagi. Rupiah ikut menguat, harga SBN naik, dan BI tidak mengubah bunga.",
            "BI tidak mengubah bunga. IHSG dan harga SBN tetap lebih tinggi selama pinjaman yen murah.",
            "Saham AS naik hari ini. Bunga surat utang AS tidak berubah besar, dan The Fed tidak mengubah bunga.",
            "Bunga The Fed tetap. Saham AS tetap lebih tinggi selama pinjaman yen murah.",
            "Yen melemah dan selera risiko naik hari ini. Perdagangan barang tidak berubah.",
            "Pinjaman dalam yen jadi lebih murah. Arus perdagangan barang tidak bergeser.",
        )
    if origin == "EU" and family == "rate":
        if up:
            return _pack(
                "Rupiah, harga SBN, dan IHSG tidak bergerak hari ini. BI tidak mengubah bunga.",
                "Bunga BI tetap, karena BI mengikuti The Fed, bukan bank sentral Eropa. Harga SBN dan IHSG tidak mendapat arah baru.",
                "Bunga surat utang AS dan saham AS tidak bergerak. The Fed tidak mengubah bunga.",
                "Bunga The Fed tetap. Saham AS tidak mengikuti keputusan Eropa.",
                "Euro menguat dan selera risiko turun hari ini. Perdagangan barang tidak berubah.",
                "Bunga pinjaman di Eropa naik. Dolar tetap, dan arus perdagangan barang tidak bergeser.",
            )
        return _pack(
                "Rupiah, harga SBN, dan IHSG tidak bergerak hari ini. BI tidak mengubah bunga.",
                "Bunga BI tetap. Harga SBN dan IHSG tidak mendapat arah baru dari Eropa.",
                "Bunga surat utang AS dan saham AS tidak bergerak. The Fed tidak mengubah bunga.",
                "Bunga The Fed tetap. Saham AS tidak mengikuti keputusan Eropa.",
                "Euro melemah dan selera risiko naik hari ini. Perdagangan barang tidak berubah.",
                "Bunga pinjaman di Eropa turun. Dolar tetap, dan arus perdagangan barang tidak bergeser.",
        )
    return _other_lines(f, up)


def _id_lines(f, up):
    family = f["family"]
    lines = _quiet()
    if family == "rate":
        lines["id_pendek"] = (
            "BI menaikkan suku bunga kebijakan, sehingga kredit lebih mahal dan harga SBN (surat utang negara) turun. Rupiah menguat karena selisih bunga lebih menarik, sedangkan di IHSG (saham Indonesia) saham bank diuntungkan dan saham properti tertekan."
            if up else
            "BI menurunkan suku bunga kebijakan, sehingga angsuran kredit lebih ringan dan harga SBN (surat utang negara) naik. Rupiah melemah karena selisih bunga menyempit, sedangkan di IHSG (saham Indonesia) saham properti naik dan saham bank tertekan."
        )
        lines["id_panjang"] = (
            "Biaya pinjaman di dalam negeri tetap lebih mahal. BI baru menurunkan suku bunga setelah rupiah dinilai aman."
            if up else
            "Biaya pinjaman di dalam negeri lebih murah. BI tidak menurunkannya jauh di bawah The Fed, agar rupiah tidak melemah kembali."
        )
    elif family == "prices":
        lines["id_pendek"] = (
            "Kenaikan harga barang mengikis daya beli, sehingga saham konsumen di IHSG (saham Indonesia) turun dan harga SBN (surat utang negara) turun. BI menahan suku bunga, dan rupiah tidak memperoleh dukungan baru pada hari rilis."
            if up else
            "Harga barang yang lebih tenang memulihkan daya beli, sehingga saham konsumen di IHSG (saham Indonesia) naik dan harga SBN (surat utang negara) naik. BI memperoleh ruang menurunkan suku bunga, dan rupiah tidak tertekan oleh data harga ini."
        )
        lines["id_panjang"] = (
            "Daya beli menipis dan BI menahan suku bunga. Harga SBN baru naik setelah laju harga mereda."
            if up else
            "Daya beli pulih dan BI menurunkan suku bunga. Harga SBN tetap lebih tinggi selama harga barang tenang."
        )
    elif family == "labor":
        lines["id_pendek"] = (
            "IHSG naik karena belanja rumah tangga lebih kuat. Rupiah dan harga SBN tidak bergerak hari ini, dan BI tidak mengubah bunga."
            if up else
            "IHSG turun karena belanja rumah tangga lebih lemah. Rupiah ikut melemah, harga SBN turun, dan BI tidak mengubah bunga hari ini."
        )
        lines["id_panjang"] = (
            "BI menahan bunga. Saham konsumen di IHSG tetap lebih kuat selama lowongan banyak."
            if up else
            "BI menurunkan bunga. Saham konsumen di IHSG tetap lebih lemah selama lowongan sepi."
        )
    elif family == "growth":
        lines["id_pendek"] = (
            "Permintaan dalam negeri yang lebih kuat menaikkan prospek laba emiten, sehingga IHSG (saham Indonesia) naik. Rupiah menguat karena aliran modal melihat pertumbuhan itu, harga SBN (surat utang negara) naik, dan BI tidak mengubah suku bunga pada hari rilis."
            if up else
            "Emiten kehilangan dukungan permintaan dalam negeri, dan IHSG (saham Indonesia) turun karena proyeksi laba dipangkas. Aliran modal yang lebih hati-hati melemahkan rupiah serta harga SBN (surat utang negara), sedangkan BI tidak mengubah suku bunga hari ini."
        )
        lines["id_panjang"] = (
            "BI tidak menurunkan suku bunga hanya untuk menopang kegiatan ekonomi yang sudah berada di atas perkiraan. IHSG tetap lebih tinggi, dan harga SBN mengikuti rupiah, selama permintaan dalam negeri bertahan."
            if up else
            "BI menurunkan suku bunga untuk menopang kegiatan ekonomi yang melambat. IHSG tetap lebih rendah, dan harga SBN baru pulih setelah pertumbuhan stabil."
        )
        lines["us_pendek"] = (
            "Imbal hasil surat utang Amerika Serikat dan saham di sana tidak bergerak, karena pertumbuhan Indonesia bukan alasan bagi The Fed untuk mengubah bunga. The Fed tidak mengubah suku bunga kebijakan."
            if up else
            "Saham dan imbal hasil surat utang Amerika Serikat tidak bereaksi, sebab perlambatan di Indonesia tidak mengubah perhitungan bunga The Fed. Suku bunga kebijakan The Fed tetap."
        )
        lines["us_panjang"] = (
            "Jalur suku bunga The Fed tetap ditentukan oleh data Amerika Serikat, bukan oleh pertumbuhan Indonesia. Saham Amerika Serikat tidak dihargai ulang."
        )
        lines["global_pendek"] = (
            "Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) terhadap aset negara berkembang meningkat karena kegiatan ekonomi Indonesia lebih kuat, sementara dolar terhadap euro tidak banyak berubah. Perdagangan barang belum berubah pada hari pengumuman."
            if up else
            "Minat terhadap aset negara berkembang menurun ketika pertumbuhan Indonesia lebih lambat. Dolar terhadap euro hampir tidak berubah, dan arus barang belum bergeser pada hari rilis."
        )
        lines["global_panjang"] = (
            "Permintaan impor bahan baku mengikuti kegiatan ekonomi yang lebih kuat. Dolar dan arus perdagangan global tidak bergeser pada hari angka ini diumumkan."
            if up else
            "Permintaan impor bahan baku menyusut mengikuti kegiatan yang lebih lambat. Dolar tidak berubah arah hanya karena pengumuman ini."
        )
    elif family == "activity":
        lines["id_pendek"] = (
            "IHSG naik hari ini. Rupiah dan harga SBN tidak bergerak, dan BI tidak mengubah bunga."
            if up else
            "IHSG turun hari ini. Rupiah dan harga SBN tidak bergerak, dan BI tidak mengubah bunga."
        )
        lines["id_panjang"] = (
            "Bunga BI tetap. IHSG kembali kalau angka bulan depan berlawanan arah."
        )
    elif family == "trade":
        lines["id_pendek"] = (
            "Rupiah menguat hari ini. Harga SBN naik, saham ekspor di IHSG naik, dan BI tidak mengubah bunga."
            if up else
            "Rupiah melemah hari ini. Harga SBN turun, saham ekspor di IHSG turun, dan BI tidak mengubah bunga."
        )
        lines["id_panjang"] = (
            "BI tidak mengubah bunga. Rupiah tetap lebih kuat selama uang dari luar tetap masuk."
            if up else
            "BI menahan bunga untuk menjaga rupiah. Rupiah tetap lebih lemah selama uang dari luar tetap keluar."
        )
    elif family == "reserves":
        lines["id_pendek"] = (
            "Rupiah menguat hari ini karena BI punya lebih banyak dolar untuk menjaganya. Harga SBN dan IHSG tidak bergerak, dan bunga BI tidak berubah."
            if up else
            "Rupiah melemah hari ini karena cadangan dolar BI berkurang. Harga SBN turun, IHSG tidak bergerak, dan BI menahan bunga."
        )
        lines["id_panjang"] = (
            "BI lebih mampu menahan pelemahan rupiah. Bunga BI tetap, dan harga SBN mengikuti rupiah."
            if up else
            "BI lebih sulit menjaga rupiah tanpa menaikkan bunga. Harga SBN tetap lebih rendah selama cadangan menipis."
        )
    elif family == "guidance":
        lines["id_pendek"] = (
            "Rupiah menguat dan harga SBN turun hari ini. Saham yang butuh pinjaman murah di IHSG turun. BI belum mengubah bunga pada hari pernyataan."
            if up else
            "Rupiah melemah dan harga SBN naik hari ini. Saham yang butuh pinjaman murah di IHSG naik. BI belum mengubah bunga pada hari pernyataan."
        )
        lines["id_panjang"] = (
            "BI menahan bunga pada rapat berikutnya. Harga SBN dan IHSG mengikuti keputusan itu."
            if up else
            "BI menurunkan bunga pada rapat berikutnya. Harga SBN dan IHSG mengikuti keputusan itu."
        )
    return lines


def _cn_lines(f, up):
    family = f["family"]
    if family in ("growth", "activity"):
        if up:
            return _pack(
                "Harga batu bara, nikel, dan sawit naik hari ini, sahamnya di IHSG naik, dan rupiah menguat. Harga SBN tidak bergerak, dan BI tidak mengubah bunga.",
                "Ekspor Indonesia ke China bertambah. BI tidak mengubah bunga, dan harga SBN mengikuti rupiah.",
                "Saham AS yang menjual ke Asia naik hari ini. Bunga surat utang AS tidak bergerak, dan The Fed tidak mengubah bunga.",
                "Bunga The Fed tetap. Saham AS yang menjual ke Asia mengikuti permintaan China, bukan bunga The Fed.",
                "Harga logam naik dan selera risiko untuk barang naik hari ini. Dolar tidak bergerak, dan perdagangan barang belum berubah pada hari rilis.",
                "Pesanan pabrik di luar China bertambah. Dolar tidak berubah.",
            )
        return _pack(
            "Harga batu bara, nikel, dan sawit turun hari ini, sahamnya di IHSG turun, dan rupiah melemah. Harga SBN turun, dan BI tidak mengubah bunga.",
            "Ekspor Indonesia ke China berkurang. BI tidak mengubah bunga, dan harga SBN mengikuti rupiah.",
            "Saham AS yang menjual ke Asia turun hari ini. Bunga surat utang AS tidak bergerak, dan The Fed tidak mengubah bunga.",
            "Bunga The Fed tetap. Saham AS yang menjual ke Asia mengikuti permintaan China, bukan bunga The Fed.",
            "Harga logam turun dan selera risiko untuk barang turun hari ini. Dolar tidak bergerak, dan perdagangan barang belum berubah pada hari rilis.",
            "Pesanan pabrik di luar China berkurang. Dolar tidak berubah.",
        )
    if family == "rate":
        if up:
            return _pack(
                "Harga batu bara, nikel, dan sawit turun hari ini, dan sahamnya di IHSG turun. Rupiah melemah, harga SBN ikut turun, dan BI tidak mengubah bunga.",
                "Ekspor Indonesia ke China berkurang. BI tidak mengubah bunga.",
                "Saham AS yang menjual ke Asia turun hari ini. Bunga surat utang AS dan The Fed tidak berubah.",
                "Bunga The Fed tetap. Saham AS tidak mengikuti bunga China.",
                "Harga logam turun dan selera risiko untuk barang turun hari ini. Dolar tidak bergerak.",
                "Pinjaman di China jadi lebih mahal. Arus perdagangan barang di luar China menyusut.",
            )
        return _pack(
            "Harga batu bara, nikel, dan sawit naik hari ini, dan sahamnya di IHSG naik. Rupiah menguat, harga SBN ikut naik, dan BI tidak mengubah bunga.",
            "Ekspor Indonesia ke China bertambah. BI tidak mengubah bunga.",
            "Saham AS yang menjual ke Asia naik hari ini. Bunga surat utang AS dan The Fed tidak berubah.",
            "Bunga The Fed tetap. Saham AS tidak mengikuti bunga China.",
            "Harga logam naik dan selera risiko untuk barang naik hari ini. Dolar tidak bergerak.",
            "Pinjaman di China jadi lebih murah. Arus perdagangan barang di luar China bertambah.",
        )
    if family == "prices":
        if up:
            return _pack(
                "Harga SBN, rupiah, dan IHSG tidak bergerak hari ini. BI tidak mengubah bunga.",
                "Ekspor Indonesia ke China tidak bertambah. Harga SBN dan bunga BI tetap.",
                "Bunga surat utang AS dan saham AS tidak bergerak. The Fed tidak mengubah bunga.",
                "Bunga The Fed tetap. Saham AS tidak mengikuti harga di China.",
                "Harga barang dari China naik. Dolar dan selera risiko tidak berubah hari ini.",
                "Bunga di China tetap tinggi. Arus perdagangan barang baru berubah setelah harga ini bertahan.",
            )
        return _pack(
            "Harga batu bara, nikel, dan sawit naik hari ini, sahamnya di IHSG naik. Rupiah menguat, dan BI tidak mengubah bunga hari ini.",
            "Ekspor Indonesia ke China bertambah. Harga SBN naik mengikuti rupiah.",
            "Bunga surat utang AS dan saham AS tidak bergerak. The Fed tidak mengubah bunga.",
            "Bunga The Fed tetap. Saham AS tidak mengikuti harga di China.",
            "Harga barang dari China turun, dan selera risiko untuk barang naik hari ini. Dolar tidak bergerak.",
            "Bunga di China turun. Pesanan barang dari China bertambah.",
        )
    if family == "labor":
        lines = _quiet()
        lines["id_panjang"] = (
            "Ekspor barang jadi Indonesia ke China bertambah. Bunga BI, harga SBN, dan rupiah tidak berubah karenanya."
            if up else
            "Ekspor barang jadi Indonesia ke China berkurang. Bunga BI, harga SBN, dan rupiah tidak berubah karenanya."
        )
        return lines
    if family == "guidance":
        if up:
            return _pack(
                "Harga batu bara, nikel, dan sawit turun hari ini, dan sahamnya di IHSG turun. Rupiah dan harga SBN tidak bergerak, dan BI tidak mengubah bunga.",
                "Ekspor Indonesia ke China berkurang kalau pinjaman di China ikut mengetat. BI tidak mengubah bunga.",
                "Saham AS yang menjual ke Asia turun hari ini. Bunga surat utang AS dan The Fed tidak berubah.",
                "Bunga The Fed tetap. Saham AS tidak mengikuti pernyataan China.",
                "Harga logam turun dan selera risiko untuk barang turun hari ini. Dolar tidak bergerak.",
                "Bunga di China belum berubah pada hari pernyataan. Arus perdagangan baru berubah setelah bunga itu benar-benar naik.",
            )
        return _pack(
            "Harga batu bara, nikel, dan sawit naik hari ini, dan sahamnya di IHSG naik. Rupiah menguat, harga SBN ikut naik, dan BI tidak mengubah bunga.",
            "Ekspor Indonesia ke China bertambah kalau pinjaman di China ikut murah. BI tidak mengubah bunga.",
            "Saham AS yang menjual ke Asia naik hari ini. Bunga surat utang AS dan The Fed tidak berubah.",
            "Bunga The Fed tetap. Saham AS tidak mengikuti pernyataan China.",
            "Harga logam naik dan selera risiko untuk barang naik hari ini. Dolar tidak bergerak.",
            "Bunga di China belum berubah pada hari pernyataan. Arus perdagangan baru berubah setelah bunga itu benar-benar turun.",
        )
    return _quiet()


def _other_lines(f, up):
    family, place = f["family"], f["place"]
    lines = _quiet()
    if family in ("prices", "labor", "rate", "guidance"):
        lines["global_pendek"] = (
            f"Dolar tidak bergerak dan selera risiko global tetap. Bunga di {place} naik, dan perdagangan barang tidak berubah hari ini."
            if up else
            f"Dolar tidak bergerak dan selera risiko global tetap. Bunga di {place} turun, dan perdagangan barang tidak berubah hari ini."
        )
        lines["global_panjang"] = (
            f"Bunga di {place} tetap lebih tinggi. Dolar, selera risiko, dan arus perdagangan barang tidak ikut."
            if up else
            f"Bunga di {place} tetap lebih rendah. Dolar, selera risiko, dan arus perdagangan barang tidak ikut."
        )
    elif family in ("growth", "activity"):
        lines["global_pendek"] = (
            f"Dolar dan selera risiko global tidak berubah hari ini. Perdagangan dengan {place} belum berubah pada hari rilis."
        )
        lines["global_panjang"] = (
            f"Pesanan dari {place} bertambah. Dolar dan selera risiko tidak ikut berubah."
            if up else
            f"Pesanan dari {place} berkurang. Dolar dan selera risiko tidak ikut berubah."
        )
    return lines


def _strip_fact(f, text):
    """Buang nama indikator kalau masih menempel di depan baris."""
    text = " ".join((text or "").split())
    for prefix in (f"{f['title']} {f['place']}:", f"{f['title']}:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def market_views(ev):
    f = _facts(ev)
    lines = _market_lines(f)
    return {k: _clip(_strip_fact(f, lines.get(k)), 520) for k in MARKET_KEYS}

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
        "Kamu menjelaskan rilis ekonomi dalam bahasa Indonesia yang formal, padat, dan lugas.\n"
        "Jangan pakai kencang, melaju, melonjak, atau motor pertumbuhan. "
        "Pakai padanan formal: lebih cepat, meningkat, menurun.\n"
        "Jangan pakai kata stance, siklus, kanal, momentum, disinflasi, durasi, atau rezim. "
        "Kalau perlu menyebut bunga, dolar, rupiah, saham, atau harga barang, pakai kata itu saja.\n"
        "Gunakan HANYA angka yang diberikan. Jangan mengarang data lain. Jangan menyuruh beli atau jual.\n"
        "Balas HANYA JSON valid, tanpa markdown, dengan kunci:\n"
        "ringkasan: fakta sekali saja. Nama indikator, angkanya, dan artinya sehari-hari "
        "(lebih banyak orang dapat kerja, harga naik lebih cepat, bunga lebih tinggi). "
        "Jangan tulis akibat ke rupiah, bunga, saham, atau dolar di ringkasan.\n"
        "id_pendek, id_panjang, us_pendek, us_panjang, global_pendek, global_panjang: "
        "masing-masing SATU atau DUA kalimat pendek, padat, dan lengkap. Keenamnya harus berbeda bentuknya.\n"
        "Jangan ulang pola yang sama seperti 'IHSG naik, rupiah naik' di setiap horizon. "
        "Untuk setiap akibat, sebutkan sebabnya dan kaitkan dengan jenis indikator ini. "
        "Data tenaga kerja, pertumbuhan, inflasi, dan suku bunga tidak boleh memakai kalimat yang sama.\n"
        "Jangan ulang nama indikator. Jangan ulang kalimat fakta dari ringkasan. "
        "Jangan pakai biasanya, bisa, atau kalimat pengisi.\n"
        "Indonesia: sebut rupiah, SBN (surat utang negara), IHSG (saham Indonesia), dan BI.\n"
        "Amerika: sebut bunga surat utang, saham, dan The Fed.\n"
        "Global: sebut dolar, selera risiko (minat menaruh uang di aset yang untungnya tidak pasti), dan perdagangan barang.\n"
        "Jangka pendek artinya hari rilis. Jangka panjang artinya beberapa bulan ke depan.\n"
        "Kalau suatu harga tidak bergerak, katakan di dalam kalimat yang sama, jangan sebagai pengumuman terpisah yang kosong.\n\n"
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
            json={"model": CLAUDE_MODEL, "max_tokens": 1400,
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
    badge = move_badge(ev)
    news = (badge or {}).get("news")
    if news == "good":
        line_color, fill_color = MOVE_GOOD, "#E3F5EA"
    elif news == "bad":
        line_color, fill_color = MOVE_BAD, BRAND_SOFT
    else:
        line_color, fill_color = MOVE_NEUTRAL, "#EEF0F2"

    fig, ax = plt.subplots(figsize=(11.4, 5.6), dpi=140)
    fig.patch.set_facecolor("white")
    xs = mdates.date2num(x)
    smooth_x, smooth_y = _smooth_curve(xs, act, n=480)
    smooth_when = mdates.num2date(smooth_x, tz=WIB)
    floor = min(act + [v for v in fc if v is not None])
    ax.fill_between(smooth_when, smooth_y, floor, color=fill_color, alpha=0.55, zorder=1)
    ax.plot(smooth_when, smooth_y, color=line_color, lw=2.8, zorder=3, label="Actual",
            solid_capstyle="round", solid_joinstyle="round")
    ax.plot(x, act, linestyle="none", marker="o", ms=7, color=line_color,
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
        ha="center", fontsize=8, color=BRAND_DARK, fontweight="bold", zorder=7, clip_on=False,
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
    summary, _badge = _with_move(read["ringkasan"], ev)
    embed = {
        "title": f"{emoji} {FLAGS.get(ev['country'], ev['country'])} {ev['title']}"
                 + (f" ({ev['period']})" if ev.get("period") else ""),
        "color": color,
        "description": _clip(summary, 1500),
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
    badge = move_badge(ev)
    log.info("TES gerak: %s", badge["text"] if badge else "tanpa pembanding")
    embed = release_embed(ev, evs, now)
    embed["title"] = "🧪 TES · " + embed["title"]
    log.info("TES ringkasan: %s", embed.get("description"))
    for field in embed.get("fields") or []:
        if field.get("name") in ("Actual", "Pasar Indonesia", "Pasar Amerika", "Global"):
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
