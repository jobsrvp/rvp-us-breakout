import yfinance as yf
import pandas as pd
import numpy as np
import os
import requests
import json
import time
from datetime import datetime
import talib as ta
import random
import io

# ─── CONFIG ─────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Minimum Market Cap Filter ($1 Billion USD)
MIN_MARKET_CAP = 2_500_000_000  # Adjust as needed (e.g., 500_000_000 for $500M)

# Popular sample tickers listed directly on NASDAQ for test metrics
MY_PORTFOLIO = [""]

BREADTH_FILE = "breadth_history.json"
METADATA_FILE = "metadata_cache.json"
RS_RATING_CACHE = "rs_rating_cache.json"
NEAR_RANGE_PCT = 10

NASDAQ_CACHE_FILE = "nasdaq_tickers_cache.csv"   # Shared with RS Rating generator

# ─── SHARED NASDAQ FILTERING (Same as RS Generator) ─────
def load_nasdaq_tradable_stocks():
    """Load from shared cache with strict filtering for common tradable stocks only"""
    if not os.path.exists(NASDAQ_CACHE_FILE):
        print("❌ NASDAQ cache file not found. Please run generate_nasdaq_rs.py first.")
        return ["AAPL", "MSFT", "NVDA"]  # fallback
    
    print("📂 Loading NASDAQ tickers from cache...")
    df = pd.read_csv(NASDAQ_CACHE_FILE)
    
    def is_common_tradable(row):
        symbol = str(row['Symbol']).strip()
        name = str(row.get('Security Name', '')).lower()
        
        if not symbol or len(symbol) > 5:
            return False
        if any(c in symbol for c in ['.', '-', '^', '/', '\\', '+', '*']):
            return False
        if symbol.endswith(('W', 'R', 'U', 'P', 'V', 'Q', 'T')):  # Warrants, Rights, Units, etc.
            return False
        
        exclude_keywords = ['warrant', 'right', 'unit', 'preferred', 'etf', 'fund', 
                           'trust', 'note', 'debenture', 'bond', 'depositary', 'acquisition']
        if any(kw in name for kw in exclude_keywords):
            return False
        
        if row.get('Financial Status') != 'N' or row.get('Market Category') not in ['Q', 'G', 'S']:
            return False
        return True
    
    tradable_df = df[df.apply(is_common_tradable, axis=1)]
    symbols = tradable_df['Symbol'].astype(str).str.strip().tolist()
    print(f"✅ Loaded {len(symbols)} tradable common stocks (ETFs, warrants, units excluded)")
    return symbols


# ─── RS RATING CACHE ─────────────────────────────────
def load_rs_ratings():
    if os.path.exists(RS_RATING_CACHE):
        try:
            with open(RS_RATING_CACHE, "r") as f:
                return json.load(f)
        except:
            print("⚠️ Could not load RS Rating cache")
    return {}


# ─── METADATA CACHE ─────────────────────────────────
def load_metadata():
    if os.path.exists(METADATA_FILE):
        with open(METADATA_FILE, "r") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_metadata(cache):
    with open(METADATA_FILE, "w") as f:
        json.dump(cache, f, indent=4)


# ─── UTIL ───────────────────────────────────────────
def get_tv_link(symbol):
    return f"https://www.tradingview.com/chart/?symbol={symbol.upper()}"


def extract_symbol_df(data_dict, symbol):
    try:
        if symbol in data_dict:
            df = data_dict[symbol].copy()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df.dropna()
    except Exception as e:
        print(f"Error extracting {symbol}: {e}")
    return None


# ─── PORTFOLIO EXIT ─────────────────────────────────
def check_two_stage_exit(df, symbol):
    if df is None or len(df) < 30:
        return "HOLD"

    df = df[['Open','High','Low','Close']].copy()
    df['EMA21'] = ta.EMA(df['Close'].values.flatten().astype(float), timeperiod=21)

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    curr_close = float(curr['Close'])
    curr_ema   = float(curr['EMA21'])
    prev_close = float(prev['Close'])
    prev_ema   = float(prev['EMA21'])
    prev_low   = float(prev['Low'])

    tv = get_tv_link(symbol)

    if prev_close < prev_ema and curr_close < prev_low:
        return f"🔴 [EXIT]({tv}) (Stage 2)"
    elif curr_close < curr_ema:
        return f"🟡 [WATCH]({tv}) (Stage 1)"

    return "HOLD"


# ─── HISTORY, STAGE, REGIME ─────────────────────────
def load_history():
    if os.path.exists(BREADTH_FILE):
        with open(BREADTH_FILE, "r") as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def save_history(entry):
    data = load_history()
    data.append(entry)
    MAX_ENTRIES = 50000
    if len(data) > MAX_ENTRIES:
        data = data[-MAX_ENTRIES:]
    with open(BREADTH_FILE, "w") as f:
        json.dump(data, f, indent=4)

def detect_stage(hist):
    if len(hist) < 10: return "UNKNOWN"
    curr = hist[-1]
    prev = hist[-5]
    b, h, l = curr["breadth_pct"], curr["near_high_pct"], curr["near_low_pct"]
    bt = "rising" if b > prev["breadth_pct"] else "falling"
    lt = "rising" if l > prev["near_low_pct"] else "falling"
    ht = "rising" if h > prev["near_high_pct"] else "falling"

    if b < 10 and l > 50: return "🔥 CAPITULATION (AGGRESSIVE BUY)" 
    if b < 25 and bt == "rising" and lt == "falling": return "🌱 EARLY RECOVERY (START ACCUMULATING)"
    if b > 80 and h > 40: return "⚠️ EXUBERANCE (START RAISING CASH)"
    if b > 60 and bt == "falling" and ht == "falling": return "🥀 DISTRIBUTION / TOPPING"
    if b > 50 and bt == "rising": return "🚀 STRONG BULL"
    if b < 40 and bt == "falling": return "📉 ESTABLISHED BEAR"
    return "🟡 TRANSITION / NEUTRAL"

def market_regime_filter(hist):
    if len(hist) < 20: return "INITIALIZING..."
    curr, w1, w4 = hist[-1], hist[-5], hist[-20]
    b, h, l = curr["breadth_pct"], curr["near_high_pct"], curr["near_low_pct"]
    b1, h1, l1 = w1["breadth_pct"], w1["near_high_pct"], w1["near_low_pct"]
    b4 = w4["breadth_pct"]
    db, dh, dl = b - b1, h - h1, l - l1

    if b < 20 and l > 40: return "🔥 CAPITULATION"
    if b < 25 and l > 35 and dl > 0: return "🛑 MELTDOWN"
    if b < 40 and db < 0 and l > 25 and dl > 0: return "⚠️ EARLY RISK-OFF"
    if b > 70 and b < b4 and h < h1 and l > l1: return "🔻 BEARISH DISTRIBUTION"
    if b > 70 and b < b4 and h >= h1 * 0.9 and l <= l1 * 1.1: return "🟠 BULLISH DISTRIBUTION"
    if 45 < b < 75 and db > 5 and dh > 3 and dl < 0: return "✅ HEALTHY BULL"
    if 25 < b < 50 and db > 0 and l < l1: return "🟢 RECOVERY"
    if b >= 40 and db > 0 and dl <= 0: return "🟩 WEAK BULL"
    if b < 60 and db < 0 and dl >= 0: return "🟥 WEAK BEAR"
    return "🟡 TRANSITION"


# ─── BREAKOUT LOGIC ─────────────────────────────────
def get_latest_pivot_high(df, length=14):
    df = df.copy()
    df['PivotHigh'] = df['High'].rolling(length*2+1, center=True).max()
    df['IsPivotHigh'] = df['High'] == df['PivotHigh']
    val, dt = None, None
    for i in range(length, len(df)-length):
        if df['IsPivotHigh'].iloc[i]:
            if df['High'].iloc[i] > df['High'].iloc[i-length:i].max() and \
               df['High'].iloc[i] > df['High'].iloc[i+1:i+length+1].max():
                val = df['High'].iloc[i]
                dt = df.index[i]
    return val, dt

def check_breakout(df, tl):
    if len(df) < 5: return False
    if pd.isna(tl.iloc[-1]): return False
    if df['Close'].iloc[-1] < tl.iloc[-1]: return False
    for i in range(-4, -1):
        if df['Close'].iloc[i] > tl.iloc[i]: return False
    return True

def calc_rs(df):
    if len(df) < 52: return 0
    return round(((df['Close'].iloc[-1] / df['Close'].iloc[-52]) - 1) * 100, 2)


# ─── PROCESS STOCK ─────────────────────────────────
def process_stock(symbol, df, metadata_cache, rs_ratings):
    if df is None or len(df) < 52: return None
    
    # METADATA & MARKET CAP
    if symbol in metadata_cache:
        sector = metadata_cache[symbol].get('sector', 'N/A')
        industry = metadata_cache[symbol].get('industry', 'N/A')
        mcap = metadata_cache[symbol].get('marketCap', 0)
    else:
        try:
            print(f"Fetching info for {symbol}...")
            t = yf.Ticker(symbol)
            info = t.info
            sector = info.get('sector', 'N/A')
            industry = info.get('industry', 'N/A')
            mcap = info.get('marketCap', 0) or 0
            
            metadata_cache[symbol] = {
                'sector': sector, 
                'industry': industry, 
                'marketCap': mcap
            }
            time.sleep(0.05) 
        except:
            sector, industry, mcap = "N/A", "N/A", 0

    # MARKET CAP FILTER
    if mcap < MIN_MARKET_CAP:
        return None

    df = df[['Open','High','Low','Close','Volume']].copy()
    df['SMA40'] = df['Close'].rolling(40).mean()
    above = float(df['Close'].iloc[-1]) > float(df['SMA40'].iloc[-1])
    df['H52'] = df['High'].rolling(52).max()
    df['L52'] = df['Low'].rolling(52).min()
    close = float(df['Close'].iloc[-1])
    near_high = close >= float(df['H52'].iloc[-1]) * (1 - NEAR_RANGE_PCT/100)
    near_low  = close <= float(df['L52'].iloc[-1]) * (1 + NEAR_RANGE_PCT/100)

    df["Body"] = abs(df["Close"] - df["Open"])
    df["Body_Avg5"] = df["Body"].rolling(5).mean().shift(1)
    df["Vol_Avg10"] = df["Volume"].rolling(10).mean().shift(1)

    b_avg = df["Body_Avg5"].iloc[-1]
    v_avg = df["Vol_Avg10"].iloc[-1]
    
    body = round(df["Body"].iloc[-1] / b_avg, 2) if b_avg > 0 else 0
    vol = round(df["Volume"].iloc[-1] / v_avg, 2) if v_avg > 0 else 0

    lph, pdate = get_latest_pivot_high(df)
    if not lph: return None

    atr = ta.ATR(df['High'].values.flatten(), df['Low'].values.flatten(), df['Close'].values.flatten(), 14)
    slope = atr[-1] / 14 if not np.isnan(atr[-1]) else 0
    tl = [np.nan]*len(df)
    idx = df.index.get_loc(pdate)
    tl[idx] = lph
    for i in range(idx+1, len(df)):
        tl[i] = tl[i-1] - slope
    df['TL'] = tl
    breakout = check_breakout(df, df['TL'])
    rs = calc_rs(df)

    rs_rating = rs_ratings.get(symbol, 0)

    return {
        "Symbol": symbol, 
        "IsBreakout": breakout, 
        "RS": rs, 
        "RS_Rating": rs_rating, 
        "Body": body, 
        "Vol": vol,
        "Sector": sector, 
        "Industry": industry,
        "Above_40WMA": above, 
        "Near_52W_High": near_high, 
        "Near_52W_Low": near_low
    }


# ─── TELEGRAM ──────────────────────────────────────
def send(msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return

    MAX_LENGTH = 4000 
    messages = []
    if len(msg) <= MAX_LENGTH:
        messages = [msg]
    else:
        current_chunk = ""
        for line in msg.split('\n'):
            if len(current_chunk) + len(line) + 1 > MAX_LENGTH:
                messages.append(current_chunk.strip())
                current_chunk = line + '\n'
            else:
                current_chunk += line + '\n'
        messages.append(current_chunk.strip())

    for m in messages:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": m, "parse_mode": "Markdown", "disable_web_page_preview": True}
            )
        except Exception as e:
            print(f"Connection Error: {e}")
        time.sleep(0.5)


# ─── MAIN ──────────────────────────────────────────
def run():
    symbols = load_nasdaq_tradable_stocks()
    if not symbols:
        print("❌ No symbols loaded.")
        return
    
    print(f"Starting scan for {len(symbols)} NASDAQ symbols...")
    metadata_cache = load_metadata()
    rs_ratings = load_rs_ratings()
    print(f"✅ Loaded RS Ratings for {len(rs_ratings)} stocks from cache.")
    
    initial_cache_size = len(metadata_cache)

    # ─── PRE-FILTER BY MARKET CAP (NEW) ──────────────────────────────
    print("🔍 Pre-filtering by market cap...")
    symbols_needing_mcap = [
        s for s in symbols
        if s not in metadata_cache or not metadata_cache[s].get('marketCap', 0)
    ]

    if symbols_needing_mcap:
        print(f"  Fetching market-cap info for {len(symbols_needing_mcap)} symbols...")
        for i, symbol in enumerate(symbols_needing_mcap, 1):
            try:
                t = yf.Ticker(symbol)
                info = t.info
                sector = info.get('sector', 'N/A')
                industry = info.get('industry', 'N/A')
                mcap = info.get('marketCap', 0) or 0

                metadata_cache[symbol] = {
                    'sector': sector,
                    'industry': industry,
                    'marketCap': mcap
                }
                if i % 50 == 0:
                    print(f"    … {i}/{len(symbols_needing_mcap)}")
                time.sleep(0.08)          # polite rate-limit
            except Exception as e:
                # keep going – treat as 0 market-cap
                metadata_cache[symbol] = {
                    'sector': 'N/A', 'industry': 'N/A', 'marketCap': 0
                }
                print(f"    ⚠️ {symbol}: {e}")

        # persist the newly fetched metadata
        save_metadata(metadata_cache)
        print(f"✅ Metadata cache updated ({len(metadata_cache)} entries)")

    # Build the final list that actually passes the filter
    filtered_symbols = [
        s for s in symbols
        if metadata_cache.get(s, {}).get('marketCap', 0) >= MIN_MARKET_CAP
    ]
    print(f"✅ After market-cap filter (≥ ${MIN_MARKET_CAP/1e6:.0f}M): "
          f"{len(filtered_symbols)} / {len(symbols)} symbols remain")

    # ─── DOWNLOAD ONLY THE FILTERED LIST + PORTFOLIO ─────────────────
    data_dict = {}
    batch_size = 50
    all_to_download = list(set(filtered_symbols + MY_PORTFOLIO))
    
    print(f"Downloading historical data for {len(all_to_download)} symbols "
          f"(batches of {batch_size})...")
    for i in range(0, len(all_to_download), batch_size):
        batch = all_to_download[i:i+batch_size]
        try:
            print(f"  Batch {i//batch_size + 1} | {len(batch)} symbols")
            batch_data = yf.download(batch, period="2y", interval="1wk",
                                     group_by='ticker', progress=False)
            
            for sym in batch:
                try:
                    data_dict[sym] = (batch_data[sym]
                                      if isinstance(batch_data.columns, pd.MultiIndex)
                                      else batch_data)
                except Exception:
                    pass
        except Exception as e:
            print(f"Batch download error: {e}")
        time.sleep(random.uniform(1.0, 2.5))

    results = []
    portfolio_alerts = []

    # Check Portfolio
    for sym in MY_PORTFOLIO:
        dfp = extract_symbol_df(data_dict, sym)
        status = check_two_stage_exit(dfp, sym)
        if "HOLD" not in status:
            portfolio_alerts.append(f"💼 {sym}: {status}")

    # Process All for Breadth & Breakouts
    for sym in symbols:
        df = extract_symbol_df(data_dict, sym)
        res = process_stock(sym, df, metadata_cache, rs_ratings)
        if res: results.append(res)

    # Save metadata cache if updated
    if len(metadata_cache) > initial_cache_size:
        save_metadata(metadata_cache)

    if not results: return

    df_res = pd.DataFrame(results)
    total = len(df_res)
    above = df_res['Above_40WMA'].sum()
    nh_pct = round(df_res['Near_52W_High'].sum()/total*100, 2)
    nl_pct = round(df_res['Near_52W_Low'].sum()/total*100, 2)
    breadth_pct = round(above/total*100, 2)

    now = datetime.now()
    save_history({"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"), 
                  "breadth_pct": breadth_pct, "near_high_pct": nh_pct, "near_low_pct": nl_pct})
    
    hist = load_history()
    msg = f"📊 NASDAQ Market Breadth: {above}/{total} ({breadth_pct}%)\n📈 Highs: {nh_pct}% | 📉 Lows: {nl_pct}%\n\n"
    msg += f"mkt stage: 🧠 {detect_stage(hist)}\nmkt regime : {market_regime_filter(hist)}\n"
    msg += f"-----------------------------------\n\n"
    
    if portfolio_alerts:
        msg += "⚠️ US PORTFOLIO\n" + "\n".join(portfolio_alerts) + "\n\n"

    b_df = df_res[df_res['IsBreakout']==True].copy()
    if not b_df.empty:
        b_df = b_df.sort_values(by="RS_Rating", ascending=False)

    msg += "🚀 BREAKOUTS\n"
    for _, r in b_df.iterrows():
        star = "🔥" if r['RS_Rating'] > 85 else "🔹"
        msg += f"{star} [{r['Symbol']}]({get_tv_link(r['Symbol'])}) RS:{r['RS_Rating']}\n"
        msg += f"    Sector: {r['Sector']} | Ind: {r['Industry']}\n\n"

    if not b_df.empty:
        msg += "🏢 BREAKOUT SUMMARY\n*Sectors:*\n"
        s_counts = b_df['Sector'].value_counts()
        for sec, count in s_counts.items(): msg += f"• {sec}: {count}\n"
        
        msg += "\n*Industries:*\n"
        i_counts = b_df['Industry'].value_counts()
        for ind, count in i_counts.items(): msg += f"• {ind}: {count}\n"
        msg += "\n"

    send(msg)


if __name__ == "__main__":
    run()
