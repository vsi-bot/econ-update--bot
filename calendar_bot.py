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
# Rilis dibaca sebagai sinyal siklus dan reaksi kebijakan, lalu dipetakan ke
# tiga pasar (Indonesia, AS, global) dan dua horizon (sesi rilis, siklus).
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


def _regime(family):
    return {
        "rate": "suku bunga", "prices": "inflasi", "labor": "pasar tenaga kerja",
        "growth": "pertumbuhan", "activity": "momentum aktivitas", "trade": "neraca eksternal",
        "reserves": "cadangan devisa", "guidance": "nada kebijakan",
    }.get(family, "data ini")


def _heat(typ, direction):
    """hot = ekonomi/kebijakan lebih ketat atau lebih kuat; unemployment dibalik."""
    if typ == "unemployment":
        return "cold" if direction > 0 else "hot"
    return "hot" if direction > 0 else "cold"


def _facts(ev):
    title = ev.get("title") or "Rilis"
    typ = indicator_type(title)
    direction, diff, base = surprise(ev)
    if diff is None:
        kind, paren = "unknown", "tanpa konsensus maupun previous"
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


def _qualifier(title, typ):
    t = (title or "").lower()
    if any(k in t for k in ("hourly", "wage", "earnings")):
        return "Ini upah, yaitu biaya tenaga kerja, bukan indeks harga barang."
    if "ppi" in t or "producer price" in t:
        return "Ini harga produsen, biasanya bergerak di depan harga konsumen."
    if "pce" in t and "core" in t:
        return "Ini PCE inti, ukuran harga yang paling ditimbang The Fed."
    if "pce" in t:
        return "Ini PCE, harga pengeluaran yang diawasi The Fed, bukan CPI headline."
    if "core" in t or "inti" in t:
        return "Ini inflasi inti, tanpa pangan dan energi yang berisik."
    if typ == "inflation" and any(k in t for k in ("inflation", "cpi", "price index")):
        return "Ini harga yang masih memuat komponen berisik, bukan ukuran inti."
    if "adp" in t:
        return "ADP adalah hitungan swasta sebelum payroll resmi, bobotnya di bawah NFP."
    if "jobless" in t or "claims" in t:
        return "Klaim mingguan berisik. Ini bukan tingkat pengangguran bulanan."
    if any(k in t for k in ("nonfarm", "non-farm", "non farm", "payroll")):
        return "Payroll adalah laju perekrutan, bukan tingkat pengangguran."
    if "minutes" in t or "notulen" in t:
        return "Notulen memberi nada kebijakan, bukan data aktivitas baru."
    if typ == "growth" and "qoq" in t and "yoy" not in t:
        return "Ini laju kuartalan, bukan pertumbuhan setahun penuh."
    return ""


def _signal(f):
    """Arti makro cetakan ini, bukan kalimat menang/kalah dari konsensus."""
    title, place, actor = f["title"], f["place"], f["actor"]
    level = f"Actual {f['actual']} ({f['paren']})."
    loc, kind, family = f"{title} {place}", f["kind"], f["family"]
    if kind == "unknown":
        return (f"{loc} hanya terbaca sebagai level. {level} Tanpa konsensus atau previous, "
                f"arah {actor} tidak bisa dinilai dari selisih.")
    if kind == "inline":
        return (f"{loc} mengonfirmasi apa yang sudah dihargai, bukan memecahkannya. {level} "
                f"Sinyalnya ada di level {_regime(family)} itu: {actor} tidak mendapat alasan baru "
                "untuk berbelok hari ini.")
    up = kind == "hot"
    if family == "rate":
        return ((f"Keputusan bunga {place} lebih ketat dari yang dihargai. {level} "
                 f"Ini sinyal langsung dari {actor}, bukan data yang masih bisa direvisi.") if up else
                (f"Keputusan bunga {place} lebih longgar dari yang dihargai. {level} "
                 f"{actor} menggeser harga uang sekarang, sebelum data aktivitas menyusul."))
    if family == "prices":
        return ((f"{loc} menunjukkan tekanan harga yang belum reda. {level} "
                 f"{actor} lebih sulit berbelok longgar selama pola ini belum balik.") if up else
                (f"{loc} menunjukkan disinflasi yang berlanjut. {level} "
                 f"Ruang {actor} untuk lebih longgar lebih lebar daripada sebelum rilis."))
    if family == "labor":
        cause = _labor_cause(f, up)
        return (f"{loc}: {cause}. {level} " +
                (f"Selama pola ini bertahan, {actor} lebih sabar untuk melonggar." if up else
                 f"Jika pola ini bertahan, {actor} mendapat alasan untuk lebih longgar."))
    if family == "growth":
        return ((f"{loc} menunjukkan permintaan yang lebih kuat dari gambaran sebelumnya. {level} "
                 f"Aktivitas lebih sehat, tetapi {actor} juga punya lebih sedikit alasan untuk memotong bunga.") if up else
                (f"{loc} menunjukkan permintaan yang lebih lemah dari gambaran sebelumnya. {level} "
                 f"Aktivitas melambat, dan {actor} mendapat alasan lebih besar untuk menahan atau melonggarkan stance."))
    if family == "activity":
        return ((f"{loc} adalah momentum bulanan yang lebih kuat, bukan reset siklus seperti GDP. {level} "
                 f"{actor} baru menggeser stance jika momentum ini bertahan, bukan karena satu bulan.") if up else
                (f"{loc} adalah momentum bulanan yang melemah, dan satu bulan masih bisa balik. {level} "
                 f"{actor} menunggu konfirmasi sebelum mengubah stance."))
    if family == "trade":
        return ((f"{loc} menunjukkan neraca eksternal yang lebih kuat. {level} "
                 f"Pasokan valas lebih tebal. Ini bukan sinyal inflasi dan tidak otomatis mengubah {actor}.") if up else
                (f"{loc} menunjukkan neraca eksternal yang lebih lemah. {level} "
                 "Pasokan valas menipis. Komposisi harga versus volume tidak terlihat di angka agregat ini."))
    if family == "reserves":
        return ((f"{loc} menunjukkan bantalan devisa yang bertambah. {level} "
                 "Kapasitas menjaga mata uang naik. Ini neraca stok, bukan pertumbuhan.") if up else
                (f"{loc} menunjukkan devisa yang terpakai. {level} "
                 "Itu sering berarti intervensi atau arus keluar, bukan serta-merta pelemahan pertumbuhan."))
    if family == "guidance":
        return ((f"{loc} bernada lebih ketat dari yang dihargai. {level} "
                 f"Yang berubah adalah reaksi {actor}, bukan data aktivitas baru.") if up else
                (f"{loc} bernada lebih longgar dari yang dihargai. {level} "
                 f"Yang berubah adalah reaksi {actor}, bukan data aktivitas baru."))
    return f"{loc}. {level}"


def _bound(f):
    """Apa yang satu rilis ini belum buktikan, plus kanal lintas pasar yang dominan."""
    origin, family, kind = f["origin"], f["family"], f["kind"]
    actor, place = f["actor"], f["place"]
    if kind == "unknown":
        return "Sebelum ada pembanding, arah kebijakan belum bisa disebut lebih ketat atau lebih longgar."
    if kind == "inline":
        return "Yang masih mengikat harga aset adalah levelnya, bukan selisih nol pada hari ini."
    if origin == "US" and family == "growth":
        return ("Dua kanal berjalan bersamaan: permintaan dan jalur bunga The Fed tidak bergerak ke arah yang sama. "
                "Sesi pertama biasanya mengikuti dolar, bukan cerita ekspor.")
    if origin == "US" and family == "activity":
        return "Satu bulan aktivitas belum mengubah siklus. Kanal ke rupiah hari ini ada, tetapi BI dan The Fed menunggu konfirmasi."
    if origin == "US" and family == "trade":
        return "Neraca dagang AS bukan input utama reaksi The Fed, jadi efeknya ke rupiah kecil dibanding inflasi, tenaga kerja, atau keputusan bunga."
    if origin == "US" and family == "reserves":
        return "Cadangan devisa AS bukan alat The Fed dan bukan sinyal permintaan untuk ekspor Indonesia."
    if origin == "US" and family in ("prices", "labor", "rate", "guidance"):
        return ("Kanal ke Indonesia berjalan lewat dolar dan imbal hasil AS yang lebih tinggi, bukan lewat rapat BI hari ini."
                if kind == "hot" else
                "Kanal ke Indonesia berjalan lewat dolar dan imbal hasil AS yang lebih longgar, sehingga ruang BI ikut lebih lega.")
    if origin == "ID":
        return "Ini cetakan domestik: yang bisa berubah adalah reaksi BI dan valas di dalam negeri. The Fed tidak memakai angka Indonesia."
    if origin == "CN" and family == "trade":
        return ("Headline neraca China tidak memberitahu apakah impornya kuat. Impor yang lemah buruk untuk komoditas RI "
                "meski surplus China terlihat sehat.")
    if origin == "CN":
        return "Kanal ke Indonesia berjalan lewat permintaan komoditas dan ekspor, bukan lewat keputusan BI hari ini. The Fed tidak bergeser karena data China."
    if origin == "JP":
        return (f"Kanal yang khas adalah biaya pinjaman yen. BI dan The Fed tidak mengubah stance hanya karena {actor}, "
                "tetapi aset risiko bisa goyang kalau pendanaan yen menyempit atau melebar.")
    if origin == "EU":
        return f"Kanal globalnya adalah imbal hasil euro. The Fed tidak berpindah karena {actor}, tetapi durasi global bisa ikut. BI lebih terikat pada The Fed."
    return f"Data {place} mengubah kondisi finansial lewat mata uang dan imbal hasil negara itu. Efek ke BI dan The Fed tidak langsung."


def rule_analysis(ev):
    f = _facts(ev)
    parts = [_signal(f)]
    qual = _qualifier(f["title"], f["typ"])
    if qual:
        parts.append(qual)
    bound = _bound(f)
    if bound:
        parts.append(bound)
    return _clip(" ".join(parts), 1200)


def _pack(id_pendek, id_panjang, us_pendek, us_panjang, global_pendek, global_panjang):
    return {
        "id_pendek": id_pendek, "id_panjang": id_panjang, "us_pendek": us_pendek,
        "us_panjang": us_panjang, "global_pendek": global_pendek, "global_panjang": global_panjang,
    }


def _prefixed(f, text):
    return f"{f['title']} {f['place']}: {text}"


def _ensure_print(f, text):
    text = " ".join((text or "").split())
    title, place = f["title"], f["place"]
    if not text:
        text = _move(f["family"], f["kind"])
    if (title and title not in text) or (place and place not in text):
        text = f"{title} {place}: {text}"
    return text


def _move(family, kind):
    if kind == "inline":
        return "sesuai pembanding, jadi bukan kejutan arah"
    if kind == "unknown":
        return "tanpa konsensus atau previous, jadi arahnya belum terbaca"
    up = kind == "hot"
    return {
        "rate": "bunganya lebih ketat dari yang dihargai" if up else "bunganya lebih longgar dari yang dihargai",
        "prices": "tekanan harganya lebih panas dari yang dihargai" if up else "tekanan harganya lebih dingin dari yang dihargai",
        "labor": "pasar kerjanya lebih ketat dari yang dihargai" if up else "pasar kerjanya lebih longgar dari yang dihargai",
        "growth": "permintaannya lebih kuat dari yang dihargai" if up else "permintaannya lebih lemah dari yang dihargai",
        "activity": "momentum bulanannya lebih kuat dari yang dihargai" if up else "momentum bulanannya lebih lemah dari yang dihargai",
        "trade": "neraca eksternalnya lebih kuat dari yang dihargai" if up else "neraca eksternalnya lebih lemah dari yang dihargai",
        "reserves": "stok devisanya naik dibanding pembanding" if up else "stok devisanya turun dibanding pembanding",
        "guidance": "nadanya lebih keras dari yang dihargai" if up else "nadanya lebih lunak dari yang dihargai",
    }.get(family, "lebih kuat dari yang dihargai" if up else "lebih lemah dari yang dihargai")


def _price_cause(f, tight):
    t = f["title"].lower()
    if any(k in t for k in ("hourly", "wage", "earnings")):
        return "biaya upah masih menekan" if tight else "tekanan upah mereda"
    if "ppi" in t or "producer" in t:
        return ("harga produsen masih tinggi dan biasanya mendahului harga konsumen" if tight else
                "harga produsen mendingin, sering mendahului harga konsumen")
    if "pce" in t:
        return ("ukuran harga yang ditimbang The Fed masih panas" if tight else
                "ukuran harga yang ditimbang The Fed mendingin")
    if "core" in t or "inti" in t:
        return "inflasi inti belum turun" if tight else "inflasi inti turun, bukan cuma harga pangan atau energi"
    return "tekanan harga belum reda" if tight else "disinflasi berlanjut"


def _labor_cause(f, tight):
    t = f["title"].lower()
    if "jobless" in t or "claims" in t:
        return ("klaim mingguan menunjukkan pasar kerja masih ketat" if tight else
                "klaim mingguan menunjukkan kelonggaran, meski angkanya berisik")
    if "adp" in t:
        return ("perekrutan swasta ADP masih kuat, bobotnya di bawah payroll resmi" if tight else
                "perekrutan swasta ADP melemah, bobotnya di bawah payroll resmi")
    if any(k in t for k in ("payroll", "nonfarm", "non-farm", "non farm")):
        return "laju perekrutan masih kuat" if tight else "laju perekrutan melemah"
    return ("tingkat pengangguran menunjukkan pasar kerja yang masih ketat" if tight else
            "tingkat pengangguran menunjukkan kelonggaran tenaga kerja")


def _us_conditions(f, cause, tight, long_id, long_us, long_gl, us_tail):
    pre = f"{f['title']} {f['place']}"
    if tight:
        id_s = f"{pre}: {cause}. Dolar dan imbal hasil AS naik di hari rilis, sehingga rupiah dan harga SBN melemah pada sesi yang sama."
        us_s = f"{pre}: {cause}. Fed funds dan UST 2 tahun dihargai lebih tinggi hari itu. {us_tail}"
        gl_s = f"{pre}: {cause}. Dolar naik terhadap mata uang utama dan EM pada sesi rilis."
    else:
        id_s = f"{pre}: {cause}. Dolar dan imbal hasil AS turun di hari rilis, sehingga rupiah dan harga SBN mendapat ruang pada sesi yang sama."
        us_s = f"{pre}: {cause}. Fed funds dan UST 2 tahun dihargai lebih rendah hari itu. {us_tail}"
        gl_s = f"{pre}: {cause}. Dolar melemah; emas dan mata uang EM menguat pada sesi yang sama."
    return _pack(id_s, long_id, us_s, long_us, gl_s, long_gl)


def _lines_us(f):
    family, tight = f["family"], f["kind"] == "hot"
    title, place = f["title"], f["place"]
    pre = f"{title} {place}"
    if family == "prices":
        cause = _price_cause(f, tight)
        if tight:
            return _us_conditions(
                f, cause, True,
                f"{pre}: selama harga ini belum mendingin, The Fed menahan bunga lebih lama dan BI kehilangan ruang memotong lebih dulu.",
                f"{pre}: satu cetakan belum membatalkan disinflasi, tetapi rangkaian yang panas membuat jalur pemangkasan Fed lebih dangkal.",
                f"{pre}: kalau tekanan harga bertahan, selisih imbal hasil AS terhadap negara lain melebar dan modal condong ke aset dolar.",
                "Saham AS yang durasinya panjang paling sensitif terhadap imbal hasil riil.",
            )
        return _us_conditions(
            f, cause, False,
            f"{pre}: disinflasi yang berlanjut membuka ruang BI, karena tekanan dolar dan imbal hasil mereda.",
            f"{pre}: The Fed baru benar-benar melonggar jika pendinginan ini bertahan, bukan karena satu bulan.",
            f"{pre}: selisih imbal hasil AS menyempit jika disinflasi berlanjut, dan arus ke EM menjadi lebih murah.",
            "Saham AS berdurasi panjang yang paling lega.",
        )
    if family == "labor":
        cause = _labor_cause(f, tight)
        if tight:
            return _us_conditions(
                f, cause, True,
                f"{pre}: pasar kerja yang tetap ketat menahan upah, jadi BI juga kehilangan ruang memotong lebih dulu lewat kanal valas.",
                f"{pre}: selama kelonggaran tenaga kerja belum muncul, pemangkasan Fed tetap dangkal meski inflasi barang melambat.",
                f"{pre}: siklus global tidak longgar serempak kalau tenaga kerja AS tetap ketat. Imbal hasil AS tetap menjadi jangkar.",
                "Kanalnya upah dan perekrutan, bukan harga barang.",
            )
        return _us_conditions(
            f, cause, False,
            f"{pre}: kelonggaran tenaga kerja AS meredakan tekanan dolar, dan ruang BI untuk lebih longgar ikut terbuka.",
            f"{pre}: The Fed baru menggeser siklus jika kelonggaran ini terlihat lagi, bukan dari satu laporan.",
            f"{pre}: imbal hasil AS yang lebih rendah, kalau bertahan, membuat pendanaan EM lebih murah.",
            "Kanalnya kelonggaran tenaga kerja, bukan kejutan harga barang.",
        )
    if family == "rate":
        cause = ("keputusan bunga lebih ketat dari harga yang sudah terpasang" if tight else
                 "keputusan bunga lebih longgar dari harga yang sudah terpasang")
        if tight:
            return _us_conditions(
                f, cause, True,
                f"{pre}: stance The Fed yang lebih tinggi menahan selisih bunga. BI tidak bisa jauh lebih longgar tanpa menekan rupiah.",
                f"{pre}: jalur bunga AS sudah bergeser. Ini perubahan harga uang, bukan data yang masih menunggu revisi.",
                f"{pre}: kondisi finansial global lebih ketat selama selisih bunga AS lebar. Arus ke EM, termasuk Indonesia, lebih mahal.",
                "Yang bergerak lebih dulu adalah kurva pendek, sebelum data bulan depan.",
            )
        return _us_conditions(
            f, cause, False,
            f"{pre}: pelonggaran Fed yang lebih dalam memberi BI ruang, selama rupiah tidak dipakai untuk alasan lain.",
            f"{pre}: jalur bunga AS turun pada keputusan itu sendiri. Data bulan depan menguji, bukan menggantikan, pergeseran ini.",
            f"{pre}: selisih bunga AS yang menyempit membuat pendanaan global lebih murah jika keputusan ini tidak dibatalkan pertemuan berikut.",
            "Kurva pendek direvisi pada hari keputusan, bukan menunggu CPI berikutnya.",
        )
    if family == "guidance":
        cause = ("nada komunikasi lebih keras, tanpa data aktivitas baru" if tight else
                 "nada komunikasi lebih lunak, tanpa data aktivitas baru")
        if tight:
            return _us_conditions(
                f, cause, True,
                f"{pre}: nada yang lebih keras hanya menahan BI jika rilis harga dan tenaga kerja berikutnya tidak membantahnya.",
                f"{pre}: komunikasi mengubah harga Fed funds sekarang, tetapi siklusnya batal jika data berikutnya lebih dingin.",
                f"{pre}: dolar global mengikuti narasi The Fed pada hari ini. Siklusnya baru berubah jika nada ini diulang.",
                "Ini kanal narasi, bukan kanal data baru.",
            )
        return _us_conditions(
            f, cause, False,
            f"{pre}: nada yang lebih lunak memberi ruang ke rupiah hanya jika data berikutnya tidak memaksa The Fed berbalik.",
            f"{pre}: satu komunikasi yang lunak belum memindahkan siklus. Data harga dan tenaga kerja tetap wasitnya.",
            f"{pre}: pelonggaran global yang mengikuti narasi ini baru nyata jika The Fed mengulanginya, bukan dari satu teks.",
            "Ini kanal narasi. Data berikutnya masih bisa membatalkannya.",
        )
    return _lines_spillover(f)


def _lines_growth_us(f):
    if f["kind"] == "hot":
        return _pack(
            _prefixed(f, "dua sinyal berjalan bersamaan hari ini. Permintaan global lebih sehat, tetapi dolar bisa tetap kuat karena The Fed punya lebih sedikit alasan untuk memotong. Rupiah biasanya mengikuti kanal dolar pada sesi pertama."),
            _prefixed(f, "ekspor RI ke AS lebih tertolong, tetapi biaya dana global tidak cepat turun. BI menghadapi permintaan yang lebih baik dan selisih bunga yang tetap lebar."),
            _prefixed(f, "saham siklikal AS dapat dukungan pertumbuhan, sementara imbal hasil jangka pendek naik karena pemangkasan Fed mundur. Dua harga itu bisa bergerak berlawanan pada hari yang sama."),
            _prefixed(f, "pertumbuhan yang bertahan tanpa pemotongan bunga yang dalam menjadi lebih mungkin. Satu kuartal masih bisa direvisi."),
            _prefixed(f, "selera risiko global naik, tetapi dolar tidak otomatis melemah. Komoditas siklikal dapat dukungan permintaan."),
            _prefixed(f, "perdagangan dunia lebih tertolong oleh permintaan AS yang kuat daripada oleh pelonggaran The Fed yang tertunda."),
        )
    return _pack(
        _prefixed(f, "permintaan yang lebih lemah menekan selera risiko hari ini, tetapi membuka peluang The Fed lebih longgar. Rupiah bisa tertekan dulu, lalu lega kalau dolar melemah."),
        _prefixed(f, "pasar ekspor RI ke AS menyusut, sementara ruang BI memotong baru terbuka kalau dolar benar-benar turun."),
        _prefixed(f, "saham siklikal AS kehilangan dukungan, sementara imbal hasil AS turun karena pasar menambah peluang pelonggaran Fed."),
        _prefixed(f, "siklus bergeser ke pertumbuhan lebih rendah dan jalur bunga lebih rendah. Keduanya perlu kuartal berikutnya, karena GDP sering direvisi."),
        _prefixed(f, "aset risiko global melemah pada hari rilis. Dolar bisa ikut melemah kalau kanal Fed mengalahkan kanal penghindaran risiko."),
        _prefixed(f, "perdagangan dunia kehilangan permintaan AS. Pelonggaran Fed, kalau menyusul, baru terasa setelah beberapa pertemuan."),
    )


def _lines_activity_us(f):
    if f["kind"] == "hot":
        return _pack(
            _prefixed(f, "ini momentum bulanan, bukan GDP. Dolar dan imbal hasil bisa naik hari ini dan rupiah menanggungnya, tetapi BI tidak mengubah bunga karena satu survei."),
            _prefixed(f, "The Fed dan BI baru menggeser siklus jika momentum ini bertahan beberapa bulan."),
            _prefixed(f, "harga Fed funds hanya bergeser di margin. Saham siklikal AS bereaksi, lalu mudah balik bulan depan."),
            _prefixed(f, "stance The Fed tidak berpindah karena satu bulan aktivitas. Yang diuji adalah apakah ini mengonfirmasi inflasi dan tenaga kerja."),
            _prefixed(f, "selera risiko global bergerak tipis. Ini bukan keputusan bunga."),
            _prefixed(f, "siklus global tidak berbelok sebelum momentum ini terlihat di pertumbuhan dan harga."),
        )
    return _pack(
        _prefixed(f, "momentum bulanan melemah. Dolar bisa sedikit turun hari ini, tetapi satu bulan yang lemah belum memberi BI ruang memotong."),
        _prefixed(f, "siklus belum berbalik. Perlu beberapa survei searah sebelum The Fed atau BI menganggap permintaan AS benar-benar melambat."),
        _prefixed(f, "saham siklikal AS tertekan tipis dan imbal hasil bisa turun di margin, mudah terhapus rilis berikutnya."),
        _prefixed(f, "jalur Fed tidak pindah karena satu bulan aktivitas yang lemah."),
        _prefixed(f, "tidak ada perubahan rezim dolar. Pergerakan global hari ini kecil."),
        _prefixed(f, "perdagangan dunia tidak kehilangan permintaan AS sebelum kelemahan ini masuk ke GDP."),
    )


def _lines_trade_us(f):
    if f["kind"] == "hot":
        return _pack(
            _prefixed(f, "neraca yang lebih kuat hanya sedikit menambah dukungan dolar lewat arus, bukan lewat The Fed. Rupiah hampir tidak bergerak."),
            _prefixed(f, "neraca dagang AS tidak mengubah ruang BI. Efek jangka panjangnya kecil dibanding inflasi, tenaga kerja, atau keputusan bunga."),
            _prefixed(f, "ini bukan input reaksi The Fed. UST dan saham AS tidak punya alasan besar untuk dihargai ulang."),
            _prefixed(f, "surplus atau defisit dagang AS mencerminkan permintaan dan dolar yang sudah terjadi, bukan tuas kebijakan baru."),
            _prefixed(f, "arus perdagangan tidak berbalik pada hari rilis. Pergerakan valas, kalau ada, kecil."),
            _prefixed(f, "komposisi harga versus volume tidak ada di angka ini, jadi ini bukan sinyal komoditas."),
        )
    return _pack(
        _prefixed(f, "neraca yang lebih lemah sedikit mengurangi dukungan dolar lewat arus. Bukan alasan rupiah bergerak besar."),
        _prefixed(f, "BI tidak mendapat ruang baru dari neraca dagang AS."),
        _prefixed(f, "The Fed tidak melonggar karena neraca dagang. UST tidak harus dihargai ulang."),
        _prefixed(f, "defisit yang melebar biasanya cermin dolar dan permintaan AS yang sudah kuat, bukan perubahan mandat Fed."),
        _prefixed(f, "tidak ada rezim valas baru pada hari rilis."),
        _prefixed(f, "pelebaran defisit bukan kejutan komoditas. Komposisinya tidak ada di headline."),
    )


def _lines_reserves_us(f):
    return _pack(
        _prefixed(f, "cadangan devisa AS bukan alat The Fed. Tidak ada alasan rupiah bergerak besar pada hari rilis ini."),
        _prefixed(f, "stok devisa AS tidak mengubah ruang BI dan bukan permintaan untuk ekspor Indonesia."),
        _prefixed(f, "Fed funds dan UST tidak dihargai ulang oleh cadangan devisa."),
        _prefixed(f, "siklus bunga AS tidak memakai angka cadangan sebagai input."),
        _prefixed(f, "dolar global tidak berbelok karena perubahan stok devisa AS."),
        _prefixed(f, "likuiditas dunia tidak berubah hanya karena satu catatan cadangan."),
    )


def _id_pack(f, id_s, id_l, us_s, gl_s, gl_l):
    pre = f"{f['title']} {f['place']}"
    return _pack(
        f"{pre}: {id_s}",
        f"{pre}: {id_l}",
        f"{pre}: {us_s}",
        f"{pre}: jalur bunga AS tidak bergeser. The Fed tidak memakai data Indonesia sebagai input.",
        f"{pre}: {gl_s}",
        f"{pre}: {gl_l}",
    )


def _lines_id(f):
    family, up = f["family"], f["kind"] == "hot"
    if family == "rate" and up:
        return _id_pack(
            f,
            "bunga lebih ketat menopang rupiah lewat selisih bunga pada hari yang sama. Saham properti tertekan; bank dapat margin tetapi volume kredit lebih berat.",
            "stance BI yang lebih tinggi menahan kredit dan valuasi durasi domestik sampai inflasi atau rupiah mengizinkan pelonggaran.",
            "Fed funds tidak direvisi. Keputusan BI bukan data The Fed.",
            "dampaknya lokal. Mata uang EM lain tidak otomatis ikut menguat.",
            "baru relevan secara global jika ini bagian dari pengetatan EM yang lebih luas, bukan sebagai penggerak dolar.",
        )
    if family == "rate":
        return _id_pack(
            f,
            "bunga lebih longgar menekan rupiah karena selisih bunga menyempit, dan menolong saham yang sensitif bunga pada hari yang sama.",
            "siklus kredit domestik lebih longgar, dengan batas stabilitas rupiah. BI tidak bisa jauh di bawah The Fed tanpa menekan valas.",
            "tidak ada revisi Fed funds pada hari keputusan BI.",
            "dolar global tidak berbelok. Yang bergerak adalah rupiah terhadap dolar.",
            "arus ke Indonesia menjadi lebih murah hanya jika pelonggaran ini tidak membuat pelemahan rupiah yang menetap.",
        )
    if family == "prices" and up:
        return _id_pack(
            f,
            "harga yang lebih panas membuat pasar mengharapkan BI lebih ketat. Harga SBN tertekan hari itu dan saham konsumen ikut; rupiah bisa tertahan kalau respons BI dianggap kredibel.",
            "daya beli dan ruang pemangkasan BI sama-sama menyempit selama inflasi ini tinggal di atas sasaran.",
            "inflasi Indonesia tidak masuk mandat The Fed, jadi UST tidak dihargai ulang hari ini.",
            "harga global tidak berpindah. Efeknya di kurva domestik.",
            "baru menjadi cerita global jika inflasi EM secara bersama menahan bank sentral EM untuk tidak memotong.",
        )
    if family == "prices":
        return _id_pack(
            f,
            "harga yang lebih dingin membuka harapan BI lebih longgar. SBN dan saham sensitif bunga dapat dukungan; rupiah aman hanya kalau selisih bunga tidak kolaps.",
            "ruang pemangkasan BI melebar selama kejutan dingin ini bukan sekali cetak.",
            "The Fed tidak membaca inflasi Indonesia. Tidak ada kanal ke UST hari ini.",
            "tidak ada impuls dolar global. Pergerakannya di aset rupiah.",
            "siklus harga dunia tidak berbelok karena inflasi Indonesia semata.",
        )
    if family == "labor" and up:
        return _id_pack(
            f,
            "pasar kerja yang lebih ketat menopang konsumsi domestik. BI kurang alasan memotong, dan rupiah tidak otomatis menguat hanya karena data tenaga kerja.",
            "upah yang bertahan menahan pelonggaran BI, tetapi ini bukan kejutan valas sebesar keputusan bunga.",
            "tenaga kerja Indonesia tidak menggeser payroll atau Fed funds.",
            "pasar kerja Indonesia tidak menggerakkan dolar.",
            "kontribusinya ke siklus global kecil. Yang diubah adalah permintaan domestik.",
        )
    if family == "labor":
        return _id_pack(
            f,
            "kelonggaran tenaga kerja menekan cerita konsumsi. Rupiah bisa ikut lemah jika pasar membaca pertumbuhan yang melambat, bukan karena The Fed.",
            "BI lebih punya alasan melonggar jika kelonggaran ini bertahan, dengan risiko rupiah kalau pertumbuhan menjadi cerita utamanya.",
            "tidak ada revisi pasar tenaga kerja AS.",
            "dolar tidak berbelok karena pengangguran Indonesia.",
            "siklus global tidak ditentukan oleh pasar kerja Indonesia.",
        )
    if family == "growth" and up:
        return _id_pack(
            f,
            "pertumbuhan yang lebih kuat menolong IHSG dan kepercayaan ke rupiah pada hari rilis, lewat ekonomi domestik bukan lewat The Fed.",
            "siklus laba dan kredit lebih sehat. BI kurang terdesak menstimulus, jadi pelonggaran tidak otomatis menyusul pertumbuhan yang kuat.",
            "GDP Indonesia tidak menggeser saham AS atau UST hari ini.",
            "selera risiko global hampir tidak bergerak. Indonesia penerima harga komoditas, bukan penentu siklus dunia pada satu rilis.",
            "kontribusi ke perdagangan dunia lewat impor Indonesia, dan itu baru berarti jika bertahan beberapa kuartal.",
        )
    if family == "growth":
        return _id_pack(
            f,
            "pertumbuhan yang lebih lemah menekan IHSG dan membuat rupiah lebih rentan pada hari rilis.",
            "tekanan agar BI atau pemerintah menstimulus naik, tetapi stimulus yang terlalu longgar bisa menekan rupiah lagi.",
            "perlambatan Indonesia tidak merevisi The Fed.",
            "permintaan impor Indonesia yang lebih lemah adalah kanal globalnya, kecil pada satu kuartal.",
            "siklus dunia tidak berbalik karena satu GDP Indonesia.",
        )
    if family == "activity" and up:
        return _id_pack(
            f,
            "ini momentum bulanan, bukan reset siklus. Konsumsi domestik terlihat lebih kuat hari ini; BI belum harus mengubah bunga.",
            "BI baru menggeser stance jika momentum ini bertahan beberapa bulan, bukan karena satu survei.",
            "survei aktivitas Indonesia tidak masuk harga Fed funds.",
            "satu PMI atau ritel Indonesia tidak menggerakkan harga global.",
            "baru berarti secara global jika kemudian terlihat di GDP dan impor.",
        )
    if family == "activity":
        return _id_pack(
            f,
            "momentum bulanan melemah. Konsumsi domestik terlihat lebih lemah hari ini, tanpa mengubah bunga BI serta-merta.",
            "satu survei yang lemah belum memaksa stimulus. BI menunggu apakah ini masuk ke pertumbuhan.",
            "survei Indonesia tidak merevisi pasar AS.",
            "harga global tidak bergerak karena satu survei aktivitas Indonesia.",
            "dunia baru kena jika kelemahan ini menekan impor Indonesia selama beberapa kuartal.",
        )
    if family == "trade" and up:
        return _id_pack(
            f,
            "neraca yang lebih kuat menambah pasokan valas, jadi rupiah dapat dukungan pada hari rilis. Bunga BI tidak otomatis berubah.",
            "bantalan eksternal yang lebih tebal mengurangi kebutuhan BI membakar cadangan. Angka agregat tidak memberitahu apakah sumbernya harga atau volume.",
            "neraca dagang Indonesia tidak menggeser The Fed.",
            "satu neraca tidak membalik harga komoditas global. Yang bergerak hari ini adalah rupiah.",
            "kalau kekuatannya dari harga komoditas, bukan volume, komposisi itu tidak tertulis di angka agregat.",
        )
    if family == "trade":
        return _id_pack(
            f,
            "neraca yang lebih lemah menipiskan pasokan valas. Rupiah lebih rentan pada hari rilis, dan BI bisa lebih berat menjaga mata uang.",
            "dukungan eksternal menipis. Tanpa komposisi harga versus volume, belum ketahuan apakah permintaan dunia atau harga komoditas yang melemah.",
            "The Fed tidak bergerak karena neraca dagang Indonesia.",
            "harga komoditas global tidak ditentukan oleh satu neraca Indonesia.",
            "kalau kelemahannya dari volume ekspor, permintaan dunia yang lesu baru terlihat setelah beberapa bulan, bukan hari ini.",
        )
    if family == "reserves" and up:
        return _id_pack(
            f,
            "stok devisa yang naik menebalkan amunisi BI menjaga rupiah. Ini bukan sinyal pertumbuhan, dan tidak harus mengangkat IHSG hari itu.",
            "bantalan yang lebih tebal memperpanjang kemampuan stabilisasi. Itu stok, bukan permintaan baru.",
            "cadangan devisa Indonesia tidak masuk reaksi The Fed.",
            "pasar global tidak dihargai ulang. Efeknya pada kredibilitas pertahanan rupiah.",
            "stok ini relevan secara global hanya jika pasar menilai Indonesia lebih tahan terhadap penguatan dolar.",
        )
    if family == "reserves":
        return _id_pack(
            f,
            "stok yang turun sering berarti BI sedang menahan rupiah atau ada arus keluar. Dukungan ke mata uang bisa semu karena amunisinya berkurang.",
            "bantalan yang menipis membatasi berapa lama BI bisa menstabilkan rupiah tanpa mengorbankan stance bunga.",
            "The Fed tidak bergerak karena cadangan Indonesia.",
            "tidak ada guncangan dolar global. Ini neraca stok domestik.",
            "ketahanan terhadap penguatan dolar menurun jika stok devisa terus turun.",
        )
    if family == "guidance" and up:
        return _id_pack(
            f,
            "yang berubah adalah nada BI yang lebih ketat, bukan data aktivitas baru. Rupiah dapat dukungan narasi, saham sensitif bunga tertekan.",
            "nada ini hanya mengubah siklus jika keputusan bunga berikutnya mengikutinya.",
            "komunikasi BI tidak merevisi Fed funds.",
            "dolar global tidak berbelok karena nada BI.",
            "satu komunikasi domestik tidak mengubah siklus pendanaan global.",
        )
    if family == "guidance":
        return _id_pack(
            f,
            "nada BI yang lebih lunak menekan rupiah jika pasar percaya pelonggaran akan menyusul, dan menolong saham sensitif bunga.",
            "siklus baru berubah jika BI benar-benar memotong, bukan dari nada semata.",
            "The Fed tidak melunak karena komunikasi BI.",
            "tidak ada impuls dolar global dari nada BI.",
            "pendanaan global tidak berubah karena satu komunikasi Indonesia.",
        )
    return _id_pack(
        f, _move(family, f["kind"]) + ". Efek utamanya domestik, lewat BI dan rupiah, bukan lewat The Fed.",
        "stance BI hanya bergeser jika pola ini bertahan, bukan karena satu rilis.",
        "pasar AS tidak merevisi Fed funds karena data Indonesia.",
        "dolar global tidak berbelok karena rilis Indonesia.",
        "siklus global tidak ditentukan oleh satu data Indonesia.",
    )


def _cn_pack(f, id_s, id_l, us_s, us_l, gl_s, gl_l):
    pre = f"{f['title']} {f['place']}"
    return _pack(
        f"{pre}: {id_s}", f"{pre}: {id_l}", f"{pre}: {us_s}",
        f"{pre}: {us_l}", f"{pre}: {gl_s}", f"{pre}: {gl_l}",
    )


def _lines_cn(f):
    family, up = f["family"], f["kind"] == "hot"
    if family == "growth" and up:
        return _cn_pack(
            f,
            "pertumbuhan yang lebih kuat menaikkan harapan permintaan batu bara, nikel, dan CPO. Rupiah dan saham komoditas RI dapat dukungan lewat harga komoditas, bukan karena BI mengubah bunga.",
            "siklus ekspor RI ke China lebih sehat jika ini bukan sekali cetak. BI bukan aktor utama.",
            "saham AS yang terekspos Asia bisa menguat tipis. Fed funds tidak direvisi.",
            "The Fed tidak mengubah bunga karena pertumbuhan China. Efek ke AS adalah pendapatan global, bukan mandat harga.",
            "logam industri dan mata uang komoditas menguat pada hari rilis. Dolar tidak harus melemah.",
            "manufaktur global mendapat permintaan dari China. Itu siklus perdagangan, bukan siklus The Fed.",
        )
    if family == "growth":
        return _cn_pack(
            f,
            "pertumbuhan yang lebih lemah memangkas harapan permintaan komoditas RI. Rupiah kehilangan dukungan harga komoditas pada hari yang sama.",
            "ekspor ke China tetap berat jika perlambatan ini berlanjut beberapa kuartal.",
            "The Fed tidak melonggar karena GDP China. Saham AS yang terekspos Asia bisa tertekan tipis.",
            "jalur bunga AS tidak mengikuti pertumbuhan China.",
            "logam dan energi kehilangan dukungan pada hari rilis.",
            "siklus manufaktur global kehilangan permintaan dari China.",
        )
    if family == "rate" and up:
        return _cn_pack(
            f,
            "pengetatan memangkas harapan stimulus. Harga komoditas dan saham ekspor RI tertekan hari itu.",
            "permintaan China yang lebih mahal menahan volume ekspor RI lebih lama.",
            "The Fed tidak ikut ketat. Saham AS paling kena lewat sentimen Asia yang tipis.",
            "jalur bunga AS tidak mengikuti PBoC.",
            "logam dan energi kehilangan dukungan pada hari keputusan yang lebih ketat.",
            "manufaktur global kehilangan satu sumber pelonggaran.",
        )
    if family == "rate":
        return _cn_pack(
            f,
            "bunga yang lebih rendah adalah stimulus. Harapan permintaan komoditas dan ekspor RI naik di hari yang sama, belum berupa volume yang sudah terjadi.",
            "ekspor RI baru tertolong jika kredit China menyusul pemotongan ini, bukan hanya suku bunganya yang turun.",
            "likuiditas global sedikit lebih mudah, tetapi Fed funds tidak direvisi karena PBoC.",
            "The Fed tidak mengikuti pemotongan China.",
            "logam dan mata uang komoditas menguat karena harapan, bukan karena data permintaan yang sudah tercetak.",
            "stimulus yang bertahan baru mengubah siklus barang dunia. Satu pemotongan belum cukup untuk menyebut siklus baru.",
        )
    if family == "prices" and up:
        return _cn_pack(
            f,
            "harga yang lebih panas membuat PBoC lebih hati-hati menstimulus. Harapan untuk ekspor RI meredup hari itu. Belum ketahuan apakah ini tekanan biaya atau permintaan.",
            "kalau China menahan stimulus karena inflasi, permintaan komoditas RI tetap lemah lebih lama.",
            "inflasi China bukan data The Fed. UST tidak direvisi.",
            "mandat The Fed tidak memakai harga China.",
            "stimulus PBoC menjadi kurang pasti hari ini. Harga barang global tidak otomatis naik atau turun sebelum komposisinya jelas.",
            "eksportir komoditas kehilangan cerita pelonggaran China selama inflasi ini menjadi alasan menunda stimulus.",
        )
    if family == "prices":
        return _cn_pack(
            f,
            "harga yang lebih dingin membuka ruang stimulus PBoC. Saham komoditas RI bisa naik karena harapan, belum karena volume ekspor yang sudah terjadi.",
            "pelonggaran China baru menolong ekspor RI jika stimulus lolos ke aktivitas.",
            "The Fed tidak bergeser. Efek ke saham AS yang terekspos Asia tipis.",
            "efek jangka panjang ke AS lewat harga barang impor, dan itu lambat. Bunga Fed tidak ikut.",
            "harapan permintaan barang global, terutama logam dan energi, naik tipis.",
            "disinflasi China yang berkelanjutan menekan harga barang dunia dan membantu inflasi impor negara lain, dengan jeda.",
        )
    if family == "trade" and up:
        return _cn_pack(
            f,
            "surplus yang lebih besar sering berarti ekspor China deras atau impornya lemah. Impor yang lemah buruk untuk komoditas RI; ekspor China yang deras menambah persaingan di pasar ketiga.",
            "kalau surplus ini datang dari impor yang lesu, siklus ekspor RI ke China tetap lemah meski headline neraca China terlihat sehat.",
            "The Fed tidak bergerak. Saham AS yang bersaing dengan barang China bisa tertekan tipis, bukan karena bunga.",
            "jalur The Fed tidak berubah. Efek jangka panjang adalah harga barang impor AS, dan itu lambat.",
            "harga barang manufaktur mendapat tekanan pasokan China. Komoditas menunggu komposisi impor, yang tidak ada di headline.",
            "surplus yang menetap menggeser pangsa perdagangan, bukan stance The Fed atau BI.",
        )
    if family == "trade":
        return _cn_pack(
            f,
            "surplus yang lebih kecil bisa berarti impor China lebih kuat, dan itu baik untuk komoditas RI, atau ekspor China yang melemah. Headline tidak memisahkan keduanya.",
            "kalau penyebabnya impor yang pulih, ekspor RI baru tertolong setelah beberapa bulan, bukan pada hari rilis.",
            "tidak ada revisi Fed funds. Efek ke AS kecil pada hari ini.",
            "The Fed tidak mengubah jalur karena neraca China.",
            "pasar barang global tidak mendapat arah yang bersih sebelum komposisi impor versus ekspor ketahuan.",
            "pangsa perdagangan baru bergeser jika pola neraca ini bertahan, bukan karena satu bulan.",
        )
    if family == "activity" and up:
        return _cn_pack(
            f,
            "ini survei bulanan, bukan GDP. Harapan permintaan batu bara, nikel, dan CPO naik, tetapi satu bulan belum mengubah BI.",
            "ekspor RI baru benar-benar tertolong jika survei ini masuk ke produksi dan impor China.",
            "Fed funds tidak direvisi oleh PMI atau aktivitas China.",
            "The Fed tidak mengubah siklus karena momentum bulanan China.",
            "logam bisa menguat tipis pada hari survei. Itu harapan, bukan volume yang sudah dikapalkan.",
            "siklus manufaktur global tidak berbelok sebelum GDP dan impor China mengonfirmasi survei ini.",
        )
    if family == "activity":
        return _cn_pack(
            f,
            "survei bulanan yang lebih lemah memangkas harapan komoditas RI hari ini. Bukan keputusan BI.",
            "satu bulan yang lemah belum membatalkan siklus ekspor, tetapi menghapus cerita percepatan China.",
            "pasar AS tidak merevisi bunga. Saham yang terekspos Asia bisa tertekan tipis.",
            "jalur The Fed independen dari survei China.",
            "logam dan energi kehilangan dukungan harapan pada hari rilis.",
            "manufaktur global kehilangan momentum Asia hanya jika kelemahan survei ini berulang.",
        )
    if family == "labor" and up:
        return _cn_pack(
            f,
            "pasar kerja China yang lebih ketat menopang konsumsi di China. Efek ke komoditas RI lebih kecil daripada data industri atau GDP.",
            "konsumsi China yang bertahan baru menolong ekspor barang jadi RI, bukan otomatis batu bara dan nikel.",
            "tenaga kerja China tidak masuk payroll AS atau Fed funds.",
            "The Fed tidak memakai pasar kerja China.",
            "dampak globalnya lewat konsumsi barang, kecil pada satu rilis.",
            "siklus dunia tidak berbelok karena pengangguran China semata.",
        )
    if family == "labor":
        return _cn_pack(
            f,
            "kelonggaran tenaga kerja China menekan harapan konsumsi. Kanal ke RI lebih lemah daripada perlambatan industri.",
            "BI tidak bereaksi. Yang panjang adalah daya beli China untuk barang impor.",
            "tidak ada revisi pasar tenaga kerja AS.",
            "jalur bunga AS tidak mengikuti pasar kerja China.",
            "selera risiko Asia bisa sedikit turun, tanpa rezim dolar yang baru.",
            "efek globalnya baru nyata jika konsumsi China yang lemah menekan impor selama beberapa kuartal.",
        )
    if family == "reserves" and up:
        return _cn_pack(
            f,
            "stok devisa China yang naik bukan sinyal permintaan komoditas. Efek ke rupiah kecil pada hari rilis.",
            "bantalan China yang lebih tebal tidak otomatis menambah ekspor RI.",
            "cadangan China tidak masuk reaksi The Fed.",
            "siklus bunga AS tidak berubah.",
            "likuiditas global tidak berbalik karena satu catatan stok.",
            "stok devisa bukan substitusi dari permintaan impor China.",
        )
    if family == "reserves":
        return _cn_pack(
            f,
            "devisa China yang terpakai bukan serta-merta permintaan komoditas yang jatuh. Jangan membaca stok sebagai volume ekspor RI.",
            "ketahanan China terhadap arus keluar naik atau turun terpisah dari kebutuhan BI.",
            "The Fed tidak bergerak karena cadangan China.",
            "tidak ada konsekuensi jalur bunga AS.",
            "pasar valas global paling kena jika pasar membaca tekanan pada yuan, dan itu tidak otomatis.",
            "satu bulan penurunan stok tidak mengubah siklus perdagangan.",
        )
    if family == "guidance" and up:
        return _cn_pack(
            f,
            "nada PBoC yang lebih keras menunda cerita stimulus. Komoditas RI kehilangan harapan hari itu; volume belum berubah.",
            "harapan ekspor baru mati jika nada keras ini diikuti suku bunga atau kredit yang lebih ketat.",
            "komunikasi PBoC tidak merevisi Fed funds.",
            "The Fed tidak mengeras karena nada China.",
            "logam kehilangan dukungan harapan pada hari komunikasi.",
            "siklus barang dunia hanya berubah jika kebijakan China benar-benar mengetat setelah nada ini.",
        )
    if family == "guidance":
        return _cn_pack(
            f,
            "nada yang lebih lunak menghidupkan harapan stimulus. Harga komoditas bisa naik sebelum aktivitas China menyusul.",
            "ekspor RI baru tertolong jika kredit dan impor China mengikuti nada ini.",
            "The Fed tidak melunak karena komunikasi PBoC.",
            "jalur bunga AS independen dari nada China.",
            "mata uang komoditas menguat karena harapan, bukan karena data permintaan yang sudah ada.",
            "satu komunikasi belum menjadi siklus pelonggaran China.",
        )
    return _cn_pack(
        f,
        _move(family, f["kind"]) + ". Kanal ke RI, kalau ada, lewat permintaan China, bukan lewat BI.",
        "efeknya baru berarti jika pola ini bertahan beberapa rilis.",
        "The Fed tidak merevisi bunga karena data China.",
        "jalur AS tidak mengikuti PBoC atau data China.",
        "harga global tidak otomatis berbalik pada satu rilis China.",
        "siklus barang dunia hanya berubah jika data China yang searah berulang.",
    )


def _lines_jp_rate(f):
    if f["kind"] == "hot":
        return _pack(
            _prefixed(f, "keputusan yang lebih ketat mengangkat yen dan bisa melepas pinjaman berbiaya yen pada hari yang sama. IHSG dan rupiah tertekan lewat selera risiko, bukan karena BI mengubah bunga."),
            _prefixed(f, "pendanaan yen yang lebih mahal menahan arus ke EM selama BoJ tidak kembali longgar. BI hanya terkena lewat kanal itu."),
            _prefixed(f, "harga UST bisa melemah jika posisi berbiaya yen ditutup hari ini. Bukan karena The Fed mengubah stance."),
            _prefixed(f, "The Fed tidak mengikuti BoJ. Yang tersisa adalah premi jangka panjang kalau pinjaman yen benar-benar menyempit."),
            _prefixed(f, "yen menguat dan aset risiko global goyang pada hari keputusan BoJ."),
            _prefixed(f, "satu sumber pendanaan murah global menipis. Siklusnya baru berubah jika BoJ meneruskan pengetatan, bukan pada satu rapat."),
        )
    return _pack(
        _prefixed(f, "keputusan yang lebih longgar melemahkan yen dan menghidupkan kembali pinjaman berbiaya yen. Aset risiko, termasuk IHSG, dapat dukungan sentimen hari itu."),
        _prefixed(f, "arus ke EM tertolong hanya selama BoJ tidak dipaksa mengetatkan lagi oleh yen yang terlalu lemah."),
        _prefixed(f, "The Fed tidak melonggar karena BoJ. UST paling kena lewat pembukaan kembali posisi yen, kecil pada satu hari."),
        _prefixed(f, "jalur bunga AS independen. Yen yang terlalu lemah justru bisa memaksa BoJ berbalik nanti."),
        _prefixed(f, "yen melemah dan selera risiko global naik pada hari keputusan yang lebih longgar."),
        _prefixed(f, "pendanaan yen yang murah kembali menambah likuiditas global, sampai inflasi Jepang membantahnya."),
    )


def _lines_eu_rate(f):
    if f["kind"] == "hot":
        return _pack(
            _prefixed(f, "bunga yang lebih ketat mengangkat imbal hasil euro dan euro itu sendiri. Rupiah hanya kena imbal hasil global, kecil dibanding keputusan The Fed."),
            _prefixed(f, "pelonggaran negara maju yang serempak mundur. BI tetap lebih terikat pada The Fed daripada pada ECB."),
            _prefixed(f, "harga UST bisa melemah karena durasi global ikut, tetapi Fed funds tidak direvisi hanya karena ECB."),
            _prefixed(f, "The Fed tidak mengikuti ECB. Yang panjang adalah korelasi imbal hasil, bukan mandat."),
            _prefixed(f, "euro menguat, imbal hasil euro naik, dan durasi global melemah pada hari keputusan."),
            _prefixed(f, "siklus pelonggaran Eropa mundur. Dolar tidak otomatis melemah kalau The Fed masih lebih ketat dari ECB."),
        )
    return _pack(
        _prefixed(f, "bunga yang lebih longgar menekan imbal hasil euro. Rupiah mendapat ruang tipis lewat imbal hasil global, bukan lewat BI."),
        _prefixed(f, "pelonggaran Eropa yang lebih dalam tidak otomatis memberi BI ruang. Batas BI tetap The Fed dan rupiah."),
        _prefixed(f, "Fed funds tidak direvisi. UST paling kena lewat korelasi durasi yang kecil."),
        _prefixed(f, "The Fed tidak ikut memotong karena ECB."),
        _prefixed(f, "euro melemah dan imbal hasil euro turun pada hari keputusan."),
        _prefixed(f, "siklus pelonggaran Eropa lebih dalam. Itu belum pelonggaran global yang serempak selama The Fed belum ikut."),
    )


def _lines_spillover(f):
    """Negara selain AS/Indonesia/China, atau jenis yang tidak punya peta khusus."""
    pre = f"{f['title']} {f['place']}"
    actor, family, kind = f["actor"], f["family"], f["kind"]
    up = kind == "hot"
    move = _move(family, kind)
    if family in ("prices", "labor", "rate", "guidance"):
        id_s = (f"{move}. Imbal hasil global {'naik' if up else 'turun'} hari ini dan rupiah menanggungnya, "
                f"tetapi {actor} bukan The Fed, jadi efeknya lebih kecil daripada data AS.")
        id_l = (f"BI tidak mengubah stance hanya karena {actor}. "
                + ("Kalau pengetatan ini bertahan, biaya dana global sedikit lebih tinggi." if up else
                   "Kalau pelonggaran ini bertahan, biaya dana global sedikit lebih ringan, dengan batas tetap The Fed."))
        us_s = f"Fed funds tidak direvisi karena {actor}. UST paling bergerak lewat korelasi imbal hasil yang tipis."
        us_l = f"The Fed tidak mengikuti {actor}. Mandat harga AS tidak memakai rilis ini."
        gl_s = f"mata uang {f['place']} dan imbal hasil {actor} bergerak {'naik' if up else 'turun'} pada sesi rilis."
        gl_l = f"siklus {actor} menjadi {'lebih ketat' if up else 'lebih longgar'} hanya jika pola ini berulang, bukan pada satu rilis."
    elif family in ("growth", "activity"):
        if f["origin"] == "JP":
            id_s = f"{move}. Sentimen manufaktur global bisa ikut, tetapi efek ke komoditas RI jauh lebih kecil daripada data China."
        elif f["origin"] == "EU":
            id_s = f"{move}. Permintaan Eropa yang berubah hanya sedikit menggeser ekspor RI. Bukan kanal komoditas China."
        else:
            id_s = f"{move}. Kanal ke rupiah tidak langsung dan kecil pada hari rilis."
        id_l = f"BI tidak bergeser karena permintaan {f['place']}. Efek panjangnya baru ada jika perdagangan dengan negara itu benar-benar berubah."
        us_s = f"pertumbuhan {f['place']} tidak merevisi Fed funds pada hari rilis."
        us_l = "jalur The Fed tetap ditentukan data AS, bukan cetakan negara ini."
        gl_s = f"selera risiko global bergerak tipis. {'Survei bulanan mudah balik.' if family == 'activity' else 'Satu cetakan pertumbuhan masih bisa direvisi.'}"
        gl_l = f"perdagangan dunia tidak berbelok sebelum data {f['place']} yang searah berulang."
    elif family == "trade":
        id_s = f"{move}. Neraca {f['place']} bukan pasokan valas Indonesia, jadi rupiah hampir tidak bergerak hari ini."
        id_l = "BI tidak mendapat informasi baru tentang cadangan atau ekspor RI dari neraca negara lain."
        us_s = f"The Fed tidak mengubah bunga karena neraca {f['place']}."
        us_l = "defisit atau surplus negara lain bukan mandat The Fed."
        gl_s = "arus perdagangan tidak berbalik pada satu rilis. Komposisi harga versus volume tidak ada di headline."
        gl_l = "pangsa perdagangan baru bergeser jika pola neraca ini bertahan beberapa bulan."
    else:
        id_s = f"{move}. Tidak ada kanal yang mengharuskan rupiah bergerak besar hari ini."
        id_l = f"BI tidak memakai { _regime(family) } {f['place']} sebagai alasan mengubah bunga."
        us_s = "pasar AS tidak dihargai ulang oleh rilis ini."
        us_l = "siklus The Fed tidak bergeser."
        gl_s = f"pasar global tidak mendapat rezim baru dari { _regime(family) } {f['place']}."
        gl_l = "satu rilis tidak mengubah siklus pendanaan global."
    if f["origin"] == "JP" and family in ("prices", "guidance", "labor"):
        gl_s += " Yen bisa ikut bergerak jika pasar mengubah ekspektasi BoJ."
    if f["origin"] == "EU":
        id_l += " BI tetap lebih terikat pada The Fed daripada pada ECB."
    return _pack(
        f"{pre}: {id_s}", f"{pre}: {id_l}", f"{pre}: {us_s}",
        f"{pre}: {us_l}", f"{pre}: {gl_s}", f"{pre}: {gl_l}",
    )


def _flat_lines(f):
    pre = f"{f['title']} {f['place']}"
    regime = _regime(f["family"])
    if f["kind"] == "unknown":
        return _pack(
            f"{pre}: belum bisa dibanding konsensus. Rupiah tidak mendapat arah baru hari ini; yang ada hanya level actual.",
            f"{pre}: BI tidak menggeser stance tanpa pembanding. Level {regime} baru berarti setelah ada tren.",
            f"{pre}: pasar AS tidak merevisi harga selama selisih terhadap konsensus tidak terukur.",
            f"{pre}: The Fed tidak mendapat informasi arah sebelum ada pembanding.",
            f"{pre}: dolar dan selera risiko tidak punya katalis selama kejutannya tidak terukur.",
            f"{pre}: siklus global tidak berbelok. Angka ini masuk peta setelah ada konsensus atau previous.",
        )
    lines = _pack(
        f"{pre}: sesuai pembanding, jadi tidak ada impuls baru ke rupiah hari ini. Harga SBN tidak harus direvisi hanya karena selisih nol.",
        f"{pre}: yang tetap mengikat adalah level {regime}, bukan kejutan yang tidak ada. Stance tidak bergeser hari ini.",
        f"{pre}: UST dan saham AS tidak dihargai ulang oleh angka yang sudah sesuai ekspektasi.",
        f"{pre}: jalur The Fed tidak mendapat alasan baru. Level {regime} yang dikonfirmasi tetap menjadi patokan.",
        f"{pre}: tidak ada katalis dolar dari angka yang sesuai konsensus. Pergerakan hari ini, kalau ada, datang dari hal lain.",
        f"{pre}: hanya mengunci {regime} {f['place']} yang sudah dihargai. Siklus global tidak berbelok.",
    )
    if f["origin"] == "US" and f["family"] in ("prices", "labor", "rate", "growth", "guidance"):
        lines["id_panjang"] = (
            f"{pre}: selisih nol tidak membuka ruang BI. Level {regime} AS yang dikonfirmasi tetap menahan The Fed, "
            "dan BI tidak bisa jauh lebih longgar dari itu."
        )
    elif f["origin"] == "ID":
        lines["id_panjang"] = (
            f"{pre}: selisih nol tidak mengubah BI hari ini. Yang tetap menentukan adalah level {regime} Indonesia."
        )
    elif f["origin"] == "CN" and f["family"] in ("growth", "activity", "trade", "prices", "rate"):
        lines["id_pendek"] = (
            f"{pre}: sesuai pembanding, jadi harapan komoditas RI tidak direvisi hari ini. "
            f"Level {regime} China yang sudah dihargai tetap menjadi patokan."
        )
    return lines


def market_views(ev):
    f = _facts(ev)
    kind, origin, family = f["kind"], f["origin"], f["family"]
    if kind in ("inline", "unknown"):
        lines = _flat_lines(f)
    elif origin == "US" and family == "growth":
        lines = _lines_growth_us(f)
    elif origin == "US" and family == "activity":
        lines = _lines_activity_us(f)
    elif origin == "US" and family == "trade":
        lines = _lines_trade_us(f)
    elif origin == "US" and family == "reserves":
        lines = _lines_reserves_us(f)
    elif origin == "US":
        lines = _lines_us(f)
    elif origin == "ID":
        lines = _lines_id(f)
    elif origin == "CN":
        lines = _lines_cn(f)
    elif origin == "JP" and family == "rate":
        lines = _lines_jp_rate(f)
    elif origin == "EU" and family == "rate":
        lines = _lines_eu_rate(f)
    else:
        lines = _lines_spillover(f)
    return {k: _clip(_ensure_print(f, (lines or {}).get(k)), 380) for k in MARKET_KEYS}


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
        "Kamu analis makro untuk investor Indonesia. Baca rilis ini sebagai sinyal tentang siklus "
        "dan reaksi kebijakan, bukan sebagai kalimat bahwa angkanya di atas atau di bawah konsensus.\n"
        "Gunakan HANYA angka yang diberikan. Jangan mengarang data lain. Jangan memberi rekomendasi beli atau jual.\n"
        "Bahasa Indonesia, lugas.\n"
        "Balas HANYA JSON valid, tanpa markdown, dengan kunci:\n"
        "ringkasan: 3 sampai 4 kalimat. Kalimat pertama adalah arti makro cetakan ini bagi siklus atau bank sentral "
        "yang relevan. Sebut nama indikator dan angkanya. Sebut juga apa yang satu rilis belum bisa buktikan.\n"
        "id_pendek, id_panjang, us_pendek, us_panjang, global_pendek, global_panjang: masing-masing SATU kalimat, "
        "dan keenamnya harus berbeda. Pasar Indonesia, pasar Amerika, dan global; masing-masing jangka pendek "
        "(sesi rilis) dan jangka panjang (siklus kebijakan). Tiap kalimat menyebut nama indikator serta arah "
        "kejutannya, plus kanal yang nyata (dolar, imbal hasil, rupiah, SBN, IHSG, komoditas, atau yen). "
        "Kalau efek ke suatu pasar kecil, katakan kecil dan sebabnya. Jangan mengarang guncangan.\n\n"
        f"Data: {json.dumps(payload, ensure_ascii=False)}\n"
        f"Kategori dampak ke Indonesia: {level}\n"
        f"Rilis berikutnya: {next_txt}\n"
        "Peta mesin berikut hanya supaya kanal penting tidak terlewat. Jangan disalin kata per kata.\n"
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


def make_chart(ev, now):
    """Grafik Actual vs Forecast. Garis actual dimuluskan, puncak dan dasar diberi bulan."""
    try:
        import io as _io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        log.warning("matplotlib belum terpasang, grafik dilewati")
        return None

    k = series_key(ev)
    pts = {e["id"]: e for e in history_events(now) + [ev]
           if series_key(e) == k and e.get("actual") is not None}
    pts = sorted(pts.values(), key=lambda e: e["date"])[-24:]
    if len(pts) < 3:
        return None
    x = [parse_dt(e["date"]).astimezone(WIB) for e in pts]
    act = [e["actual"] for e in pts]
    fc = [e.get("forecast") for e in pts]
    suffix = (ev.get("scale") or "") + ("%" if ev.get("unit") == "%" else "")

    fig, ax = plt.subplots(figsize=(8, 4.3), dpi=150)
    fig.patch.set_facecolor("white")
    xs = mdates.date2num(x)
    smooth_x, smooth_y = _smooth_curve(xs, act)
    smooth_when = mdates.num2date(smooth_x)
    floor = min(act + [v for v in fc if v is not None])
    ax.fill_between(smooth_when, smooth_y, floor, color=BRAND_SOFT, alpha=0.85, zorder=1)
    ax.plot(smooth_when, smooth_y, color=BRAND_RED, lw=2.6, zorder=3, label="Actual", solid_capstyle="round")
    ax.plot(x, act, linestyle="none", marker="o", ms=4.5, color=BRAND_RED, zorder=4)
    fx = [(a, b) for a, b in zip(x, fc) if b is not None]
    if fx:
        ax.plot([a for a, _ in fx], [b for _, b in fx], color=BRAND_DARK, lw=1.5,
                ls=(0, (4, 3)), zorder=2, label="Forecast/Konsensus")
    ax.scatter([x[-1]], [act[-1]], s=70, color=BRAND_RED, edgecolor="white", lw=2, zorder=5)
    ax.annotate(f"{act[-1]:g}{suffix}", (x[-1], act[-1]), xytext=(-8, 12), ha="right",
                textcoords="offset points", color=BRAND_RED, fontsize=11, fontweight="bold")

    peak_i = _extreme_index(act, x, True)
    trough_i = _extreme_index(act, x, False)
    if act[peak_i] != act[trough_i]:
        for idx, kind, dy in ((peak_i, "Puncak", 14), (trough_i, "Dasar", -18)):
            when = x[idx]
            label = f"{kind} {BULAN[when.month - 1]} {when.year}"
            ax.annotate(
                label, (when, act[idx]), xytext=(0, dy), textcoords="offset points",
                ha="center", va="bottom" if dy > 0 else "top", fontsize=8, color=BRAND_DARK,
                fontweight="bold", zorder=6, clip_on=False,
                bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "#E0E0E0", "lw": 0.6},
            )

    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#BDBDBD")
    ax.grid(axis="y", color=BRAND_GRID, lw=0.8)
    ax.tick_params(colors="#666666", labelsize=9, length=0)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
    if suffix:
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}{suffix}"))
    fig.add_artist(matplotlib.lines.Line2D([0.06, 0.14], [0.955, 0.955], color=BRAND_RED, lw=4))
    fig.text(0.06, 0.87, ev["title"], fontsize=13, fontweight="bold", color=BRAND_DARK)
    fig.text(0.06, 0.82, f"{ev.get('country')} · {len(pts)} rilis terakhir · sumber: TradingView",
             fontsize=8.5, color="#777777")
    ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=BRAND_DARK)
    fig.subplots_adjust(left=0.09, right=0.96, top=0.74, bottom=0.14)
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
def test_send(now):
    """Kirim rilis berdampak SEDANG/TINGGI terakhir (7 hari) ke Discord + Sheets.
    Catatan bot tidak diubah, jadi aman dijalankan berkali-kali."""
    min_rank = LEVEL_RANK.get(MIN_ALERT, 1)
    evs = fetch_events(now, back_days=7, fwd_days=0)
    done = sorted((e for e in evs if e.get("actual") is not None and parse_dt(e["date"]) <= now
                   and LEVEL_RANK[classify(e)[0]] >= min_rank), key=lambda e: e["date"])
    if not done:
        log.warning("TES: tidak ada rilis berdampak sedang/tinggi dalam 7 hari terakhir")
        return
    ev = done[-1]
    log.info("TES: mengirim %s %s (%s)", ev.get("country"), ev.get("title"), ev.get("date"))
    embed = release_embed(ev, evs, now)
    embed["title"] = "🧪 TES · " + embed["title"]
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
