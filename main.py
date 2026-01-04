import requests
import pandas as pd
import pandas_ta as ta
import numpy as np
import time
from datetime import datetime, timedelta

# ==========================================
# 1. 核心配置區
# ==========================================
DISCORD_URL = "https://discord.com/api/webhooks/1457246379242950797/LB6npSWu5J9ZbB8NYp90N-gpmDrjOK2qPqtkaB5AP6YztzdfzmBF6oxesKJybWQ04xoU"
# 已改為 0.25 小時 (即 15 分鐘)
COOL_DOWN_HOURS = 0.25
# ==========================================
# 2. 策略計算函式 (對應 TheConcept 指標)
# ==========================================
def check_signal(df, symbol, interval):
    if len(df) < 200:
        return None, 0, 0, ""

    # 計算技術指標
    df['tr'] = ta.true_range(df['high'], df['low'], df['close'])
    df['atr_200'] = ta.atr(df['high'], df['low'], df['close'], length=200)
    df['ema7'] = ta.ema(df['close'], length=7)
    df['ema21'] = ta.ema(df['close'], length=21)
    df['ema200'] = ta.ema(df['close'], length=200)
    
    # VIDYA 計算邏輯
    vidya_length, vidya_mom = 10, 20
    mom = df['close'].diff()
    pos_mom = mom.where(mom >= 0, 0).rolling(vidya_mom).sum()
    neg_mom = (-mom.where(mom < 0, 0)).rolling(vidya_mom).sum()
    cmo = (100 * (pos_mom - neg_mom) / (pos_mom + neg_mom)).abs()
    alpha = 2 / (vidya_length + 1)
    
    vidya = [0.0] * len(df)
    for i in range(1, len(df)):
        v_alpha = (alpha * cmo.iloc[i] / 100) if not np.isnan(cmo.iloc[i]) else 0
        vidya[i] = v_alpha * df['close'].iloc[i] + (1 - v_alpha) * vidya[i-1]
    df['vidya'] = pd.Series(vidya, index=df.index)
    df['vidya_sma'] = ta.sma(df['vidya'], length=15)
    
    # 趨勢帶與狀態
    band_dist = 2
    upper_band = df['vidya_sma'] + df['atr_200'] * band_dist
    lower_band = df['vidya_sma'] - df['atr_200'] * band_dist
    
    is_trend_up = [False] * len(df)
    for i in range(1, len(df)):
        if df['close'].iloc[i] > upper_band.iloc[i]: is_trend_up[i] = True
        elif df['close'].iloc[i] < lower_band.iloc[i]: is_trend_up[i] = False
        else: is_trend_up[i] = is_trend_up[i-1]
    df['is_trend_up'] = is_trend_up

    # 輔助指標 CCI 20
    this_cci_20 = ta.cci(df['close'], length=20)
    
    # 止損止盈距離 (ATR 14 為基底)
    rma_tr = ta.rma(df['tr'], length=14)
    tp1_dist = rma_tr.iloc[-1] * 2.55
    tp2_dist = rma_tr.iloc[-1] * 5.1
    
    curr = df.iloc[-1]
    side, entry, sl, tp_str = None, curr['close'], 0, ""

    # 多單判斷: 趨勢+價格過EMA200+快慢線金叉+CCI正值
    if curr['is_trend_up'] and curr['close'] > curr['ema200'] and curr['ema7'] > curr['ema21'] and this_cci_20.iloc[-1] >= 0:
        side = "LONG"
        sl = curr['low'] - tp1_dist
        tp_str = f"TP1: {curr['high']+tp1_dist:.4f}, TP2: {curr['high']+tp2_dist:.4f}"
        
    # 空單判斷
    elif not curr['is_trend_up'] and curr['close'] < curr['ema200'] and curr['ema7'] < curr['ema21'] and this_cci_20.iloc[-1] < 0:
        side = "SHORT"
        sl = curr['high'] + tp1_dist
        tp_str = f"TP1: {curr['low']-tp1_dist:.4f}, TP2: {curr['low']-tp2_dist:.4f}"

    return side, entry, sl, tp_str

# ==========================================
# 3. 系統核心
# ==========================================
class TradingBot:
    def __init__(self):
        self.sent_signals = {}
        self.symbols = []
        self.last_update = datetime.min

    def get_top_symbols(self):
        """獲取幣安 24H 交易量前 10 名的 USDT 交易對"""
        if datetime.now() - self.last_update > timedelta(hours=4):
            try:
                res = requests.get("https://api.binance.com/api/v3/ticker/24hr").json()
                usdt = [i for i in res if i['symbol'].endswith('USDT')]
                sorted_usdt = sorted(usdt, key=lambda x: float(x['quoteVolume']), reverse=True)
                self.symbols = [x['symbol'] for x in sorted_usdt[:10]]
                self.last_update = datetime.now()
                print(f"[{datetime.now()}] 幣種排名更新: {self.symbols}")
            except: 
                print("幣種更新失敗")
        return self.symbols

    def fetch_and_run(self, symbol):
        try:
            url = "https://api.binance.com/api/v3/klines"
            res = requests.get(url, params={"symbol":symbol, "interval":"15m", "limit":300}).json()
            df = pd.DataFrame(res).iloc[:, :6]
            df.columns = ['timestamp','open','high','low','close','volume']
            df = df.astype(float)
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            # 合成不同週期 (使用修正後的語法避免警告)
            data_map = {
                "15M": df,
                "30M": df.resample('30min', on='timestamp').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna().reset_index(),
                "1H": df.resample('1h', on='timestamp').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna().reset_index()
            }

            for interval, d in data_map.items():
                side, price, sl, tp = check_signal(d, symbol, interval)
                if side:
                    self.notify(symbol, side, interval, price, sl, tp)
            time.sleep(0.5) # 避開 API 頻率限制
        except Exception as e: 
            print(f"處理 {symbol} 時出錯: {e}")

    def notify(self, symbol, side, interval, entry, sl, tp):
        key = (symbol, side, interval)
        # 冷卻時間檢查
        if key in self.sent_signals:
            if datetime.now() - self.sent_signals[key] < timedelta(hours=COOL_DOWN_HOURS):
                return

        color = 0x17dfad if side == "LONG" else 0xdd326b
        payload = {
            "embeds": [{
                "title": f"🚨 {symbol} 訊號觸發",
                "color": color,
                "fields": [
                    {"name": "方向", "value": f"**{side}**", "inline": True},
                    {"name": "週期", "value": interval, "inline": True},
                    {"name": "當前進場價", "value": f"{entry:.4f}", "inline": False},
                    {"name": "止損建議 (SL)", "value": f"{sl:.4f}", "inline": True},
                    {"name": "止盈建議 (TP)", "value": tp, "inline": False}
                ],
                "footer": {"text": f"偵測時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"}
            }]
        }
        try:
            res = requests.post(DISCORD_URL, json=payload)
            if res.status_code == 204:
                self.sent_signals[key] = datetime.now()
                print(f">>> 已發送推播: {symbol} ({interval} {side})")
        except:
            print(f"Discord 發送失敗 ({symbol})")

# ==========================================
# 4. 程式執行入口
# ==========================================
if __name__ == "__main__":
    bot = TradingBot()
    print("【啟動】監控機器人已運作，每 5 分鐘掃描一次市場...")
    
    while True:
        try:
            current_symbols = bot.get_top_symbols()
            for s in current_symbols:
                bot.fetch_and_run(s)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 輪詢完成，等待下次掃描...")
        except Exception as e:
            print(f"主循環異常: {e}")
        
        time.sleep(300) # 每 5 分鐘執行一次
