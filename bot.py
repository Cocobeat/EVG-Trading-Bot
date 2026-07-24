"""
Bot di trading v4 — Pump Catcher

Scansiona TUTTE le coppie EUR su Kraken via Ticker (efficiente),
individua quelle che stanno pompando (prezzo in salita + volume alto),
entra con ordine a mercato e esce con TP / trailing stop / SL.

Il bot adatta il rischio al regime di mercato (Fear & Greed + BTC trend)
e invia notifiche Telegram con pulsanti inline per operazioni veloci.
"""

import base64
import hashlib
import hmac
import json
import math
import os
import time
import urllib.parse
from datetime import datetime, timezone

import requests

# ================== CONFIGURAZIONE ==================
CONFIG = {
    "QUOTE_CURRENCY": "EUR",

    # ---- Scan ----
    "SCAN_TIMEFRAME": 5,              # candele a 5 minuti per pump detection
    "SCAN_LOOKBACK": 60,              # candele da scaricare (~5 ore)
    "MIN_24H_VOLUME_EUR": 300,        # volume minimo 24h in EUR per considerare una coppia
    "MIN_TICKER_CHANGE_PCT": 2.0,     # almeno +2% dal prezzo apertura 24h per passare il pre-filtro

    # ---- Pump detection ----
    "PUMP_CANDLE_MIN_PCT": 1.5,       # candela 5min: close almeno +1.5% sopra open
    "PUMP_VOLUME_SURGE": 1.5,         # volume candela >= 1.5x media
    "PUMP_RSI_MAX": 85,               # non entrare se RSI gia' troppo alto
    "PUMP_RSI_PERIOD": 14,

    # ---- Exit ----
    "TAKE_PROFIT_PCT": 8.0,           # TP fisso
    "STOP_LOSS_PCT": 4.0,             # SL fisso
    "TRAIL_ARM_PCT": 3.0,             # trailing stop si arma dopo +3%
    "TRAIL_DISTANCE_PCT": 2.5,        # trailing: vende se scende 2.5% dal picco

    # ---- Posizioni ----
    "EUR_PER_TRADE": 30.0,            # valore base, scalato dal regime
    "MAX_OPEN_POSITIONS": 3,          # valore base, scalato dal regime
    "MAX_TOTAL_LOSS_EUR": 30.0,

    # ---- Regime ----
    "FGI_BULL_THRESHOLD": 55,
    "FGI_BEAR_THRESHOLD": 35,
    "FGI_EXTREME_FEAR_THRESHOLD": 20,
    "BTC_PAIR": "XXBTZEUR",
    "BTC_SMA_PERIOD": 50,

    # ---- Esecuzione ----
    "KRAKEN_DRY_RUN": False,
    "STATE_FILE": "state.json",
    "TICKER_BATCH_SIZE": 80,
}

RISK_PROFILES = {
    "BULL_AGGRESSIVE": {
        "eur_mult": 1.3, "pos_add": 2, "tp_mult": 1.3, "sl_mult": 1.3,
    },
    "BULL_MODERATE": {
        "eur_mult": 1.15, "pos_add": 1, "tp_mult": 1.15, "sl_mult": 1.0,
    },
    "NEUTRAL": {
        "eur_mult": 1.0, "pos_add": 0, "tp_mult": 1.0, "sl_mult": 1.0,
    },
    "BEAR_DEFENSIVE": {
        "eur_mult": 0.7, "pos_add": -1, "tp_mult": 0.8, "sl_mult": 0.7,
    },
    "EXTREME_FEAR": {
        "eur_mult": 0.5, "pos_add": -1, "tp_mult": 0.7, "sl_mult": 0.6,
    },
}

REGIME_LABEL = {
    "BULL_AGGRESSIVE": "\U0001F7E2 BULL AGGRESSIVO",
    "BULL_MODERATE": "\U0001F7E1 BULL MODERATO",
    "NEUTRAL": "⚪ NEUTRALE",
    "BEAR_DEFENSIVE": "\U0001F534 BEAR DIFENSIVO",
    "EXTREME_FEAR": "\U0001F7E3 PAURA ESTREMA",
}

KRAKEN_PUBLIC = "https://api.kraken.com/0/public"
KRAKEN_PRIVATE = "https://api.kraken.com"
FGI_API = "https://api.alternative.me/fng/"


# ================== TELEGRAM (con pulsanti inline) ==================

def telegram_send(text, buttons=None):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(text)
        return
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data=payload, timeout=15)
        if r.status_code != 200:
            print(f"[TG ERR] {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[TG ERR] {e}")


def answer_callback(callback_id):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                      data={"callback_query_id": callback_id}, timeout=5)
    except Exception:
        pass


def check_telegram_commands(state):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    offset = state.get("telegram_update_offset", 0)
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[ERR] Telegram getUpdates: {e}")
        return
    if not data.get("ok"):
        return

    updates = data.get("result", [])
    max_id = offset - 1
    for upd in updates:
        max_id = max(max_id, upd["update_id"])

        # Messaggio testuale
        msg = upd.get("message") or upd.get("edited_message")
        if msg:
            if str(msg.get("chat", {}).get("id", "")) == str(chat_id):
                txt = (msg.get("text") or "").strip().lower().lstrip("/")
                handle_command(txt, state)
            continue

        # Callback da pulsante inline
        cb = upd.get("callback_query")
        if cb:
            if str(cb.get("message", {}).get("chat", {}).get("id", "")) == str(chat_id):
                handle_command((cb.get("data") or "").strip().lower(), state)
                answer_callback(cb["id"])

    if updates:
        state["telegram_update_offset"] = max_id + 1


def handle_command(text, state):
    if text in ("pausa", "stop", "ferma"):
        state["trading_paused"] = True
        telegram_send("⏸ Trading in pausa. Posizioni aperte ancora monitorate.",
                      control_buttons())

    elif text in ("riprendi", "resume", "riattiva"):
        state["trading_paused"] = False
        telegram_send("▶️ Trading riattivato.", control_buttons())

    elif text in ("stato", "status"):
        send_status(state)

    elif text in ("vendi_tutto", "venditutto", "panic", "emergenza"):
        state["trading_paused"] = True
        force_close_all(state)

    elif text.startswith("vendi_"):
        base = text[6:].upper()
        force_close_one(state, base)


def send_status(state):
    pos = state.get("open_positions", {})
    regime = state.get("current_regime", "NEUTRAL")
    fgi = state.get("last_fgi")
    lines = [
        f"{'⏸ PAUSA' if state.get('trading_paused') else '▶️ Attivo'} | {REGIME_LABEL.get(regime, regime)}",
        f"F&G: {fgi if fgi is not None else '?'} | P&L: {state.get('cumulative_pnl_eur', 0.0):+.2f}€",
    ]
    btns = []
    if pos:
        lines.append(f"\n<b>Posizioni ({len(pos)}):</b>")
        for base, p in pos.items():
            chg = (p.get("last_price", p["entry_price"]) - p["entry_price"]) / p["entry_price"] * 100
            lines.append(f"• {base}: {chg:+.1f}% (entry {fp(p['entry_price'])})")
            btns.append([{"text": f"Vendi {base}", "callback_data": f"vendi_{base.lower()}"}])
    else:
        lines.append("\nNessuna posizione aperta.")
    btns.extend(control_buttons())
    telegram_send("\n".join(lines), btns)


def control_buttons():
    return [
        [{"text": "⏸ Pausa", "callback_data": "pausa"},
         {"text": "▶️ Riprendi", "callback_data": "riprendi"}],
        [{"text": "📊 Stato", "callback_data": "stato"},
         {"text": "🛑 Vendi tutto", "callback_data": "vendi_tutto"}],
    ]


# ================== CHIUSURA POSIZIONI ==================

def force_close_all(state):
    positions = state.get("open_positions", {})
    if not positions:
        telegram_send("🛑 Pausa attivata. Nessuna posizione da chiudere.", control_buttons())
        return
    telegram_send(f"🛑 Chiusura emergenza {len(positions)} posizioni...")
    for base in list(positions.keys()):
        force_close_one(state, base)
    telegram_send("✅ Tutto chiuso. /riprendi per riattivare.", control_buttons())


def force_close_one(state, base):
    positions = state.get("open_positions", {})
    pos = positions.get(base)
    if not pos:
        telegram_send(f"Nessuna posizione aperta per {base}.")
        return

    current_price = pos.get("last_price", pos["entry_price"])
    order_note = ""
    if trading_enabled():
        try:
            result = place_order(pos["pair"], "sell", pos["volume"])
            txid = result.get("txid", ["(ok)"])[0]
            order_note = f"\nOrdine: {txid} ({mode_label()})"
        except Exception as e:
            telegram_send(f"⚠️ Errore vendita {base}: {e}")
            return

    entry = pos["entry_price"]
    chg = (current_price - entry) / entry * 100
    pnl = (current_price - entry) * pos["volume"]
    state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + pnl

    telegram_send(
        f"🔴 VENDUTO {base}/{CONFIG['QUOTE_CURRENCY']}\n"
        f"{fp(entry)} → {fp(current_price)} ({chg:+.1f}%)\n"
        f"P&L: {pnl:+.2f}€ (cum: {state['cumulative_pnl_eur']:+.2f}€){order_note}",
        control_buttons(),
    )
    del positions[base]


# ================== STATO ==================

def load_state():
    if os.path.exists(CONFIG["STATE_FILE"]):
        with open(CONFIG["STATE_FILE"], "r") as f:
            return json.load(f)
    return {
        "open_positions": {}, "last_heartbeat_date": None,
        "cumulative_pnl_eur": 0.0, "trading_paused": False,
        "telegram_update_offset": 0, "current_regime": "NEUTRAL", "last_fgi": None,
    }


def save_state(state):
    with open(CONFIG["STATE_FILE"], "w") as f:
        json.dump(state, f, indent=2)


# ================== REGIME DETECTION ==================

def get_fgi():
    try:
        r = requests.get(FGI_API, timeout=10)
        r.raise_for_status()
        return int(r.json()["data"][0]["value"])
    except Exception as e:
        print(f"[WARN] FGI: {e}")
        return None


def get_btc_above_sma():
    try:
        ohlc = get_ohlc(CONFIG["BTC_PAIR"], 60, CONFIG["BTC_SMA_PERIOD"] + 10)
        closes = ohlc["closes"]
        if len(closes) < CONFIG["BTC_SMA_PERIOD"]:
            return None
        sma = sum(closes[-CONFIG["BTC_SMA_PERIOD"]:]) / CONFIG["BTC_SMA_PERIOD"]
        return closes[-1] > sma
    except Exception as e:
        print(f"[WARN] BTC trend: {e}")
        return None


def detect_regime():
    fgi = get_fgi()
    btc = get_btc_above_sma()
    print(f"F&G: {fgi} | BTC>SMA: {btc}")
    if fgi is None and btc is None:
        return "NEUTRAL", fgi, btc
    if fgi is not None and fgi <= CONFIG["FGI_EXTREME_FEAR_THRESHOLD"]:
        return "EXTREME_FEAR", fgi, btc
    if fgi is not None and fgi >= CONFIG["FGI_BULL_THRESHOLD"] and btc is True:
        return "BULL_AGGRESSIVE", fgi, btc
    if btc is True and (fgi is None or fgi >= CONFIG["FGI_BEAR_THRESHOLD"]):
        return "BULL_MODERATE", fgi, btc
    if (fgi is not None and fgi < CONFIG["FGI_BEAR_THRESHOLD"]) or btc is False:
        return "BEAR_DEFENSIVE", fgi, btc
    return "NEUTRAL", fgi, btc


def get_params(regime):
    p = RISK_PROFILES.get(regime, RISK_PROFILES["NEUTRAL"])
    return {
        "eur": round(CONFIG["EUR_PER_TRADE"] * p["eur_mult"], 2),
        "max_pos": max(1, CONFIG["MAX_OPEN_POSITIONS"] + p["pos_add"]),
        "tp_pct": CONFIG["TAKE_PROFIT_PCT"] * p["tp_mult"],
        "sl_pct": CONFIG["STOP_LOSS_PCT"] * p["sl_mult"],
        "trail_arm": CONFIG["TRAIL_ARM_PCT"],
        "trail_dist": CONFIG["TRAIL_DISTANCE_PCT"],
    }


# ================== KRAKEN API ==================

def get_ohlc(pair, interval=None, lookback=None):
    if interval is None:
        interval = CONFIG["SCAN_TIMEFRAME"]
    if lookback is None:
        lookback = CONFIG["SCAN_LOOKBACK"]
    r = requests.get(f"{KRAKEN_PUBLIC}/OHLC",
                     params={"pair": pair, "interval": interval}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"OHLC {pair}: {data['error']}")
    result = data["result"]
    key = [k for k in result if k != "last"][0]
    candles = result[key]
    n = lookback
    return {
        "closes": [float(c[4]) for c in candles][-n:],
        "opens": [float(c[1]) for c in candles][-n:],
        "volumes": [float(c[6]) for c in candles][-n:],
        "highs": [float(c[2]) for c in candles][-n:],
        "lows": [float(c[3]) for c in candles][-n:],
    }


def get_all_eur_pairs():
    r = requests.get(f"{KRAKEN_PUBLIC}/AssetPairs", timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"AssetPairs: {data['error']}")
    pairs = {}
    for name, info in data["result"].items():
        ws = info.get("wsname", "") or ""
        if ws.endswith(f"/{CONFIG['QUOTE_CURRENCY']}"):
            pairs[name] = {
                "base": ws.split("/")[0],
                "ordermin": float(info.get("ordermin", 0) or 0),
                "lot_decimals": int(info.get("lot_decimals", 8)),
            }
    return pairs


def get_ticker_batch(pair_names):
    """Ticker per tutte le coppie, a batch."""
    all_tickers = {}
    names = list(pair_names)
    batch = CONFIG["TICKER_BATCH_SIZE"]
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        try:
            r = requests.get(f"{KRAKEN_PUBLIC}/Ticker",
                             params={"pair": ",".join(chunk)}, timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("result"):
                all_tickers.update(data["result"])
        except Exception as e:
            print(f"[WARN] Ticker batch {i}: {e}")
        time.sleep(0.3)
    return all_tickers


def trading_enabled():
    return bool(os.environ.get("KRAKEN_API_KEY")) and bool(os.environ.get("KRAKEN_API_SECRET"))


def _kraken_sig(path, data, secret):
    post = urllib.parse.urlencode(data)
    enc = (str(data["nonce"]) + post).encode()
    msg = path.encode() + hashlib.sha256(enc).digest()
    mac = hmac.new(base64.b64decode(secret), msg, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def kraken_private(path, data):
    key = os.environ.get("KRAKEN_API_KEY")
    secret = os.environ.get("KRAKEN_API_SECRET")
    if not key or not secret:
        raise RuntimeError("Chiavi Kraken mancanti")
    payload = dict(data)
    payload["nonce"] = str(int(time.time() * 1000))
    headers = {"API-Key": key, "API-Sign": _kraken_sig(path, payload, secret)}
    r = requests.post(KRAKEN_PRIVATE + path, headers=headers, data=payload, timeout=20)
    r.raise_for_status()
    res = r.json()
    if res.get("error"):
        raise RuntimeError(f"Kraken {path}: {res['error']}")
    return res["result"]


def place_order(pair, side, volume):
    data = {"pair": pair, "type": side, "ordertype": "market", "volume": f"{volume}"}
    if CONFIG["KRAKEN_DRY_RUN"]:
        data["validate"] = "true"
    return kraken_private("/0/private/AddOrder", data)


def mode_label():
    if not trading_enabled():
        return "SEGNALE"
    return "DRY-RUN" if CONFIG["KRAKEN_DRY_RUN"] else "LIVE"


# ================== INDICATORI ==================

def compute_rsi(closes, period=14):
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    rsis = [100.0 if al == 0 else 100 - 100 / (1 + ag / al)]
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        rsis.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return rsis


def fp(p):
    return f"{p:.6f}".rstrip("0").rstrip(".")


def round_vol(v, dec):
    f = 10 ** dec
    return math.floor(v * f) / f


# ================== SCAN & FILTER ==================

def scan_pumping_pairs(all_pairs, tickers):
    """Pre-filtra le coppie che stanno pompando usando i dati del Ticker."""
    pumping = []
    for pair_name, info in all_pairs.items():
        tick = tickers.get(pair_name)
        if not tick:
            continue

        last_price = float(tick["c"][0])
        open_price = float(tick["o"])
        vol_24h = float(tick["v"][1])  # volume 24h in base currency

        if open_price <= 0:
            continue

        # Volume in EUR
        vol_eur = vol_24h * last_price
        if vol_eur < CONFIG["MIN_24H_VOLUME_EUR"]:
            continue

        change_pct = (last_price - open_price) / open_price * 100
        if change_pct < CONFIG["MIN_TICKER_CHANGE_PCT"]:
            continue

        pumping.append({
            "pair": pair_name,
            "base": info["base"],
            "ordermin": info["ordermin"],
            "lot_decimals": info["lot_decimals"],
            "last_price": last_price,
            "change_today_pct": change_pct,
            "vol_eur_24h": vol_eur,
        })

    # Ordina per variazione oggi (i pump più forti prima)
    pumping.sort(key=lambda x: x["change_today_pct"], reverse=True)
    return pumping


# ================== SELL LOGIC ==================

def check_sells(positions, tickers, state, params):
    for base, pos in list(positions.items()):
        tick = tickers.get(pos["pair"])
        if not tick:
            continue
        price = float(tick["c"][0])
        pos["last_price"] = price
        entry = pos["entry_price"]
        chg = (price - entry) / entry * 100

        pos["highest_price"] = max(pos.get("highest_price", entry), price)
        peak = pos["highest_price"]

        tp_pct = pos.get("tp_pct", params["tp_pct"])
        sl_pct = pos.get("sl_pct", params["sl_pct"])
        trail_arm = pos.get("trail_arm", params["trail_arm"])
        trail_dist = pos.get("trail_dist", params["trail_dist"])

        reason = None

        # 1. Stop loss
        if chg <= -sl_pct:
            reason = f"Stop loss ({chg:+.1f}%)"

        # 2. Take profit
        elif chg >= tp_pct:
            reason = f"Take profit ({chg:+.1f}%)"

        # 3. Trailing stop
        elif (peak - entry) / entry * 100 >= trail_arm:
            drop_from_peak = (peak - price) / peak * 100
            if drop_from_peak >= trail_dist:
                reason = f"Trailing stop (picco {fp(peak)}, {chg:+.1f}%)"

        if not reason:
            continue

        order_note = ""
        if trading_enabled():
            try:
                result = place_order(pos["pair"], "sell", pos["volume"])
                txid = result.get("txid", ["(ok)"])[0]
                order_note = f"\nOrdine: {txid}"
            except Exception as e:
                telegram_send(f"⚠️ Errore vendita {base}: {e}")
                continue

        pnl = (price - entry) * pos["volume"]
        state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + pnl

        telegram_send(
            f"🔴 <b>VENDI {base}</b>\n"
            f"{reason}\n"
            f"{fp(entry)} → {fp(price)} ({chg:+.1f}%)\n"
            f"P&L: {pnl:+.2f}€ | Cum: {state['cumulative_pnl_eur']:+.2f}€\n"
            f"{mode_label()}{order_note}",
            control_buttons(),
        )
        del positions[base]

        # Kill-switch
        if state["cumulative_pnl_eur"] <= -CONFIG["MAX_TOTAL_LOSS_EUR"]:
            state["trading_paused"] = True
            telegram_send(
                f"🛑 KILL-SWITCH: perdita {state['cumulative_pnl_eur']:.2f}€ oltre limite.\n"
                f"/riprendi per riattivare.",
                control_buttons(),
            )


# ================== BUY LOGIC (PUMP CATCHER) ==================

def check_pump_buys(positions, pumping_pairs, state, params):
    """Cerca pump candle + volume surge nelle coppie pre-filtrate dal Ticker."""
    bought = 0
    for c in pumping_pairs:
        if len(positions) >= params["max_pos"]:
            break
        base = c["base"]
        if base in positions:
            continue

        try:
            ohlc = get_ohlc(c["pair"])
        except Exception as e:
            print(f"[ERR] OHLC {c['pair']}: {e}")
            continue

        closes = ohlc["closes"]
        opens = ohlc["opens"]
        volumes = ohlc["volumes"]

        if len(closes) < CONFIG["PUMP_RSI_PERIOD"] + 5:
            continue

        # Pump candle: ultima candela chiusa forte
        # Usiamo la penultima (l'ultima potrebbe essere ancora in formazione)
        idx = -2 if len(closes) > 2 else -1
        candle_open = opens[idx]
        candle_close = closes[idx]
        if candle_open <= 0:
            continue
        candle_chg = (candle_close - candle_open) / candle_open * 100
        if candle_chg < CONFIG["PUMP_CANDLE_MIN_PCT"]:
            time.sleep(0.5)
            continue

        # Volume surge
        avg_period = min(20, len(volumes) - 3)
        if avg_period < 3:
            continue
        avg_vol = sum(volumes[-avg_period - 3:-3]) / avg_period if avg_period > 0 else 0
        candle_vol = volumes[idx]
        vol_ratio = candle_vol / avg_vol if avg_vol > 0 else 0
        if vol_ratio < CONFIG["PUMP_VOLUME_SURGE"]:
            time.sleep(0.5)
            continue

        # RSI check
        rsis = compute_rsi(closes, CONFIG["PUMP_RSI_PERIOD"])
        if not rsis:
            continue
        rsi = rsis[-1]
        if rsi > CONFIG["PUMP_RSI_MAX"]:
            time.sleep(0.5)
            continue

        # Current price (from last close or ticker data)
        price = c["last_price"]

        # Volume e ordine
        eur = params["eur"]
        raw_vol = eur / price
        vol = round_vol(raw_vol, c["lot_decimals"])
        if vol <= 0 or vol < c["ordermin"]:
            continue

        order_note = ""
        if trading_enabled():
            try:
                result = place_order(c["pair"], "buy", vol)
                txid = result.get("txid", ["(ok)"])[0]
                order_note = f"\nOrdine: {txid}"
            except Exception as e:
                telegram_send(f"⚠️ Errore acquisto {base}: {e}")
                time.sleep(0.5)
                continue

        regime = state.get("current_regime", "NEUTRAL")
        tp = params["tp_pct"]
        sl = params["sl_pct"]

        telegram_send(
            f"🚀 <b>COMPRA {base}/{CONFIG['QUOTE_CURRENCY']}</b>\n"
            f"{REGIME_LABEL.get(regime, regime)}\n"
            f"Prezzo: {fp(price)} | {eur:.0f}€\n"
            f"Pump oggi: +{c['change_today_pct']:.1f}% | Candela: +{candle_chg:.1f}%\n"
            f"Volume: {vol_ratio:.1f}x media | RSI: {rsi:.0f}\n"
            f"TP: +{tp:.1f}% | SL: -{sl:.1f}% | Trail: +{params['trail_arm']:.0f}%→{params['trail_dist']:.1f}%\n"
            f"{mode_label()}{order_note}",
            [[{"text": f"🔴 Vendi {base}", "callback_data": f"vendi_{base.lower()}"}],
             *control_buttons()],
        )

        positions[base] = {
            "pair": c["pair"],
            "entry_price": price,
            "volume": vol,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "highest_price": price,
            "last_price": price,
            "tp_pct": tp,
            "sl_pct": sl,
            "trail_arm": params["trail_arm"],
            "trail_dist": params["trail_dist"],
            "strategy": "pump",
        }
        bought += 1
        time.sleep(0.5)

    return bought


# ================== MAIN ==================

def run():
    state = load_state()
    positions = state.get("open_positions", {})

    print("Telegram commands...")
    check_telegram_commands(state)

    # Regime
    regime, fgi, btc = detect_regime()
    params = get_params(regime)
    state["current_regime"] = regime
    state["last_fgi"] = fgi
    print(f"Regime: {REGIME_LABEL.get(regime)} | {params['eur']}€/trade, max {params['max_pos']} pos")
    print(f"Mode: {mode_label()}")

    # Scan ALL pairs
    all_pairs = get_all_eur_pairs()
    print(f"{len(all_pairs)} coppie EUR su Kraken")

    tickers = get_ticker_batch(all_pairs.keys())
    print(f"Ticker ricevuto per {len(tickers)} coppie")

    # Pre-filter: chi sta pompando?
    pumping = scan_pumping_pairs(all_pairs, tickers)
    print(f"{len(pumping)} coppie con pump attivo (>{CONFIG['MIN_TICKER_CHANGE_PCT']}% oggi)")
    if pumping:
        top5 = ", ".join(f"{p['base']}(+{p['change_today_pct']:.0f}%)" for p in pumping[:5])
        print(f"  Top: {top5}")

    # Sells
    check_sells(positions, tickers, state, params)

    # Buys
    if state.get("trading_paused"):
        print("Trading in pausa.")
    elif len(positions) < params["max_pos"]:
        n = check_pump_buys(positions, pumping, state, params)
        if n:
            print(f"Aperte {n} nuove posizioni")
        else:
            print("Nessun pump entry signal scattato")

    # Heartbeat
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_heartbeat_date") != today:
        btc_arrow = "↑" if btc else "↓" if btc is False else "?"
        pos_summary = ""
        if positions:
            pos_summary = "\n" + "\n".join(
                f"• {b}: {((p.get('last_price',p['entry_price'])-p['entry_price'])/p['entry_price']*100):+.1f}%"
                for b, p in positions.items()
            )
        telegram_send(
            f"✅ <b>Bot v4 ({mode_label()})</b>\n"
            f"{REGIME_LABEL.get(regime, regime)}\n"
            f"F&G: {fgi if fgi is not None else '?'} | BTC: {btc_arrow}\n"
            f"{params['eur']:.0f}€/trade | max {params['max_pos']} pos\n"
            f"Pump attivi: {len(pumping)} | Posizioni: {len(positions)}\n"
            f"P&L: {state.get('cumulative_pnl_eur', 0.0):+.2f}€"
            f"{pos_summary}"
            + ("\n⚠️ PAUSA" if state.get("trading_paused") else ""),
            control_buttons(),
        )
        state["last_heartbeat_date"] = today

    state["open_positions"] = positions
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    run()
