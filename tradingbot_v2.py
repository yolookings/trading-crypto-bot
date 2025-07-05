import ccxt
import pandas as pd
import telegram
import time
import asyncio
import logging
from datetime import datetime
import os
import sys

# --- SETUP LOGGING ---
# Log ini akan muncul di dashboard Railway atau di terminal Anda.
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.StreamHandler()
                    ])

# --- MEMBACA KONFIGURASI DARI ENVIRONMENT VARIABLES ---
logging.info("Memuat konfigurasi dari Environment Variables...")
try:
    # Mengambil variabel rahasia dari lingkungan server (Railway)
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    CHAT_ID = os.getenv('CHAT_ID')
    
    # Memberikan nilai default jika variabel tidak diatur, jadi bot tetap bisa jalan
    # dengan konfigurasi standar jika tidak dispesifikasikan di Railway.
    SYMBOL = os.getenv('SYMBOL', 'BTC/USDT')
    TIMEFRAME_SIGNAL = os.getenv('TIMEFRAME_SIGNAL', '15m')
    TIMEFRAME_TREND = os.getenv('TIMEFRAME_TREND', '4h')
    CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '900'))

    # Memeriksa apakah variabel penting sudah diisi. Jika tidak, bot akan berhenti.
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise ValueError("TELEGRAM_TOKEN dan CHAT_ID harus diatur di Environment Variables.")
        
except (ValueError, TypeError) as e:
    logging.critical(f"Error konfigurasi: {e}. Bot berhenti.")
    sys.exit(1)

# --- KONFIGURASI STRATEGI (MA & RSI) ---
SHORT_MA = 10
LONG_MA = 30
TREND_MA = 50
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

# --- INISIALISASI ---
try:
    exchange = ccxt.binance()
    exchange.load_markets() # Memuat semua pasar yang tersedia
    if SYMBOL not in exchange.markets:
        logging.critical(f"Simbol {SYMBOL} tidak valid atau tidak tersedia di Binance. Bot berhenti.")
        sys.exit(1)
        
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
except Exception as e:
    logging.critical(f"Gagal inisialisasi Bursa atau Telegram: {e}. Bot berhenti.")
    sys.exit(1)

last_signal = None # Variabel untuk menyimpan sinyal terakhir

# --- FUNGSI-FUNGSI BANTUAN ---

async def send_telegram_message(message):
    """Mengirim pesan ke Telegram dengan penanganan error."""
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        logging.info("Pesan notifikasi berhasil dikirim.")
    except telegram.error.Unauthorized:
        logging.error("GAGAL OTORISASI TELEGRAM. Periksa nilai TELEGRAM_TOKEN di Railway.")
    except telegram.error.BadRequest as e:
        if "Chat not found" in str(e):
            logging.error("CHAT ID TIDAK DITEMUKAN. Periksa nilai CHAT_ID di Railway.")
        else:
            logging.error(f"Telegram BadRequest: {e}")
    except Exception as e:
        logging.error(f"Gagal mengirim pesan Telegram (Error Umum): {e}")

def get_market_data(symbol, timeframe, limit):
    """Mengambil data pasar dengan penanganan error jaringan."""
    try:
        exchange.rateLimit = True # Menghormati batasan API bursa
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except ccxt.NetworkError as e:
        logging.error(f"Gangguan Jaringan saat mengambil data {symbol} ({timeframe}): {e}")
        return None
    except ccxt.ExchangeError as e:
        logging.error(f"Error dari Bursa saat mengambil data {symbol} ({timeframe}): {e}")
        return None
    except Exception as e:
        logging.error(f"Error tak terduga saat mengambil data: {e}")
        return None

def calculate_rsi(data, period=14):
    """Menghitung Relative Strength Index (RSI)."""
    delta = data['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

# --- FUNGSI INTI ANALISIS ---

async def check_signals():
    """Fungsi utama untuk memeriksa semua kondisi dan menghasilkan sinyal."""
    global last_signal
    logging.info(f"--- Memulai pemeriksaan sinyal untuk {SYMBOL} ---")

    # 1. Analisis Tren Jangka Panjang (Filter Utama)
    df_trend = get_market_data(SYMBOL, TIMEFRAME_TREND, TREND_MA + 5)
    if df_trend is None or len(df_trend) < TREND_MA:
        logging.warning("Data tren tidak cukup/gagal diambil. Melewati siklus ini.")
        return

    df_trend['trend_ma'] = df_trend['close'].rolling(window=TREND_MA).mean()
    last_trend_data = df_trend.iloc[-1]
    main_trend = 'BULLISH' if last_trend_data['close'] > last_trend_data['trend_ma'] else 'BEARISH'
    logging.info(f"Tren Utama ({TIMEFRAME_TREND}): {main_trend}")

    # 2. Analisis Sinyal Masuk (Timeframe Lebih Rendah)
    required_data = max(LONG_MA, RSI_PERIOD) + 5
    df_signal = get_market_data(SYMBOL, TIMEFRAME_SIGNAL, required_data)
    if df_signal is None or len(df_signal) < required_data:
        logging.warning("Data sinyal tidak cukup/gagal diambil. Melewati siklus ini.")
        return

    # Hitung semua indikator
    df_signal['short_ma'] = df_signal['close'].rolling(window=SHORT_MA).mean()
    df_signal['long_ma'] = df_signal['close'].rolling(window=LONG_MA).mean()
    df_signal['rsi'] = calculate_rsi(df_signal, period=RSI_PERIOD)
    
    prev_row = df_signal.iloc[-2]
    curr_row = df_signal.iloc[-1]
    
    current_price = curr_row['close']
    current_rsi = curr_row['rsi']

    # 3. Logika Sinyal Gabungan
    signal = None

    # Kondisi untuk sinyal BUY
    is_golden_cross = prev_row['short_ma'] < prev_row['long_ma'] and curr_row['short_ma'] > curr_row['long_ma']
    if is_golden_cross:
        logging.info("Golden Cross terdeteksi. Memeriksa filter Trend dan RSI...")
        if main_trend == 'BULLISH' and current_rsi < RSI_OVERBOUGHT:
            signal = 'BUY'
            logging.info(f"Semua kondisi BUY terpenuhi.")
        else:
            logging.info(f"Golden Cross diabaikan. Trend: {main_trend}, RSI: {current_rsi:.2f}")

    # Kondisi untuk sinyal SELL
    is_death_cross = prev_row['short_ma'] > prev_row['long_ma'] and curr_row['short_ma'] < curr_row['long_ma']
    if is_death_cross:
        logging.info("Death Cross terdeteksi. Memeriksa filter Trend dan RSI...")
        if main_trend == 'BEARISH' and current_rsi > RSI_OVERSOLD:
            signal = 'SELL'
            logging.info(f"Semua kondisi SELL terpenuhi.")
        else:
            logging.info(f"Death Cross diabaikan. Trend: {main_trend}, RSI: {current_rsi:.2f}")

    # 4. Kirim Notifikasi jika ada sinyal baru yang valid
    if signal and signal != last_signal:
        last_signal = signal
        message = (
            f"🚨 *SINYAL BARU: {signal} UNTUK {SYMBOL}* 🚨\n\n"
            f"*Harga Saat Ini:* `${current_price:,.2f}`\n"
            f"*Timeframe Sinyal:* `{TIMEFRAME_SIGNAL}`\n\n"
            f"*Kondisi Terpenuhi:*\n"
            f"✅ *Crossover:* `{signal} signal`\n"
            f"✅ *Tren Utama ({TIMEFRAME_TREND}):* `{main_trend}`\n"
            f"✅ *RSI ({RSI_PERIOD}):* `{current_rsi:.2f}`\n\n"
            f"_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Harap lakukan riset lebih lanjut._"
        )
        await send_telegram_message(message)
    elif not signal:
        logging.info("Tidak ada sinyal trading baru yang memenuhi semua kriteria.")

# --- LOOP UTAMA BOT ---
async def main():
    """Fungsi utama yang menjalankan bot secara terus-menerus."""
    logging.info(f"Bot Cerdas v2.0 Dimulai! Memantau {SYMBOL}.")
    # Kirim pesan startup ke Telegram untuk menandakan bot telah berhasil di-deploy
    await send_telegram_message(
        f"✅ *Bot Cerdas v2.0 Aktif di Server* ✅\n"
        f"Memantau: `{SYMBOL}`\n"
        f"Timeframe Sinyal: `{TIMEFRAME_SIGNAL}`\n"
        f"Timeframe Trend: `{TIMEFRAME_TREND}`"
    )
    
    while True:
        try:
            await check_signals()
        except Exception as e:
            # Menangkap error tak terduga agar bot tidak crash total
            logging.error(f"Error tak terduga di loop utama: {e}", exc_info=True)
            await send_telegram_message(f"⚠️ *Error pada Bot:* Terjadi kesalahan internal. Bot akan mencoba lagi di siklus berikutnya.")

        logging.info(f"Pemeriksaan selesai. Menunggu {CHECK_INTERVAL} detik.")
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot dihentikan secara manual.")