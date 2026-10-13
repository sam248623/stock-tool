"""
My Stock Tool - personal portfolio tracker + buy/sell helper.
Run:  streamlit run app.py
Data: Yahoo Finance via yfinance (about 1 min delay is fine, nothing here trades).
Indian stocks: use exchange NSE (.NS) or BSE (.BO), e.g. RELIANCE, TCS, INFY.
"""
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf
from streamlit_js_eval import streamlit_js_eval

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")

st.set_page_config(page_title="My Stock Tool", page_icon="📈", layout="wide")


# ---------- storage (saved inside YOUR phone/browser, so it survives restarts) ----------
LS_KEY = "my_stock_portfolio"
LS_WATCH = "my_stock_scanlist"
DEFAULT_POOL = [
    "AHCL.NS", "RHETAN.NS", "SUBEXLTD.NS", "SYNCOMF.NS", "IDEA.NS", "YESBANK.NS", "UCOBANK.NS",
    "SOUTHBANK.NS", "IOB.NS", "CENTRALBK.NS", "JPPOWER.NS", "RPOWER.NS", "RTNPOWER.NS",
    "RTNINDIA.NS", "GTLINFRA.NS", "SUZLON.NS", "HCC.NS", "NBCC.NS", "IRFC.NS", "NHPC.NS",
    "SJVN.NS", "TRIDENT.NS", "VIKASLIFE.NS", "PCJEWELLER.NS", "JPASSOCIAT.NS", "MMTC.NS",
    "ITI.NS", "HFCL.NS", "SAKSOFT.NS", "SPCENET.NS", "UJJIVANSFB.NS", "EASEMYTRIP.NS",
]


def init_storage():
    if "portfolio" not in st.session_state:
        raw = streamlit_js_eval(
            js_expressions=f"localStorage.getItem('{LS_KEY}') || '[]'", key="ls_get")
        raw_w = streamlit_js_eval(
            js_expressions=f"localStorage.getItem('{LS_WATCH}') || 'none'", key="ls_get_w")
        if not isinstance(raw, str) or not isinstance(raw_w, str):
            st.info("Loading your saved shares...")
            st.stop()
        try:
            st.session_state.portfolio = json.loads(raw)
        except Exception:
            st.session_state.portfolio = []
        if raw_w == "none":
            st.session_state.watch = DEFAULT_POOL[:]
        elif raw_w == "empty":
            st.session_state.watch = []
        else:
            st.session_state.watch = [x for x in raw_w.split(",") if x]
        st.session_state.save_n = 0
        st.session_state.save_w = 0
    st.session_state.setdefault("watch", DEFAULT_POOL[:])
    st.session_state.setdefault("save_w", 0)
    if st.session_state.save_n > 0:
        payload = json.dumps(json.dumps(st.session_state.portfolio))
        streamlit_js_eval(js_expressions=f"localStorage.setItem('{LS_KEY}', {payload})",
                          key=f"ls_set_{st.session_state.save_n}")
    if st.session_state.save_w > 0:
        wv = ",".join(st.session_state.watch) or "empty"
        streamlit_js_eval(js_expressions=f"localStorage.setItem('{LS_WATCH}', {json.dumps(wv)})",
                          key=f"ls_setw_{st.session_state.save_w}")


def persist():
    st.session_state.save_n += 1


# ---------- data ----------
@st.cache_data(ttl=60)
def get_price(symbol):
    try:
        t = yf.Ticker(symbol)
        p = t.fast_info.get("last_price")
        if p is None or pd.isna(p):
            h = t.history(period="5d")
            p = float(h["Close"].iloc[-1]) if not h.empty else None
        return float(p) if p is not None else None
    except Exception:
        return None


@st.cache_data(ttl=300)
def get_history(symbol):
    try:
        h = yf.Ticker(symbol).history(period="1y", auto_adjust=False)
        return h.dropna(subset=["Close"])
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=600)
def get_news(symbol):
    import urllib.parse
    import urllib.request
    import xml.etree.ElementTree as ET

    out, seen = [], set()

    def add(title, summary, link):
        key = title.strip().lower()
        if title and key not in seen:
            seen.add(key)
            out.append({"title": title.strip(), "summary": summary or "", "link": link or ""})

    # source 1: Yahoo Finance (often thin for Indian stocks)
    try:
        for n in (yf.Ticker(symbol).news or [])[:15]:
            c = n.get("content") if isinstance(n.get("content"), dict) else n
            link = ""
            if isinstance(c.get("canonicalUrl"), dict):
                link = c["canonicalUrl"].get("url", "")
            add(c.get("title") or "", c.get("summary") or "", link or c.get("link", ""))
    except Exception:
        pass

    # source 2: Google News RSS (better coverage, last 7 days)
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
            add(it.findtext("title") or "", "", it.findtext("link") or "")
    except Exception:
        pass
    return out


# ---------- analysis ----------
POSITIVE = ["contract", "order win", "wins order", "bags order", "order", "profit rises", "profit jumps",
            "record", "growth", "upgrade", "buy rating", "outperform", "beats", "surge", "rally",
            "expansion", "dividend", "bonus", "acquisition", "partnership", "approval", "strong",
            "raises guidance", "deal", "gains", "soars", "jumps"]
NEGATIVE = ["loss", "falls", "plunge", "downgrade", "sell rating", "underperform", "misses", "probe",
            "fraud", "lawsuit", "penalty", "fine", "default", "debt", "cut", "slump", "decline",
            "resigns", "recall", "ban", "raid", "weak", "warning", "drops", "tumbles", "crash"]


def score_news(items):
    import re
    total, scored = 0, []
    for n in items:
        text = (n["title"] + " " + n["summary"]).lower()
        pos = sum(1 for w in POSITIVE if re.search(r"\b" + re.escape(w), text))
        neg = sum(1 for w in NEGATIVE if re.search(r"\b" + re.escape(w), text))
        sv = 1 if pos > neg else -1 if neg > pos else 0
        total += sv
        scored.append({**n, "sentiment": sv})
    return total, scored


NEWS_TOPICS = {
    "orders": (["order win", "wins order", "bags order", "secures order", "new order", "new orders", "order inflow",
                "order book", "contract", "contracts", "letter of award", "loa", "awarded", "order worth",
                "orders worth"],
               "the company seems to have won new orders or contracts, which usually brings more income", "good"),
    "results_up": (["profit rises", "profit jumps", "profit surges", "profit up", "profit growth", "record profit",
                    "revenue growth", "revenue rises", "revenue jumps", "strong results", "strong earnings",
                    "beats estimates", "beats", "margin expansion", "turnaround", "turns profitable"],
                   "the company reported better profit or sales", "good"),
    "upgrade": (["upgrade", "upgrades", "upgraded", "buy rating", "target price raised", "raises target",
                 "outperform", "bullish", "accumulate"],
                "analysts are turning positive on the share", "good"),
    "expansion": (["expansion", "expands", "acquisition", "acquires", "acquire", "partnership", "joint venture",
                   "tie-up", "tie up", "mou", "new plant", "capacity addition", "launches", "launch"],
                  "the company is expanding or tying up with other firms", "good"),
    "holders": (["dividend", "bonus", "buyback", "stock split", "bonus issue"],
                "the company is rewarding shareholders (dividend, bonus or buyback)", "good"),
    "approval": (["approval", "approved", "clearance", "wins case", "relief"],
                 "it got an approval or a favourable decision", "good"),
    "momentum": (["surge", "surges", "rally", "rallies", "jumps", "soars", "upper circuit", "52-week high",
                  "record high", "gains"],
                 "the share price has been rising on market buzz", "good"),
    "results_down": (["loss", "losses", "net loss", "profit falls", "profit drops", "profit declines",
                      "profit down", "revenue falls", "revenue drops", "weak results", "weak earnings",
                      "misses", "margin pressure"],
                     "the company reported lower profit or a loss", "bad"),
    "downgrade": (["downgrade", "downgrades", "downgraded", "sell rating", "target price cut", "cuts target",
                   "underperform", "bearish"],
                  "analysts are turning negative on the share", "bad"),
    "debt": (["debt", "default", "insolvency", "nclt", "bankruptcy", "npa"],
             "the company has debt or loan-repayment worries", "bad"),
    "legal": (["probe", "fraud", "lawsuit", "raid", "sebi", "penalty", "penalised", "investigation",
               "show-cause", "show cause", "scam", "tax notice"],
              "the company faces legal or regulator trouble (probe, penalty or fraud claims)", "bad"),
    "people": (["resigns", "resignation", "steps down", "quits", "promoter pledge", "pledge", "pledged",
                "promoter sells", "stake sale", "offloads"],
               "senior people are leaving, or promoters are selling or pledging shares", "bad"),
    "price_down": (["falls", "plunge", "plunges", "slump", "slumps", "crash", "crashes", "tumbles",
                    "lower circuit", "52-week low", "sinks", "slides", "drops"],
                   "the share price has been falling sharply", "bad"),
    "setback": (["cancels", "cancelled", "canceled", "terminated", "shutdown", "halt", "halted", "fire",
                 "recall", "ban", "banned", "strike"],
                "the company had a business setback (cancelled order, shutdown or ban)", "bad"),
    "dilution": (["rights issue", "preferential issue", "qip", "dilution", "fund raise", "fundraise"],
                 "the company is raising money by issuing new shares, which can reduce existing holders' share", "bad"),
}


def explain_news(items):
    """Turns the recent headlines into a short plain-English summary (no headlines shown)."""
    if not items:
        return "No recent news could be found, so news was not counted."
    good, bad = {}, {}
    for n in items:
        text = (n["title"] + " " + n["summary"]).lower()
        for key, (words, _plain, kind) in NEWS_TOPICS.items():
            if any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words):
                d = good if kind == "good" else bad
                d[key] = d.get(key, 0) + 1

    def top(d):
        best = sorted(d.items(), key=lambda kv: -kv[1])[:3]
        return "; ".join(NEWS_TOPICS[k][1] + (f" ({c} stories)" if c > 1 else "") for k, c in best)

    gs, bs = sum(good.values()), sum(bad.values())
    g_txt = top(good) if good else "nothing clearly positive found"
    b_txt = top(bad) if bad else "nothing serious found"
    if gs == 0 and bs == 0:
        overall = "mostly general market chatter, nothing that clearly moves the price"
    elif gs > bs:
        overall = "leans GOOD"
    elif bs > gs:
        overall = "leans BAD"
    else:
        overall = "MIXED, good and bad in about equal measure"
    return (f"**Good:** {g_txt}.\n\n**Bad:** {b_txt}.\n\n**Overall:** {overall} "
            f"(automatic summary of {len(items)} recent stories, so details can be missed).")


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    down = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / down
    return 100 - 100 / (1 + rs)


PERIODS = [("1 week", 5), ("1 month", 21), ("3 months", 63), ("6 months", 126), ("1 year", 252)]


def analyse(symbol, as_of, buy_price=None):
    hist = get_history(symbol)
    if hist.empty:
        return None
    hist = hist[hist.index.date <= as_of]
    if len(hist) < 20:
        return None
    close = hist["Close"]
    price = float(close.iloc[-1])
    reasons, score = [], 0.0

    # 1. peaks / lows
    rows = []
    pos_scores = []
    for label, days in PERIODS:
        w = hist.tail(days)
        hi, lo = float(w["High"].max()), float(w["Low"].min())
        pos = (price - lo) / (hi - lo) if hi > lo else 0.5
        rows.append({"Period": label, "Low": round(lo, 2), "High": round(hi, 2),
                     "Price position in range": f"{pos*100:.0f}%"})
        pos_scores.append(1 if pos < 0.25 else -1 if pos > 0.75 else 0)
    range_score = sum(pos_scores) / len(pos_scores) * 2  # -2..+2
    score += range_score
    if range_score >= 1:
        reasons.append("Price is near the LOW end of most periods (1w to 1y) - cheap zone.")
    elif range_score <= -1:
        reasons.append("Price is near the HIGH end of most periods (1w to 1y) - expensive zone.")
    else:
        reasons.append("Price is mid-range across the periods - no strong high/low signal.")

    # 2. volume on last traded day
    last_vol = float(hist["Volume"].iloc[-1])
    avg_vol = float(hist["Volume"].iloc[-21:-1].mean()) if len(hist) > 21 else float(hist["Volume"].mean())
    vol_ratio = last_vol / avg_vol if avg_vol else 1
    day_chg = (price / float(close.iloc[-2]) - 1) * 100
    if vol_ratio >= 1.5 and day_chg > 0:
        score += 1
        reasons.append(f"Last day volume {vol_ratio:.1f}x average with price UP {day_chg:.1f}% - strong buying.")
    elif vol_ratio >= 1.5 and day_chg < 0:
        score -= 1
        reasons.append(f"Last day volume {vol_ratio:.1f}x average with price DOWN {abs(day_chg):.1f}% - heavy selling.")
    else:
        reasons.append(f"Last day volume is {vol_ratio:.1f}x the 20-day average - nothing unusual.")

    # 3. news (only meaningful for today / latest)
    news_total, news_items = 0, []
    news_text = "News is not available for past dates."
    if as_of >= date.today():
        news_total, news_items = score_news(get_news(symbol))
        news_text = explain_news(news_items)
        n_score = max(-2, min(2, news_total))
        score += n_score
        npos = sum(1 for x in news_items if x["sentiment"] > 0)
        nneg = sum(1 for x in news_items if x["sentiment"] < 0)
        if not news_items:
            reasons.append("NO news could be fetched (check internet). This is not the same as neutral news.")
        elif n_score > 0:
            reasons.append(f"News leans POSITIVE: {npos} positive vs {nneg} negative out of {len(news_items)} headlines.")
        elif n_score < 0:
            reasons.append(f"News leans NEGATIVE: {nneg} negative vs {npos} positive out of {len(news_items)} headlines.")
        else:
            reasons.append(f"News is mixed/neutral: {len(news_items)} headlines checked, {npos} positive, {nneg} negative.")
    else:
        reasons.append("News is not available for past dates, so it was skipped.")

    # 4. other parameters: RSI, moving averages, your holding
    r = float(rsi(close).iloc[-1])
    sma20, sma50 = float(close.tail(20).mean()), float(close.tail(50).mean())
    if r < 30:
        score += 1
        reasons.append(f"RSI {r:.0f} - oversold, bounce is likely.")
    elif r > 70:
        score -= 1
        reasons.append(f"RSI {r:.0f} - overbought, pullback is likely.")
    else:
        reasons.append(f"RSI {r:.0f} - normal.")
    if price > sma20 > sma50:
        score += 0.5
        reasons.append("Trend is UP (price above 20 and 50 day averages).")
    elif price < sma20 < sma50:
        score -= 0.5
        reasons.append("Trend is DOWN (price below 20 and 50 day averages).")

    pl_pct = None
    if buy_price:
        pl_pct = (price / buy_price - 1) * 100
        if pl_pct > 0:
            reasons.append(f"You are in PROFIT of {pl_pct:.1f}% on this share.")
        else:
            reasons.append(f"You are in LOSS of {abs(pl_pct):.1f}% on this share.")

    if score >= 1.5:
        verdict = "BUY"
    elif score <= -1.5:
        verdict = "SELL"
    else:
        verdict = "HOLD / WAIT"
    if verdict == "SELL" and pl_pct is not None and pl_pct < 0:
        reasons.append("Selling now books a loss; consider whether you can wait for recovery.")
    if verdict == "SELL" and pl_pct is not None and pl_pct > 0:
        reasons.append("Selling now would book a profit.")

    return {"price": price, "score": score, "verdict": verdict, "reasons": reasons,
            "range_table": pd.DataFrame(rows), "news": news_items, "news_text": news_text, "hist": hist}


def norm_symbol(sym, exch):
    sym = sym.strip().upper()
    if not sym:
        return ""
    if sym.endswith((".NS", ".BO")):
        return sym
    return sym + (".NS" if "NSE" in exch else ".BO" if "BSE" in exch else "")


def atr(hist, n=14):
    h, l, c = hist["High"], hist["Low"], hist["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def touch_stats(hist, need_pct, horizon):
    """Historical hit-rate: how often did this stock rise at least need_pct (at any close)
    within the next `horizon` trading days? Also checked only for days when it sat near its
    6-month low (like now, if it does)."""
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
    w_now = close[-126:]
    lo, hi = w_now.min(), w_now.max()
    now_low = ((close[-1] - lo) / (hi - lo) if hi > lo else 0.5) < 0.3
    base = float(np.mean(hits_all))
    n_low = len(hits_low)
    est = base
    if now_low and n_low:
        wt = n_low / (n_low + 40)
        est = wt * float(np.mean(hits_low)) + (1 - wt) * base
    return {"base": base, "n_low": n_low, "est": est, "now_low": now_low}


def evaluate_candidate(symbol, need_pct, horizon):
    res = analyse(symbol, date.today())
    if not res:
        return None
    hist = res["hist"]
    ts = touch_stats(hist, need_pct, horizon)
    if not ts:
        return None
    live = get_price(symbol) or res["price"]
    a = atr(hist)
    stop = max(live - 2 * a, live * 0.5)
    adv = float(hist["Volume"].tail(20).mean())
    factor = max(0.5, min(1.5, 1 + 0.1 * res["score"]))  # news / volume / trend / RSI adjust it
    chance = max(0.0, min(0.95, ts["est"] * factor))
    return {"symbol": symbol, "live": live, "stop": stop, "adv": adv, "chance": chance,
            "base": ts["base"], "now_low": ts["now_low"], "score": res["score"],
            "verdict": res["verdict"], "reasons": res["reasons"], "news": res["news"],
            "news_text": res["news_text"]}


IST = timezone(timedelta(hours=5, minutes=30))
PRICE_MIN, PRICE_MAX = 12.0, 35.0


@st.cache_data(ttl=900)
def download_many(tickers, period="1y"):
    """One batch download of 1 year of daily data for many shares."""
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
                if len(sub):
                    out[t] = sub
    elif len(tickers) == 1:
        sub = data.dropna(subset=["Close"])
        if len(sub):
            out[tickers[0]] = sub
    return out


@st.cache_data(ttl=86400)
def get_nse_universe():
    """All NSE equity symbols (official list). Returns [] if the download is blocked."""
    import io
    import urllib.request
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
        return sorted({str(x).strip().upper() + ".NS" for x in df["SYMBOL"]})
    except Exception:
        return []


def prefilter_universe(universe, min_move, progress=None):
    """Quick pass over a big list: keep only shares priced near 12-35 that swing enough."""
    keep, chunk_n = [], 150
    for i in range(0, len(universe), chunk_n):
        chunk = tuple(universe[i:i + chunk_n])
        data = download_many(chunk, "5d")
        for t, sub in data.items():
            if len(sub) < 3:
                continue
            last = float(sub["Close"].iloc[-1])
            if not (PRICE_MIN * 0.9 <= last <= PRICE_MAX * 1.1):
                continue
            rng_pct = float(((sub["High"] - sub["Low"]) / sub["Close"]).mean() * 100)
            if rng_pct >= min_move * 0.6:
                keep.append(t)
        if progress is not None:
            progress.progress(min(1.0, (i + chunk_n) / len(universe)),
                              text=f"Checking prices... {min(i + chunk_n, len(universe))} of {len(universe)}")
    return keep


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
    return {
        "price": price, "atr": a, "atr_pct": a / price * 100,
        "recent_low": recent_low,
        "low_pos": (recent_low - lo6) / (hi6 - lo6) if hi6 > lo6 else 0.5,
        "bounce": (price / recent_low - 1) * 100,
        "base_vol": base_vol,
        "spike": float(vol.tail(5).max()) / base_vol if base_vol else 0.0,
        "rsi": float(rsi(close).iloc[-1]),
        "sma5": float(close.tail(5).mean()),
        "up_days": int((close.diff().tail(3) > 0).sum()),
    }


def tech_score(m, min_move=2.2):
    """Returns (score, reasons) or None if the share does not fit the rebound pattern."""
    if m["base_vol"] < 100000:
        return None
    s, why = 0.0, []
    if m["atr_pct"] < min_move:
        return None
    if m["atr_pct"] >= 5:
        s += 3
        why.append(f"Very volatile: swings about {m['atr_pct']:.1f}% a day, so profit (and loss) can come fast.")
    elif m["atr_pct"] >= 3:
        s += 2
        why.append(f"Volatile: swings about {m['atr_pct']:.1f}% a day.")
    else:
        s += 1
        why.append(f"Fairly active: swings about {m['atr_pct']:.1f}% a day.")
    if m["low_pos"] <= 0.25:
        s += 3
        why.append("Touched a low in the last 10 days, near its 6-month bottom.")
    elif m["low_pos"] <= 0.4:
        s += 1.5
        why.append("Touched a low in the last 10 days, in the lower part of its 6-month range.")
    else:
        return None
    b = m["bounce"]
    if 3 <= b <= 18 and m["price"] > m["sma5"] and m["up_days"] >= 2:
        s += 3
        why.append(f"Rebounding: up {b:.1f}% from its low with {m['up_days']} of the last 3 days higher.")
    elif b < 3:
        s += 1
        why.append("Still sitting at the low; the bounce is not confirmed yet.")
    elif b > 18:
        why.append(f"Already up {b:.0f}% from the low, so you may be late.")
    else:
        s += 1.5
        why.append(f"Up {b:.1f}% from the low but not clearly confirmed.")
    if m["spike"] >= 2:
        s += 2
        why.append(f"Volume jumped to {m['spike']:.1f}x normal in the last 5 days.")
    elif m["spike"] >= 1.5:
        s += 1
        why.append(f"Volume rose to {m['spike']:.1f}x normal in the last 5 days.")
    else:
        why.append("No volume jump yet.")
    if 35 <= m["rsi"] <= 65:
        s += 1
        why.append(f"RSI {m['rsi']:.0f}: room to move up.")
    elif m["rsi"] > 72:
        s -= 1
        why.append(f"RSI {m['rsi']:.0f}: already overheated.")
    return s, why


def run_scan(tickers, min_move=2.2):
    data = download_many(tuple(tickers))
    rows = []
    for t, sub in data.items():
        m = rebound_metrics(sub)
        if not m or not (PRICE_MIN <= m["price"] <= PRICE_MAX):
            continue
        ts = tech_score(m, min_move)
        if ts is None:
            continue
        rows.append({"symbol": t, "m": m, "score": ts[0], "why": ts[1], "hist": sub})
    rows.sort(key=lambda r: r["score"], reverse=True)
    top = rows[:10]
    for r in top:
        nt, items = score_news(get_news(r["symbol"]))
        r["news"] = items
        r["news_score"] = max(-2, min(2, nt))
        r["news_text"] = explain_news(items)
        r["final"] = r["score"] + r["news_score"]
    top = [r for r in top if r["news_score"] > -2]
    top.sort(key=lambda r: r["final"], reverse=True)
    return {"top": top[:5], "downloaded": len(data), "in_range": len(rows)}


# ---------- buy timing ----------
def market_state():
    now = datetime.now(IST)
    wait_t = st.session_state.get("wait_time", time(12, 0))
    t = now.time()
    if now.weekday() >= 5:
        return "closed", now, wait_t
    if t < time(9, 15):
        return "pre", now, wait_t
    if t >= time(15, 30):
        return "closed", now, wait_t
    if t < wait_t:
        return "wait", now, wait_t
    return "ok", now, wait_t


def timing_banner():
    state, now, wait_t = market_state()
    if state == "wait":
        target = now.replace(hour=wait_t.hour, minute=wait_t.minute, second=0, microsecond=0)
        mins = int((target - now).total_seconds() // 60) + 1
        st.warning(f"⏰ WAIT before buying. It is {now:%H:%M} IST and your rule is not to buy before "
                   f"{wait_t:%H:%M}. About {mins // 60}h {mins % 60}m to go. The morning hours are often the "
                   "most jumpy, so buying early can mean paying more.")
    elif state == "pre":
        st.info(f"Market opens at 09:15 IST. Plan to buy after {wait_t:%H:%M}.")
    elif state == "ok":
        extra = " The last 30 minutes can be sharp, so avoid chasing." if now.time() >= time(15, 0) else ""
        st.success(f"✅ Your buy window is open ({now:%H:%M} IST, after {wait_t:%H:%M}).{extra}")
    else:
        st.info(f"Market is closed now ({now:%a %H:%M} IST). Check again after {wait_t:%H:%M} on the next trading day. "
                "(NSE holidays are not tracked here.)")
    return state


@st.cache_data(ttl=900)
def get_intraday(symbol):
    try:
        d = yf.Ticker(symbol).history(period="1mo", interval="5m", auto_adjust=False)
        return d.dropna(subset=["Close"])
    except Exception:
        return pd.DataFrame()


def _to_ist(df):
    d = df.copy()
    try:
        d.index = d.index.tz_convert(IST)
    except Exception:
        pass
    return d


def timing_stats(df):
    """Tests the rule 'buying after 12:00 beats buying around 10:30' on this share's last month."""
    if df is None or df.empty:
        return None
    d = _to_ist(df)
    rows = []
    for _, g in d.groupby(d.index.date):
        if len(g) < 30:
            continue
        tt = g.index.time

        def at(hh, mm):
            m = g[[x >= time(hh, mm) for x in tt]]
            return float(m["Close"].iloc[0]) if len(m) else None

        early, late, close = at(10, 30), at(12, 15), float(g["Close"].iloc[-1])
        if early is None or late is None:
            continue
        low_t = g["Low"].idxmin().time()
        rows.append((late / early - 1, close / early - 1, close / late - 1, low_t >= time(12, 0)))
    if len(rows) < 5:
        return None
    arr = np.array(rows, dtype=float)
    return {"days": len(rows), "late_cheaper": float((arr[:, 0] < 0).mean()),
            "median_move": float(np.median(arr[:, 0]) * 100),
            "ret_early": float(arr[:, 1].mean() * 100), "ret_late": float(arr[:, 2].mean() * 100),
            "low_after_noon": float(arr[:, 3].mean())}


def today_position(df):
    if df is None or df.empty:
        return None
    d = _to_ist(df)
    last_day = d.index.date[-1]
    g = d[d.index.date == last_day]
    lo, hi, now_p = float(g["Low"].min()), float(g["High"].max()), float(g["Close"].iloc[-1])
    return {"day": last_day, "low": lo, "high": hi, "now": now_p,
            "pos": (now_p - lo) / (hi - lo) if hi > lo else 0.5}


# ---------- UI ----------
st.title("📈 My Stock Tool")
init_storage()
portfolio = st.session_state.portfolio

with st.sidebar:
    st.header("➕ Add share")
    with st.form("add_form", clear_on_submit=True):
        sym = st.text_input("Symbol (e.g. RELIANCE, TCS, AAPL)").strip().upper()
        exch = st.selectbox("Exchange", ["NSE (.NS)", "BSE (.BO)", "US / other (no suffix)"])
        qty = st.number_input("Quantity", min_value=1, value=1, step=1)
        bp = st.number_input("Your buying price (per share)", min_value=0.0, value=0.0, step=0.5)
        add = st.form_submit_button("Add")
    if add and sym and bp > 0:
        suffix = ".NS" if "NSE" in exch else ".BO" if "BSE" in exch else ""
        full = sym if sym.endswith((".NS", ".BO")) else sym + suffix
        portfolio.append({"symbol": full, "qty": int(qty), "buy_price": float(bp)})
        persist()
        st.success(f"Added {full}")
        st.rerun()
    with st.expander("💾 Backup / restore"):
        st.download_button("Download backup", json.dumps(portfolio, indent=2),
                           file_name="my_shares_backup.json", mime="application/json")
        up = st.file_uploader("Restore from backup file", type=["json"])
        if up is not None and st.button("Import this file"):
            try:
                data = json.loads(up.read().decode("utf-8"))
                clean = [{"symbol": str(d["symbol"]), "qty": int(d["qty"]), "buy_price": float(d["buy_price"])}
                         for d in data]
                st.session_state.portfolio = clean
                persist()
                st.rerun()
            except Exception:
                st.error("That file is not a valid backup.")
    if st.button("🔄 Refresh prices"):
        st.cache_data.clear()
        st.rerun()
    st.time_input("Don't buy before (IST)", value=time(12, 0), key="wait_time")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["Portfolio", "Predict buy / sell", "Loss Recovery Planner",
                                         "Rebound Radar", "Buy Timer"])

with tab1:
    if not portfolio:
        st.info("No shares yet. Add one from the sidebar.")
    else:
        rows = []
        for i, h in enumerate(portfolio):
            p = get_price(h["symbol"])
            invested = h["qty"] * h["buy_price"]
            value = h["qty"] * p if p else None
            pl = value - invested if value is not None else None
            rows.append({"#": i, "Symbol": h["symbol"], "Qty": h["qty"], "Buy price": h["buy_price"],
                         "Live price": round(p, 2) if p else None, "Invested": round(invested, 2),
                         "Value": round(value, 2) if value else None,
                         "P/L": round(pl, 2) if pl is not None else None,
                         "P/L %": round(pl / invested * 100, 2) if pl is not None else None})
        df = pd.DataFrame(rows)
        tot_inv = df["Invested"].sum()
        tot_val = df["Value"].sum()
        c1, c2, c3 = st.columns(3)
        c1.metric("Invested", f"{tot_inv:,.2f}")
        c2.metric("Current value", f"{tot_val:,.2f}")
        c3.metric("Total P/L", f"{tot_val - tot_inv:,.2f}", f"{(tot_val / tot_inv - 1) * 100:.2f}%" if tot_inv else None)

        def colour(v):
            if pd.isna(v):
                return ""
            return "color: green" if v > 0 else "color: red" if v < 0 else ""

        st.dataframe(df.drop(columns="#").style.map(colour, subset=["P/L", "P/L %"]),
                     use_container_width=True, hide_index=True)

        with st.expander("Remove a share"):
            rm = st.selectbox("Select", [f"{r['#']} - {r['Symbol']} ({r['Qty']} @ {r['Buy price']})" for r in rows])
            if st.button("Delete selected"):
                portfolio.pop(int(rm.split(" - ")[0]))
                persist()
                st.rerun()

with tab2:
    if not portfolio:
        st.info("Add a share first.")
    else:
        labels = [f"{h['symbol']} (bought @ {h['buy_price']})" for h in portfolio]
        choice = st.selectbox("Select share", labels)
        h = portfolio[labels.index(choice)]
        when = st.date_input("Date to decide for", value=date.today(), max_value=date.today())
        if st.button("🔮 Predict buy / sell", type="primary"):
            with st.spinner("Analysing..."):
                res = analyse(h["symbol"], when, h["buy_price"])
            if not res:
                st.error("Not enough data for this symbol/date. Check the symbol and exchange.")
            else:
                colour_map = {"BUY": "green", "SELL": "red"}
                st.markdown(f"## Verdict: :{colour_map.get(res['verdict'], 'orange')}[{res['verdict']}]")
                st.caption(f"Price on {when}: {res['price']:.2f}  |  Score: {res['score']:+.1f} (range -5 to +5)")
                if res["verdict"] == "BUY":
                    timing_banner()
                st.subheader("Why")
                for r in res["reasons"]:
                    st.write("• " + r)
                st.subheader("Peak high / low")
                st.dataframe(res["range_table"], hide_index=True, use_container_width=True)
                st.line_chart(res["hist"]["Close"])
                st.subheader("What the news says")
                st.write(res["news_text"])
                st.warning("This is a rule-based helper, not financial advice. Markets can move against any signal.")


with tab3:
    st.subheader("Loss Recovery Planner")
    st.caption("Compare HOLDING a losing share against SWITCHING that money into other shares. "
               "Chances are estimates from this stock's own last-year history plus today's news, "
               "volume, trend and RSI. They are not guarantees.")
    losers = []
    for i, h in enumerate(portfolio):
        p = get_price(h["symbol"])
        if p and p < h["buy_price"]:
            losers.append((i, h, p))
    if not losers:
        st.success("None of your shares is currently in loss (or prices could not be fetched).")
    else:
        labels3 = [f"{h['symbol']}  |  loss {(h['buy_price'] - p) * h['qty']:,.0f}" for _, h, p in losers]
        pick = st.selectbox("Share I am in loss on", labels3)
        _, lh, lp = losers[labels3.index(pick)]
        loss_amt = (lh["buy_price"] - lp) * lh["qty"]

        others = [h["symbol"] for h in portfolio if h["symbol"] != lh["symbol"]]
        chosen = st.multiselect("Shares to consider buying (from my holdings)", others)
        ex1, ex2 = st.columns([2, 1])
        extra_txt = ex1.text_input("Other shares to consider (comma separated, e.g. SUZLON, IDEA)")
        extra_exch = ex2.selectbox("Exchange for these", ["NSE (.NS)", "BSE (.BO)", "US / other (no suffix)"],
                                   key="extra_exch")
        c1, c2, c3, c4 = st.columns(4)
        hz_label = c1.selectbox("Time I can wait", ["1 week", "1 month", "3 months"], index=1)
        horizon = {"1 week": 5, "1 month": 21, "3 months": 63}[hz_label]
        extra_cash = c2.number_input("Extra cash to add", min_value=0.0, value=0.0, step=1000.0)
        risk_total = c3.number_input("Max extra loss I accept", min_value=0.0, value=3000.0, step=500.0)
        cost_pct = c4.number_input("Charges per trade %", min_value=0.0, value=0.2, step=0.05)

        if st.button("Analyse recovery chances", type="primary"):
            cands = list(chosen)
            for t in extra_txt.split(","):
                f = norm_symbol(t, extra_exch)
                if f and f not in cands and f != lh["symbol"]:
                    cands.append(f)
            cands = cands[:6]
            if not cands:
                st.warning("Select or type at least one share to compare.")
                st.stop()

            proceeds = lp * lh["qty"] * (1 - cost_pct / 100)
            deploy = proceeds + extra_cash
            costs = deploy * cost_pct / 100 * 3
            need_pct = (loss_amt + costs) / deploy * 100

            with st.spinner("Checking history, news, volume and trend..."):
                results = [r for r in (evaluate_candidate(c, need_pct, horizon) for c in cands) if r]
                hold_need = (lh["buy_price"] / lp - 1) * 100
                hold_ts = touch_stats(get_history(lh["symbol"]), hold_need, horizon)
                hold_res = analyse(lh["symbol"], date.today(), lh["buy_price"])

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Current loss", f"{loss_amt:,.0f}")
            m2.metric("Money to redeploy", f"{deploy:,.0f}")
            m3.metric(f"Gain needed in {hz_label}", f"{need_pct:.1f}%")
            m4.metric("Charges (3 trades)", f"{costs:,.0f}")
            if need_pct > 15:
                st.warning(f"You need about +{need_pct:.0f}% on the new money just to win the loss back. "
                           "That is a big move for a short wait, so chances below will usually look low.")

            st.markdown("### Option A: keep the losing share")
            if hold_ts:
                hf = max(0.5, min(1.5, 1 + 0.1 * hold_res["score"])) if hold_res else 1
                hold_chance = max(0.0, min(0.95, hold_ts["est"] * hf))
                st.write(f"**{lh['symbol']}** needs **+{hold_need:.1f}%** to get back to your buying price "
                         f"({lh['buy_price']}). Estimated chance within {hz_label}: **{hold_chance*100:.0f}%**. "
                         f"Its own signal today: **{hold_res['verdict'] if hold_res else 'n/a'}**.")
            else:
                hold_chance = None
                st.write("Not enough history for this share.")

            st.markdown("### Option B: sell it and buy these")
            if not results:
                st.error("Could not get data for the selected shares. Check symbols and exchange.")
            else:
                eligible = [r for r in results if r["chance"] >= 0.10 and r["verdict"] != "SELL"]
                tot = sum(r["chance"] for r in eligible)
                table = []
                for r in results:
                    w = r["chance"] / tot if (tot and r in eligible) else 0
                    alloc = deploy * w
                    risk_ps = max(r["live"] - r["stop"], r["live"] * 0.01)
                    qty = int(min(alloc / r["live"], (risk_total * w) / risk_ps)) if w else 0
                    liq = ""
                    if qty and r["adv"] and qty / r["adv"] > 0.02:
                        liq = "Low volume: hard to exit"
                    if not w:
                        liq = "Not bought: bearish outlook" if r["verdict"] == "SELL" else "Not bought: chance under 10%"
                    if r["chance"] >= 0.35 and r["score"] >= 0.5:
                        tag = "Strong"
                    elif r["chance"] >= 0.2:
                        tag = "Possible"
                    else:
                        tag = "Weak / avoid"
                    target_px = r["live"] * (1 + need_pct / 100)
                    profit = qty * (target_px - r["live"])
                    outlook = {"BUY": "Bullish", "SELL": "Bearish"}.get(r["verdict"], "Neutral")
                    table.append({"Share": r["symbol"], "Live": round(r["live"], 2),
                                  "Chance of reaching target": f"{r['chance']*100:.0f}%",
                                  "Rating": tag, "Its own outlook": outlook,
                                  "Buy qty": qty, "Amount": round(qty * r["live"]),
                                  "Stop-loss": round(r["stop"], 2),
                                  "Target": round(target_px, 2),
                                  "Profit if target hit": round(profit),
                                  "Of your loss": f"{profit / loss_amt * 100:.0f}%" if loss_amt else "",
                                  "Note": liq})
                st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
                if any(t["Buy qty"] for t in table):
                    timing_banner()
                spend = sum(t["Amount"] for t in table)
                tot_profit = sum(t["Profit if target hit"] for t in table)
                st.caption(f"Total to buy: {spend:,.0f}. The rest ({deploy - spend:,.0f}) stays as cash. "
                           "'Its own outlook' is that share's own trend/news/volume reading, not advice about "
                           "your losing share.")
                bt = max(results, key=lambda r: r["chance"])
                dist = (bt["live"] - bt["stop"]) / bt["live"]
                st.warning(
                    f"Reality check: if every target is hit you make about {tot_profit:,.0f}, which is only "
                    f"{tot_profit / loss_amt * 100:.0f}% of your {loss_amt:,.0f} loss. The size is small because "
                    f"your 'max extra loss' limit ({risk_total:,.0f}) caps how much you can put in. To win the "
                    f"whole loss back you would have to put in about all {deploy:,.0f}, and if the stop-loss "
                    f"then hit you would lose roughly {deploy * dist:,.0f} more. Recovering a large loss "
                    f"quickly needs a big bet, and that is the trap this tool is meant to warn you about.")

                best = max(results, key=lambda r: r["chance"])
                if hold_chance is not None:
                    if best["chance"] > hold_chance + 0.10 and best["verdict"] != "SELL":
                        st.success(f"Switching looks better: best pick {best['symbol']} at about "
                                   f"{best['chance']*100:.0f}% vs {hold_chance*100:.0f}% for staying.")
                    elif hold_chance >= best["chance"]:
                        st.info(f"Staying looks as good or better: holding gives about {hold_chance*100:.0f}% "
                                f"vs {best['chance']*100:.0f}% for the best alternative, and switching locks in "
                                f"today's loss of {loss_amt:,.0f} plus charges.")
                    else:
                        st.info("No clear winner. The difference is too small to justify booking the loss.")

                for r in results:
                    with st.expander(f"Why {r['symbol']}: {r['chance']*100:.0f}%"):
                        for x in r["reasons"]:
                            st.write("• " + x)
                        st.write(f"Historically it rose this much within {hz_label} about "
                                 f"{r['base']*100:.0f}% of the time"
                                 + (" (now near its 6-month low, so lows were weighted in)." if r["now_low"] else "."))
                        st.write("**News in plain words**")
                        st.write(r["news_text"])
            st.warning("A low share price (for example 15) is not the same as cheap. What matters is the % move, "
                       "volume and news. Selling a loser to chase a quick win is how losses grow, so keep to the stop-loss.")


with tab4:
    st.subheader("Rebound Radar")
    st.caption("Finds volatile shares priced between 12 and 35 that hit a recent low, are bouncing back, show a "
               "volume jump and have positive news, and compares them with the sleepy shares tying up your money.")

    rows4 = []
    for h in portfolio:
        p = get_price(h["symbol"])
        hh = get_history(h["symbol"])
        if not p or hh.empty or len(hh) < 20:
            continue
        ap = atr(hh) / p * 100
        val = p * h["qty"]
        rows4.append({"Share": h["symbol"], "Value": round(val),
                      "P/L": round(val - h["qty"] * h["buy_price"]),
                      "Daily move %": round(ap, 2), "Daily move on this money": round(val * ap / 100),
                      "Type": "Sleepy" if ap < 1.5 else "Active", "_p": p, "_q": h["qty"]})
    rows4.sort(key=lambda r: r["Daily move %"])
    amount = 0.0
    move_sym = None
    if rows4:
        st.markdown("#### Where your money sits (sleepiest first)")
        st.dataframe(pd.DataFrame(rows4).drop(columns=["_p", "_q"]), hide_index=True, use_container_width=True)
        move_sym = st.selectbox("Money I would move out of", [r["Share"] for r in rows4])
        mrow = next(r for r in rows4 if r["Share"] == move_sym)
        amount = st.number_input("Amount to invest in the new share", min_value=0.0, value=float(mrow["Value"]),
                                 step=1000.0, key=f"amt_{move_sym}")
        st.caption(f"Selling {move_sym} books a P/L of {mrow['P/L']:,} and frees about {mrow['Value']:,}. "
                   f"Its usual daily swing on this money is only about {mrow['Daily move on this money']:,}.")
    else:
        amount = st.number_input("Amount to invest", min_value=0.0, value=100000.0, step=1000.0)

    r1, r2, r3 = st.columns(3)
    tgt = r1.number_input("Profit target %", min_value=1.0, value=6.0, step=0.5)
    hz4_label = r2.selectbox("Time I can wait", ["1 week", "1 month"], key="hz4")
    hz4 = {"1 week": 5, "1 month": 21}[hz4_label]
    min_move = r3.number_input("Min typical daily move %", min_value=1.0, value=5.0, step=0.5)

    with st.expander(f"Scan list ({len(st.session_state.watch)} shares). Only these are scanned."):
        txt = st.text_area("One or more symbols, comma separated (NSE assumed if no .NS or .BO)",
                           ", ".join(st.session_state.watch), height=120)
        if st.button("Save scan list"):
            new = []
            for t in txt.replace("\n", ",").split(","):
                t = t.strip().upper()
                if t:
                    t = t if "." in t else t + ".NS"
                    if t not in new:
                        new.append(t)
            st.session_state.watch = new
            st.session_state.save_w += 1
            st.rerun()
        st.caption("The starting list is a guess of low-priced shares. I could not check today's prices, so "
                   "remove or add shares as you like. Prices outside 12 to 35 are skipped automatically.")

    scan_all = st.checkbox("Scan the whole NSE market too (slower, about 1 to 3 minutes)")
    if st.button("Find top 5 rebound shares", type="primary"):
        pool = list(dict.fromkeys(st.session_state.watch + [h["symbol"] for h in portfolio]))[:120]
        if scan_all:
            uni = get_nse_universe()
            if not uni:
                st.warning("Could not download the NSE share list (it may be blocked on your network). "
                           "Scanning only your scan list instead.")
            else:
                prog = st.progress(0.0, text="Checking prices...")
                found = prefilter_universe(uni, min_move, prog)
                prog.empty()
                st.caption(f"Whole market: {len(uni)} NSE shares checked, {len(found)} priced near 12 to 35 with enough movement.")
                pool = list(dict.fromkeys(pool + found))[:400]
        if not pool:
            st.warning("Your scan list is empty.")
        else:
            with st.spinner(f"Scanning {len(pool)} shares (history, volume, news)..."):
                out = run_scan(pool, min_move)
            st.caption(f"Data found for {out['downloaded']} of {len(pool)} shares. "
                       f"{out['in_range']} were in the 12 to 35 range and fit the rebound pattern.")
            top = out["top"]
            st.session_state.radar_top = [r["symbol"] for r in top]
            if not top:
                st.info("No share matches right now. That is a valid answer: forcing a trade when nothing "
                        "fits is how losses happen. Try again later or widen your scan list.")
            else:
                table = []
                for rank, r in enumerate(top, 1):
                    m = r["m"]
                    live = get_price(r["symbol"]) or m["price"]
                    stop = max(live - 2 * m["atr"], m["recent_low"] * 0.985)
                    if stop >= live:
                        stop = live * 0.97
                    qty = int(amount // live) if live else 0
                    tpx = live * (1 + tgt / 100)
                    ts = touch_stats(r["hist"], tgt, hz4)
                    chance = None
                    if ts:
                        chance = max(0.0, min(0.9, ts["est"] * max(0.6, min(1.4, 0.6 + 0.07 * r["final"]))))
                    r["chance"] = chance
                    table.append({"#": rank, "Share": r["symbol"], "Live": round(live, 2),
                                  "Daily move %": round(m["atr_pct"], 1), "Setup score": round(r["final"], 1),
                                  f"Chance of +{tgt:g}% in {hz4_label}": f"{chance*100:.0f}%" if chance is not None else "n/a",
                                  "Qty": qty, "Amount": round(qty * live), "Stop-loss": round(stop, 2),
                                  "Target": round(tpx, 2), "Profit at target": round(qty * (tpx - live)),
                                  "Loss at stop": round(qty * (live - stop)),
                                  "Reward/Risk": round((tpx / live - 1) / ((live - stop) / live), 2) if live > stop else None,
                                  "Typical daily move": round(qty * live * m["atr_pct"] / 100)})
                st.markdown("### Top picks")
                st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
                st.caption("Qty assumes you put the whole amount into that one share. You can split it across "
                           "several. Reward/Risk below 1 means you risk more than you can gain: very volatile shares "
                           "need a wide stop-loss, so raise your profit target or invest less in them.")
                if len(top) < 5:
                    st.info(f"Only {len(top)} share(s) met every condition (price 12 to 35, daily move of at least "
                            f"{min_move:g}%, near a recent low, bouncing, no bad news). Lower the minimum daily move "
                            "or add more shares to the scan list for more picks.")
                timing_banner()
                for r in top:
                    with st.expander(f"Why {r['symbol']} (setup score {r['final']:.1f})"):
                        for x in r["why"]:
                            st.write("• " + x)
                        st.write("**News in plain words**")
                        st.write(r["news_text"])
                st.warning("Spotting a rebound after it has happened is easy; catching one before it happens is "
                           "not. Volatile shares fall as fast as they rise, so always place the stop-loss, and "
                           "remember that selling a holding books its P/L plus charges.")

with tab5:
    st.subheader("Buy Timer")
    st.caption("Press the button before you buy anything on your broker app. This tool never places orders.")
    opts = list(dict.fromkeys([h["symbol"] for h in portfolio] + st.session_state.get("radar_top", [])))
    b1, b2 = st.columns(2)
    sel5 = b1.selectbox("Share", opts) if opts else None
    typed5 = b2.text_input("...or type a symbol (NSE assumed)").strip().upper()
    if typed5 and "." not in typed5:
        typed5 += ".NS"
    if st.button("Buy suggestion", type="primary"):
        sym5 = typed5 or sel5
        if not sym5:
            st.warning("Pick or type a share first.")
        else:
            state5 = timing_banner()
            if not sym5.endswith((".NS", ".BO")):
                st.info("The intraday check is built for Indian market hours, so it is skipped for this symbol.")
            else:
                with st.spinner("Checking the last month of intraday prices..."):
                    intr = get_intraday(sym5)
                    stt = timing_stats(intr)
                    pos = today_position(intr)
                if stt:
                    st.markdown(f"#### Does waiting until after 12:00 help {sym5}?")
                    a1, a2, a3 = st.columns(3)
                    a1.metric("Cheaper at 12:15 than 10:30", f"{stt['late_cheaper']*100:.0f}% of days")
                    a2.metric("Day's low came after noon", f"{stt['low_after_noon']*100:.0f}% of days")
                    a3.metric("Typical 10:30 to 12:15 move", f"{stt['median_move']:+.2f}%")
                    st.write(f"Over the last {stt['days']} trading days, buying at 10:30 gained on average "
                             f"**{stt['ret_early']:+.2f}%** by close, while buying at 12:15 gained "
                             f"**{stt['ret_late']:+.2f}%**.")
                    if stt["late_cheaper"] >= 0.6:
                        st.success("Your rule holds for this share: it was usually cheaper after noon.")
                    elif stt["late_cheaper"] <= 0.4:
                        st.info("For this share the pattern was the opposite: it usually rose after 10:30, so waiting "
                                "often cost you. Your rule is still a safe habit, but the data does not back it here.")
                    else:
                        st.info("No clear pattern for this share. Treat the wait rule as a habit, not a fact.")
                    st.caption("Only about a month of data, so treat this as a hint, not proof.")
                else:
                    st.info("Not enough intraday data to test the pattern for this share.")
                if pos:
                    st.write(f"Latest session ({pos['day']}): low {pos['low']:.2f}, high {pos['high']:.2f}, "
                             f"last {pos['now']:.2f}, sitting {pos['pos']*100:.0f}% of the way up the day's range.")
                    if state5 == "ok" and pos["pos"] > 0.8:
                        st.warning("It is near today's high. Consider waiting for a small dip before buying.")
                    elif state5 == "ok":
                        st.success("The price is not stretched in today's range, and your wait time has passed.")