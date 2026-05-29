import os
import time
import json
import requests
from datetime import datetime, timezone, timedelta

# Binance Spot Klines endpoint (/api/v3/klines) [2](https://cronbuilder.dev/blog/github-actions-cron-schedule.html)
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# ---- 설정값(환경변수로 바꿀 수 있음) ----
SYMBOL = os.getenv("SYMBOL", "BTCUSDC")
INTERVAL = os.getenv("INTERVAL", "1h")          # 1m,5m,15m,1h,4h,1d...
DAYS = int(os.getenv("DAYS", "90"))             # 3개월=90일
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))

OVERBOUGHT = float(os.getenv("OVERBOUGHT", "70"))
OVERSOLD = float(os.getenv("OVERSOLD", "30"))

# “같은 구간에서 스팸 방지”용: 같은 상태면 일정 시간 안에 재발송 금지(기본 24시간)
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "24"))

STATE_FILE = "state.json"

# ---- 유틸 ----
def interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    val = int(interval[:-1])
    if unit == "m":
        return val * 60_000
    if unit == "h":
        return val * 3_600_000
    if unit == "d":
        return val * 86_400_000
    raise ValueError("INTERVAL 지원 예: 1m, 5m, 15m, 1h, 4h, 1d")

def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int):
    """limit 최대 1000이므로 pagination으로 필요한 만큼 수집"""
    out = []
    step = interval_to_ms(interval)

    while True:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000
        }
        r = requests.get(BINANCE_KLINES_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data:
            break

        out.extend(data)

        last_open = int(data[-1][0])
        next_start = last_open + step

        if len(data) < 1000 or next_start >= end_ms:
            break

        start_ms = next_start
        time.sleep(0.2)

    # openTime 기준 중복 제거
    uniq = {}
    for row in out:
        uniq[int(row[0])] = row
    return [uniq[k] for k in sorted(uniq.keys())]

def wilders_rsi(closes, period=14):
    """Wilder RSI"""
    if len(closes) <= period + 1:
        return [None] * len(closes)

    deltas = [None]
    for i in range(1, len(closes)):
        deltas.append(closes[i] - closes[i - 1])

    gains = [0.0 if d is None else max(d, 0.0) for d in deltas]
    losses = [0.0 if d is None else max(-d, 0.0) for d in deltas]

    rsi = [None] * len(closes)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    def rsi_value(ag, al):
        if al == 0 and ag == 0:
            return 50.0
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    rsi[period] = rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i] = rsi_value(avg_gain, avg_loss)

    return rsi

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # zone: neutral/overbought/oversold
        # last_sent_utc: ISO string
        return {"zone": "neutral", "last_sent_utc": None}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def cooldown_ok(last_sent_utc: str | None) -> bool:
    if not last_sent_utc:
        return True
    try:
        last_dt = datetime.fromisoformat(last_sent_utc.replace("Z", "+00:00"))
    except Exception:
        return True
    return (datetime.now(timezone.utc) - last_dt) >= timedelta(hours=COOLDOWN_HOURS)

def send_sms(body: str):
    """
    Twilio로 SMS 발송 (환경변수로 설정)
    Twilio SDK 사용 방식은 공식 퀵스타트 형태 [3](https://github.com/StephenDsouza90/az-function-time-trigger-app)
    """
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_no = os.getenv("TWILIO_FROM_NUMBER")
    to_no = os.getenv("TO_NUMBER")

    if not all([sid, token, from_no, to_no]):
        raise RuntimeError("Twilio 환경변수(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TO_NUMBER)가 필요합니다.")

    from twilio.rest import Client
    client = Client(sid, token)
    client.messages.create(body=body, from_=from_no, to=to_no)

def main():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=DAYS)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    klines = fetch_klines(SYMBOL, INTERVAL, start_ms, end_ms)
    if len(klines) < RSI_PERIOD + 10:
        raise RuntimeError(f"캔들 데이터가 부족합니다: {len(klines)}개")

    closes = [float(k[4]) for k in klines]  # close index=4
    rsi_series = wilders_rsi(closes, RSI_PERIOD)

    rsi_vals = [x for x in rsi_series if x is not None]
    avg_rsi_3m = sum(rsi_vals) / len(rsi_vals)

    last_price = closes[-1]
    last_open_ms = int(klines[-1][0])
    ts = datetime.fromtimestamp(last_open_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if avg_rsi_3m >= OVERBOUGHT:
        zone = "overbought"
    elif avg_rsi_3m <= OVERSOLD:
        zone = "oversold"
    else:
        zone = "neutral"

    state = load_state()
    prev_zone = state.get("zone", "neutral")
    last_sent_utc = state.get("last_sent_utc")

    # “나왔을 때” = (1) 구간 진입 or (2) 같은 구간이라도 쿨다운 지나면 재알림
    should_send = False
    if zone in ("overbought", "oversold"):
        if zone != prev_zone:
            should_send = True
        elif cooldown_ok(last_sent_utc):
            should_send = True

    if should_send:
        tag = "🚨 과매수" if zone == "overbought" else "🧊 과매도"
        msg = (
            f"{tag} (BTC/USDC)\n"
            f"3개월평균 RSI={avg_rsi_3m:.2f} (OB≥{OVERBOUGHT}, OS≤{OVERSOLD})\n"
            f"가격={last_price:,.2f} USDC\n"
            f"시간={ts}"
        )
        send_sms(msg)
        state["last_sent_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    state["zone"] = zone
    save_state(state)

    print(f"OK zone={zone}, prev={prev_zone}, avg_rsi_3m={avg_rsi_3m:.2f}, price={last_price:.2f}, time={ts}")

if __name__ == "__main__":
    main()
