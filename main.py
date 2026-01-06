import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
import time
import requests
import os
from datetime import datetime, timedelta

# ==========================================
# 1. 配置設定
# ==========================================
# 優先讀取 Zeabur 環境變數，如果沒有則使用預設值
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1457246379242950797/LB6npSWu5J9ZbB8NYp90N-gpmDrjOK2qPqtkaB5AP6YztzdfzmBF6oxesKJybWQ04xoU")

# 交易所設定 (不需 API Key，只需讀取公開數據)
exchange = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

# 策略參數
VIDYA_LEN = 10
VIDYA_MOM = 20
CCI_LEN = 200
ATR_LEN = 5
COOLDOWN_BARS = 6  # 對應 Pine Script 的 can_show_signal (6根K線)

# ==========================================
# 2. 指標計算邏輯 (核心演算法)
# ==========================================
def calculate_vidya(df, length=10, momentum=20):
    """計算 VIDYA 指標"""
    src = df['close']
    mom = src.diff()
    
    # 計算 CMO
    # Pine: sum_pos = math.sum((momentum >= 0) ? momentum : 0.0, vidya_momentum)
    pos_mom = mom.where(mom >= 0, 0).rolling(momentum).sum()
    neg_mom = (-mom.where(mom < 0, 0)).rolling(momentum).sum()
    
    denominator = pos_mom + neg_mom
    cmo = (100 * (pos_mom - neg_mom) / denominator.replace(0, 1)).abs()
    
    # 計算 VIDYA (遞迴計算)
    alpha = 2 / (length + 1)
    vidya = np.zeros_like(src)
    vidya[:] = np.nan
    
    # 初始化第一個非 NaN 的值
    start_idx = momentum 
    if start_idx < len(src):
        vidya[start_idx] = src.iloc[start_idx]

    src_values = src.values
    cmo_values = cmo.values
    
    for i in range(start_idx + 1, len(df)):
        val_alpha = (alpha * cmo_values[i] / 100)
        # Pine: vidya_value := alpha * abs_cmo / 100 * src + (1 - alpha * abs_cmo / 100) * nz(vidya_value[1])
        prev_vidya = vidya[i-1] if not np.isnan(vidya[i-1]) else src_values[i]
        vidya[i] = val_alpha * src_values[i] + (1 - val_alpha) * prev_vidya
        
    # 最後做一次 SMA 平滑
    # Pine: ta.sma(vidya_value, 15)
    return ta.sma(pd.Series(vidya), length=15)

def process_data(df):
    """計算所有需要的指標並產生訊號"""
    if len(df) < 250: return None
    
    # ---------------------------
    # 基礎指標
    # ---------------------------
    df['ema7'] = ta.ema(df['close'], length=7)
    df['ema21'] = ta.ema(df['close'], length=21)
    df['ema200'] = ta.ema(df['close'], length=200)
    df['atr_200'] = ta.atr(df['high'], df['low'], df['close'], length=200)
    df['tr'] = ta.true_range(df['high'], df['low'], df['close'])
    
    # ---------------------------
    # VIDYA & Trend Up
    # ---------------------------
    df['vidya_sma'] = calculate_vidya(df, VIDYA_LEN, VIDYA_MOM)
    df['upper_band'] = df['vidya_sma'] + df['atr_200'] * 2
    df['lower_band'] = df['vidya_sma'] - df['atr_200'] * 2
    
    # 計算 is_trend_up (狀態機)
    is_trend_up = np.full(len(df), False)
    close_vals = df['close'].values
    u_band = df['upper_band'].values
    l_band = df['lower_band'].values
    
    for i in range(1, len(df)):
        if np.isnan(u_band[i]): 
            is_trend_up[i] = is_trend_up[i-1]
            continue
            
        if close_vals[i] > u_band[i]:
            is_trend_up[i] = True
        elif close_vals[i] < l_band[i]:
            is_trend_up[i] = False
        else:
            is_trend_up[i] = is_trend_up[i-1]
            
    df['is_trend_up'] = is_trend_up

    # ---------------------------
    # Magic Trend & Buffers (X line)
    # ---------------------------
    # 計算 ATR for Buffer
    sma_tr_5 = ta.sma(df['tr'], length=ATR_LEN)
    df['cci_200'] = ta.cci(df['high'], df['low'], df['close'], length=CCI_LEN)
    df['cci_20'] = ta.cci(df['high'], df['low'], df['close'], length=20) # 小週期用
    
    # 初始化陣列
    buffer_up = np.zeros(len(df))
    buffer_dn = np.zeros(len(df))
    x_line = np.zeros(len(df))
    magic_trend = np.zeros(len(df))
    
    highs = df['high'].values
    lows = df['low'].values
    cci_200 = df['cci_200'].values
    atr_vals = sma_tr_5.values
    cci_20 = df['cci_20'].values
    
    # 迭代計算 Buffer (X Line) 與 Magic Trend
    # 這種遞迴計算無法向量化，必須跑迴圈
    for i in range(1, len(df)):
        curr_atr = atr_vals[i] if not np.isnan(atr_vals[i]) else 0
        
        # --- Buffer Logic ---
        b_dn = highs[i] + curr_atr
        b_up = lows[i] - curr_atr
        
        prev_cci = cci_200[i-1]
        curr_cci = cci_200[i]
        
        # CCI 穿越邏輯
        if curr_cci >= 0 and prev_cci < 0: b_up = buffer_dn[i-1]
        if curr_cci <= 0 and prev_cci > 0: b_dn = buffer_up[i-1]
        
        # 平滑邏輯
        if curr_cci >= 0:
            if b_up < buffer_up[i-1]: b_up = buffer_up[i-1]
        else: # curr_cci <= 0
            if b_dn > buffer_dn[i-1]: b_dn = buffer_dn[i-1]
            
        buffer_up[i] = b_up
        buffer_dn[i] = b_dn
        
        # 計算 X
        if curr_cci >= 0: x_line[i] = b_up
        elif curr_cci <= 0: x_line[i] = b_dn
        else: x_line[i] = x_line[i-1]
        
        # --- Magic Trend Logic (Small Period) ---
        up_t = lows[i] - curr_atr
        down_t = highs[i] + curr_atr
        prev_magic = magic_trend[i-1]
        
        if cci_20[i] >= 0:
            if up_t < prev_magic: magic_trend[i] = prev_magic
            else: magic_trend[i] = up_t
        else:
            if down_t > prev_magic: magic_trend[i] = prev_magic
            else: magic_trend[i] = down_t
            
    df['x'] = x_line
    df['magic_trend'] = magic_trend
    
    # ---------------------------
    # 訊號判斷
    # ---------------------------
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    # 定義 Crossovers
    # Python 的 crossover: 前一根 <= 線 且 當前 > 線
    cross_over_x = (prev['close'] <= prev['x']) and (curr['close'] > curr['x'])
    cross_under_x = (prev['close'] >= prev['x']) and (curr['close'] < curr['x'])
    
    cross_over_magic = (prev['close'] <= prev['magic_trend']) and (curr['close'] > curr['magic_trend'])
    cross_under_magic = (prev['close'] >= prev['magic_trend']) and (curr['close'] < curr['magic_trend'])
    
    cross_over_ema200 = (prev['close'] <= prev['ema200']) and (curr['close'] > curr['ema200'])
    cross_under_ema200 = (prev['close'] >= prev['ema200']) and (curr['close'] < curr['ema200'])

    sorignal = curr['cci_20'] >= 0
    bigmagicTrend = curr['cci_200'] >= 0
    
    # 邏輯條件 (參考 Pine Script)
    # 1. Original Strategy
    original_long = (
        curr['is_trend_up'] and 
        cross_over_x and 
        cross_over_magic and 
        curr['close'] > curr['ema200'] and 
        curr['close'] > curr['ema7'] and 
        curr['ema7'] > curr['ema21']
    )
    
    original_short = (
        not curr['is_trend_up'] and 
        cross_under_x and 
        cross_under_magic and 
        curr['close'] < curr['ema200'] and 
        curr['close'] < curr['ema7'] and 
        curr['ema7'] < curr['ema21']
    )
    
    # 2. Cross 200 Strategy
    cross200_long = (
        sorignal and 
        bigmagicTrend and 
        curr['close'] > curr['ema7'] and 
        curr['close'] > curr['ema21'] and 
        cross_over_ema200
    )
    
    cross200_short = (
        not sorignal and 
        not bigmagicTrend and 
        curr['close'] < curr['ema7'] and 
        curr['close'] < curr['ema21'] and 
        cross_under_ema200
    )

    side = None
    if original_long or cross200_long:
        side = "LONG"
    elif original_short or cross200_short:
        side = "SHORT"
        
    return side, df

# ==========================================
# 3. 機器人主程式
# ==========================================
class TradingBot:
    def __init__(self):
        self.last_signals = {} 
        self.symbols = []
        self.last_update = datetime.min

    def update_top_symbols(self):
        if datetime.now() - self.last_update > timedelta(hours=4):
            try:
                tickers = exchange.fetch_tickers()
                valid_tickers = []
                # 嚴格排除穩定幣
                exclude = ['USDC', 'DAI', 'FDUSD', 'USDE', 'BUSD', 'TUSD', 'PYUSD', 'USDD']
                for s, t in tickers.items():
                    if '/USDT' in s:
                        # 檢查 symbol 名稱中是否包含排除的關鍵字
                        is_stable = any(ex in s for ex in exclude)
                        if not is_stable:
                            vol = t['quoteVolume'] if t.get('quoteVolume') else 0
                            valid_tickers.append({'symbol': s, 'vol': vol})
                            
                self.symbols = [x['symbol'] for x in sorted(valid_tickers, key=lambda x: x['vol'], reverse=True)[:50]]
                self.last_update = datetime.now()
                print(f"[{datetime.now().strftime('%H:%M')}] 更新監控: {self.symbols}")
            except: self.symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT' 'BNB/USDT', 'XRP/USDT']
        return self.symbols

    def calculate_sl_tp(self, df, side):
        curr = df.iloc[-1]
        rma_tr = ta.rma(df['tr'], length=14).iloc[-1]
        m_tp1, m_tp2, m_tp3 = 2.55, 5.1, 7.65
        entry = curr['close']
        
        if side == "LONG":
            sl = curr['low'] - (rma_tr * m_tp1)
            tp1 = curr['high'] + (rma_tr * m_tp1)
            tp2 = curr['high'] + (rma_tr * m_tp2)
            tp3 = curr['high'] + (rma_tr * m_tp3)
        else: # SHORT
            sl = curr['high'] + (rma_tr * m_tp1)
            tp1 = curr['low'] - (rma_tr * m_tp1)
            tp2 = curr['low'] - (rma_tr * m_tp2)
            tp3 = curr['low'] - (rma_tr * m_tp3)
        return entry, sl, tp1, tp2, tp3

    def run_analysis(self):
        symbols = self.update_top_symbols()
        timeframes = ['15m', '30m', '1h']
        
        for symbol in symbols:
            for tf in timeframes:
                try:
                    bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=500)
                    df = pd.DataFrame(bars, columns=['timestamp','open','high','low','close','volume'])
                    df = df.astype(float)
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    
                    side, df_result = process_data(df)
                    
                    if side:
                        signal_key = f"{symbol}_{tf}_{side}"
                        last_ts = self.last_signals.get(signal_key, 0)
                        current_ts = df['timestamp'].iloc[-1]
                        
                        if current_ts != last_ts:
                            entry, sl, tp1, tp2, tp3 = self.calculate_sl_tp(df_result, side)
                            self.send_discord(symbol, side, tf, entry, sl, tp1, tp2, tp3)
                            self.last_signals[signal_key] = current_ts
                    time.sleep(0.1)
                except Exception as e:
                    print(f"Error {symbol}: {e}")

    # ==========================================
    # 4. 修改後的通知格式 (嚴格對齊圖片)
    # ==========================================
    def send_discord(self, symbol, side, interval, entry, sl, tp1, tp2, tp3):
        # 強制加 8 小時 (解決 Zeabur 時區問題)
        tw_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%H:%M')
        
        # 中文方向
        side_cn = "做多" if side == "LONG" else "做空"
        
        # 顯示名稱
        exchange_name = "BYBIT" # 這裡可以改成 BIGGET 或 COINGLASS
        
        # 格式化數字 (保留4位小數，去除尾端多餘0)
        def fmt(num): return f"{num:.4f}".rstrip('0').rstrip('.')
        
        # 這裡的排版完全按照你的要求
        msg = (
            f"🚨\n"
            f"{symbol} 訊號 {exchange_name}\n"
            f"方向 {side_cn}\n"
            f"週期:{interval.upper()}\n"
            f"進場:{fmt(entry)}\n"
            f"SL:{fmt(sl)}\n"
            f"TP1: {fmt(tp1)}\n"
            f"TP2: {fmt(tp2)}\n"
            f"TP3: {fmt(tp3)}\n"
            f"偵測時間: 台灣時間 {tw_time}"
        )
        
        payload = {"content": msg}
        
        try:
            requests.post(DISCORD_URL, json=payload)
            print(f"已發送: {symbol} {side}")
        except Exception as e:
            print(f"Discord 失敗: {e}")

if __name__ == "__main__":
    bot = TradingBot()
    print("🚀 Zeabur Trading Bot (格式嚴格修正版) 已啟動...")
    
    # 測試訊號 (格式檢查用)
    bot.send_discord("TEST/USDT", "SHORT", "30m", 0.0282, 0.0292, 0.0267, 0.0250, 0.0230)
    
    while True:
        bot.run_analysis()
        time.sleep(60)


