import aiohttp
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

# Настройка логирования с ротацией
handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=3)  # 5 МБ, 3 резервных файла
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)

# === НАСТРОЙКИ ===
BOT_TOKEN = '7977899864:AAEoviMG0NgG2Al0kUefPY4fUcmJYgUwVxY'
CHAT_ID = -1002471944428  # ID канала с минусом
CHANGE_THRESHOLD = 30  # % изменения
CHECK_INTERVAL = 60  # секунд
WINDOW_MINUTES = 90  # период сравнения в минутах
RETRY_DELAY = 5  # Задержка при ошибке API (сек)

# История цен (в памяти)
price_history = {}

# Отправка сообщения в Telegram
async def send_telegram_message(session: aiohttp.ClientSession, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=10, ssl=False) as response:
            if response.status != 200:
                error_text = await response.text()
                logging.error(f"Ошибка отправки в Telegram: {error_text}")
                print(f"Ошибка отправки в Telegram: {error_text}")
            else:
                logging.info("Сообщение успешно отправлено в Telegram")
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {str(e)}")
        print(f"Ошибка отправки в Telegram: {str(e)}")

# Получение спотовой цены
async def get_spot_price(session: aiohttp.ClientSession, symbol: str):
    try:
        url = f"https://api.mexc.com/api/v3/ticker/price?symbol={symbol.replace('_USDT', 'USDT')}"
        async with session.get(url, timeout=10) as response:
            data = await response.json()
            return float(data.get("price", 0))
    except Exception as e:
        logging.error(f"Ошибка получения спотовой цены для {symbol}: {str(e)}")
        print(f"Ошибка получения спотовой цены для {symbol}: {str(e)}")
        return 0

# Форматирование сообщения
def build_message(ticker, change, max_price, min_price, now_price, fair_price, spot_price, volume_24h, minutes_passed, max_size):
    emoji = "🟢" if change > 0 else "🔴"
    timestamp = (datetime.utcnow() + timedelta(hours=3)).strftime('%H:%M:%S')

    return (
        f"{emoji} ${ticker.replace('_USDT', '')}\n"
        f"     Change: {change:+.2f}%\n\n"
        f"MAX: {max_price:.4f}\n"
        f"MIN: {min_price:.4f}\n\n"
        f"Max size: ${max_size:.2f}\n\n"
        f"Now last price: ${now_price:.4f}\n"
        f"Fair price: ${fair_price:.4f}\n\n"
        f"Spot price: ${spot_price:.6f}\n\n"
        f"⏱️ {minutes_passed:.1f} min\n"
        f"🌊 Volume 24h: ${volume_24h / 1e6:.2f}m\n\n"
        f"🕓 {timestamp} UTC+3"
    )

# Очистка старых данных в price_history
def clean_price_history():
    now = int(time.time())
    for symbol in list(price_history.keys()):
        price_history[symbol] = [p for p in price_history[symbol] if now - p["time"] <= WINDOW_MINUTES * 60]
        if not price_history[symbol]:
            del price_history[symbol]

# Основной цикл
async def monitor_futures():
    logging.info("Инициализация сессии aiohttp")
    connector = aiohttp.TCPConnector(ssl=True)  # Включаем SSL
    async with aiohttp.ClientSession(connector=connector) as session:
        logging.info("Отправка стартового сообщения в Telegram")
        try:
            await send_telegram_message(session, "🔔 Бот запущен! Начинаю мониторинг цен...")
            logging.info("Бот успешно запущен")
        except Exception as e:
            logging.error(f"Не удалось отправить стартовое сообщение: {str(e)}")
            print(f"Не удалось отправить стартовое сообщение: {str(e)}")
            return
        
        while True:
            try:
                url = "https://contract.mexc.com/api/v1/contract/ticker"
                logging.info("Запрос к API MEXC")
                async with session.get(url, timeout=15) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logging.error(f"Ошибка API MEXC: {error_text}")
                        print(f"Ошибка API MEXC: {error_text}")
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    data = (await response.json())["data"]
                
                now = int(time.time())
                logging.info(f"Получено {len(data)} токенов с API MEXC")

                for item in data:
                    symbol = item["symbol"]
                    last_price = float(item.get("lastPrice", 0))
                    volume_24h = float(item.get("volume24", 0))  # Исправлено: volume24 вместо volume24h
                    fair_price = float(item.get("fairPrice", item.get("indexPrice", last_price)))

                    # Логирование ответа API для отладки
                    logging.info(f"API response for {symbol}: {item}")

                    if symbol not in price_history:
                        price_history[symbol] = []
                    price_history[symbol].append({"time": now, "price": last_price, "volume": volume_24h})

                    if len(price_history[symbol]) >= 2:
                        old_price = price_history[symbol][0]["price"]
                        if old_price == 0:
                            continue
                        price_change = ((last_price - old_price) / old_price) * 100
                        logging.info(f"Токен {symbol}: Изменение {price_change:.2f}% (порог {CHANGE_THRESHOLD}%)")

                        if abs(price_change) >= CHANGE_THRESHOLD:
                            prices = [p["price"] for p in price_history[symbol]]
                            volumes = [p["volume"] for p in price_history[symbol]]
                            spot_price = await get_spot_price(session, symbol)
                            max_size = max(volumes) / len(volumes) * 0.01 if volumes else 0

                            msg = build_message(
                                symbol,
                                price_change,
                                max(prices),
                                min(prices),
                                last_price,
                                fair_price,
                                spot_price,
                                volume_24h,
                                (now - price_history[symbol][0]["time"]) / 60,
                                max_size
                            )
                            await send_telegram_message(session, msg)
                            logging.info(f"Отправлен сигнал для {symbol}: Изменение {price_change:.2f}%")
                            price_history[symbol] = [price_history[symbol][-1]]
                
                # Очистка старых данных
                clean_price_history()
                
            except Exception as e:
                logging.error(f"Ошибка в основном цикле: {str(e)}")
                print(f"Ошибка в основном цикле: {str(e)}")
                await asyncio.sleep(RETRY_DELAY)
            
            await asyncio.sleep(CHECK_INTERVAL)

# Старт
if __name__ == "__main__":
    logging.info("Запуск бота")
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logging.info("Событийный цикл создан")
        loop.run_until_complete(monitor_futures())
    except KeyboardInterrupt:
        logging.info("Бот остановлен пользователем (Ctrl+C)")
        print("Бот остановлен пользователем (Ctrl+C)")
    except Exception as e:
        logging.error(f"Критическая ошибка при запуске: {str(e)}")
        print(f"Критическая ошибка при запуске: {str(e)}")
    finally:
        logging.info("Завершение работы бота")
        print("Бот успешно завершен")
        if 'loop' in locals() and not loop.is_closed():
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
