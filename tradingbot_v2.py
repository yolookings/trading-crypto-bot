import ccxt
import pandas as pd
import telegram
import time
import asyncio
import logging
from datetime import datetime
import configparser  # <- Tambahkan import ini

# --- MEMBACA KONFIGURASI DARI FILE ---
config = configparser.ConfigParser()
config.read('config.ini')

# --- KONFIGURASI BOT ---
TELEGRAM_TOKEN = config['telegram']['token']
CHAT_ID = config['telegram']['chat_id']

# Konfigurasi Aset & Timeframe
SYMBOL = config['settings']['symbol'] 
TIMEFRAME = '15m'       # Timeframe untuk sinyal masuk (entry)
TREND_TIMEFRAME = '4h'  # Timeframe untuk menentukan tren utama

# Konfigurasi Strategi
SHORT_MA = 10           # MA pendek untuk crossover
LONG_MA = 30            # MA panjang untuk crossover
TREND_MA = 50           # MA untuk menentukan tren di timeframe panjang
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

# Konfigurasi Operasional
CHECK_INTERVAL_SECONDS = 900  # Interval pengecekan (900 detik = 15 menit)

# --- SETUP ---

# 1. Setup Logging
# Ini akan membuat file log bernama 'trading_bot.log'
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("trading_bot.log"),
                        logging.StreamHandler() # Juga menampilkan log di terminal
                    ])

# 2. Inisialisasi Bursa & Bot
try:
    exchange = ccxt.binance()
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
except Exception as e:
    logging.error(f"Gagal inisialisasi awal: {e}")
    exit()

# 3. Variabel Status
last_signal = None # Menyimpan sinyal terakhir ('BUY' atau 'SELL')

# --- FUNGSI-FUNGSI ---

async def send_telegram_message(message):
    """Mengirim pesan ke Telegram dengan penanganan error."""
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        logging.info("Pesan notifikasi berhasil dikirim.")
    except Exception as e:
        logging.error(f"Gagal mengirim pesan Telegram: {e}")

def get_market_data(symbol, timeframe, limit):
    """Mengambil dan memproses data pasar dari bursa."""
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        logging.error(f"Gagal mengambil data pasar untuk {symbol} ({timeframe}): {e}")
        return None

def calculate_rsi(data, period=14):
    """Menghitung Relative Strength Index (RSI)."""
    delta = data['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

async def check_signals():
    """Fungsi inti yang menganalisis pasar dan mencari sinyal."""
    global last_signal
    logging.info(f"Memulai pemeriksaan sinyal untuk {SYMBOL}...")

    # --- Langkah 1: Analisis Tren Jangka Panjang (4 Jam) ---
    df_trend = get_market_data(SYMBOL, TREND_TIMEFRAME, TREND_MA + 5)
    if df_trend is None or len(df_trend) < TREND_MA:
        logging.warning("Data tren tidak cukup untuk analisis, pemeriksaan dibatalkan.")
        return

    df_trend['trend_ma'] = df_trend['close'].rolling(window=TREND_MA).mean()
    last_trend_price = df_trend['close'].iloc[-1]
    last_trend_ma = df_trend['trend_ma'].iloc[-1]
    
    # Tentukan tren utama
    main_trend = 'BULLISH' if last_trend_price > last_trend_ma else 'BEARISH'
    logging.info(f"Tren utama ({TREND_TIMEFRAME}): {main_trend} (Harga: {last_trend_price:.2f}, MA{TREND_MA}: {last_trend_ma:.2f})")

    # --- Langkah 2: Analisis Sinyal Masuk (15 Menit) ---
    df_signal = get_market_data(SYMBOL, TIMEFRAME, LONG_MA + RSI_PERIOD + 5)
    if df_signal is None or len(df_signal) < LONG_MA:
        logging.warning("Data sinyal tidak cukup untuk analisis, pemeriksaan dibatalkan.")
        return

    # Hitung indikator untuk timeframe sinyal
    df_signal['short_ma'] = df_signal['close'].rolling(window=SHORT_MA).mean()
    df_signal['long_ma'] = df_signal['close'].rolling(window=LONG_MA).mean()
    df_signal['rsi'] = calculate_rsi(df_signal, period=RSI_PERIOD)
    
    # Ambil data candle terakhir dan sebelumnya untuk deteksi crossover
    prev_row = df_signal.iloc[-2]
    curr_row = df_signal.iloc[-1]
    
    current_price = curr_row['close']
    current_rsi = curr_row['rsi']

    # --- Langkah 3: Logika Sinyal Gabungan ---
    signal = None

    # Kondisi Golden Cross (Sinyal Beli)
    is_golden_cross = prev_row['short_ma'] < prev_row['long_ma'] and curr_row['short_ma'] > curr_row['long_ma']
    if is_golden_cross and main_trend == 'BULLISH' and current_rsi < RSI_OVERBOUGHT:
        signal = 'BUY'
        logging.info(f"Terdeteksi Sinyal Potensial BUY: Golden Cross, Tren Bullish, RSI ({current_rsi:.2f}) < {RSI_OVERBOUGHT}")

    # Kondisi Death Cross (Sinyal Jual)
    is_death_cross = prev_row['short_ma'] > prev_row['long_ma'] and curr_row['short_ma'] < curr_row['long_ma']
    if is_death_cross and main_trend == 'BEARISH' and current_rsi > RSI_OVERSOLD:
        signal = 'SELL'
        logging.info(f"Terdeteksi Sinyal Potensial SELL: Death Cross, Tren Bearish, RSI ({current_rsi:.2f}) > {RSI_OVERSOLD}")

    # --- Langkah 4: Kirim Notifikasi Jika Ada Sinyal Baru ---
    if signal and signal != last_signal:
        last_signal = signal
        message = (
            f"🚨 *SINYAL BARU: {signal} UNTUK {SYMBOL}* 🚨\n\n"
            f"*Harga Saat Ini:* `${current_price:,.2f}`\n"
            f"*Timeframe Sinyal:* `{TIMEFRAME}`\n\n"
            f"*Kondisi Terpenuhi:*\n"
            f"✅ *Crossover:* `{signal} signal`\n"
            f"✅ *Tren Utama ({TREND_TIMEFRAME}):* `{main_trend}`\n"
            f"✅ *RSI ({RSI_PERIOD}):* `{current_rsi:.2f}` (Dalam rentang aman)\n\n"
            f"_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Harap lakukan riset lebih lanjut._"
        )
        await send_telegram_message(message)
    else:
        if signal and signal == last_signal:
            logging.info(f"Sinyal {signal} terdeteksi lagi, tetapi sudah pernah dikirim. Tidak ada notifikasi baru.")
        else:
            logging.info("Tidak ada sinyal trading yang memenuhi semua kriteria.")

async def main():
    """Loop utama untuk menjalankan bot secara periodik."""
    logging.info("Bot Cerdas v2.0 Dimulai!")
    await send_telegram_message(f"✅ *Bot Cerdas v2.0 untuk {SYMBOL} telah aktif!*")
    while True:
        try:
            await check_signals()
            logging.info(f"Pemeriksaan selesai. Menunggu {CHECK_INTERVAL_SECONDS} detik untuk siklus berikutnya.")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        except Exception as e:
            logging.error(f"Terjadi error tak terduga di loop utama: {e}")
            await asyncio.sleep(60) # Tunggu sebentar sebelum mencoba lagi jika ada error

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot dihentikan secara manual.")
    except Exception as e:
        logging.critical(f"Bot berhenti karena error fatal: {e}")