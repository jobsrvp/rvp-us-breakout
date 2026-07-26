import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import requests
import io
from datetime import datetime
import time
import random

# ========================= CONFIG =========================
CACHE_FILE = "nasdaq_tickers_cache.csv"
CACHE_DAYS = 30
HISTORY_FILE = "rs_rating_history.json"
CACHE_FILE_OUT = "rs_rating_cache.json"

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
# =========================================================

def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured.")
        print(message[:500] + "..." if len(message) > 500 else message)
        return
    
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=15
        )
        print("✅ Telegram message sent successfully.")
    except Exception as e:
        print(f"Telegram error: {e}")


def get_nasdaq_tickers():
    cache_path = CACHE_FILE
    if os.path.exists(cache_path):
        cache_age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_path))
        if cache_age.days < CACHE_DAYS:
            print(f"📂 Using cached tickers ({cache_age.days} days old)")
            df = pd.read_csv(cache_path)
            df['Symbol'] = df['Symbol'].astype(str).str.strip()
            return df

    print("🌐 Downloading fresh NASDAQ tickers...")
    url = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    
    df = pd.read_csv(io.StringIO(response.text), sep='|')
    
    # Clean data
    df = df.dropna(subset=['Symbol'])
    df['Symbol'] = df['Symbol'].astype(str).str.strip()
    df = df[df['Symbol'] != 'nan']
    
    df.to_csv(cache_path, index=False)
    print(f"✅ Cached {len(df)} tickers.")
    return df


def is_common_tradable_stock(row):
    symbol = str(row['Symbol']).strip()
    
    if not symbol or symbol == 'nan' or len(symbol) > 6: 
        return False
    if any(c in symbol for c in ['.', '-', '^', '/', '\\', '+', '*', ' ']): 
        return False
    if symbol.endswith(('W', 'R', 'U', 'P', 'V', 'Q', 'T', 'F')): 
        return False
    
    name = str(row.get('Security Name', '')).lower()
    exclude = ['warrant','right','unit','preferred','etf','fund','trust','note','bond','depositary']
    if any(kw in name for kw in exclude): 
        return False
    
    if row.get('Financial Status') != 'N':
        return False
    if row.get('Market Category') not in ['Q', 'G', 'S']:
        return False
    
    return True


def load_existing_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def generate_daily_rs():
    print(f"🚀 NASDAQ Daily RS Rating Update")
    print(f"🕒 Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    nasdaq_df = get_nasdaq_tickers()
    tradable_df = nasdaq_df[nasdaq_df.apply(is_common_tradable_stock, axis=1)].copy()
    symbols = tradable_df['Symbol'].tolist()
    
    print(f"✅ Processing {len(symbols)} tradable common stocks\n")
    
    rs_history = load_existing_history()
    today = datetime.now().strftime('%Y-%m-%d')
    
    print("📥 Downloading latest weekly data...")
    data = yf.download(symbols, period="2y", interval="1wk", group_by='ticker', 
                      progress=False, threads=True, auto_adjust=True)
    
    scores = {}
    count_processed = 0
    
    for symbol in symbols:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                ticker_data = data[symbol]
            else:
                ticker_data = data
            
            col = 'Adj Close' if 'Adj Close' in ticker_data.columns else 'Close'
            series = ticker_data[col].dropna()
            
            if len(series) < 52: 
              continue
                
            p0 = series.iloc[-1]
            p1 = series.iloc[-13]
            p2 = series.iloc[-26]
            p3 = series.iloc[-39]
            p4 = series.iloc[-52]
            
            score = (p0/p1 * 0.4) + (p1/p2 * 0.2) + (p2/p3 * 0.2) + (p3/p4 * 0.2)
            
            if not np.isinf(score) and not np.isnan(score):
                scores[symbol] = score
                count_processed += 1
        except:
            continue
    
    print(f"✅ Processed {count_processed} stocks for ranking.")
    
    if scores:
        df = pd.DataFrame.from_dict(scores, orient='index', columns=['Score'])
        df['RS_Rating'] = (df['Score'].rank(pct=True) * 99 + 1).astype(int)
        
        # Latest Cache
        latest_cache = {sym: int(row['RS_Rating']) for sym, row in df.iterrows()}
        with open(CACHE_FILE_OUT, "w") as f:
            json.dump(latest_cache, f, indent=2)
        
        # Update History
        for sym, row in df.iterrows():
            if sym not in rs_history:
                rs_history[sym] = {}
            rs_history[sym][today] = int(row['RS_Rating'])
        
        with open(HISTORY_FILE, "w") as f:
            json.dump(rs_history, f, indent=4)
        
        print(f"✅ Saved cache + history ({len(latest_cache)} stocks)")
        
        # === TOP 100 TELEGRAM MESSAGE ===
        top100 = df.nlargest(100, 'RS_Rating').reset_index()
        top100 = top100.rename(columns={'index': 'Symbol'})
        
        msg = f"🏆 *NASDAQ TOP 100 RS RATING* ({today})\n\n"
        msg += f"Total Stocks Ranked: `{len(df)}`\n\n"
        
        for i, row in top100.iterrows():
            rank = i + 1
            symbol = row['Symbol']
            rating = int(row['RS_Rating'])
            star = "🔥" if rating >= 90 else "⭐" if rating >= 80 else "🔹"
            msg += f"{star} `{rank:2d}`. `{symbol}` — *{rating}*\n"
            
            if rank % 20 == 0 and rank < 100:
                #send_telegram_msg(msg)
                msg = f"🏆 *NASDAQ TOP 100 RS RATING* (continued...)\n\n"
        
        #send_telegram_msg(msg)
        
        # Top 10 summary
        top20_msg = "🔝 *TOP 10 TODAY*\n"
        for i, row in top100.head(20).iterrows():
            top20_msg += f"`{row['Symbol']}`: *{int(row['RS_Rating'])}*\n"
        send_telegram_msg(top20_msg)
    
    print(f"\n🎉 Daily Update Completed!")
    print(f"🕒 Finished at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    generate_daily_rs()
