#!/usr/bin/env python3
"""
Economic Outlook Bot
--------------------
Memantau sumber resmi (BI, BPS, The Fed/FRED), menyimpan data terbaru,
mendeteksi perubahan, dan kirim notifikasi Telegram kalau ada rilis baru.

Output (folder data/):
  latest.json  -> nilai terbaru semua indikator
  history.csv  -> log setiap kali ada perubahan nilai
  state.json   -> memori bot (nilai & berita yang sudah dilihat)
  OUTLOOK.md   -> ringkasan siap baca (tampil rapi di GitHub)

Env vars:
  FRED_API_KEY      (gratis: https://fred.stlouisfed.org/docs/api/api_key.html)
  BPS_API_KEY       (gratis: https://webapi.bps.go.id/developer/)
  TELEGRAM_TOKEN    (opsional, dari @BotFather)
  TELEGRAM_CHAT_ID  (opsional)
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

FRED_KEY = os.getenv("FRED_API_KEY")
BPS_KEY = os.getenv("BPS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

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

BI_PAGES = {
    "BI-Rate (%)": "https://www.bi.go.id/id/statistik/indikator/bi-rate.aspx",
    "Inflasi IHK - BI (% yoy)": "https://www.bi.go.id/id/statistik/indikator/data-inflasi.aspx",
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
    """Data AS dari FRED (St. Louis Fed). Return list of indikator."""
    if not FRED_KEY:
        log.warning("FRED_API_KEY kosong, FRED dilewati")
        return []
    out = []
    for sid, label, units in FRED_SERIES:
        try:
            j = get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": sid, "api_key": FRED_KEY, "file_type": "json",
                    "sort_order": "desc", "limit": 10, "units": units,
                },
            ).json()
            obs = next(o for o in j["observations"] if o["value"] not in (".", ""))
            out.append({
                "key": f"FRED:{sid}", "source": "The Fed / FRED", "indicator": label,
                "period": obs["date"], "value": round(float(obs["value"]), 2),
                "url": f"https://fred.stlouisfed.org/series/{sid}",
            })
        except Exception as e:  # satu seri gagal jangan matiin semua
            log.warning("FRED %s gagal: %s", sid, e)
    return out


def fetch_bps():
    """Indikator strategis BPS (inflasi, pertumbuhan ekonomi, pengangguran, dll)."""
    if not BPS_KEY:
        log.warning("BPS_API_KEY kosong, BPS dilewati")
        return []
    url = (f"https://webapi.bps.go.id/v1/api/list/model/indicators/"
           f"lang/ind/domain/0000/key/{BPS_KEY}/")
    try:
        j = get(url).json()
    except Exception as e:
        log.warning("BPS indikator gagal: %s", e)
        return []
    if j.get("data-availability") != "available":
        log.warning("BPS: data tidak tersedia (%s)", j.get("status"))
        return []
    items = j["data"][1] if len(j.get("data", [])) > 1 else []
    out = []
    for it in items:
        title = it.get("title") or it.get("name") or "?"
        period = it.get("periode") or it.get("period") or it.get("data_source") or ""
        value = it.get("value")
        unit = it.get("unit") or ""
        if value in (None, ""):
            continue
        out.append({
            "key": f"BPS:{it.get('indicator_id', title)}", "source": "BPS",
            "indicator": f"{title} ({unit})" if unit else title,
            "period": str(period), "value": value, "url": "https://www.bps.go.id",
        })
    return out


def fetch_bps_news():
    """Berita Resmi Statistik (BRS) terbaru BPS."""
    if not BPS_KEY:
        return []
    url = (f"https://webapi.bps.go.id/v1/api/list/model/pressrelease/"
           f"lang/ind/domain/0000/key/{BPS_KEY}/")
    try:
        j = get(url).json()
        items = j["data"][1] if len(j.get("data", [])) > 1 else []
    except Exception as e:
        log.warning("BPS BRS gagal: %s", e)
        return []
    return [{
        "id": f"BPS:{it.get('brs_id')}", "source": "BPS",
        "title": it.get("title", ""), "date": it.get("rl_date", ""),
        "url": "https://www.bps.go.id/id/pressrelease",
    } for it in items[:15]]


def _pick_col(cols, words):
    for c in cols:
        if any(w in str(c).lower() for w in words):
            return c
    return None


def fetch_bi():
    """Scrape tabel indikator di bi.go.id (BI belum punya API publik)."""
    out = []
    for label, url in BI_PAGES.items():
        try:
            tables = pd.read_html(io.StringIO(get(url).text))
            df = next(t for t in tables if len(t) > 0 and t.shape[1] >= 2)
            cols = list(df.columns)
            pcol = _pick_col(cols, ["tanggal", "periode", "bulan", "date"]) or cols[0]
            vcol = _pick_col(cols, ["rate", "inflasi", "%", "nilai"]) or cols[-1]
            row = df.iloc[0]
            out.append({
                "key": f"BI:{label}", "source": "Bank Indonesia", "indicator": label,
                "period": str(row[pcol]).strip(), "value": str(row[vcol]).strip(),
                "url": url,
            })
        except Exception as e:
            log.warning("BI %s gagal (mungkin struktur halaman berubah/diblok): %s", label, e)
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
    lines = [f"# Economic Outlook Monitor", "", f"_Update terakhir: {now_utc()}_", ""]
    for src in ["Bank Indonesia", "BPS", "The Fed / FRED"]:
        rows = [i for i in indicators if i["source"] == src]
        if not rows:
            continue
        lines += [f"## {src}", "", "| Indikator | Periode | Nilai |", "|---|---|---|"]
        lines += [f"| [{r['indicator']}]({r['url']}) | {r['period']} | {r['value']} |" for r in rows]
        lines.append("")
    if news:
        lines += ["## Rilis terbaru", ""]
        lines += [f"- **{n['source']}** · {n['date']} · [{n['title']}]({n['url']})" for n in news[:20]]
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    DATA.mkdir(exist_ok=True)
    state = load_json(STATE_FILE, {"values": {}, "seen_news": []})
    first_run = not state["values"]

    indicators = fetch_bi() + fetch_bps() + fetch_fred()
    news = fetch_fed_news() + fetch_bps_news()

    # --- deteksi perubahan indikator
    changed = []
    for ind in indicators:
        sig = f"{ind['period']}|{ind['value']}"
        if state["values"].get(ind["key"]) != sig:
            changed.append(ind)
            state["values"][ind["key"]] = sig
    if changed:
        append_history(changed)
        if not first_run:
            msg = "📊 Data ekonomi baru:\n" + "\n".join(
                f"• [{c['source']}] {c['indicator']}: {c['value']} ({c['period']})" for c in changed)
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
