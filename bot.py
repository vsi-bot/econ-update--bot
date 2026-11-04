#!/usr/bin/env python3
"""
Economic Outlook Bot
--------------------
Memantau data ekonomi TANPA API key:
  - Indikator Indonesia (BI-Rate, inflasi, GDP, neraca dagang, dll) dari
    angka actual terakhir di kalender ekonomi TradingView
  - Data AS dari unduhan CSV publik FRED (tanpa key)
  - Press release kebijakan moneter The Fed (RSS)
Lalu menyimpan data terbaru, mendeteksi perubahan, dan kirim notifikasi.

Output (folder data/):
  latest.json  -> nilai terbaru semua indikator
  history.csv  -> log setiap kali ada perubahan nilai
  state.json   -> memori bot (nilai & berita yang sudah dilihat)
  OUTLOOK.md   -> ringkasan siap baca (tampil rapi di GitHub)

Env vars (semua opsional):
  TV_COUNTRIES      negara dari kalender TradingView, default ID
  TELEGRAM_TOKEN    (opsional, dari @BotFather)
  TELEGRAM_CHAT_ID  (opsional)
  DISCORD_WEBHOOK_URL (opsional) notifikasi ke Discord
"""
import csv
import datetime as dt
import io
import json
import logging
import os
from pathlib import Path

import feedparser
import pandas as pd
import requests

# ---------------------------------------------------------------- konfigurasi
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "state.json"
HISTORY_FILE = DATA / "history.csv"
LATEST_FILE = DATA / "latest.json"
REPORT_FILE = DATA / "OUTLOOK.md"

TV_COUNTRIES = os.getenv("TV_COUNTRIES", "ID")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")

HEADERS = {"User-Agent": "Mozilla/5.0 (EconOutlookBot/1.0; personal research)"}
TIMEOUT = 30

# (series_id, label, units)  units: lin = apa adanya, pc1 = % yoy
FRED_SERIES = [
    ("DFEDTARU", "Fed Funds Rate - batas atas target (%)", "lin"),
    ("FEDFUNDS", "Fed Funds Rate efektif (%)", "lin"),
    ("CPIAUCSL", "Inflasi CPI AS (% yoy)", "pc1"),
    ("PCEPILFE", "Core PCE AS (% yoy)", "pc1"),
    ("UNRATE", "Tingkat pengangguran AS (%)", "lin"),
    ("A191RL1Q225SBEA", "Pertumbuhan GDP riil AS (% qoq, annualized)", "lin"),
    ("DGS10", "US Treasury 10 tahun (%)", "lin"),
    ("DTWEXBGS", "Indeks dolar AS (broad)", "lin"),
    ("CCUSMA02IDM618N", "USD/IDR rata-rata bulanan (OECD via FRED)", "lin"),
]

# Indikator Indonesia yang ditampilkan, urut sesuai tampilan.
# (kelompok, label, kata yang WAJIB ada di judul TradingView, kata yang TIDAK boleh ada)
ID_INDICATORS = [
    ("Moneter", "BI-Rate", ["interest rate decision"], []),
    ("Moneter", "Suku bunga Deposit Facility", ["deposit facility"], []),
    ("Moneter", "Suku bunga Lending Facility", ["lending facility"], []),
    ("Moneter", "Uang beredar M2 (yoy)", ["m2"], []),
    ("Moneter", "Pertumbuhan kredit (yoy)", ["loan growth"], []),
    ("Inflasi", "Inflasi (yoy)", ["inflation rate", "yoy"], ["core"]),
    ("Inflasi", "Inflasi (mtm)", ["inflation rate", "mom"], ["core"]),
    ("Inflasi", "Inflasi inti (yoy)", ["core inflation", "yoy"], []),
    ("Pertumbuhan", "Pertumbuhan GDP (yoy)", ["gdp growth rate", "yoy"], ["full year"]),
    ("Pertumbuhan", "Pertumbuhan GDP (qoq)", ["gdp growth rate", "qoq"], []),
    ("Pertumbuhan", "Pertumbuhan GDP setahun penuh", ["full year gdp"], []),
    ("Eksternal", "Neraca perdagangan", ["balance of trade"], []),
    ("Eksternal", "Ekspor (yoy)", ["exports"], []),
    ("Eksternal", "Impor (yoy)", ["imports"], []),
    ("Eksternal", "Cadangan devisa", ["foreign exchange reserves"], []),
    ("Eksternal", "Transaksi berjalan", ["current account"], ["gdp"]),
    ("Eksternal", "Transaksi berjalan (% GDP)", ["current account", "gdp"], []),
    ("Konsumsi & aktivitas", "PMI manufaktur", ["manufacturing pmi"], []),
    ("Konsumsi & aktivitas", "Indeks keyakinan konsumen", ["consumer confidence"], []),
    ("Konsumsi & aktivitas", "Penjualan ritel (yoy)", ["retail sales", "yoy"], []),
    ("Konsumsi & aktivitas", "Penjualan mobil (yoy)", ["car sales"], []),
    ("Konsumsi & aktivitas", "Penjualan motor (yoy)", ["motorbike sales"], []),
    ("Konsumsi & aktivitas", "Kunjungan wisman (yoy)", ["tourist arrivals"], []),
    ("Tenaga kerja", "Tingkat pengangguran", ["unemployment rate"], []),
]

FRED_GROUP = {
    "DFEDTARU": "Moneter", "FEDFUNDS": "Moneter", "DGS10": "Pasar",
    "CPIAUCSL": "Inflasi", "PCEPILFE": "Inflasi", "A191RL1Q225SBEA": "Pertumbuhan",
    "UNRATE": "Tenaga kerja", "DTWEXBGS": "Pasar", "CCUSMA02IDM618N": "Pasar",
}

FED_FEEDS = [
    "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "https://www.federalreserve.gov/feeds/press_all.xml",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("econbot")


# ---------------------------------------------------------------- utilitas
def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def notify(text: str):
    log.info("NOTIF: %s", text.replace("\n", " | "))
    if DISCORD_WEBHOOK:
        try:
            for i in range(0, len(text), 1900):  # batas Discord 2000 karakter
                requests.post(DISCORD_WEBHOOK, timeout=TIMEOUT, json={
                    "username": "Economic Outlook Bot", "content": text[i:i + 1900]})
        except requests.RequestException as e:
            log.warning("Discord gagal: %s", e)
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": text[:4000], "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning("Telegram gagal: %s", e)


# ---------------------------------------------------------------- sumber data
def fetch_fred():
    """Data AS dari CSV publik FRED (tanpa API key)."""
    out = []
    for sid, label, units in FRED_SERIES:
        try:
            csv_text = get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                           params={"id": sid}).text
            df = pd.read_csv(io.StringIO(csv_text))
            df.columns = ["date", "value"]
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df = df.dropna()
            if units == "pc1":  # % perubahan dari 12 bulan sebelumnya (data bulanan)
                df["value"] = df["value"].pct_change(12) * 100
                df = df.dropna()
            last = df.iloc[-1]
            out.append({
                "key": f"FRED:{sid}", "source": "Amerika Serikat (FRED)",
                "group": FRED_GROUP.get(sid, "Lainnya"), "indicator": label,
                "period": str(last["date"]), "value": round(float(last["value"]), 2),
                "url": f"https://fred.stlouisfed.org/series/{sid}",
            })
        except Exception as e:  # satu seri gagal jangan matiin semua
            log.warning("FRED %s gagal: %s", sid, e)
    return out


def fetch_tradingview():
    """Angka actual terakhir tiap indikator dari kalender TradingView (tanpa API key)."""
    now = dt.datetime.now(dt.timezone.utc)
    params = {
        "from": (now - dt.timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "to": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "countries": TV_COUNTRIES,
    }
    try:
        r = requests.get("https://economic-calendar.tradingview.com/events", params=params,
                         timeout=TIMEOUT, headers={**HEADERS,
                                                   "Origin": "https://www.tradingview.com",
                                                   "Referer": "https://www.tradingview.com/"})
        r.raise_for_status()
        j = r.json()
        events = j.get("result", j) if isinstance(j, dict) else j
    except Exception as e:
        log.warning("TradingView gagal: %s", e)
        return []

    def match(title):
        t = (title or "").lower()
        for i, (group, label, need, avoid) in enumerate(ID_INDICATORS):
            if all(w in t for w in need) and not any(w in t for w in avoid):
                return i, group, label
        return None

    latest = {}  # satu nilai terbaru per indikator yang ada di daftar
    for e in events:
        m = match(e.get("title"))
        if e.get("actual") is None or e.get("country") != "ID" or not m:
            continue
        k = e.get("ticker") or f"{e.get('country')}|{e.get('title')}"
        if k not in latest or e["date"] > latest[k][0]["date"]:
            latest[k] = (e, m)

    out = []
    for k, (e, (order, group, label)) in sorted(latest.items(), key=lambda kv: kv[1][1][0]):
        unit, scale = e.get("unit") or "", e.get("scale") or ""
        sign = "-" if e["actual"] < 0 else ""
        num = f"{abs(e['actual']):,.2f}".rstrip("0").rstrip(".") + scale
        if unit == "%":
            val = sign + num + "%"
        elif unit in ("$", "€", "¥", "£"):
            val = sign + ("US$" if unit == "$" else unit) + num
        else:
            val = f"{sign}{num} {unit}".strip()
        out.append({
            "key": f"TV:{k}", "source": "Indonesia", "group": group, "indicator": label,
            "period": e.get("period") or e["date"][:10], "value": val,
            "url": e.get("source_url") or "https://www.tradingview.com/economic-calendar/",
        })
    return out


def fetch_fed_news():
    """Press release kebijakan moneter The Fed (FOMC statement, minutes, dll)."""
    for feed_url in FED_FEEDS:
        try:
            feed = feedparser.parse(get(feed_url).content)
            if feed.entries:
                return [{
                    "id": f"FED:{e.get('id') or e.get('link')}", "source": "The Fed",
                    "title": e.get("title", ""), "date": e.get("published", ""),
                    "url": e.get("link", ""),
                } for e in feed.entries[:15]]
        except Exception as e:
            log.warning("Fed RSS %s gagal: %s", feed_url, e)
    return []


# ---------------------------------------------------------------- inti bot
def append_history(rows):
    new_file = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["detected_at", "source", "indicator", "period", "value"])
        for r in rows:
            w.writerow([now_utc(), r["source"], r["indicator"], r["period"], r["value"]])


def write_report(indicators, news):
    lines = ["# Economic Outlook Monitor", "", f"_Update terakhir: {now_utc()}_", ""]
    for src in ["Indonesia", "Amerika Serikat (FRED)"]:
        rows = [i for i in indicators if i["source"] == src]
        if not rows:
            continue
        lines += [f"## {src}", "", "| Kelompok | Indikator | Periode | Nilai |", "|---|---|---|---|"]
        prev = None
        for r in rows:
            g = r.get("group", "")
            lines.append(f"| {g if g != prev else ''} | [{r['indicator']}]({r['url']}) "
                         f"| {r['period']} | {r['value']} |")
            prev = g
        lines.append("")
    if news:
        lines += ["## Rilis terbaru The Fed", ""]
        lines += [f"- {n['date']} · [{n['title']}]({n['url']})" for n in news[:10]]
    lines += ["", "_Sumber: kalender ekonomi TradingView (data BPS/BI), FRED, federalreserve.gov_"]
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    DATA.mkdir(exist_ok=True)
    state = load_json(STATE_FILE, {"values": {}, "seen_news": []})
    first_run = not state["values"]

    indicators = fetch_tradingview() + fetch_fred()
    news = fetch_fed_news()

    # --- deteksi perubahan indikator
    changed, notify_list = [], []
    for ind in indicators:
        sig = f"{ind['period']}|{ind['value']}"
        old = state["values"].get(ind["key"])
        if old != sig:
            changed.append(ind)
            if old is not None:  # indikator baru ditambahkan ke daftar -> jangan dinotifikasi
                notify_list.append(ind)
            state["values"][ind["key"]] = sig
    if changed:
        append_history(changed)
    if notify_list and not first_run:
        msg = "📊 Data ekonomi baru:\n" + "\n".join(
            f"• [{c['source']}] {c['indicator']}: {c['value']} ({c['period']})" for c in notify_list)
        notify(msg)

    # --- deteksi rilis/berita baru
    seen = set(state["seen_news"])
    fresh = [n for n in news if n["id"] not in seen]
    if fresh and not first_run:
        notify("📰 Rilis resmi baru:\n" + "\n".join(
            f"• [{n['source']}] {n['title']}\n  {n['url']}" for n in fresh[:10]))
    state["seen_news"] = (list(seen) + [n["id"] for n in fresh])[-500:]

    save_json(STATE_FILE, state)
    save_json(LATEST_FILE, {"updated_at": now_utc(), "indicators": indicators, "news": news})
    write_report(indicators, news)
    log.info("Selesai: %d indikator, %d berubah, %d rilis baru",
             len(indicators), len(changed), len(fresh))


if __name__ == "__main__":
    main()
