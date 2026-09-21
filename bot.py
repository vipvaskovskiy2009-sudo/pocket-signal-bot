import os
import json
import time
import urllib.request
import urllib.parse

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}/"

PAIRS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
}

def api(method, data=None):
    url = API + method
    if data:
        data = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=data)
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def get_signal(symbol):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + symbol + "?interval=1m&range=1d"
    )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())

    closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    closes = [x for x in closes if x is not None]

    if len(closes) < 20:
        return "⚪ ПРОПУСК"

    fast = sum(closes[-5:]) / 5
    slow = sum(closes[-13:]) / 13

    if fast > slow:
        return "🟢 ВВЕРХ ↑\n⏱ 1 минута"
    elif fast < slow:
        return "🔴 ВНИЗ ↓\n⏱ 1 минута"

    return "⚪ ПРОПУСК"

def keyboard():
    return json.dumps({
        "inline_keyboard": [
            [
                {"text": "EUR/USD", "callback_data": "EUR/USD"},
                {"text": "GBP/USD", "callback_data": "GBP/USD"}
            ],
            [
                {"text": "USD/JPY", "callback_data": "USD/JPY"}
            ]
        ]
    })

def send(chat_id, text):
    api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": keyboard()
    })

def main():
    offset = 0

    while True:
        try:
            result = api("getUpdates", {
                "timeout": 25,
                "offset": offset
            })

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                if "message" in update:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg.get("text", "")

                    if text == "/start":
                        send(
                            chat_id,
                            "🤖 Pocket Signal\n\n"
                            "Выбери валютную пару.\n"
                            "Я посмотрю минутные данные и дам:\n\n"
                            "🟢 ВВЕРХ ↑\n"
                            "или\n"
                            "🔴 ВНИЗ ↓\n\n"
                            "Экспирация: 1 минута."
                        )

                if "callback_query" in update:
                    q = update["callback_query"]
                    chat_id = q["message"]["chat"]["id"]
                    pair = q["data"]

                    api("answerCallbackQuery", {
                        "callback_query_id": q["id"]
                    })

                    try:
                        signal = get_signal(PAIRS[pair])
                        send(
                            chat_id,
                            f"📊 {pair}\n\n"
                            f"{signal}\n\n"
                            "⚠️ Это технический сигнал, "
                            "не гарантия результата."
                        )
                    except Exception:
                        send(
                            chat_id,
                            f"📊 {pair}\n\n"
                            "⚪ ПРОПУСК\n"
                            "Не удалось получить минутные данные."
                        )

        except Exception:
            time.sleep(5)

if __name__ == "__main__":
    main()
