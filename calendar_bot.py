#!/usr/bin/env python3
"""
Calendar Bot: TradingView Economic Calendar -> analisis dampak ke Indonesia -> Discord
-------------------------------------------------------------------------------------
Yang dilakukan setiap kali jalan (idealnya tiap 10-15 menit via GitHub Actions):
  1. Ambil kalender ekonomi TradingView (3 hari ke belakang s/d 100 hari ke depan).
  2. Kategorikan dampak ke market & ekonomi Indonesia: TINGGI / SEDANG / RENDAH.
  3. Jadwal hanya dipantau (tidak dikirim). Opsional: SEND_PREVIEW=1 / SEND_REMINDER=1.
  4. Begitu angka actual keluar: kirim Actual vs Forecast (konsensus) vs Previous,
     analisis dampaknya, dan jadwal rilis berikutnya.

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
SHEETS_URL = os.getenv("SHEETS_WEBHOOK_URL", "")
SHEETS_TOKEN = os.getenv("SHEETS_TOKEN", "")
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "400"))
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
    if any(k in t for k in ["trade balance", "exports"]):
        return "trade"
    if "reserves" in t:
        return "reserves"
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


def rule_analysis(ev):
    c, typ = ev.get("country"), indicator_type(ev.get("title", ""))
    d, diff, base = surprise(ev)
    if d == 0:
        head = (f"Rilis sesuai {base}, jadi minim kejutan; pasar biasanya fokus ke tren dan "
                "komentar pejabat terkait." if diff == 0 else "Belum ada pembanding untuk menilai kejutan.")
        return head
    arah = "di atas" if d > 0 else "di bawah"
    head = f"Angka {arah} {base} ({diff:+g}). "
    strong = d > 0 if typ != "unemployment" else d < 0  # data 'kuat/panas'?

    if c == "US":
        if typ == "rate":
            body = ("Kejutan hawkish dari The Fed" if d > 0 else "Kejutan dovish dari The Fed")
        elif typ in ("inflation", "jobs", "growth", "activity", "unemployment"):
            body = ("Data AS lebih kuat/panas dari perkiraan, mendorong ekspektasi Fed menahan suku bunga "
                    "tinggi lebih lama" if strong else
                    "Data AS lebih lemah dari perkiraan, membuka peluang Fed lebih longgar")
        else:
            body = "Dampak ke Fed relatif terbatas"
        hawk = (d > 0) if typ == "rate" else strong
        tail = (". Biasanya USD dan yield UST naik, sehingga rupiah, SBN, dan IHSG berisiko tertekan "
                "lewat arus keluar dana asing." if hawk else
                ". Biasanya USD dan yield UST turun, memberi ruang penguatan rupiah dan masuknya "
                "dana asing ke SBN dan IHSG.")
        return head + body + tail

    if c == "ID":
        texts = {
            ("rate", True): "BI lebih ketat dari ekspektasi: mendukung rupiah, tapi negatif untuk saham "
                            "sensitif suku bunga (perbankan, properti).",
            ("rate", False): "BI lebih longgar dari ekspektasi: positif untuk IHSG terutama perbankan dan "
                             "properti, namun rupiah bisa tertekan karena selisih bunga menyempit.",
            ("inflation", True): "Inflasi lebih tinggi mempersempit ruang BI untuk memangkas suku bunga; "
                                 "bisa menahan sentimen saham konsumer.",
            ("inflation", False): "Inflasi lebih rendah membuka ruang BI melonggarkan kebijakan; positif "
                                  "untuk SBN dan saham sensitif suku bunga.",
            ("growth", True): "Ekonomi tumbuh lebih kuat: positif untuk IHSG dan kepercayaan investor ke rupiah.",
            ("growth", False): "Pertumbuhan di bawah ekspektasi: sentimen negatif untuk IHSG, dan menambah "
                               "tekanan agar BI/pemerintah memberi stimulus.",
            ("trade", True): "Surplus lebih besar: pasokan valas lebih kuat, mendukung rupiah.",
            ("trade", False): "Surplus lebih kecil dari perkiraan: dukungan valas berkurang, rupiah lebih rentan.",
            ("reserves", True): "Cadangan devisa naik: bantalan BI untuk stabilisasi rupiah lebih tebal.",
            ("reserves", False): "Cadangan devisa turun: bisa menandakan intervensi BI menjaga rupiah.",
        }
        key = (typ, d > 0) if typ in ("rate", "inflation", "trade", "reserves") else (
            ("growth", strong) if typ in ("growth", "activity", "jobs", "unemployment") else None)
        return head + texts.get(key, "Dampak ke pasar domestik cenderung terbatas.")

    if c == "CN":
        if typ == "rate":
            return head + ("Pengetatan di China, negatif untuk permintaan komoditas." if d > 0 else
                           "Stimulus moneter China: positif untuk permintaan komoditas dan ekspor RI.")
        return head + ("Ekonomi China lebih kuat: positif untuk harga komoditas (batu bara, CPO, nikel), "
                       "ekspor RI, saham komoditas, dan rupiah." if strong else
                       "Ekonomi China melemah: negatif untuk permintaan komoditas dan ekspor RI.")

    return head + ("Data lebih kuat dari perkiraan; dampak ke Indonesia umumnya tidak langsung, lewat "
                   "sentimen risiko global dan pergerakan USD." if strong else
                   "Data lebih lemah dari perkiraan; dampak ke Indonesia umumnya tidak langsung, lewat "
                   "sentimen risiko global dan pergerakan USD.")


def llm_analysis(ev, level, rule_text, next_txt):
    if not ANTHROPIC_KEY:
        return None
    payload = {k: ev.get(k) for k in ("title", "country", "period", "actual", "forecast",
                                       "previous", "unit", "scale", "source")}
    prompt = (
        "Kamu analis makro untuk investor Indonesia. Tulis analisis 3-4 kalimat bahasa Indonesia, "
        "lugas, tanpa basa-basi, tentang rilis data ini dan implikasinya ke rupiah, IHSG, SBN, "
        "dan kebijakan BI. Gunakan HANYA angka yang diberikan, jangan mengarang data lain. "
        "Jangan memberi rekomendasi beli/jual.\n\n"
        f"Data: {json.dumps(payload, ensure_ascii=False)}\n"
        f"Kategori dampak ke Indonesia: {level}\n"
        f"Catatan awal (berbasis aturan): {rule_text}\n"
        f"Rilis berikutnya: {next_txt}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 400,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"]).strip() or None
    except Exception as e:
        log.warning("Claude API gagal, pakai analisis aturan: %s", e)
        return None


# ---------------------------------------------------------------- utilitas
def parse_dt(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_wib(d):
    w = d.astimezone(WIB)
    return f"{HARI[w.weekday()]}, {w.day:02d} {BULAN[w.month - 1]} {w.year} {w:%H:%M} WIB"


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
            r.raise_for_status()
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


def make_chart(ev, now):
    """Grafik Actual vs Forecast 24 rilis terakhir untuk indikator ini. Return PNG bytes / None."""
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

    fig, ax = plt.subplots(figsize=(8, 4), dpi=150)
    fig.patch.set_facecolor("white")
    ax.fill_between(x, act, min(act + [v for v in fc if v is not None]),
                    color=BRAND_SOFT, alpha=0.7, zorder=1)
    ax.plot(x, act, color=BRAND_RED, lw=2.4, marker="o", ms=4, zorder=3, label="Actual")
    fx = [(a, b) for a, b in zip(x, fc) if b is not None]
    if fx:
        ax.plot([a for a, _ in fx], [b for _, b in fx], color=BRAND_DARK, lw=1.6,
                ls=(0, (4, 3)), zorder=2, label="Forecast/Konsensus")
    ax.scatter([x[-1]], [act[-1]], s=70, color=BRAND_RED, edgecolor="white", lw=2, zorder=4)
    ax.annotate(f"{act[-1]:g}{suffix}", (x[-1], act[-1]), xytext=(-10, 10), ha="right",
                textcoords="offset points", color=BRAND_RED, fontsize=11, fontweight="bold")

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
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=BRAND_DARK)
    fig.subplots_adjust(left=0.09, right=0.96, top=0.76, bottom=0.11)
    buf = _io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def release_embed(ev, events, now):
    level, _ = classify(ev)
    emoji, color = LEVEL_STYLE[level]
    nxt = next_release(ev, events, now)
    rule_txt = rule_analysis(ev)
    analysis = llm_analysis(ev, level, rule_txt, nxt) or rule_txt
    d, diff, base = surprise(ev)
    surp = "–" if diff is None else ("sesuai" if d == 0 else f"{round(diff, 4):+g} vs {base}")
    return {
        "title": f"{emoji} {FLAGS.get(ev['country'], ev['country'])} {ev['title']}"
                 + (f" ({ev['period']})" if ev.get("period") else ""),
        "color": color,
        "description": analysis[:3500],
        "fields": [
            {"name": "Actual", "value": f"**{fmt_val(ev, 'actual')}**", "inline": True},
            {"name": "Forecast/Konsensus", "value": fmt_val(ev, "forecast"), "inline": True},
            {"name": "Previous", "value": fmt_val(ev, "previous"), "inline": True},
            {"name": "Selisih", "value": surp, "inline": True},
            {"name": "Dampak ke RI", "value": level, "inline": True},
            {"name": "Rilis", "value": fmt_wib(parse_dt(ev["date"])), "inline": True},
            {"name": "Next update", "value": nxt, "inline": False},
        ],
        "footer": {"text": " · ".join(x for x in ["Sumber: TradingView", ev.get("source"),
                                                     "bukan rekomendasi investasi"] if x)},
    }


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


def sheet_row(ev):
    _, diff, _ = surprise(ev)
    unit = (ev.get("scale") or "") + (ev.get("unit") or "")
    return {
        "sheet": sheet_name(ev), "id": ev["id"],
        "date": parse_dt(ev["date"]).astimezone(WIB).isoformat(),
        "period": ev.get("period") or "", "actual": ev.get("actual"),
        "forecast": ev.get("forecast"), "previous": ev.get("previous"),
        "surprise": None if diff is None else round(diff, 4), "unit": unit,
        "impact": classify(ev)[0], "country": ev.get("country"), "title": ev.get("title"),
    }


def push_sheets(rows):
    """Kirim baris ke Apps Script (per 150 baris). Return True kalau semua sukses."""
    if not rows:
        return True
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


def backfill_sheets(st, now, min_rank, budget_s):
    """Isi riwayat dicicil per sheet sampai batas waktu, lanjut di run berikutnya."""
    t0 = time.time()
    hist = fetch_events(now, back_days=BACKFILL_DAYS, fwd_days=0)
    groups = {}
    for e in hist:
        if (e.get("actual") is not None and parse_dt(e["date"]) <= now
                and LEVEL_RANK[classify(e)[0]] >= min_rank):
            row = sheet_row(e)
            groups.setdefault(row["sheet"], []).append(row)
    done = set(st.get("backfill_done", []))
    todo = sorted(g for g in groups if g not in done)
    log.info("Riwayat Google Sheets: %d sheet selesai, %d tersisa", len(done), len(todo))
    for name in todo:
        if time.time() - t0 > budget_s:
            break
        if not push_sheets(groups[name]):
            break
        done.add(name)
        st["backfill_done"] = sorted(done)
        save_state(st)  # simpan progres tiap sheet, aman kalau run terhenti
    if all(g in done for g in groups):
        st["sheets_backfilled"] = True
        st.pop("backfill_done", None)
        log.info("Riwayat Google Sheets lengkap")


# ---------------------------------------------------------------- main
def main():
    now = dt.datetime.now(dt.timezone.utc)
    st = load_state()
    first_run = "posted" not in st
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
        st["sheet_pending"] = [] if push_sheets(pending) else pending[-500:]
        save_state(st)
        if not st.get("sheets_backfilled"):
            backfill_sheets(st, now, min_rank, SHEETS_BUDGET)
        save_state(st)
    log.info("Selesai: %d rilis baru diproses", len(released))


if __name__ == "__main__":
    main()
