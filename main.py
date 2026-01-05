import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
import time
import requests
from datetime import datetime, timedelta

# ==========================================
# 1. 核心配置區
# ==========================================
DISCORD_URL = "YOUR_WEBHOOK_URL" # 請填入你的 Webhook
COOL_DOWN_HOURS = 0.25 

EXCHANGE = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'} 
})

# ==========================================
# 2. 策略計算函式
# ==========================================
def calculate_magic_trend_and_buffers(df):
    # (此區段邏輯保持不變，計算 MagicTrend 與 Buffer)
    df['cci_200'] = ta.cci(df['high'], df['low'], df['close'], length=200)
    df['atr_5'] = ta.atr(df['high'], df['low'], df['close'], length=5)
    df['tr'] = ta.true_range(df['high'], df['low'], df['close'])
    df['cci_20'] = ta.cci(df['high'], df['low'], df['close'], length=20)
    
    buffer_up = [0.0] * len(df)
    buffer_dn = [0.0] * len(df)
    x = [0.0] * len(df)
    
    multiplier = 1.0
    sma_tr_5 = ta.sma(df['tr'], length=5) * multiplier
    
    highs = df['high'].values
    lows = df['low'].values
    cci_200 = df['cci_200'].values
    sma_tr = sma_tr_5.values
    
    buffer_dn[0] = highs[0] + (sma_tr[0] if not np.isnan(sma_tr[0]) else 0)
    buffer_up[0] = lows[0] - (sma_tr[0] if not np.isnan(sma_tr[0]) else 0)
    
    for i in range(1, len(df)):
        curr_atr = sma_tr[i] if not np.isnan(sma_tr[i]) else 0
        b_dn = highs[i] + curr_atr
        b_up = lows[i] - curr_atr
        prev_cci = cci_200[i-1]
        curr_cci = cci_200[i]
        
        if curr_cci >= 0 and prev_cci < 0: b_up = buffer_dn[i-1]
        if curr_cci <= 0 and prev_cci > 0: b_dn = buffer_up[i-1]
            
        if curr_cci >= 0:
            if b_up < buffer_up[i-1]: b_up = buffer_up[i-1]
        else:
            if curr_cci <= 0:
                if b_dn > buffer_dn[i-1]: b_dn = buffer_dn[i-1]
        
        buffer_up[i] = b_up
        buffer_dn[i] = b_dn
        
        if curr_cci >= 0: x[i] = b_up
        elif curr_cci <= 0: x[i] = b_dn
        else: x[i] = x[i-1]
            
    df['x'] = x

    magic_trend = [0.0] * len(df)
    atrs_5 = ta.sma(df['tr'], length=5).values
    coeff = 1.0
    cci_20 = df['cci_20'].values
    
    for i in range(1, len(df)):
        curr_atr = atrs_5[i] if not np.isnan(atrs_5[i]) else 0
        up_t = lows[i] - curr_atr * coeff
        down_t = highs[i] + curr_atr * coeff
        prev_magic = magic_trend[i-1]
        
        if cci_20[i] >= 0:
            if up_t < prev_magic: magic_trend[i] = prev_magic
            else: magic_trend[i] = up_t
        else:
            if down_t > prev_magic: magic_trend[i] = prev_magic
            else: magic_trend[i] = down_t
                
    df['magic_trend'] = magic_trend
    return df

def check_signal(df, symbol, interval):
    # 增加回傳值數量，改為回傳 side, entry, sl, tp1, tp2, tp3
    if len(df) < 250: return None, 0, 0, 0, 0, 0
    
    df['atr_200'] = ta.atr(df['high'], df['low'], df['close'], length=200)
    df['ema7'] = ta.ema(df['close'], length=7)
    df['ema21'] = ta.ema(df['close'], length=21)
    df['ema200'] = ta.ema(df['close'], length=200)
    
    vidya_length, vidya_mom = 10, 20
    mom = df['close'].diff()
    pos_mom = mom.where(mom >= 0, 0).rolling(vidya_mom).sum()
    neg_mom = (-mom.where(mom < 0, 0)).rolling(vidya_mom).sum()
    denominator = pos_mom + neg_mom
    cmo = (100 * (pos_mom - neg_mom) / denominator.replace(0, 1)).abs()
    
    alpha = 2 / (vidya_length + 1)
    vidya = [df['close'].iloc[0]] * len(df)
    cmo_vals = cmo.values
    close_vals = df['close'].values
    
    for i in range(1, len(df)):
        v_alpha = (alpha * cmo_vals[i] / 100) if not np.isnan(cmo_vals[i]) else 0
        vidya[i] = v_alpha * close_vals[i] + (1 - v_alpha) * vidya[i-1]
    df['vidya_sma'] = ta.sma(pd.Series(vidya), length=15)
    
    upper_band = df['vidya_sma'] + df['atr_200'] * 2
    lower_band = df['vidya_sma'] - df['atr_200'] * 2
    
    is_trend_up = [False] * len(df)
    u_band = upper_band.values
    l_band = lower_band.values
    
    for i in range(1, len(df)):
        if close_vals[i] > u_band[i]: is_trend_up[i] = True
        elif close_vals[i] < l_band[i]: is_trend_up[i] = False
        else: is_trend_up[i] = is_trend_up[i-1]
    df['is_trend_up'] = is_trend_up
    
    df = calculate_magic_trend_and_buffers(df)
    
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    cross_over_x = (prev['close'] <= prev['x']) and (curr['close'] > curr['x'])
    cross_under_x = (prev['close'] >= prev['x']) and (curr['close'] < curr['x'])
    cross_over_magic = (prev['close'] <= prev['magic_trend']) and (curr['close'] > curr['magic_trend'])
    cross_under_magic = (prev['close'] >= prev['magic_trend']) and (curr['close'] < curr['magic_trend'])
    cross_over_ema200 = (prev['close'] <= prev['ema200']) and (curr['close'] > curr['ema200'])
    cross_under_ema200 = (prev['close'] >= prev['ema200']) and (curr['close'] < curr['ema200'])

    sorignal = curr['cci_20'] >= 0
    bigmagicTrend = curr['cci_200'] >= 0
    
    original_long = (curr['is_trend_up'] and cross_over_x and cross_over_magic and curr['close'] > curr['ema200'] and curr['close'] > curr['ema7'] and curr['ema7'] > curr['ema21'])
    original_short = (not curr['is_trend_up'] and cross_under_x and cross_under_magic and curr['close'] < curr['ema200'] and curr['close'] < curr['ema7'] and curr['ema7'] < curr['ema21'])
    cross200_long = (sorignal and bigmagicTrend and curr['close'] > curr['ema7'] and curr['close'] > curr['ema21'] and cross_over_ema200)
    cross200_short = (not sorignal and not bigmagicTrend and curr['close'] < curr['ema7'] and curr['close'] < curr['ema21'] and cross_under_ema200)

    side = None
    sl, tp1, tp2, tp3 = 0, 0, 0, 0
    
    rma_tr = ta.rma(df['tr'], length=14).iloc[-1]
    tp1_dist = rma_tr * 2.55
    tp2_dist = rma_tr * 5.1
    tp3_dist = rma_tr * 7.65 # 增加 TP3 距離計算
    
    if original_long or cross200_long:
        side = "LONG"
        sl = curr['low'] - tp1_dist
        tp1 = curr['high'] + tp1_dist
        tp2 = curr['high'] + tp2_dist
        tp3 = curr['high'] + tp3_dist
        
    elif original_short or cross200_short:
        side = "SHORT"
        sl = curr['high'] + tp1_dist
        tp1 = curr['low'] - tp1_dist
        tp2 = curr['low'] - tp2_dist
        tp3 = curr['low'] - tp3_dist

    # 分別回傳數值，方便 Notify 格式化
    return side, curr['close'], sl, tp1, tp2, tp3

# ==========================================
# 3. 系統核心
# ==========================================
class TradingBot:
    def __init__(self):
        self.sent_signals = {}
        self.symbols = []
        self.last_update = datetime.min

    def update_top_symbols(self):
        """自動獲取 Bybit 交易量前 10 名的 USDT 幣對 (排除穩定幣)"""
        if datetime.now() - self.last_update > timedelta(hours=4):
            try:
                tickers = EXCHANGE.fetch_tickers()
                exclude_list = ['USDC', 'DAI', 'FDUSD', 'USDE', 'TUSD', 'EUR', 'BUSD', 'UP', 'DOWN', 'BEAR', 'BULL', '3S', '3L']
                
                valid_tickers = []
                for s, t in tickers.items():
                    if '/USDT' in s and not any(ex in s for ex in exclude_list):
                        vol = t['quoteVolume'] if t.get('quoteVolume') else 0
                        valid_tickers.append({'symbol': s, 'vol': vol})

                sorted_list = sorted(valid_tickers, key=lambda x: x['vol'], reverse=True)
                self.symbols = [x['symbol'] for x in sorted_list[:10]]
                self.last_update = datetime.now()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔥 熱門幣種更新: {self.symbols}")
            except Exception as e:
                print(f"更新排名失敗: {e}")
                if not self.symbols: self.symbols = ['BTC/USDT', 'ETH/USDT']
        return self.symbols

    def fetch_and_run(self, symbol):
        try:
            bars = EXCHANGE.fetch_ohlcv(symbol, timeframe='15m', limit=1000)
            df = pd.DataFrame(bars, columns=['timestamp','open','high','low','close','volume'])
            df = df.astype(float)
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            data_map = {
                "15M": df,
                "30M": df.resample('30min', on='timestamp').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna().reset_index(),
                "1H": df.resample('1h', on='timestamp').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna().reset_index()
            }

            for interval, d in data_map.items():
                try:
                    # 接收 6 個回傳值
                    side, price, sl, tp1, tp2, tp3 = check_signal(d, symbol, interval)
                    if side:
                        self.notify(symbol, side, interval, price, sl, tp1, tp2, tp3)
                except Exception as inner:
                    print(f"計算 {symbol} {interval} 錯誤: {inner}")
            time.sleep(0.5)
        except Exception as e:
            print(f"抓取 {symbol} 失敗: {e}")

    def notify(self, symbol, side, interval, entry, sl, tp1, tp2, tp3):
        key = (symbol, side, interval)
        if key in self.sent_signals and (datetime.now() - self.sent_signals[key] < timedelta(hours=COOL_DOWN_HOURS)):
            return
        
        print(f"🚀 訊號觸發: {symbol} {side} ({interval})")
        
        # 計算台灣時間 (UTC+8)
        tw_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
        
        # 中文方向
        side_cn = "多" if side == "LONG" else "空"
        
        # 組合純文字訊息 (符合你的圖片格式)
        message_content = (
            f"🚨\n"
            f"{symbol} 訊號 BYBIT\n"
            f"方向 {side_cn}\n"
            f"週期: {interval}\n"
            f"進場: {entry:.4f}\n"
            f"SL: {sl:.4f}\n"
            f"TP1: {tp1:.4f}\n"
            f"TP2: {tp2:.4f}\n"
            f"TP3: {tp3:.4f}\n"
            f"偵測時間: {tw_time}"
        )

        payload = {
            "content": message_content
        }
        
        try:
            requests.post(DISCORD_URL, json=payload, timeout=10)
            self.sent_signals[key] = datetime.now()
        except: pass

if __name__ == "__main__":
    bot = TradingBot()
    print("Bot 啟動中...")
    # 測試訊息，確認格式
    bot.notify("SYSTEM", "LONG", "TEST", 0, 0, 0, 0, 0)
    
    while True:
        try:
            current_symbols = bot.update_top_symbols()
            for s in current_symbols:
                bot.fetch_and_run(s)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 輪詢完成")
        except Exception as e:
            print(f"主循環異常: {e}")
        time.sleep(300)
