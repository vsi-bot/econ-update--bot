#!/usr/bin/env python3
"""
Calendar Bot: TradingView Economic Calendar -> analisis dampak ke Indonesia -> Discord
-------------------------------------------------------------------------------------
Yang dilakukan setiap kali jalan (tiap 5 menit via GitHub Actions pada jam rilis):
  1. Ambil kalender ekonomi TradingView (3 hari ke belakang s/d 100 hari ke depan).
  2. Kategorikan dampak ke market & ekonomi Indonesia: TINGGI / SEDANG / RENDAH.
  3. Jadwal harian dikirim pukul 07:00 WIB (SEND_PREVIEW). Pengingat pra-rilis tetap mati.
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
SEND_PREVIEW = os.getenv("SEND_PREVIEW", "0") == "1"    # jadwal harian 07:00 WIB
SEND_REMINDER = os.getenv("SEND_REMINDER", "0") == "1"  # pengingat sebelum rilis
PREVIEW_NOW = os.getenv("PREVIEW_NOW", "0") == "1"  # kirim jadwal hari ini sekali, lalu berhenti
TEST_SEND = os.getenv("TEST_SEND", "0") == "1"  # kirim 1 rilis terakhir sebagai tes, lalu berhenti
TEST_MATCH = os.getenv("TEST_MATCH", "").strip()  # kalau diisi, tes hanya rilis yang judulnya cocok
TEST_COUNTRY = os.getenv("TEST_COUNTRY", "").strip().upper()
TEST_BACK_DAYS = int(os.getenv("TEST_BACK_DAYS", "7") or "7")
CATCHUP_SEND = os.getenv("CATCHUP_SEND", "0") == "1"
CATCHUP_IDS = [x.strip() for x in os.getenv("CATCHUP_IDS", "").split(",") if x.strip()]
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


def _meaning(f):
    """Satu kalimat arti, tanpa menyebut pasar. Horizon yang menguraikan salurannya."""
    if f["kind"] in ("unknown", "inline"):
        return ""
    up = f["kind"] == "hot"
    typ = f["typ"]
    if typ == "unemployment":
        gloss = "Ini hitungan orang yang sedang mencari kerja dan belum mendapatkannya, bukan jumlah lowongan baru."
        why = ("Bank sentral melihat lebih sedikit alasan menurunkan bunga." if up else
               "Bank sentral melihat lebih banyak alasan mempertimbangkan bunga yang lebih rendah.")
        return f"{gloss} {why}"
    if typ == "jobs":
        gloss = "Ini jumlah orang yang baru dipekerjakan, bukan persentase yang belum mendapat kerja."
        why = ("Perusahaan masih membutuhkan pekerja, sehingga pemotongan bunga kurang mendesak." if up else
               "Perusahaan menambah pekerja lebih sedikit, sehingga pemotongan bunga lebih mudah dipertimbangkan.")
        return f"{gloss} {why}"
    if typ == "inflation":
        gloss = "Ini harga barang yang dibeli rumah tangga di toko, bukan harga di pabrik."
        why = ("Kenaikan yang lebih cepat membuat pemotongan bunga lebih sulit." if up else
               "Kenaikan yang lebih lambat memberi ruang bagi bunga yang lebih rendah.")
        return f"{gloss} {why}"
    if typ == "rate":
        return ("Yang berubah hari ini adalah harga uang itu sendiri." if up else
                "Yang turun hari ini adalah harga uang itu sendiri.")
    if typ == "growth":
        return ("Kegiatan ekonomi lebih besar daripada yang ditunggu." if up else
                "Kegiatan ekonomi lebih kecil daripada yang ditunggu.")
    return ""


def rule_analysis(ev):
    """Fakta sekali, plus arti singkat. Enam horizon menguraikan saluran pasarnya."""
    f = _facts(ev)
    parts = [_everyday(f), f"{f['title']} {f['place']} {f['actual']} ({f['paren']})."]
    meaning = _meaning(f)
    if meaning:
        parts.append(meaning)
    note = _note(f["title"])
    if note:
        parts.append(note)
    return _clip(" ".join(parts), 700)


def _pack(id_pendek, id_panjang, us_pendek, us_panjang, global_pendek, global_panjang):
    return {
        "id_pendek": id_pendek, "id_panjang": id_panjang, "us_pendek": us_pendek,
        "us_panjang": us_panjang, "global_pendek": global_pendek, "global_panjang": global_panjang,
    }


def _quiet():
    """Angka sesuai perkiraan: pasar disebut satu per satu, dengan alasan mengapa tidak bergerak."""
    return _pack(
        "Rupiah tetap di tempat karena selisih bunga terhadap dolar tidak berubah pada hari ini. Harga SBN (surat utang negara) tidak bergerak, sebab imbal hasil acuan tidak memberi alasan baru bagi pembeli. IHSG (saham Indonesia) tidak menilai ulang laba emiten ketika hasilnya sudah sesuai yang ditunggu. BI tidak mengubah suku bunga.",
        "Beberapa bulan ke depan suku bunga BI tetap pada jalur yang sudah disampaikan, karena rilis ini tidak menambah tekanan pada rupiah. Harga SBN (surat utang negara) menunggu imbal hasil luar negeri sebelum harganya bergeser. IHSG (saham Indonesia) tidak mendapat arah baru selama permintaan dan biaya dana tetap seperti sebelumnya. Rupiah mengikuti dolar, bukan kejutan yang tidak muncul hari ini.",
        "Imbal hasil surat utang Amerika Serikat tidak berubah, karena The Fed tidak memperoleh informasi baru tentang kapan bunga akan turun. Saham di sana tetap, sebab biaya pinjaman perusahaan tidak bergeser dari yang sudah ditunggu. Suku bunga kebijakan The Fed tidak diubah pada hari pengumuman. Pasar sudah memasukkan angka ini sebelum rilis keluar.",
        "Jalur suku bunga The Fed tetap ditentukan oleh data berikutnya, bukan oleh angka yang sesuai perkiraan. Saham Amerika Serikat tidak dihargai ulang selama biaya pinjaman tidak berubah. Imbal hasil surat utang jangka pendek menunggu kejutan yang benar-benar menggeser harapan. Sampai ada kejutan itu, harga saham dan bunga utang berjalan pada jalur semula.",
        "Dolar terhadap euro dan yen tidak bergerak, karena imbal hasil Amerika Serikat tidak berubah. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) tetap, sebab biaya dana global tidak lebih mahal dan tidak lebih murah. Perdagangan barang tidak bergeser pada hari pengumuman. Pesanan ekspor tidak disusun ulang oleh angka yang sudah ditunggu.",
        "Arus perdagangan barang tetap seperti sebelumnya, karena rilis ini tidak mengubah permintaan impor. Dolar tidak mencari arah baru selama selisih bunga antarnegara tenang. Selera risiko baru berubah setelah ada kejutan pada bunga atau pertumbuhan, bukan pada hari angka ini sesuai perkiraan. Pinjaman di luar Amerika Serikat tidak menjadi lebih mahal dan tidak menjadi lebih murah.",
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
    """Perekrutan: tambahan tenaga kerja, bukan jumlah orang yang mencari kerja."""
    if up:
        return _pack(
            "Perusahaan di Amerika Serikat masih menambah tenaga kerja, sehingga pasar menunda harapan penurunan bunga The Fed dan biaya dana dalam dolar tetap tinggi. Rupiah melemah karena dana lebih memilih imbal hasil di sana daripada aset dalam rupiah. Harga SBN (surat utang negara) turun ketika pembeli meminta imbal hasil yang lebih tinggi. IHSG (saham Indonesia) tertekan oleh biaya dana itu, sedangkan BI tidak mengubah suku bunga pada hari yang sama.",
            "Selama perusahaan masih merekrut, The Fed menahan pemotongan bunga dan BI mempertahankan suku bunga agar selisih terhadap dolar tidak menyempit terlalu cepat. Harga SBN (surat utang negara) baru pulih setelah biaya dana dolar mereda. IHSG (saham Indonesia) tetap lebih rendah selama pinjaman luar negeri mahal, lalu membaik ketika rupiah berhenti melemah. Tekanan itu datang dari bunga di Amerika Serikat, bukan dari lowongan di dalam negeri.",
            "Imbal hasil surat utang jangka pendek naik karena The Fed tidak memperoleh alasan menurunkan bunga ketika permintaan tenaga kerja masih kuat. Saham yang bergantung pada pinjaman murah turun, sebab biaya utang perusahaan tidak mereda pada hari rilis. Suku bunga kebijakan The Fed tidak berubah; yang bergeser adalah harapan bahwa pemotongan akan datang lebih lambat. Imbal hasil jangka pendek itu langsung menjadi patokan harga utang yang jatuh tempo dalam waktu dekat.",
            "The Fed mempertahankan suku bunga lebih lama selama perusahaan masih mampu menambah pekerja. Saham yang membutuhkan pinjaman murah tetap tertekan sampai imbal hasil surat utang jangka pendek turun. Pemotongan baru dihitung setelah permintaan tenaga kerja melemah, bukan setelah satu bulan yang kuat. Biaya utang perusahaan mengikuti jeda itu.",
            "Dolar menguat terhadap euro dan yen karena imbal hasil Amerika Serikat tetap lebih menarik ketika perusahaan di sana masih merekrut. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) menurun, sebab pinjaman global tidak menjadi lebih murah. Arus perdagangan barang tidak berubah pada hari rilis. Pesanan ekspor tidak disusun dari laporan perekrutan.",
            "Pinjaman di luar Amerika Serikat tetap mahal selama dolar kuat dan perusahaan di sana masih menambah pekerja. Arus perdagangan barang tidak bergeser hanya karena ada tambahan tenaga kerja. Selera risiko baru pulih setelah imbal hasil di Amerika Serikat turun. Dolar menahan biaya dana itu sampai permintaan perusahaan benar-benar melemah.",
        )
    return _pack(
        "Perusahaan menambah tenaga kerja lebih sedikit, sehingga peluang penurunan bunga The Fed terbuka dan biaya dana luar negeri mulai dihitung lebih rendah. Rupiah menguat ketika dana kembali melihat imbal hasil aset dalam rupiah. Harga SBN (surat utang negara) naik karena imbal hasil acuan mereda. IHSG (saham Indonesia) naik mengikuti biaya dana yang lebih ringan, sementara BI tidak mengubah suku bunga pada hari yang sama.",
        "BI memperoleh ruang menurunkan suku bunga setelah dolar mereda akibat perekrutan yang lebih sepi. Harga SBN (surat utang negara) menguat bersama rupiah selama imbal hasil acuan tetap lebih rendah. IHSG (saham Indonesia) bertahan lebih tinggi apabila pinjaman luar negeri tidak mahal lagi. Ruang itu hilang jika perusahaan di Amerika Serikat kembali merekrut dengan cepat.",
        "Imbal hasil surat utang jangka pendek turun karena pasar mulai memperhitungkan penurunan bunga The Fed. Saham yang membutuhkan pinjaman murah naik, sedangkan suku bunga kebijakan belum diubah pada hari rilis. Yang turun hari ini adalah harga utang di pasar, bukan keputusan rapat. Patokan bunga jangka pendek mengikuti harapan itu sampai rapat berikutnya.",
        "The Fed menurunkan suku bunga apabila permintaan tenaga kerja tetap lemah dalam beberapa bulan. Saham yang bergantung pada pinjaman murah bertahan lebih tinggi selama imbal hasil surat utang rendah. Jika perekrutan menguat kembali, pemotongan itu ditunda dan saham yang sensitif terhadap bunga melemah lagi. Biaya utang perusahaan mengikuti jalur itu, bukan satu bulan saja.",
        "Dolar melemah terhadap euro dan yen seiring turunnya imbal hasil Amerika Serikat. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat, karena pinjaman global dihitung lebih murah. Perdagangan barang antarnegara tidak berubah pada hari rilis. Arus pesanan tidak disusun ulang hanya karena perusahaan kurang merekrut.",
        "Pinjaman di luar Amerika Serikat menjadi lebih murah mengikuti dolar yang lebih lemah. Arus barang antarnegara tidak bergeser pada hari pengumuman. Selera risiko bertahan lebih tinggi selama imbal hasil di Amerika Serikat tidak naik kembali. Dolar baru menguat jika permintaan tenaga kerja di sana pulih.",
    )


def _us_unemployment(up):
    """Orang yang mencari kerja dan klaim mingguan, bukan tambahan tenaga kerja."""
    if up:
        return _pack(
            "Kejutan ini penting karena The Fed memotong bunga ketika banyak orang belum mendapat kerja, dan hari ini alasan itu melemah. Bunga dolar yang tetap tinggi membuat menyimpan dolar lebih untung daripada menyimpan rupiah, sehingga rupiah melemah. Pembeli SBN (surat utang negara) meminta bunga lebih tinggi agar tidak kalah dari bunga dolar, dan harga SBN turun karena harga utang bergerak berlawanan dengan bunga yang diminta. Saham di IHSG (saham Indonesia) tertekan karena laba perusahaan dihitung dengan biaya pinjaman yang lebih mahal, sedangkan BI tidak mengubah bunga hari ini sebab keputusannya tidak mengikuti satu laporan dari luar.",
            "Beberapa bulan ke depan The Fed baru memotong bunga jika pelamar kerja bertambah, jadi selisih bunga BI terhadap dolar tidak menyempit. BI mempertahankan bunganya supaya menaruh uang di rupiah tidak kalah menarik dari dolar. Harga SBN baru naik setelah bunga dolar turun dan pembeli tidak lagi meminta tambahan. IHSG pulih belakangan, ketika pinjaman luar negeri mereda, bukan pada hari angka ini diumumkan.",
            "Bunga surat utang jangka pendek, yaitu bunga bagi pembeli utang yang segera jatuh tempo, naik karena pasar menggeser tanggal pemotongan The Fed ke belakang. Saham yang hidup dari pinjaman murah turun, sebab cicilan utang perusahaan tidak menjadi lebih ringan hari ini. The Fed tidak mengubah bunga kebijakannya; yang berubah adalah harga utang di pasar, sebelum rapat. Perusahaan juga kurang terdorong menurunkan tawaran upah ketika pelamar lebih sedikit, dan itu menahan pelonggaran.",
            "Pemotongan bunga masuk jadwal hanya jika pelamar tetap banyak selama beberapa bulan dan kenaikan upah mereda. Sampai syarat itu terpenuhi, bunga utang jangka pendek tetap menjadi biaya pinjaman yang lebih mahal. Saham yang butuh utang baru tetap lebih rendah karena proyeksi labanya memakai bunga yang lebih tinggi. Satu bulan dengan pelamar yang sedikit tidak membalik jalur itu.",
            "Dolar menguat terhadap euro dan yen karena bunga di Amerika Serikat tetap lebih tinggi daripada bunga di Eropa dan Jepang. Selera risiko (minat menaruh dana pada aset yang hasilnya tidak pasti) menurun, karena pinjaman untuk membeli aset itu tidak lebih murah. Pengiriman barang tidak berubah hari ini: pesanan ekspor sudah disusun sebelum angka ini keluar. Yang bergeser adalah harga uang, bukan jumlah barang yang dikirim.",
            "Utang di luar Amerika Serikat yang dihitung dalam dolar menjadi lebih mahal selama dolar kuat. Arus barang baru menyusut jika perusahaan di sana mengurangi pesanan karena biaya dana, dan itu baru terlihat beberapa bulan kemudian. Minat pada aset yang hasilnya tidak pasti pulih setelah ada keyakinan bunga The Fed akan turun, bukan pada hari pengumuman. Selama pelamar kerja tetap sedikit, alasan untuk penurunan bunga itu belum ada.",
        )
    return _pack(
        "Kejutan ini penting karena bertambahnya orang yang belum mendapat kerja memberi The Fed alasan menimbang bunga yang lebih rendah. Bunga dolar yang dihitung lebih rendah membuat menyimpan rupiah tidak kalah menarik, sehingga rupiah menguat. Pembeli SBN (surat utang negara) menerima bunga yang lebih rendah, dan harga SBN naik karena harga utang naik ketika bunga yang diminta turun. Saham di IHSG (saham Indonesia) naik karena biaya pinjaman luar negeri yang lebih ringan memperbaiki hitungan laba, sementara BI tidak mengubah bunga hari ini.",
        "BI memperoleh ruang menurunkan bunga setelah dolar mereda, sebab selisih bunga terhadap dolar tidak lagi menekan rupiah. Harga SBN bertahan lebih tinggi selama bunga dolar tidak naik kembali. IHSG menjaga kenaikan itu apabila pinjaman luar negeri tetap lebih murah. Ruang tersebut tertutup jika pelamar kerja di Amerika Serikat berkurang lagi.",
        "Bunga surat utang jangka pendek turun karena pasar memajukan tanggal pemotongan The Fed. Saham yang membutuhkan pinjaman murah naik, sebab cicilan utang perusahaan dihitung lebih ringan. Bunga kebijakan belum diubah hari ini; yang turun adalah harga utang di pasar. Rapat berikutnya yang mengesahkan atau menunda harapan itu.",
        "The Fed menurunkan bunga apabila pelamar tetap banyak selama beberapa bulan, bukan karena satu laporan. Saham yang butuh pinjaman murah bertahan lebih tinggi selama bunga utang tetap rendah. Jika pelamar menyusut lagi, pemotongan ditunda dan saham yang peka terhadap bunga melemah. Biaya utang perusahaan mengikuti jeda keputusan itu.",
        "Dolar melemah terhadap euro dan yen karena bunga Amerika Serikat yang diharapkan lebih rendah. Selera risiko (minat menaruh dana pada aset yang hasilnya tidak pasti) meningkat, sebab pinjaman untuk membeli aset itu dihitung lebih murah. Pesanan barang tidak berubah pada hari rilis. Kontrak ekspor tidak disusun ulang oleh data pelamar kerja.",
        "Pinjaman dalam dolar di luar Amerika Serikat menjadi lebih murah mengikuti dolar yang lebih lemah. Arus barang tidak berubah hanya karena lebih banyak orang belum mendapat kerja. Minat pada aset yang hasilnya tidak pasti bertahan selama bunga di Amerika Serikat tidak naik kembali. Dolar menguat lagi jika pelamar di sana menyusut.",
    )


def _us_inflation(up):
    """Harga barang di toko: daya beli, bukan laporan tenaga kerja."""
    if up:
        return _pack(
            "Kejutan ini penting karena The Fed sulit memotong bunga ketika belanja rumah tangga tergerus harga yang naik lebih cepat. Bunga dolar yang tetap tinggi membuat rupiah kalah menarik, sehingga rupiah melemah. Pembeli SBN (surat utang negara) meminta bunga lebih tinggi sebagai ganti, dan harga SBN turun. Saham konsumen di IHSG (saham Indonesia) tertekan dari dua arah: pembeli di dalam negeri tidak langsung terpengaruh, tetapi biaya pinjaman dolar yang lebih mahal memotong hitungan laba, dan BI menahan bunga agar selisih terhadap dolar tidak menyempit.",
            "BI mempertahankan bunga selama harga di Amerika Serikat menahan The Fed dari pemotongan. Harga SBN baru naik setelah laju harga di sana mereda dan bunga dolar turun. IHSG yang menjual ke rumah tangga menunggu daya beli di dalam negeri, sedangkan saham yang berutang dalam dolar menunggu biaya dana itu mereda. Rupiah stabil kembali ketika pasar kembali menghitung bunga The Fed lebih rendah.",
            "Bunga surat utang jangka pendek naik karena bukti harga di toko menjauhkan pemotongan The Fed. Saham perusahaan yang menjual barang ke rumah tangga melemah, sebab pembeli mengurangi jumlah barang, bukan hanya karena bunga. Bunga kebijakan tidak diubah pada hari rilis; yang naik adalah harga utang di pasar. Patokan utang jangka pendek mengikuti laju harga itu sampai ada bulan yang lebih tenang.",
            "The Fed menahan bunga lebih lama sampai kenaikan harga mendekati sasaran yang sudah disampaikan. Saham yang butuh pinjaman murah tetap tertekan selama bunga utang tinggi, terpisah dari saham yang lemah karena pembeli berhemat. Pemotongan baru dihitung setelah daya beli pulih, bukan setelah satu bulan yang mahal. Biaya utang perusahaan mengikuti jeda itu.",
            "Dolar menguat terhadap euro dan yen karena bunga mengikuti harga barang yang lebih cepat naik. Selera risiko (minat menaruh dana pada aset yang hasilnya tidak pasti) menurun, sebab pinjaman global tidak lebih murah. Volume barang yang dikirim tidak berubah pada hari rilis. Pesanan ekspor tidak disusun ulang oleh laporan harga satu bulan.",
            "Barang dari Amerika Serikat yang menjadi patokan harga di negara lain menghadapi tekanan naik. Pinjaman dalam dolar tetap mahal selama dolar kuat dan harga di sana belum reda. Minat pada aset yang hasilnya tidak pasti pulih setelah pasar yakin The Fed dapat memotong bunga. Arus perdagangan bergeser lebih lambat daripada harga uang.",
        )
    return _pack(
        "Kejutan ini penting karena harga di toko yang lebih tenang mengembalikan daya beli dan memberi The Fed ruang memotong bunga. Bunga dolar yang dihitung lebih rendah membuat rupiah menguat. Pembeli SBN (surat utang negara) menerima bunga yang lebih rendah, dan harga SBN naik. Saham di IHSG (saham Indonesia) naik karena biaya pinjaman mereda, dan BI memperoleh ruang menurunkan bunga tanpa memperlebar selisih terhadap dolar.",
        "BI menurunkan bunga setelah dolar mereda karena harga di Amerika Serikat lebih tenang. Harga SBN tetap lebih tinggi selama bunga acuan tidak naik kembali. IHSG bertahan naik apabila pinjaman luar negeri tetap lebih murah. Ruang itu menyempit jika harga di toko di sana kembali naik lebih cepat.",
        "Bunga surat utang jangka pendek turun karena The Fed memperoleh alasan memotong bunga ketika belanja tidak lagi tergerus secepat sebelumnya. Saham yang menjual ke rumah tangga naik karena pembeli mampu membeli lebih banyak, terpisah dari saham yang naik hanya karena utang lebih murah. Bunga kebijakan belum diubah hari ini; yang turun adalah harga utang di pasar. Rapat berikutnya yang mengesahkan harapan itu.",
        "The Fed menurunkan bunga apabila harga tetap lebih tenang selama beberapa bulan. Saham yang butuh pinjaman murah menguat selama bunga utang tidak naik kembali. Jika harga di toko kembali naik lebih cepat, pemotongan ditunda dan saham yang bergantung pada daya beli melemah. Biaya utang perusahaan mengikuti bukti harga, bukan satu bulan yang tenang.",
        "Dolar melemah terhadap euro dan yen karena bunga turun mengikuti harga yang lebih tenang. Selera risiko (minat menaruh dana pada aset yang hasilnya tidak pasti) meningkat, sebab pinjaman global dihitung lebih murah. Volume barang yang dikirim tidak berubah pada hari rilis. Pesanan tidak disusun ulang oleh laporan harga satu bulan.",
        "Harga barang di luar Amerika Serikat yang memakai patokan dari sana menghadapi tekanan yang lebih ringan. Pinjaman dalam dolar menjadi lebih murah mengikuti dolar yang lebih lemah. Minat pada aset yang hasilnya tidak pasti bertahan selama harga di Amerika Serikat tidak naik lagi. Dolar menguat kembali jika The Fed kehilangan alasan memotong bunga.",
    )


def _us_rates(up):
    """Keputusan suku bunga: harga uang itu sendiri. Perubahan dalam basis poin ada di ringkasan."""
    if up:
        return _pack(
            "Suku bunga kebijakan Amerika Serikat naik, sehingga selisih bunga terhadap Indonesia melebar dan dana memilih dolar. Rupiah melemah karena memegang rupiah menjadi kurang menarik pada hari yang sama. Harga SBN (surat utang negara) turun ketika imbal hasil di dalam negeri harus mengikuti acuan yang lebih tinggi. IHSG (saham Indonesia) turun karena biaya dana luar negeri naik, dan BI tidak mengubah suku bunga pada hari keputusan.",
            "BI menahan suku bunga karena bunga kebijakan Amerika Serikat lebih tinggi dan pemotongan di dalam negeri akan memperlebar selisih itu. Harga SBN (surat utang negara) tetap tertekan selama imbal hasil acuan tidak turun. IHSG (saham Indonesia) baru pulih setelah biaya pinjaman dolar mereda. Rupiah mengikuti selisih bunga itu dalam beberapa bulan, bukan hanya pada hari pengumuman.",
            "The Fed menaikkan suku bunga kebijakan, dan imbal hasil surat utang jangka pendek naik mengikuti keputusan itu pada hari yang sama. Saham turun karena biaya pinjaman perusahaan menjadi lebih mahal dan laba yang diharapkan turun. Ini keputusan harga uang, bukan laporan tenaga kerja atau harga konsumen. Patokan utang jangka pendek langsung memakai bunga kebijakan yang baru.",
            "Biaya utang perusahaan Amerika Serikat tetap lebih mahal selama bunga kebijakan belum diturunkan kembali. Saham yang membutuhkan pinjaman baru pulih setelah The Fed memotong bunga. Imbal hasil surat utang jangka pendek bertahan tinggi selama keputusan itu tidak dibalik. Beberapa bulan ke depan ditentukan oleh rapat berikutnya, bukan oleh hari kenaikan ini.",
            "Dolar menguat terhadap euro dan yen karena bunga kebijakan Amerika Serikat lebih tinggi daripada bunga di sana. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) menurun, sebab pinjaman global menjadi lebih mahal. Perdagangan barang tidak berubah pada hari keputusan. Pesanan ekspor tidak disusun ulang oleh satu rapat bunga.",
            "Pinjaman di luar Amerika Serikat menjadi lebih mahal selama dolar kuat dan selisih bunga tetap lebar. Arus perdagangan barang tidak bergeser pada hari keputusan. Selera risiko baru pulih setelah The Fed memberi isyarat pemotongan. Dolar menahan biaya dana itu sampai bunga kebijakan turun.",
        )
    return _pack(
        "Suku bunga kebijakan Amerika Serikat turun, sehingga selisih bunga terhadap Indonesia menyempit dan dolar kurang menarik. Rupiah menguat karena imbal hasil aset dalam rupiah relatif lebih baik. Harga SBN (surat utang negara) naik ketika imbal hasil acuan mereda dan pembeli menerima imbal hasil yang lebih rendah. IHSG (saham Indonesia) naik karena biaya dana luar negeri turun, dan BI tidak mengubah suku bunga pada hari yang sama.",
        "BI menurunkan suku bunga setelah dolar mereda akibat pelonggaran The Fed, karena selisih bunga tidak lagi menekan rupiah. Harga SBN (surat utang negara) menguat bersama rupiah selama imbal hasil acuan tetap lebih rendah. IHSG (saham Indonesia) bertahan lebih tinggi apabila pinjaman luar negeri tidak mahal kembali. Ruang itu menyempit jika The Fed menaikkan bunga lagi.",
        "The Fed menurunkan suku bunga kebijakan, dan imbal hasil surat utang jangka pendek turun mengikuti keputusan itu. Saham naik karena biaya pinjaman perusahaan lebih murah dan proyeksi laba tidak lagi terpotong bunga. Yang berubah hari ini adalah harga uang itu sendiri. Patokan utang jangka pendek langsung memakai bunga kebijakan yang baru.",
        "Tagihan utang perusahaan menjadi lebih ringan selama bunga kebijakan tetap lebih rendah. Saham yang membutuhkan pinjaman murah bertahan lebih tinggi sampai ada kenaikan bunga kembali. Imbal hasil surat utang jangka pendek mengikuti jalur pelonggaran itu dalam beberapa bulan. Jika inflasi atau perekrutan menguat, The Fed dapat menghentikan pemotongan dan saham yang sensitif terhadap bunga melemah.",
        "Dolar melemah terhadap euro dan yen karena bunga kebijakan Amerika Serikat lebih rendah. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat, sebab pinjaman global dihitung lebih murah. Perdagangan barang tidak berubah pada hari keputusan. Arus pesanan tidak disusun ulang oleh satu rapat bunga.",
        "Pinjaman di luar Amerika Serikat menjadi lebih murah mengikuti dolar yang lebih lemah. Arus perdagangan barang tidak bergeser pada hari keputusan. Selera risiko bertahan lebih tinggi selama The Fed tidak menaikkan bunga kembali. Dolar baru menguat jika selisih bunga melebar lagi.",
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
                "Pernyataan bank sentral terdengar lebih condong menahan bunga tinggi, sehingga pasar menunda harapan pemotongan dan rupiah melemah terhadap dolar. Harga SBN (surat utang negara) turun karena imbal hasil acuan tidak mereda. IHSG (saham Indonesia) turun karena laba perusahaan dihitung dengan biaya pinjaman dolar yang lebih mahal. BI tidak mengubah suku bunga pada hari pernyataan, karena keputusan bunga ada pada rapat, bukan pada kalimat hari ini.",
                "BI tidak mengubah suku bunga sebelum rapat The Fed, sebab pernyataan saja belum menjadi keputusan. Harga SBN (surat utang negara) mengikuti dolar sampai rapat itu memberi angka bunga yang baru. IHSG (saham Indonesia) tetap tertekan selama pasar menghitung pinjaman tetap mahal. Rupiah baru stabil jika rapat berikutnya terdengar lebih lunak.",
                "Imbal hasil surat utang jangka pendek naik karena pasar mendengar bunga akan ditahan lebih lama. Saham turun sebab biaya pinjaman yang diharapkan tidak turun. Suku bunga kebijakan The Fed belum berubah pada hari pernyataan. Yang bergerak adalah harapan, dan harapan itu menunggu rapat untuk menjadi keputusan.",
                "Bunga The Fed baru berubah pada rapat berikutnya, bukan pada hari pernyataan ini. Saham mengikuti imbal hasil surat utang sampai rapat itu. Jika nada tetap ketat, biaya utang perusahaan bertahan tinggi selama beberapa bulan. Jika nada melunak, imbal hasil turun lebih dulu daripada keputusan bunga.",
                "Dolar menguat terhadap euro dan yen karena imbal hasil yang diharapkan lebih tinggi. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) menurun, sebab pinjaman global dihitung lebih mahal. Perdagangan barang tidak berubah pada hari pernyataan. Pesanan ekspor tidak disusun dari kalimat bank sentral.",
                "Dolar tetap lebih kuat jika The Fed menahan bunga pada rapat berikut. Arus perdagangan barang tidak bergeser hanya karena nada pernyataan. Selera risiko baru pulih setelah ada isyarat pemotongan. Pinjaman di luar Amerika Serikat mengikuti dolar itu sampai rapat mengubahnya.",
            )
        return _pack(
            "Pernyataan bank sentral terdengar lebih condong menurunkan bunga, sehingga rupiah menguat terhadap dolar. Harga SBN (surat utang negara) naik karena imbal hasil acuan yang diharapkan lebih rendah. IHSG (saham Indonesia) naik ketika biaya dana dolar dihitung lebih ringan. BI tidak mengubah suku bunga pada hari pernyataan.",
            "BI belum menurunkan suku bunga sebelum rapat The Fed, karena pernyataan belum menjadi keputusan. Harga SBN (surat utang negara) dan IHSG (saham Indonesia) mengikuti dolar sampai rapat itu. Jika nada lunak berlanjut, biaya pinjaman luar negeri tetap lebih murah. Jika nada berbalik, rupiah kehilangan dukungan itu.",
            "Imbal hasil surat utang jangka pendek turun karena pasar mendengar pemotongan lebih dekat. Saham naik sebab biaya pinjaman yang diharapkan lebih murah. Suku bunga kebijakan The Fed belum berubah pada hari pernyataan. Harga utang bergerak lebih dulu daripada keputusan rapat.",
            "Bunga The Fed baru berubah pada rapat berikutnya. Saham mengikuti imbal hasil surat utang sampai rapat itu mengesahkan atau menunda harapan. Beberapa bulan ke depan ditentukan oleh keputusan, bukan oleh satu pernyataan. Biaya utang perusahaan baru benar-benar turun setelah bunga kebijakan dipotong.",
            "Dolar melemah terhadap euro dan yen karena imbal hasil yang diharapkan lebih rendah. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat. Perdagangan barang tidak berubah pada hari pernyataan. Arus pesanan tidak disusun ulang oleh nada bank sentral.",
            "Dolar tetap lebih lemah jika The Fed menurunkan bunga pada rapat berikut. Arus perdagangan barang tidak bergeser hanya karena pernyataan. Selera risiko bertahan lebih tinggi selama isyarat pemotongan tidak ditarik. Pinjaman di luar Amerika Serikat menjadi lebih murah setelah keputusan itu, bukan pada hari ini.",
        )
    if origin == "US" and family == "growth":
        if up:
            return _pack(
                "Permintaan di Amerika Serikat lebih kuat, sehingga dolar menarik dana dan rupiah melemah. Harga SBN (surat utang negara) turun karena imbal hasil acuan tidak mereda. Saham ekspor di IHSG (saham Indonesia) naik karena pembeli di sana mampu memesan lebih banyak, sementara saham yang bergantung pada pinjaman murah tidak ikut. BI tidak mengubah suku bunga pada hari yang sama.",
                "BI menahan suku bunga selama dolar kuat akibat permintaan yang lebih tinggi di Amerika Serikat. Saham ekspor di IHSG (saham Indonesia) tetap lebih diuntungkan daripada saham yang butuh pinjaman murah. Harga SBN (surat utang negara) baru pulih setelah imbal hasil acuan turun. Rupiah mengikuti selisih bunga itu, bukan laba eksportir semata.",
                "Saham perusahaan yang menjual barang naik karena pesanan lebih besar, dan imbal hasil surat utang jangka pendek ikut naik karena The Fed kurang punya alasan memotong bunga. Suku bunga kebijakan belum diubah pada hari rilis. Yang naik bersamaan adalah laba penjual barang dan harga utang. Saham yang butuh pinjaman murah tidak mendapat bantuan dari kombinasi itu.",
                "The Fed menahan suku bunga selama permintaan tetap lebih kuat. Saham penjual barang bertahan lebih tinggi daripada saham yang butuh pinjaman murah. Imbal hasil surat utang jangka pendek tetap tinggi sampai ada tanda permintaan melambat. Beberapa bulan ke depan memisahkan dua jenis saham itu.",
                "Dolar menguat terhadap euro dan yen karena imbal hasil mengikuti permintaan yang lebih kuat. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) naik untuk saham barang, bukan untuk utang. Perdagangan belum berubah pada hari pengumuman, karena pesanan butuh waktu untuk tercatat. Yang bergerak hari ini adalah harga aset, bukan kapal yang sudah berangkat.",
                "Pesanan barang dari Amerika Serikat bertambah apabila permintaan ini bertahan. Dolar yang kuat tetap membuat pinjaman di luar Amerika Serikat mahal. Selera risiko pada saham barang bertahan selama pesanan tidak dibatalkan. Arus barang baru terlihat pada data perdagangan bulan berikutnya.",
            )
        return _pack(
            "Permintaan di Amerika Serikat lebih lemah, sehingga harapan bunga The Fed turun dan rupiah menguat. Harga SBN (surat utang negara) naik karena imbal hasil acuan mereda. Saham ekspor di IHSG (saham Indonesia) turun karena pesanan dari sana susut, meskipun biaya dana lebih ringan. BI tidak mengubah suku bunga pada hari yang sama.",
            "BI memperoleh ruang menurunkan suku bunga setelah dolar mereda. Saham ekspor di IHSG (saham Indonesia) tetap lebih lemah karena pesanan dari Amerika Serikat susut. Harga SBN (surat utang negara) menguat bersama rupiah. Dua arah itu berjalan bersama: biaya dana turun, tetapi pembeli luar lebih sedikit.",
            "Saham perusahaan yang menjual barang turun karena pesanan lebih kecil, dan imbal hasil surat utang jangka pendek ikut turun. The Fed belum mengubah suku bunga kebijakan pada hari rilis. Yang melemah adalah prospek laba penjual barang. Yang turun pada bunga adalah harga utang, karena pemotongan menjadi lebih mungkin.",
            "The Fed menurunkan suku bunga apabila permintaan tetap lebih lemah. Saham penjual barang tetap lebih lemah meskipun pinjaman lebih murah, karena masalahnya ada pada pesanan, bukan pada biaya bunga semata. Imbal hasil surat utang jangka pendek mengikuti pelonggaran itu. Saham yang butuh pinjaman murah mendapat bantuan yang tidak diterima oleh penjual barang.",
            "Dolar melemah terhadap euro dan yen seiring imbal hasil yang lebih rendah. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) turun untuk saham barang. Perdagangan belum berubah pada hari pengumuman. Pembatalan pesanan baru tercatat pada data bulan berikutnya.",
            "Pesanan barang dari Amerika Serikat berkurang jika permintaan ini bertahan lemah. Pinjaman di luar Amerika Serikat menjadi lebih murah mengikuti dolar yang lebih lemah. Selera risiko pada saham barang baru pulih setelah pesanan kembali. Arus barang dan harga uang tidak bergerak pada hari yang sama.",
        )
    if origin == "US" and family == "activity":
        if up:
            return _pack(
                "Survei kegiatan di Amerika Serikat lebih kuat, sehingga pasar menunda harapan pemotongan bunga dan rupiah melemah. Harga SBN (surat utang negara) turun karena imbal hasil acuan tidak mereda. IHSG (saham Indonesia) ikut turun karena biaya pinjaman dolar memotong hitungan laba. BI tidak mengubah suku bunga, karena ini gambaran satu bulan, bukan keputusan bunga.",
                "Suku bunga BI tetap, sebab survei satu bulan belum mengubah jalur bunga di dalam negeri. Harga SBN (surat utang negara) dan IHSG (saham Indonesia) mudah kembali jika angka bulan depan berlawanan arah. Rupiah mengikuti dolar hanya selama harapan bunga The Fed tidak dilonggarkan. Data bulan berikutnya lebih menentukan daripada satu survei.",
                "Saham perusahaan Amerika Serikat naik karena kegiatan yang disurvei lebih sibuk, dan imbal hasil surat utang jangka pendek ikut naik. The Fed tidak mengubah suku bunga pada hari rilis. Survei bukan keputusan bunga dan bukan jumlah orang yang mendapat kerja. Harga saham dan harga utang sama-sama bereaksi karena pasar membaca kegiatan yang lebih panas.",
                "Suku bunga The Fed tetap sampai ada data yang lebih lengkap daripada satu survei. Saham Amerika Serikat mudah kembali ke posisi semula pada angka bulan depan. Imbal hasil surat utang jangka pendek juga mudah berbalik jika survei berikutnya lebih lemah. Beberapa bulan ke depan tidak ditetapkan oleh satu bulan saja.",
                "Dolar menguat terhadap euro dan yen karena imbal hasil yang diharapkan lebih tinggi. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) menurun. Perdagangan barang tidak berubah pada hari rilis. Survei kegiatan tidak mengubah pesanan yang sudah dikirim.",
                "Dolar dan arus perdagangan barang kembali ke posisi semula jika angka berikutnya berlawanan arah. Selera risiko mengikuti pembalikan itu, bukan bertahan hanya karena satu bulan yang kuat. Pinjaman di luar Amerika Serikat menjadi lebih mahal hanya selama dolar ini bertahan. Data perdagangan bulan depan yang akan menunjukkan apakah barang ikut bergerak.",
            )
        return _pack(
            "Survei kegiatan di Amerika Serikat lebih lemah, sehingga harapan bunga The Fed turun dan rupiah menguat. Harga SBN (surat utang negara) naik karena imbal hasil acuan mereda. IHSG (saham Indonesia) ikut naik ketika biaya dana dolar lebih ringan. BI tidak mengubah suku bunga pada hari rilis.",
            "Suku bunga BI tetap, karena pelemahan satu bulan di Amerika Serikat belum menjadi alasan memotong bunga di dalam negeri. Harga SBN (surat utang negara) dan IHSG (saham Indonesia) kembali jika angka bulan depan berlawanan arah. Rupiah kehilangan dukungan jika harapan pemotongan The Fed dibatalkan. Satu survei tidak menetapkan beberapa bulan ke depan.",
            "Saham perusahaan Amerika Serikat turun karena kegiatan yang disurvei lebih sepi, dan imbal hasil surat utang jangka pendek ikut turun. The Fed tidak mengubah suku bunga. Yang melemah adalah harapan laba jangka sangat pendek. Yang turun pada bunga adalah harga utang di pasar, bukan keputusan rapat.",
            "Suku bunga The Fed tetap sampai data yang lebih lengkap menggesernya. Saham Amerika Serikat mudah kembali pada angka bulan depan. Imbal hasil surat utang jangka pendek juga mudah naik jika survei berikutnya lebih kuat. Jalur beberapa bulan tidak dikunci oleh satu bulan yang lemah.",
            "Dolar melemah terhadap euro dan yen karena imbal hasil yang diharapkan lebih rendah. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat. Perdagangan barang tidak berubah pada hari rilis. Barang yang sudah dalam perjalanan tidak berbalik karena satu survei.",
            "Dolar dan arus perdagangan barang kembali ke posisi semula jika angka berikutnya berlawanan arah. Selera risiko mengikuti pembalikan itu. Pinjaman di luar Amerika Serikat menjadi lebih murah hanya selama dolar ini lemah. Data bulan depan yang menunjukkan apakah pesanan benar-benar berkurang.",
        )
    if origin == "US" and family == "trade":
        lines = _quiet()
        lines["global_pendek"] = (
            "Dolar menguat karena neraca barang Amerika Serikat lebih baik dari yang ditunggu, sementara selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) tetap. Perdagangan barang pada hari rilis tidak berubah, sebab angka ini mencatat arus yang sudah terjadi. Imbal hasil tidak bergeser hanya karena catatan ekspor dan impor. Yang bergerak adalah harga dolar, bukan pesanan baru."
            if up else
            "Dolar melemah karena neraca barang Amerika Serikat lebih lemah dari yang ditunggu, sementara selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) tetap. Perdagangan barang pada hari rilis tidak berubah, sebab angka ini mencatat arus yang sudah terjadi. Imbal hasil tidak bergeser hanya karena catatan ekspor dan impor. Harga dolar yang bergerak, bukan kontrak yang baru ditandatangani."
        )
        lines["global_panjang"] = (
            "Dolar yang lebih kuat tidak dengan sendirinya menambah arus barang pada bulan berikutnya. Selera risiko tetap selama bunga The Fed tidak berubah karena catatan perdagangan ini. Pinjaman di luar Amerika Serikat menjadi lebih mahal hanya jika dolar ini bertahan. Pesanan baru baru terlihat pada rilis perdagangan yang akan datang."
            if up else
            "Dolar yang lebih lemah tidak dengan sendirinya mengurangi arus barang pada bulan berikutnya. Selera risiko tetap selama bunga The Fed tidak berubah karena catatan perdagangan ini. Pinjaman di luar Amerika Serikat menjadi lebih murah hanya jika dolar ini bertahan. Arah pesanan baru menunggu rilis perdagangan berikutnya."
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
                "Bunga di Jepang naik, sehingga pinjaman dalam yen yang membiayai aset luar menjadi lebih mahal dan orang menutup posisi itu. IHSG (saham Indonesia) turun karena sebagian saham dibiayai yen. Rupiah ikut melemah dan harga SBN (surat utang negara) turun ketika dana ditarik untuk menutup pinjaman. BI tidak mengubah suku bunga, karena keputusan ini bukan keputusan BI.",
                "BI tidak mengubah suku bunga hanya karena bunga yen naik. IHSG (saham Indonesia) dan harga SBN (surat utang negara) tetap lebih berat selama pinjaman yen mahal. Rupiah baru stabil setelah posisi yang dibiayai yen selesai ditutup. Tekanan beberapa bulan datang dari biaya yen, bukan dari data dalam negeri.",
                "Saham Amerika Serikat turun karena posisi yang dibiayai yen ditutup, dan imbal hasil surat utang jangka panjang naik ketika obligasi ikut dijual. The Fed tidak mengubah suku bunga. Ini aliran dana dari Jepang, bukan laporan harga atau tenaga kerja di Amerika Serikat. Harga saham dan harga utang bergerak karena posisi ditutup, bukan karena The Fed rapat.",
                "Suku bunga The Fed tetap. Saham Amerika Serikat bertahan lebih rendah selama pinjaman yen mahal. Imbal hasil jangka panjang mereda setelah penutupan posisi selesai. Beberapa bulan ke depan mengikuti biaya yen, bukan jalur bunga The Fed.",
                "Yen menguat terhadap dolar karena selisih bunga Jepang menyempit dari sisi Jepang. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) menurun ketika pinjaman yen ditutup. Perdagangan barang tidak berubah pada hari keputusan. Kapal dan pesanan tidak berbalik karena bunga yen.",
                "Pinjaman dalam yen menjadi lebih mahal bagi pembeli aset di luar Jepang. Arus perdagangan barang tidak bergeser pada hari keputusan. Selera risiko baru pulih setelah penutupan posisi mereda. Yen yang lebih kuat menahan dana itu sampai bunga Jepang turun lagi.",
            )
        return _pack(
            "Bunga di Jepang turun, sehingga pinjaman yen murah kembali dipakai untuk membeli aset luar. IHSG (saham Indonesia) naik karena dana itu mencari saham. Rupiah ikut menguat dan harga SBN (surat utang negara) naik. BI tidak mengubah suku bunga pada hari keputusan.",
            "BI tidak mengubah suku bunga hanya karena bunga yen turun. IHSG (saham Indonesia) dan harga SBN (surat utang negara) tetap lebih tinggi selama pinjaman yen murah. Rupiah mendapat aliran itu selama selisih bunga menguntungkan. Dukungan hilang jika Jepang menaikkan bunga kembali.",
            "Saham Amerika Serikat naik karena dana pinjaman yen kembali mencari aset. Imbal hasil surat utang Amerika Serikat tidak berubah besar, dan The Fed tidak mengubah suku bunga. Aliran ini tidak mengubah perhitungan bunga The Fed. Yang naik adalah harga saham, bukan bunga kebijakan.",
            "Suku bunga The Fed tetap. Saham Amerika Serikat bertahan lebih tinggi selama pinjaman yen murah. Imbal hasil tidak mengikuti yen jika The Fed tidak rapat. Beberapa bulan ke depan, saham yang dibiayai yen sensitif pada bunga Jepang, bukan hanya pada bunga The Fed.",
            "Yen melemah karena selisih bunga membuat yen kurang menarik untuk disimpan. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat. Perdagangan barang tidak berubah pada hari keputusan. Arus barang tidak disusun dari bunga yen.",
            "Pinjaman dalam yen menjadi lebih murah bagi pembeli aset di luar Jepang. Arus perdagangan barang tidak bergeser. Selera risiko bertahan lebih tinggi selama bunga Jepang tetap rendah. Yen baru menguat jika bank sentral Jepang menaikkan bunga lagi.",
        )
    if origin == "EU" and family == "rate":
        if up:
            return _pack(
                "Bunga kebijakan Eropa naik, tetapi rupiah tidak bergerak karena selisih yang dihitung pasar adalah terhadap dolar, bukan terhadap euro. Harga SBN (surat utang negara) tidak bergerak. IHSG (saham Indonesia) tidak menilai ulang laba. BI tidak mengubah suku bunga, karena BI mengikuti The Fed, bukan bank sentral Eropa.",
                "Suku bunga BI tetap pada jalur The Fed, bukan pada keputusan Eropa. Harga SBN (surat utang negara) dan IHSG (saham Indonesia) tidak mendapat arah baru selama dolar tenang. Rupiah baru terpengaruh jika euro yang lebih kuat menggeser dolar secara berarti. Beberapa bulan ke depan, acuan bunga Indonesia tetap bunga Amerika Serikat.",
                "Imbal hasil surat utang Amerika Serikat dan saham di sana tidak bergerak, karena keputusan Eropa bukan alasan bagi The Fed untuk mengubah bunga. Suku bunga kebijakan The Fed tetap. Pasar memisahkan harga uang di Eropa dari harga uang di Amerika Serikat. Saham Amerika Serikat tidak dihargai ulang oleh rapat di Frankfurt.",
                "Jalur suku bunga The Fed tetap ditentukan oleh data Amerika Serikat. Saham di sana tidak mengikuti keputusan Eropa dalam beberapa bulan. Imbal hasil surat utang Amerika Serikat baru berubah jika The Fed sendiri rapat. Keputusan Eropa tidak masuk ke perhitungan itu.",
                "Euro menguat terhadap dolar karena bunga Eropa lebih tinggi. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) menurun di aset Eropa yang sensitif terhadap bunga. Perdagangan barang tidak berubah pada hari keputusan. Pesanan antarnegara tidak disusun ulang oleh satu rapat.",
                "Bunga pinjaman di Eropa naik dan biaya utang perusahaan di sana lebih mahal. Dolar tidak harus mengikuti euro. Arus perdagangan barang tidak bergeser pada hari keputusan. Selera risiko di luar Eropa pulih atau melemah mengikuti The Fed, bukan mengikuti bank sentral Eropa.",
            )
        return _pack(
            "Bunga kebijakan Eropa turun, tetapi rupiah, harga SBN (surat utang negara), dan IHSG (saham Indonesia) tidak bergerak karena acuan dana global tetap dolar. BI tidak mengubah suku bunga. Selisih bunga Indonesia dihitung terhadap The Fed. Keputusan Eropa tidak mengubah biaya dana dolar pada hari yang sama.",
            "Suku bunga BI tetap. Harga SBN (surat utang negara) dan IHSG (saham Indonesia) tidak mendapat arah baru dari Eropa. Rupiah mengikuti dolar. Beberapa bulan ke depan, pelonggaran Eropa hanya terasa jika ia menggeser dolar secara berarti.",
            "Imbal hasil surat utang Amerika Serikat dan saham di sana tidak bergerak. The Fed tidak mengubah suku bunga karena bank sentral Eropa memotong bunga. Dua keputusan itu terpisah. Saham Amerika Serikat tidak dihargai ulang oleh biaya pinjaman di Eropa.",
            "Suku bunga The Fed tetap pada data Amerika Serikat. Saham di sana tidak mengikuti keputusan Eropa. Imbal hasil surat utang jangka pendek menunggu rapat The Fed. Pelonggaran Eropa tidak memotong bunga kebijakan di Washington.",
            "Euro melemah karena bunga Eropa lebih rendah. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) meningkat pada aset Eropa yang butuh pinjaman murah. Perdagangan barang tidak berubah pada hari keputusan. Arus pesanan tidak berbalik karena satu pemotongan.",
            "Bunga pinjaman di Eropa turun dan tagihan utang perusahaan di sana lebih ringan. Dolar tetap pada jalurnya sendiri. Arus perdagangan barang tidak bergeser pada hari keputusan. Selera risiko global tetap lebih peka pada The Fed daripada pada bank sentral Eropa.",
        )
    return _other_lines(f, up)


def _id_lines(f, up):
    family = f["family"]
    lines = _quiet()
    if family == "rate":
        lines["id_pendek"] = (
            "BI menaikkan suku bunga kebijakan, sehingga angsuran kredit lebih mahal pada hari yang sama. Harga SBN (surat utang negara) turun karena imbal hasil yang baru lebih tinggi dan harga obligasi bergerak berlawanan. Rupiah menguat karena selisih bunga terhadap dolar menjadi lebih menarik. Di IHSG (saham Indonesia), saham bank diuntungkan oleh selisih bunga simpanan, sedangkan saham properti tertekan oleh cicilan yang lebih berat."
            if up else
            "BI menurunkan suku bunga kebijakan, sehingga angsuran kredit lebih ringan. Harga SBN (surat utang negara) naik karena imbal hasil yang baru lebih rendah. Rupiah melemah karena selisih bunga terhadap dolar menyempit dan dana dolar menjadi lebih menarik. Di IHSG (saham Indonesia), saham properti naik karena cicilan lebih ringan, sedangkan saham bank tertekan oleh selisih bunga yang lebih sempit."
        )
        lines["id_panjang"] = (
            "Biaya pinjaman di dalam negeri tetap lebih mahal selama BI belum memotong bunga kembali. BI baru menurunkan suku bunga setelah rupiah dinilai aman terhadap dolar. Harga SBN (surat utang negara) mengikuti imbal hasil kebijakan itu. IHSG (saham Indonesia) yang butuh pinjaman murah baru pulih setelah cicilan turun."
            if up else
            "Biaya pinjaman di dalam negeri lebih murah selama pemotongan ini bertahan. BI tidak menurunkannya jauh di bawah The Fed, agar rupiah tidak melemah kembali. Harga SBN (surat utang negara) bertahan lebih tinggi selama imbal hasil tidak naik lagi. IHSG (saham Indonesia) yang sensitif terhadap cicilan mendapat dukungan itu, sedangkan saham bank tidak."
        )
    elif family == "prices":
        lines["id_pendek"] = (
            "Harga barang naik lebih cepat, sehingga daya beli rumah tangga tergerus dan saham konsumen di IHSG (saham Indonesia) turun. Harga SBN (surat utang negara) turun karena pasar menghitung BI lebih sulit memotong bunga. BI menahan suku bunga agar laju harga tidak lepas. Rupiah tidak memperoleh dukungan baru pada hari rilis, karena data harga ini bukan aliran dolar."
            if up else
            "Harga barang lebih tenang, sehingga daya beli pulih dan saham konsumen di IHSG (saham Indonesia) naik. Harga SBN (surat utang negara) naik karena pasar menghitung ruang pemotongan bunga. BI memperoleh ruang menurunkan suku bunga. Rupiah tidak tertekan oleh data harga ini, karena yang berubah adalah harga barang, bukan selisih bunga terhadap dolar pada hari yang sama."
        )
        lines["id_panjang"] = (
            "Daya beli menipis selama harga barang tetap naik lebih cepat, dan BI menahan suku bunga. Harga SBN (surat utang negara) baru naik setelah laju harga mereda dan bunga yang diminta pembeli turun. IHSG (saham Indonesia) yang menjual ke rumah tangga tetap lebih rendah sampai belanja pulih. Rupiah baru mendapat dukungan jika BI tidak perlu menaikkan bunga untuk menahan harga."
            if up else
            "Daya beli pulih dan BI menurunkan suku bunga apabila harga barang tetap tenang. Harga SBN (surat utang negara) tetap lebih tinggi selama imbal hasil tidak naik kembali. IHSG (saham Indonesia) yang menjual ke rumah tangga bertahan lebih tinggi. Rupiah tidak perlu dibela dengan bunga yang lebih tinggi selama laju harga jinak."
        )
    elif family == "labor":
        lines["id_pendek"] = (
            "Lebih banyak orang memperoleh penghasilan, sehingga belanja rumah tangga mengangkat saham konsumen di IHSG (saham Indonesia). Rupiah tidak bergerak pada hari ini karena data ini bukan aliran dolar. Harga SBN (surat utang negara) tidak bergerak, sebab BI tidak mendapat alasan baru untuk mengubah imbal hasil. BI tidak mengubah suku bunga pada hari rilis."
            if up else
            "Lebih sedikit orang memperoleh penghasilan, sehingga belanja rumah tangga melemah dan IHSG (saham Indonesia) turun. Rupiah ikut melemah jika pasar membaca kegiatan dalam negeri yang lebih sepi. Harga SBN (surat utang negara) turun ketika permintaan domestik yang lemah mengurangi minat pada aset rupiah. BI tidak mengubah suku bunga hari ini."
        )
        lines["id_panjang"] = (
            "BI menahan suku bunga selama penghasilan rumah tangga tetap kuat dan tidak menekan harga. Saham konsumen di IHSG (saham Indonesia) tetap lebih tinggi selama lowongan banyak. Harga SBN (surat utang negara) mengikuti rupiah, yang tidak bergerak hanya karena data tenaga kerja dalam negeri. Dukungan pada saham itu hilang jika lowongan menyusut pada bulan berikutnya."
            if up else
            "BI memperoleh alasan menurunkan suku bunga jika penghasilan rumah tangga tetap lemah dan harga barang tidak naik. Saham konsumen di IHSG (saham Indonesia) tetap lebih rendah selama lowongan sepi. Harga SBN (surat utang negara) baru pulih setelah kegiatan dalam negeri stabil. Rupiah mengikuti minat pada aset dalam negeri itu, bukan laporan tenaga kerja luar."
        )
    elif family == "growth":
        lines["id_pendek"] = (
            "Permintaan dalam negeri yang lebih kuat menaikkan prospek laba emiten, sehingga IHSG (saham Indonesia) naik pada hari pengumuman. Rupiah menguat karena aliran modal melihat kegiatan yang lebih besar dan tidak menuntut bunga yang lebih tinggi untuk tinggal. Harga SBN (surat utang negara) naik mengikuti rupiah. BI tidak mengubah suku bunga pada hari rilis, karena pertumbuhan yang lebih tinggi bukan alasan memotong atau menaikkan bunga hari itu."
            if up else
            "Emiten kehilangan dukungan permintaan dalam negeri, dan IHSG (saham Indonesia) turun karena proyeksi laba dipangkas. Aliran modal yang lebih hati-hati melemahkan rupiah. Harga SBN (surat utang negara) turun bersama rupiah. BI tidak mengubah suku bunga hari ini, karena pelonggaran butuh rapat, bukan pengumuman pertumbuhan."
        )
        lines["id_panjang"] = (
            "BI tidak menurunkan suku bunga hanya untuk menopang kegiatan yang sudah berada di atas perkiraan. IHSG (saham Indonesia) tetap lebih tinggi selama permintaan dalam negeri bertahan dan laba emiten tidak dipangkas. Harga SBN (surat utang negara) mengikuti rupiah. Jika permintaan kembali melambat, bunga BI dan harga saham sama-sama kehilangan alasan untuk tetap."
            if up else
            "BI menurunkan suku bunga untuk menopang kegiatan yang melambat, setelah rapat melihat bahwa permintaan tidak pulih sendiri. IHSG (saham Indonesia) tetap lebih rendah sampai laba emiten berhenti dipangkas. Harga SBN (surat utang negara) baru pulih setelah pertumbuhan stabil dan rupiah tidak melemah lagi. Pemotongan bunga tidak langsung mengembalikan pesanan."
        )
        lines["us_pendek"] = (
            "Imbal hasil surat utang Amerika Serikat tidak bergerak, karena kegiatan di Indonesia bukan alasan bagi The Fed untuk mengubah bunga. Saham di sana juga tidak bergerak. The Fed tidak mengubah suku bunga kebijakan. Data pertumbuhan suatu negara di Asia tidak masuk ke perhitungan bunga dolar pada hari yang sama."
            if up else
            "Saham Amerika Serikat tidak bereaksi, sebab perlambatan di Indonesia tidak mengubah perhitungan bunga The Fed. Imbal hasil surat utang di sana tetap. Suku bunga kebijakan The Fed tidak berubah. Yang lemah adalah permintaan di dalam negeri, bukan pesanan yang menentukan laba perusahaan Amerika."
        )
        lines["us_panjang"] = (
            "Jalur suku bunga The Fed tetap ditentukan oleh data Amerika Serikat, bukan oleh pertumbuhan Indonesia. Saham Amerika Serikat tidak dihargai ulang dalam beberapa bulan karena satu pengumuman di Jakarta. Imbal hasil surat utang jangka pendek menunggu inflasi dan tenaga kerja di sana. BI dapat bergerak tanpa The Fed ikut bergerak."
        )
        lines["global_pendek"] = (
            "Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) terhadap aset negara berkembang meningkat karena kegiatan di Indonesia lebih kuat. Dolar terhadap euro tidak banyak berubah, sebab ini bukan berita bunga Amerika Serikat. Perdagangan barang belum berubah pada hari pengumuman. Aliran modal bergerak lebih cepat daripada kapal barang."
            if up else
            "Minat terhadap aset negara berkembang menurun ketika kegiatan di Indonesia lebih lambat. Dolar terhadap euro hampir tidak berubah. Arus barang belum bergeser pada hari rilis, karena impor bahan baku tidak dibatalkan pada hari pengumuman. Yang turun hari ini adalah harga aset, bukan volume yang sudah dimuat."
        )
        lines["global_panjang"] = (
            "Permintaan impor bahan baku mengikuti kegiatan yang lebih kuat dalam beberapa bulan, bukan pada hari angka diumumkan. Dolar dan arus perdagangan global tidak bergeser hanya karena pengumuman ini. Selera risiko pada aset negara berkembang bertahan selama kegiatan itu tidak melambat kembali. Perdagangan baru terlihat pada data bea cukai berikutnya."
            if up else
            "Permintaan impor bahan baku menyusut mengikuti kegiatan yang lebih lambat. Dolar tidak berubah arah hanya karena pengumuman ini. Selera risiko pada aset negara berkembang baru pulih setelah kegiatan stabil. Arus barang menyesuaikan lebih lambat daripada harga saham dan harga obligasi."
        )
    elif family == "activity":
        lines["id_pendek"] = (
            "Survei bulan ini lebih sibuk, sehingga IHSG (saham Indonesia) naik karena laba jangka sangat pendek dihitung lebih baik. Rupiah tidak bergerak, sebab survei bukan aliran dolar. Harga SBN (surat utang negara) tidak bergerak, karena BI tidak mendapat alasan mengubah imbal hasil dari satu bulan survei. BI tidak mengubah suku bunga."
            if up else
            "Survei bulan ini lebih sepi, sehingga IHSG (saham Indonesia) turun karena laba jangka sangat pendek dihitung lebih rendah. Rupiah dan harga SBN (surat utang negara) tidak bergerak pada hari rilis. BI tidak mengubah suku bunga. Satu bulan survei belum menjadi alasan mengganti harga uang."
        )
        lines["id_panjang"] = (
            "Suku bunga BI tetap, karena survei satu bulan tidak mengubah jalur bunga. IHSG (saham Indonesia) kembali jika angka bulan depan berlawanan arah. Harga SBN (surat utang negara) dan rupiah baru ikut jika survei yang lemah atau kuat bertahan sampai mengubah harapan bunga. Sampai itu terjadi, yang bergerak hanya saham."
        )
    elif family == "trade":
        lines["id_pendek"] = (
            "Ekspor lebih unggul, sehingga devisa masuk dan rupiah menguat pada hari pengumuman. Harga SBN (surat utang negara) naik karena rupiah yang lebih kuat membuat imbal hasil rupiah lebih menarik. Saham ekspor di IHSG (saham Indonesia) naik karena penerimaan dolar emiten lebih besar. BI tidak mengubah suku bunga, sebab cadangan devisa yang masuk bukan keputusan bunga."
            if up else
            "Impor lebih unggul, sehingga devisa keluar dan rupiah melemah. Harga SBN (surat utang negara) turun ketika rupiah kurang menarik. Saham ekspor di IHSG (saham Indonesia) turun karena penerimaan luar negeri lebih tipis. BI tidak mengubah suku bunga pada hari rilis."
        )
        lines["id_panjang"] = (
            "BI tidak mengubah suku bunga selama devisa dari luar tetap masuk dan rupiah tidak perlu dibela dengan bunga yang lebih tinggi. Rupiah tetap lebih kuat selama arus itu berlanjut. Harga SBN (surat utang negara) mengikuti rupiah. Saham ekspor di IHSG (saham Indonesia) kehilangan dukungan jika neraca barang berbalik."
            if up else
            "BI menahan suku bunga untuk menjaga rupiah selama devisa tetap keluar. Rupiah tetap lebih lemah sampai neraca barang membaik. Harga SBN (surat utang negara) ikut tertekan. Saham ekspor di IHSG (saham Indonesia) baru pulih setelah penerimaan luar negeri kembali."
        )
    elif family == "reserves":
        lines["id_pendek"] = (
            "Cadangan dolar BI bertambah, sehingga rupiah menguat karena pasar melihat lebih banyak dolar yang dapat dipakai menjaga nilai tukar. Harga SBN (surat utang negara) tidak harus bergerak pada hari yang sama, sebab imbal hasil tidak diubah. IHSG (saham Indonesia) tidak bergerak, karena cadangan bukan laba emiten. Suku bunga BI tidak berubah."
            if up else
            "Cadangan dolar BI berkurang, sehingga rupiah melemah karena pasar melihat lebih sedikit peluru untuk menjaga nilai tukar. Harga SBN (surat utang negara) turun jika pelemahan rupiah membuat imbal hasil rupiah kurang menarik. IHSG (saham Indonesia) tidak bergerak pada hari rilis. BI menahan suku bunga agar cadangan tidak terkuras lebih cepat."
        )
        lines["id_panjang"] = (
            "BI lebih mampu menahan pelemahan rupiah selama cadangan tetap tebal. Suku bunga BI tidak perlu naik hanya untuk menjaga nilai tukar. Harga SBN (surat utang negara) mengikuti rupiah yang lebih tenang. IHSG (saham Indonesia) baru terpengaruh jika cadangan menipis sampai bunga harus naik."
            if up else
            "BI lebih sulit menjaga rupiah tanpa menaikkan suku bunga apabila cadangan terus menipis. Harga SBN (surat utang negara) tetap lebih rendah selama tekanan pada rupiah berlanjut. IHSG (saham Indonesia) tertekan hanya jika bunga akhirnya naik. Cadangan yang pulih akan mengendurkan tekanan itu."
        )
    elif family == "guidance":
        lines["id_pendek"] = (
            "Pernyataan BI terdengar lebih condong menahan bunga tinggi, sehingga rupiah menguat karena selisih bunga yang diharapkan lebih besar. Harga SBN (surat utang negara) turun karena imbal hasil yang diharapkan lebih tinggi. Saham yang butuh pinjaman murah di IHSG (saham Indonesia) turun. BI belum mengubah suku bunga pada hari pernyataan; yang berubah adalah harapan sebelum rapat."
            if up else
            "Pernyataan BI terdengar lebih condong menurunkan bunga, sehingga rupiah melemah karena selisih bunga yang diharapkan lebih sempit. Harga SBN (surat utang negara) naik karena imbal hasil yang diharapkan lebih rendah. Saham yang butuh pinjaman murah di IHSG (saham Indonesia) naik. BI belum mengubah suku bunga pada hari pernyataan."
        )
        lines["id_panjang"] = (
            "BI menahan suku bunga pada rapat berikutnya jika nada hari ini berlanjut. Harga SBN (surat utang negara) dan IHSG (saham Indonesia) mengikuti keputusan rapat itu, bukan kalimat hari ini. Rupiah tetap lebih kuat selama selisih bunga tidak dipersempit. Jika rapat berbalik melunak, ketiga harga itu bergerak lagi."
            if up else
            "BI menurunkan suku bunga pada rapat berikutnya jika nada hari ini berlanjut. Harga SBN (surat utang negara) naik lebih lanjut dan saham yang butuh pinjaman murah di IHSG (saham Indonesia) bertahan lebih tinggi setelah keputusan, bukan hanya setelah pernyataan. Rupiah perlu dijaga agar pemotongan tidak memicu pelemahan. Jika rupiah melemah terlalu jauh, pemotongan dapat ditunda."
        )
    return lines


def _cn_lines(f, up):
    family = f["family"]
    if family in ("growth", "activity"):
        if up:
            return _pack(
                "Permintaan di China lebih kuat, sehingga harga batu bara, nikel, dan sawit naik dan saham komoditas di IHSG (saham Indonesia) naik. Rupiah menguat karena ekspor bahan itu dibayar dalam dolar. Harga SBN (surat utang negara) tidak bergerak pada hari pengumuman, sebab BI tidak mengubah imbal hasil. BI tidak mengubah suku bunga; yang berubah adalah harga barang tambang, bukan harga uang di dalam negeri.",
                "Ekspor Indonesia ke China bertambah apabila permintaan ini bertahan beberapa bulan. BI tidak mengubah suku bunga hanya karena pesanan dari China. Harga SBN (surat utang negara) mengikuti rupiah jika devisa ekspor benar-benar masuk. IHSG (saham Indonesia) yang menjual komoditas kehilangan dukungan jika permintaan China berbalik.",
                "Saham Amerika Serikat yang menjual ke Asia naik karena pembeli di China dihitung lebih mampu. Imbal hasil surat utang Amerika Serikat tidak bergerak. The Fed tidak mengubah suku bunga, karena kegiatan di China bukan data harga atau tenaga kerja di Amerika Serikat. Yang naik adalah saham eksportir, bukan harga utang dolar.",
                "Suku bunga The Fed tetap pada data Amerika Serikat. Saham yang menjual ke Asia mengikuti permintaan China, bukan bunga The Fed. Imbal hasil surat utang jangka pendek tidak ikut naik hanya karena pabrik di China lebih sibuk. Beberapa bulan ke depan, saham eksportir dan bunga dolar tetap berjalan pada alasan yang berbeda.",
                "Harga logam naik, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) untuk barang naik. Dolar terhadap euro tidak bergerak, karena ini bukan berita bunga Amerika Serikat. Perdagangan barang belum berubah pada hari rilis. Kontrak tambang bergerak lebih cepat daripada kapal yang mengangkutnya.",
                "Pesanan pabrik di luar China bertambah jika permintaan ini bertahan. Dolar tidak berubah arah hanya karena kegiatan di China. Selera risiko pada barang bertahan selama harga logam tidak jatuh kembali. Arus perdagangan baru tercatat pada data bea cukai, bukan pada hari pengumuman.",
            )
        return _pack(
            "Permintaan di China lebih lemah, sehingga harga batu bara, nikel, dan sawit turun dan saham komoditas di IHSG (saham Indonesia) turun. Rupiah melemah karena devisa ekspor bahan itu dihitung lebih kecil. Harga SBN (surat utang negara) turun mengikuti rupiah. BI tidak mengubah suku bunga pada hari pengumuman.",
            "Ekspor Indonesia ke China berkurang jika permintaan tetap lemah. BI tidak mengubah suku bunga hanya karena pesanan China menyusut. Harga SBN (surat utang negara) mengikuti rupiah selama devisa lebih sedikit. IHSG (saham Indonesia) yang menjual komoditas baru pulih setelah harga barang tambang naik kembali.",
            "Saham Amerika Serikat yang menjual ke Asia turun karena pembeli di China dihitung lebih hati-hati. Imbal hasil surat utang Amerika Serikat tidak bergerak. The Fed tidak mengubah suku bunga. Pelemahan permintaan di China tidak masuk ke perhitungan bunga dolar pada hari yang sama.",
            "Suku bunga The Fed tetap. Saham yang menjual ke Asia mengikuti permintaan China, bukan jalur bunga The Fed. Imbal hasil surat utang tidak turun hanya karena pabrik di China lebih sepi. Beberapa bulan ke depan, saham eksportir dan bunga The Fed bergerak karena alasan yang berbeda.",
            "Harga logam turun, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) untuk barang turun. Dolar terhadap euro tidak bergerak. Perdagangan barang belum berubah pada hari rilis, karena muatan yang sudah berangkat tidak berbalik. Yang turun hari ini adalah harga kontrak, bukan volume yang sudah tercatat.",
            "Pesanan pabrik di luar China berkurang jika permintaan tetap lemah. Dolar tidak berubah arah. Selera risiko pada barang baru pulih setelah harga logam naik kembali. Arus perdagangan menyesuaikan pada data bulan berikutnya.",
        )
    if family == "rate":
        if up:
            return _pack(
                "Bunga di China naik, sehingga permintaan bahan tambang dihitung lebih lemah. Harga batu bara, nikel, dan sawit turun, dan sahamnya di IHSG (saham Indonesia) turun. Rupiah melemah karena devisa ekspor itu lebih kecil, dan harga SBN (surat utang negara) ikut turun. BI tidak mengubah suku bunga, karena keputusan ini bukan keputusan BI.",
                "Ekspor Indonesia ke China berkurang jika pinjaman di sana tetap lebih mahal. BI tidak mengubah suku bunga hanya karena biaya dana di China. Harga SBN (surat utang negara) mengikuti rupiah selama devisa lebih sedikit. IHSG (saham Indonesia) yang menjual komoditas baru pulih setelah permintaan China kembali.",
                "Saham Amerika Serikat yang menjual ke Asia turun karena pembeli di China menghadapi bunga yang lebih mahal. Imbal hasil surat utang Amerika Serikat tidak berubah. The Fed tidak mengubah suku bunga. Bunga di China tidak masuk ke perhitungan bunga dolar pada hari yang sama.",
                "Suku bunga The Fed tetap pada data Amerika Serikat. Saham di sana tidak mengikuti bunga China dalam beberapa bulan. Imbal hasil surat utang jangka pendek menunggu rapat The Fed. Dua harga uang itu tidak bergerak bersama hanya karena satu keputusan di China.",
                "Harga logam turun, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) untuk barang turun. Dolar terhadap euro tidak bergerak. Perdagangan barang belum berubah pada hari keputusan, karena pesanan yang sudah ada tidak dibatalkan seketika. Yang turun adalah harga kontrak tambang.",
                "Pinjaman di China menjadi lebih mahal dan proyek yang butuh kredit menunda pembelian. Arus perdagangan barang di luar China menyusut lebih lambat daripada harga logam. Selera risiko pada barang pulih setelah bunga di China turun kembali. Dolar tidak harus menguat hanya karena keputusan ini.",
            )
        return _pack(
            "Bunga di China turun, sehingga permintaan bahan tambang dihitung lebih kuat. Harga batu bara, nikel, dan sawit naik, dan sahamnya di IHSG (saham Indonesia) naik. Rupiah menguat karena devisa ekspor dihitung lebih besar, dan harga SBN (surat utang negara) ikut naik. BI tidak mengubah suku bunga pada hari keputusan.",
            "Ekspor Indonesia ke China bertambah jika pinjaman di sana tetap lebih murah. BI tidak mengubah suku bunga hanya karena biaya dana di China. Harga SBN (surat utang negara) mengikuti rupiah selama devisa masuk. IHSG (saham Indonesia) yang menjual komoditas kehilangan dukungan jika bunga China naik lagi.",
            "Saham Amerika Serikat yang menjual ke Asia naik karena kredit di China lebih murah bagi pembeli. Imbal hasil surat utang Amerika Serikat dan suku bunga The Fed tidak berubah. Keputusan bunga China bukan data yang dipakai The Fed pada hari yang sama. Yang naik adalah saham eksportir, bukan harga utang dolar.",
            "Suku bunga The Fed tetap. Saham Amerika Serikat yang menjual ke Asia mengikuti permintaan China, bukan jalur The Fed. Imbal hasil surat utang tidak turun hanya karena bunga di China lebih rendah. Beberapa bulan ke depan, kedua bank sentral itu berjalan sendiri-sendiri.",
            "Harga logam naik, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) untuk barang naik. Dolar terhadap euro tidak bergerak. Perdagangan barang belum berubah pada hari keputusan. Kontrak bertanda lebih dulu daripada muatan kapal.",
            "Pinjaman di China menjadi lebih murah dan pembelian bahan baku di luar China bertambah jika pelonggaran ini bertahan. Arus perdagangan tidak berubah pada hari keputusan. Selera risiko pada barang bertahan selama bunga tidak naik kembali. Dolar tetap pada jalurnya sendiri.",
        )
    if family == "prices":
        if up:
            return _pack(
                "Harga konsumen di China naik lebih cepat, tetapi harga SBN (surat utang negara), rupiah, dan IHSG (saham Indonesia) tidak bergerak pada hari rilis. BI tidak mengubah suku bunga. Data harga di China bukan selisih bunga dolar dan bukan laba emiten Indonesia pada hari yang sama. Pasar dalam negeri menunggu apakah harga itu mengubah permintaan barang tambang.",
                "Ekspor Indonesia ke China tidak otomatis bertambah hanya karena harga di sana lebih tinggi. Harga SBN (surat utang negara) dan suku bunga BI tetap. IHSG (saham Indonesia) baru bergerak jika permintaan bahan baku benar-benar berubah. Rupiah mengikuti devisa, bukan laporan harga China hari ini.",
                "Imbal hasil surat utang Amerika Serikat dan saham di sana tidak bergerak. The Fed tidak mengubah suku bunga karena harga konsumen China bukan data harga di Amerika Serikat. Dua laju harga itu tidak dicampur pada hari rilis. Saham Amerika Serikat tidak dihargai ulang.",
                "Suku bunga The Fed tetap pada inflasi dan tenaga kerja di Amerika Serikat. Saham di sana tidak mengikuti harga di China dalam beberapa bulan. Imbal hasil surat utang jangka pendek menunggu data domestik. Laporan harga China tidak menggeser rapat The Fed.",
                "Harga barang dari China naik, sehingga barang impor yang memakai patokan itu menghadapi tekanan naik. Dolar tidak berubah. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) tidak berubah pada hari rilis, karena ini laporan harga, bukan keputusan bunga. Perdagangan barang belum disusun ulang.",
                "Bunga di China tetap tinggi jika laju harga tidak mereda, dan pembelian kredit menjadi lebih mahal. Arus perdagangan barang baru berubah setelah harga ini bertahan beberapa bulan. Dolar tidak harus menguat. Selera risiko pada barang menunggu bukti bahwa permintaan benar-benar menyusut.",
            )
        return _pack(
            "Harga konsumen di China lebih tenang, sehingga pasar menghitung bunga di sana lebih mudah turun dan permintaan bahan tambang lebih longgar. Harga batu bara, nikel, dan sawit naik, dan sahamnya di IHSG (saham Indonesia) naik. Rupiah menguat karena devisa ekspor dihitung lebih besar. BI tidak mengubah suku bunga pada hari rilis.",
            "Ekspor Indonesia ke China bertambah jika harga yang lebih tenang membuka pelonggaran bunga di sana. Harga SBN (surat utang negara) naik mengikuti rupiah. IHSG (saham Indonesia) yang menjual komoditas bertahan lebih tinggi selama permintaan tidak berbalik. BI tidak memotong bunga hanya karena harga di China melambat.",
            "Imbal hasil surat utang Amerika Serikat dan saham di sana tidak bergerak. The Fed tidak mengubah suku bunga. Harga yang lebih tenang di China bukan bukti harga di Amerika Serikat melambat. Saham Amerika Serikat tidak mengikuti laporan itu.",
            "Suku bunga The Fed tetap pada data domestik. Saham Amerika Serikat tidak mengikuti harga di China. Imbal hasil surat utang jangka pendek tidak turun hanya karena laju harga di China lebih rendah. Beberapa bulan ke depan, The Fed membaca harga dan tenaga kerja di negaranya.",
            "Harga barang dari China turun, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) untuk barang naik karena bahan baku dihitung lebih murah. Dolar tidak bergerak. Perdagangan barang belum berubah pada hari rilis. Harga kontrak bergerak lebih dulu daripada volume yang tercatat.",
            "Bunga di China lebih mudah turun jika laju harga tetap tenang, dan pesanan barang dari China bertambah. Arus perdagangan menyesuaikan dalam beberapa bulan, bukan pada hari pengumuman. Dolar tetap. Selera risiko pada barang bertahan selama harga tidak naik kembali.",
        )
    if family == "labor":
        lines = _quiet()
        lines["id_panjang"] = (
            "Ekspor barang jadi Indonesia ke China bertambah jika lebih banyak orang di sana berpenghasilan dan belanja bertahan. Suku bunga BI, harga SBN (surat utang negara), dan rupiah tidak berubah hanya karena data tenaga kerja China. IHSG (saham Indonesia) yang menjual ke konsumen China baru bergerak setelah pesanan tercatat. Satu bulan lapangan kerja tidak mengganti harga uang di dalam negeri."
            if up else
            "Ekspor barang jadi Indonesia ke China berkurang jika lebih banyak orang di sana kehilangan penghasilan. Suku bunga BI, harga SBN (surat utang negara), dan rupiah tidak berubah hanya karena data itu. IHSG (saham Indonesia) yang menjual ke konsumen China melemah setelah pesanan menyusut, bukan pada hari pengumuman. BI tidak memotong bunga karena lapangan kerja di China."
        )
        lines["global_panjang"] = (
            "Permintaan barang konsumen dari China menguat jika penghasilan di sana bertahan. Dolar tidak berubah arah. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) pada barang konsumsi naik lebih lambat daripada harga saham. Arus perdagangan baru terlihat pada data bulan berikutnya."
            if up else
            "Permintaan barang konsumen dari China melemah jika penghasilan di sana tetap lebih rendah. Dolar tidak berubah arah. Selera risiko pada barang konsumsi menurun setelah pesanan dibatalkan, bukan pada hari angka keluar. Arus perdagangan menyesuaikan pada data berikutnya."
        )
        return lines
    if family == "guidance":
        if up:
            return _pack(
                "Pernyataan bank sentral China terdengar lebih ketat, sehingga harga batu bara, nikel, dan sawit turun dan sahamnya di IHSG (saham Indonesia) turun. Rupiah dan harga SBN (surat utang negara) tidak bergerak pada hari pernyataan, karena bunga di China belum benar-benar naik. BI tidak mengubah suku bunga. Yang berubah adalah harapan atas permintaan bahan tambang.",
                "Ekspor Indonesia ke China berkurang jika pinjaman di China ikut mengetat setelah rapat. BI tidak mengubah suku bunga hanya karena nada pernyataan di China. Harga SBN (surat utang negara) mengikuti rupiah apabila devisa benar-benar menyusut. IHSG (saham Indonesia) yang menjual komoditas baru pulih jika nada itu melunak.",
                "Saham Amerika Serikat yang menjual ke Asia turun karena pembeli China dihitung lebih hati-hati. Imbal hasil surat utang Amerika Serikat tidak berubah. The Fed tidak mengubah suku bunga. Pernyataan di China bukan data yang dipakai rapat The Fed.",
                "Suku bunga The Fed tetap. Saham Amerika Serikat tidak mengikuti pernyataan China dalam beberapa bulan. Imbal hasil surat utang menunggu data domestik. Nada bank sentral China tidak memotong atau menaikkan bunga dolar.",
                "Harga logam turun, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) untuk barang turun. Dolar tidak bergerak. Perdagangan barang tidak berubah pada hari pernyataan. Pesanan yang sudah ditandatangani tidak dibatalkan oleh satu kalimat.",
                "Bunga di China belum berubah pada hari pernyataan. Arus perdagangan baru berubah setelah bunga itu benar-benar naik. Selera risiko pada barang pulih jika rapat berikutnya lebih lunak. Dolar tetap pada selisih bunga Amerika Serikat.",
            )
        return _pack(
            "Pernyataan bank sentral China terdengar lebih lunak, sehingga harga batu bara, nikel, dan sawit naik dan sahamnya di IHSG (saham Indonesia) naik. Rupiah menguat dan harga SBN (surat utang negara) ikut naik karena devisa ekspor dihitung lebih besar. BI tidak mengubah suku bunga pada hari pernyataan. Harapan permintaan bergerak lebih dulu daripada keputusan bunga.",
            "Ekspor Indonesia ke China bertambah jika pinjaman di China benar-benar lebih murah setelah rapat. BI tidak mengubah suku bunga hanya karena nada itu. Harga SBN (surat utang negara) mengikuti rupiah. IHSG (saham Indonesia) yang menjual komoditas kehilangan dukungan jika rapat berbalik lebih ketat.",
            "Saham Amerika Serikat yang menjual ke Asia naik. Imbal hasil surat utang Amerika Serikat dan suku bunga The Fed tidak berubah. Pernyataan China bukan alasan The Fed mengubah bunga. Yang naik adalah saham eksportir, bukan harga utang dolar.",
            "Suku bunga The Fed tetap. Saham Amerika Serikat tidak mengikuti pernyataan China sebagai jalur bunga. Imbal hasil surat utang jangka pendek menunggu data Amerika Serikat. Beberapa bulan ke depan, permintaan China dan bunga The Fed tetap dua cerita yang berbeda.",
            "Harga logam naik, dan selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) untuk barang naik. Dolar tidak bergerak. Perdagangan barang tidak berubah pada hari pernyataan. Kontrak tambang bergerak lebih cepat daripada kapal.",
            "Bunga di China belum berubah pada hari pernyataan. Arus perdagangan baru berubah setelah bunga itu benar-benar turun. Selera risiko pada barang bertahan jika pelonggaran dikonfirmasi. Dolar tidak melemah hanya karena nada bank sentral China.",
        )
    return _quiet()


def _other_lines(f, up):
    family, place = f["family"], f["place"]
    lines = _quiet()
    if family in ("prices", "labor", "rate", "guidance"):
        lines["global_pendek"] = (
            f"Dolar terhadap euro tidak bergerak, karena keputusan atau data di {place} bukan berita bunga Amerika Serikat. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) global tetap. Bunga di {place} naik, dan biaya utang di sana lebih mahal pada hari rilis. Perdagangan barang tidak berubah, sebab pesanan antarnegara tidak disusun ulang oleh satu pengumuman."
            if up else
            f"Dolar terhadap euro tidak bergerak, karena data di {place} bukan berita bunga Amerika Serikat. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) global tetap. Bunga atau tekanan harga di {place} lebih rendah pada hari rilis. Perdagangan barang tidak berubah, karena arus yang sudah berjalan tidak berbalik hari ini."
        )
        lines["global_panjang"] = (
            f"Bunga di {place} tetap lebih tinggi selama beberapa bulan jika data ini tidak berbalik. Dolar, selera risiko, dan arus perdagangan barang global tidak ikut hanya karena harga uang di sana. Pinjaman di {place} lebih mahal bagi perusahaan setempat. Pasar Indonesia dan Amerika Serikat tetap membaca BI dan The Fed."
            if up else
            f"Bunga di {place} tetap lebih rendah jika pelonggaran ini bertahan. Dolar, selera risiko, dan arus perdagangan barang global tidak ikut. Pinjaman di {place} lebih murah bagi perusahaan setempat. Acuan dana global tetap bunga The Fed, bukan bunga di {place}."
        )
    elif family in ("growth", "activity"):
        lines["global_pendek"] = (
            f"Dolar tidak berubah, karena kegiatan di {place} bukan alasan mengubah bunga Amerika Serikat. Selera risiko (minat menempatkan dana pada aset yang imbal hasilnya tidak pasti) global tidak berubah pada hari rilis. Perdagangan dengan {place} belum berubah, sebab pesanan butuh waktu untuk tercatat. Yang boleh bergerak hari ini adalah harga aset yang menjual ke sana, bukan volume kapal."
        )
        lines["global_panjang"] = (
            f"Pesanan dari {place} bertambah jika kegiatan ini bertahan beberapa bulan. Dolar dan selera risiko global tidak ikut berubah hanya karena satu pengumuman. Arus barang baru terlihat pada data perdagangan berikutnya. BI dan The Fed tidak mengubah suku bunga karena permintaan di {place}."
            if up else
            f"Pesanan dari {place} berkurang jika kegiatan tetap lebih lemah. Dolar dan selera risiko global tidak ikut berubah pada hari pengumuman. Arus barang menyusut lebih lambat daripada harga saham. Suku bunga BI dan The Fed tetap pada data mereka sendiri, bukan pada {place}."
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
    return {k: _clip(_strip_fact(f, lines.get(k)), 1000) for k in MARKET_KEYS}

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
        "Kamu menjelaskan rilis ekonomi untuk pembaca pemula, dalam bahasa Indonesia yang formal dan padat.\n"
        "Formal, bukan obrolan. Jangan pakai kencang, melaju, melonjak, atau motor pertumbuhan. "
        "Pakai padanan: lebih cepat, meningkat, menurun.\n"
        "Jangan pakai stance, siklus, kanal, saluran, momentum, disinflasi, durasi, rezim, atau valuasi. "
        "Istilah yang tetap dipakai harus disebut bersama artinya pada pemakaian pertama: "
        "SBN (surat utang negara), IHSG (saham Indonesia), imbal hasil (bunga yang diterima pembeli utang), "
        "selera risiko (minat menaruh dana pada aset yang hasilnya tidak pasti).\n"
        "Gunakan HANYA angka yang diberikan. Jangan mengarang data lain. Jangan menyuruh beli atau jual.\n"
        "Balas HANYA JSON valid, tanpa markdown, dengan kunci:\n"
        "ringkasan: fakta sekali. Sebut arti sehari-hari indikator ini SATU kali "
        "(orang yang mencari kerja, harga barang di toko, bunga kebijakan). "
        "Jangan tulis akibat ke rupiah, saham, atau dolar di ringkasan.\n"
        "id_pendek, id_panjang, us_pendek, us_panjang, global_pendek, global_panjang: "
        "masing-masing TIGA sampai LIMA kalimat, di bawah 1000 karakter. Jangan dipotong.\n"
        "Padat artinya setiap kalimat menambah alasan yang belum dikatakan. "
        "Urutannya: mengapa selisih dari perkiraan itu penting, lalu bagaimana itu sampai ke harga, bunga, atau nilai tukar. "
        "Jangan memanjangkan dengan mengulang gerakan yang sama. "
        "Jangan jatuh ke pola tetap seperti 'IHSG naik, rupiah naik'.\n"
        "Indonesia menyebut rupiah, SBN, IHSG, dan BI, masing-masing dengan alasan yang berbeda. "
        "Amerika menyebut bunga surat utang, saham, dan The Fed. "
        "Global menyebut dolar, selera risiko, dan perdagangan barang.\n"
        "Keenam horizon harus berbeda bentuk kalimatnya. "
        "Tenaga kerja, pertumbuhan, harga konsumen, dan suku bunga tidak boleh memakai kalimat yang sama.\n"
        "Jangan ulang nama indikator. Jangan ulang kalimat fakta dari ringkasan. "
        "Jangan pakai biasanya, bisa, atau kalimat pengisi.\n"
        "Jangka pendek adalah hari rilis. Jangka panjang adalah beberapa bulan ke depan.\n"
        "Kalau suatu harga tidak bergerak, jelaskan sebabnya dalam kalimat utuh.\n\n"
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
            json={"model": CLAUDE_MODEL, "max_tokens": 2400,
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
    """Satu kolom per horizon supaya beberapa kalimat tidak terpotong batas 1024 karakter Discord."""
    rows = (
        ("Indonesia · jangka pendek", "id_pendek"),
        ("Indonesia · jangka panjang", "id_panjang"),
        ("Amerika · jangka pendek", "us_pendek"),
        ("Amerika · jangka panjang", "us_panjang"),
        ("Global · jangka pendek", "global_pendek"),
        ("Global · jangka panjang", "global_panjang"),
    )
    return [{"name": name, "value": _clip(read[key], 1000), "inline": False} for name, key in rows]


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


def preview_embed(evs, title, color, today=None):
    lines = []
    for ev in evs:
        level, _ = classify(ev)
        t = parse_dt(ev["date"]).astimezone(WIB)
        stamp = f"{t:%H:%M}"
        if today is not None and t.date() != today:
            stamp = f"{t.day:02d} {BULAN[t.month - 1]} {t:%H:%M}"
        lines.append(f"`{stamp}` {LEVEL_STYLE[level][0]} {FLAGS.get(ev['country'], ev['country'])} "
                     f"**{ev['title']}** — F: {fmt_val(ev, 'forecast')} | P: {fmt_val(ev, 'previous')}")
    return {"title": title, "color": color,
            "description": "\n".join(lines)[:4000] or "Tidak ada rilis penting.",
            "footer": {"text": "🔴 Tinggi · 🟠 Sedang · F = Forecast/Konsensus · P = Previous"}}


def _on_daily_slate(ev, today):
    """Rilis hari ini, plus besok pagi sebelum jam kirim jadwal (07:00 WIB)."""
    when = parse_dt(ev["date"]).astimezone(WIB)
    if when.date() == today:
        return True
    tomorrow = today + dt.timedelta(days=1)
    return when.date() == tomorrow and when.hour < PREVIEW_HOUR


def daily_preview_embeds(events, now):
    """Satu pesan: jadwal hari ini. Senin menambah ringkasan TINGGI seminggu di pesan yang sama."""
    today = now.astimezone(WIB).date()
    todays = sorted(
        (e for e in events if _on_daily_slate(e, today) and LEVEL_RANK[classify(e)[0]] >= 1),
        key=lambda e: e["date"],
    )
    embeds = [preview_embed(
        todays,
        f"📅 Jadwal rilis hari ini · {fmt_wib(now).split(' ', 1)[1][:11]}",
        0x3498DB,
        today=today,
    )]
    embeds[0]["footer"]["text"] += " · besok pagi sebelum 07:00 WIB masuk jadwal hari sebelumnya"
    if today.weekday() == 0:
        week = sorted((e for e in events
                       if 0 <= (parse_dt(e["date"]).astimezone(WIB).date() - today).days < 7
                       and classify(e)[0] == "TINGGI"), key=lambda e: e["date"])
        emb = preview_embed(week, "🗓️ Event berdampak TINGGI minggu ini", 0x9B59B6)
        dated = []
        for e, line in zip(week, emb["description"].split("\n")):
            w = parse_dt(e["date"]).astimezone(WIB)
            dated.append(f"{HARI[w.weekday()]}, {w.day:02d} {BULAN[w.month - 1]} · {line}")
        emb["description"] = "\n".join(dated) if dated else emb["description"]
        embeds.append(emb)
    return embeds


def send_daily_preview(now):
    events = fetch_events(now, back_days=1, fwd_days=7)
    embeds = daily_preview_embeds(events, now)
    discord(embeds)
    if not DRY_RUN:
        st = load_state()
        st["preview_date"] = str(now.astimezone(WIB).date())
        save_state(st)
    log.info("Jadwal harian terkirim (%d bagian)", len(embeds))


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


def _catchup_actuals():
    raw = os.getenv("CATCHUP_ACTUALS", "").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    return {str(k): float(v) for k, v in data.items()}


def catchup_send(now):
    """Kirim id yang terlewat, sekali. Lewati yang sudah ada di posted. Tidak ke Sheets."""
    overrides = _catchup_actuals()
    ids = CATCHUP_IDS or list(overrides)
    evs = fetch_events(now, back_days=4, fwd_days=1)
    by_id = {e["id"]: e for e in evs}
    st = load_state()
    posted = set(str(x) for x in st.get("posted") or [])
    min_rank = LEVEL_RANK.get(MIN_ALERT, 1)
    for eid in ids:
        ev = by_id.get(str(eid))
        if ev is None:
            log.warning("CATCHUP: id %s tidak ketemu", eid)
            continue
        if str(eid) in posted:
            log.info("CATCHUP: %s %s sudah terkirim, dilewati", ev.get("country"), ev.get("title"))
            continue
        if ev.get("actual") is None and str(eid) in overrides:
            ev["actual"] = overrides[str(eid)]
        if ev.get("actual") is None:
            log.warning("CATCHUP: %s belum punya actual", ev.get("title"))
            continue
        if LEVEL_RANK[classify(ev)[0]] < min_rank:
            log.info("CATCHUP: %s di bawah ambang, dilewati", ev.get("title"))
            continue
        log.info("CATCHUP: mengirim %s %s (%s)", ev.get("country"), ev.get("title"), ev.get("date"))
        badge = move_badge(ev)
        log.info("CATCHUP gerak: %s", badge["text"] if badge else "tanpa pembanding")
        embed = release_embed(ev, evs, now)
        log.info("CATCHUP ringkasan: %s", embed.get("description"))
        for field in embed.get("fields") or []:
            if "jangka" in (field.get("name") or ""):
                log.info("CATCHUP %s: %s", field["name"], (field.get("value") or "").replace("\n", " | "))
        png = make_chart(ev, now)
        if png:
            embed["image"] = {"url": "attachment://chart.png"}
        discord([embed], png=png)
        posted.add(str(eid))
    if not DRY_RUN:
        st["posted"] = list(posted)
        save_state(st)


def test_send(now):
    """Kirim satu rilis berdampak SEDANG/TINGGI. State tidak diubah.
    TEST_MATCH membatasi judul, supaya tes tidak mengirim rilis lain."""
    min_rank = LEVEL_RANK.get(MIN_ALERT, 1)
    evs = fetch_events(now, back_days=max(TEST_BACK_DAYS, 1), fwd_days=0)
    done = sorted((e for e in evs if e.get("actual") is not None and parse_dt(e["date"]) <= now
                   and LEVEL_RANK[classify(e)[0]] >= min_rank), key=lambda e: e["date"])
    if TEST_COUNTRY:
        done = [e for e in done if (e.get("country") or "").upper() == TEST_COUNTRY]
    if not done:
        log.warning("TES: tidak ada rilis berdampak sedang/tinggi dalam %s hari terakhir", TEST_BACK_DAYS)
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
        if field.get("name") in ("Actual",) or "jangka" in (field.get("name") or ""):
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
    if CATCHUP_SEND:
        catchup_send(now)
        return
    if PREVIEW_NOW:
        send_daily_preview(now)
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

    # 1) jadwal harian, sekali, pada run pertama pukul 07:00 WIB atau sesudahnya
    today = now.astimezone(WIB).date()
    if SEND_PREVIEW and now.astimezone(WIB).hour >= PREVIEW_HOUR and st.get("preview_date") != str(today):
        discord(daily_preview_embeds(events, now))
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
