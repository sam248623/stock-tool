"""
SuperPredictor scheduled scan.
Runs in GitHub Actions (no Streamlit here — this is a plain script). It scans the whole NSE
equity list against the SuperPredictor rules, writes the matches into shared_portfolio.json on
GitHub (so the app picks them up automatically), and sends a push notification via ntfy.sh.

Rules:
  - price between Rs 12 and 18
  - sitting at (or within 3%% of) its 1-week or 1-month low
  - average daily swing (high-low range) between 4%% and 9%%
  - decent liquidity (avg turnover >= SP_MIN_TURNOVER) as a "not an obscure/illiquid share" proxy
  - no negative news in the last ~7 days
  - history-based odds of a next-day rise of 1.5%%+ is at least 45%%

Env vars required:
  GH_TOKEN   - a token with write access to REPO (the built-in GITHUB_TOKEN works if the repo's
               Actions "Workflow permissions" is set to Read and write)
  REPO       - "owner/repo", e.g. sam248623/stock-tool
  GH_PATH    - file path in the repo to update (default shared_portfolio.json)
  GH_BRANCH  - branch (default main)
  NTFY_TOPIC - your private ntfy.sh topic name for push notifications (optional but recommended)
"""
import base64
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
SP_PRICE_MIN, SP_PRICE_MAX = 12.0, 18.0
SP_SWING_MIN, SP_SWING_MAX = 4.0, 9.0
SP_MIN_TURNOVER = 500000

GH_TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("REPO", "")
GH_PATH = os.environ.get("GH_PATH", "shared_portfolio.json")
GH_BRANCH = os.environ.get("GH_BRANCH", "main")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

POSITIVE = ["contract", "order win", "wins order", "bags order", "order", "profit rises", "profit jumps",
            "record", "growth", "upgrade", "buy rating", "outperform", "beats", "surge", "rally",
            "expansion", "dividend", "bonus", "acquisition", "partnership", "approval", "strong",
            "raises guidance", "deal", "gains", "soars", "jumps"]
NEGATIVE = ["loss", "falls", "plunge", "downgrade", "sell rating", "underperform", "misses", "probe",
            "fraud", "lawsuit", "penalty", "fine", "default", "debt", "cut", "slump", "decline",
            "resigns", "recall", "ban", "raid", "weak", "warning", "drops", "tumbles", "crash"]


def get_nse_universe():
    """Live NSE download usually fails from GitHub Actions too (NSE blocks datacenter IPs) —
    falls back to nse_universe.csv committed in the repo root."""
    try:
        req = urllib.request.Request(
            "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*",
                     "Referer": "https://www.nseindia.com/"})
        with urllib.request.urlopen(req, timeout=25) as r:
            df = pd.read_csv(io.BytesIO(r.read()))
        df.columns = [c.strip() for c in df.columns]
        if "SERIES" in df.columns:
            df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
        out = sorted({str(x).strip().upper() + ".NS" for x in df["SYMBOL"]})
        if out:
            return out
    except Exception:
        pass
    try:
        df = pd.read_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "nse_universe.csv"))
        df.columns = [c.strip() for c in df.columns]
        if "SERIES" in df.columns:
            df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
        return sorted({str(x).strip().upper() + ".NS" for x in df["SYMBOL"]})
    except Exception:
        return []


def download_many(tickers, period="1y"):
    try:
        data = yf.download(list(tickers), period=period, group_by="ticker", auto_adjust=False,
                           threads=True, progress=False)
    except Exception:
        return {}
    out = {}
    if data is None or data.empty:
        return out
    if isinstance(data.columns, pd.MultiIndex):
        if "Close" in set(data.columns.get_level_values(0)):
            data = data.swaplevel(0, 1, axis=1)
        top = set(data.columns.get_level_values(0))
        for t in tickers:
            if t in top:
                sub = data[t].dropna(subset=["Close"])
                if not sub.empty:
                    out[t] = sub
    else:
        t = tickers[0]
        sub = data.dropna(subset=["Close"])
        if not sub.empty:
            out[t] = sub
    return out


def atr(hist, n=14):
    h, l, c = hist["High"], hist["Low"], hist["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def rebound_metrics(sub):
    if len(sub) < 80:
        return None
    close, vol = sub["Close"], sub["Volume"]
    price = float(close.iloc[-1])
    a = atr(sub)
    if price <= 0 or a != a:
        return None
    recent_low = float(sub["Low"].tail(10).min())
    w6 = sub.tail(126)
    lo6, hi6 = float(w6["Low"].min()), float(w6["High"].max())
    base_vol = float(vol.iloc[-30:-5].mean())
    return {"price": price, "atr": a, "atr_pct": a / price * 100, "recent_low": recent_low,
            "low_pos": (recent_low - lo6) / (hi6 - lo6) if hi6 > lo6 else 0.5,
            "bounce": (price / recent_low - 1) * 100, "base_vol": base_vol,
            "spike": float(vol.tail(5).max()) / base_vol if base_vol else 0.0,
            "rsi": float(rsi(close).iloc[-1]), "sma5": float(close.tail(5).mean()),
            "up_days": int((close.diff().tail(3) > 0).sum())}


def tech_score(m, min_move):
    if m["base_vol"] < 100000 or m["atr_pct"] < min_move:
        return None
    s, why = 0.0, []
    if m["atr_pct"] >= 5:
        s += 3
        why.append(f"Very volatile: swings about {m['atr_pct']:.1f}% a day.")
    else:
        s += 2
        why.append(f"Volatile: swings about {m['atr_pct']:.1f}% a day.")
    if m["low_pos"] <= 0.25:
        s += 3
        why.append("Near its 6-month bottom.")
    elif m["low_pos"] <= 0.4:
        s += 1.5
    else:
        return None
    if m["spike"] >= 1.5:
        s += 1.5
        why.append(f"Volume rose to {m['spike']:.1f}x normal.")
    if 35 <= m["rsi"] <= 65:
        s += 1
    elif m["rsi"] > 72:
        s -= 1
    return s, why


def touch_stats(hist, need_pct, horizon):
    close = hist["Close"].to_numpy(dtype=float)
    n = len(close)
    if n < horizon + 70:
        return None
    hits_all, hits_low = [], []
    for i in range(60, n - horizon):
        fmax = close[i + 1:i + 1 + horizon].max()
        hit = (fmax / close[i] - 1) * 100 >= need_pct
        hits_all.append(hit)
        w = close[max(0, i - 125):i + 1]
        lo, hi = w.min(), w.max()
        pos = (close[i] - lo) / (hi - lo) if hi > lo else 0.5
        if pos < 0.3:
            hits_low.append(hit)
    if not hits_all:
        return None
    base = float(np.mean(hits_all))
    n_low = len(hits_low)
    est = base
    if n_low:
        wt = n_low / (n_low + 40)
        est = wt * float(np.mean(hits_low)) + (1 - wt) * base
    return {"base": base, "est": est}


def get_news(symbol):
    out, seen = [], set()

    def add(title, link):
        key = title.strip().lower()
        if title and key not in seen:
            seen.add(key)
            out.append({"title": title.strip(), "link": link or ""})

    try:
        base = symbol.split(".")[0]
        name = ""
        try:
            name = yf.Ticker(symbol).info.get("shortName") or ""
        except Exception:
            pass
        for junk in [" LTD.", " LTD", " LIMITED", " INC.", " INC", " CORP"]:
            name = name.upper().replace(junk, "")
        q = f"{name.strip() or base} share when:7d"
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
               + "&hl=en-IN&gl=IN&ceid=IN:en")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())
        for it in list(root.iter("item"))[:20]:
            add(it.findtext("title") or "", it.findtext("link") or "")
    except Exception:
        pass
    return out


def score_news(items):
    total, hits = 0, []
    for it in items:
        t = it["title"].lower()
        pos = sum(1 for k in POSITIVE if k in t)
        neg = sum(1 for k in NEGATIVE if k in t)
        if pos or neg:
            total += pos - neg
            hits.append(it["title"])
    return total, hits[:3]


def superpredictor_check(symbol, hist):
    if len(hist) < 150:
        return None
    m = rebound_metrics(hist)
    if not m:
        return None
    price = m["price"]
    if not (SP_PRICE_MIN <= price <= SP_PRICE_MAX):
        return None
    if not (SP_SWING_MIN <= m["atr_pct"] <= SP_SWING_MAX):
        return None
    turnover = m["base_vol"] * price
    if turnover < SP_MIN_TURNOVER:
        return None
    low5 = float(hist["Low"].tail(5).min())
    low21 = float(hist["Low"].tail(21).min())
    near_week_low = price <= low5 * 1.03
    near_month_low = price <= low21 * 1.03
    if not (near_week_low or near_month_low):
        return None
    ts = touch_stats(hist, 1.5, 1)
    if not ts:
        return None
    news_items = get_news(symbol)
    news_score, hits = score_news(news_items)
    if news_score < 0:
        return None
    tscore = tech_score(m, SP_SWING_MIN)
    if tscore is None:
        return None
    factor = max(0.6, min(1.5, 1 + 0.12 * (tscore[0] + news_score)))
    chance = max(0.0, min(0.95, ts["est"] * factor))
    if chance < 0.45:
        return None
    why = tscore[1] + [f"At its {'1-week' if near_week_low else '1-month'} low.",
                        f"History-based odds of a next-day rise of 1.5%+: about {chance*100:.0f}%."]
    return {"symbol": symbol, "price": round(price, 2), "swing_pct": round(m["atr_pct"], 1),
            "chance": round(chance, 3), "turnover": round(turnover), "why": why,
            "news_text": ("Recent headlines: " + "; ".join(hits)) if hits else "No notable recent news."}


def gh_get(repo, path, branch, token):
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                 "Authorization": f"Bearer {token}",
                                                 "User-Agent": "superpredictor-scan"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode("utf-8"))
        content = base64.b64decode(j["content"]).decode("utf-8")
        return json.loads(content), j["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}, None
        raise


def gh_put(repo, path, branch, token, data, sha):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    body = {"message": "SuperPredictor auto-scan update", "branch": branch,
            "content": base64.b64encode(json.dumps(data, indent=2).encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="PUT",
                                  headers={"Accept": "application/vnd.github+json",
                                           "Authorization": f"Bearer {token}",
                                           "Content-Type": "application/json",
                                           "User-Agent": "superpredictor-scan"})
    with urllib.request.urlopen(req, timeout=15) as r:
        json.loads(r.read().decode("utf-8"))


def notify(picks):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set, skipping push notification.")
        return
    if not picks:
        return
    lines = [f"{p['symbol']}: Rs{p['price']} | swing {p['swing_pct']}% | {p['chance']*100:.0f}% next-day odds"
             for p in picks]
    msg = "\n".join(lines)
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"), method="POST",
            headers={"Title": f"SuperPredictor: {len(picks)} match(es)", "Priority": "high", "Tags": "chart_with_upwards_trend"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("ntfy push failed:", e)


def main():
    if not REPO or not GH_TOKEN:
        raise SystemExit("REPO and GH_TOKEN must be set")

    universe = get_nse_universe()
    print(f"Universe: {len(universe)} symbols")
    if not universe:
        return

    hits = []
    chunk = 150
    for i in range(0, len(universe), chunk):
        batch = universe[i:i + chunk]
        data = download_many(batch)
        for t, sub in data.items():
            try:
                r = superpredictor_check(t, sub)
                if r:
                    hits.append(r)
            except Exception:
                continue
        print(f"Checked {min(i + chunk, len(universe))}/{len(universe)} — {len(hits)} match(es) so far")

    hits.sort(key=lambda r: r["chance"], reverse=True)
    hits = hits[:10]

    shared, sha = gh_get(REPO, GH_PATH, GH_BRANCH, GH_TOKEN)
    shared.setdefault("portfolio", [])
    shared.setdefault("tracker", [])
    shared.setdefault("watch", [])
    shared.setdefault("monthly_pl", {})
    shared["superpredictor"] = {"updated_at": datetime.now(IST).strftime("%d %b %Y, %H:%M IST"), "picks": hits}
    gh_put(REPO, GH_PATH, GH_BRANCH, GH_TOKEN, shared, sha)
    print(f"Updated {GH_PATH} with {len(hits)} pick(s)")

    notify(hits)


if __name__ == "__main__":
    main()
