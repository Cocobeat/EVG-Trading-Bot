"""
Bot di trading v5.1 — Pump Catcher Pro

Novita' rispetto a v5.0 (fix strutturali da audit):
  - Save garantito: try/except/finally attorno a run(), lo stato si salva
    sempre anche se qualcosa esplode a meta'. Alert Telegram su crash.
  - Balance reale: prima di comprare controlla l'EUR disponibile su Kraken
    (non solo la matematica dei profili) ed evita di sforare il capitale.
  - Vendita sicura: prima di vendere controlla il saldo reale dell'asset,
    evita errori "insufficient funds" dovuti a fee/dust.
  - Prezzo di carico reale: dopo un acquisto live, interroga Kraken per il
    prezzo di riempimento effettivo invece di fidarsi del Ticker stantio.
  - Rate limit: se Kraken risponde 429, il bot si ferma per quel run invece
    di insistere; tetto massimo di controlli OHLC per run.

Logica di base (invariata):
  1. Scansiona TUTTE le coppie EUR su Kraken via Ticker batch
  2. Filtra pump per variazione giornaliera, volume, spread
  3. Deep-dive OHLC: candela pump + volume surge + RSI
  4. Entry con SL/TP/trailing fissati come prezzo all'acquisto
  5. SL ritardato (min hold) per assorbire dip temporanei
  6. Cooldown per coin dopo SL (no re-entry su stessa coin)
  7. Max 1 acquisto per run (riduce correlazione)
  8. Regime detection (F&G + BTC) scala i parametri
  9. 3 profili rischio switchabili da Telegram
"""

import base64
import hashlib
import hmac
import json
import math
import os
import time
import traceback
import urllib.parse
from datetime import datetime, timezone

import requests

# ================== PROFILI UTENTE ==================

USER_PROFILES = {
    "sicuro": {
        "label": "\U0001F6E1 SICURO",
        "eur_per_trade": 15.0,
        "max_open_positions": 2,
        "take_profit_pct": 5.0,
        "stop_loss_pct": 4.0,
        "trail_arm_pct": 2.5,
        "trail_distance_pct": 1.5,
        "pump_candle_min_pct": 2.0,
        "pump_volume_surge": 2.0,
        "pump_rsi_max": 75,
        "max_total_loss_eur": 25.0,
        "min_ticker_change_pct": 2.0,
        "min_hold_minutes": 20,
        "max_spread_pct": 1.5,
        "desc": "Pochi trade, protezione capitale",
    },
    "medio": {
        "label": "⚖️ MEDIO",
        "eur_per_trade": 25.0,
        "max_open_positions": 4,
        "take_profit_pct": 10.0,
        "stop_loss_pct": 8.0,
        "trail_arm_pct": 3.0,
        "trail_distance_pct": 2.0,
        "pump_candle_min_pct": 1.5,
        "pump_volume_surge": 1.5,
        "pump_rsi_max": 85,
        "max_total_loss_eur": 50.0,
        "min_ticker_change_pct": 1.0,
        "min_hold_minutes": 15,
        "max_spread_pct": 2.0,
        "desc": "Bilanciato rischio/rendimento",
    },
    "aggressivo": {
        "label": "\U0001F525 AGGRESSIVO",
        "eur_per_trade": 30.0,
        "max_open_positions": 5,
        "take_profit_pct": 15.0,
        "stop_loss_pct": 12.0,
        "trail_arm_pct": 4.0,
        "trail_distance_pct": 3.0,
        "pump_candle_min_pct": 0.8,
        "pump_volume_surge": 1.2,
        "pump_rsi_max": 92,
        "max_total_loss_eur": 80.0,
        "min_ticker_change_pct": 0.5,
        "min_hold_minutes": 10,
        "max_spread_pct": 3.0,
        "desc": "Entra su tutto, massima esposizione",
    },
}

# ================== CONFIG ==================

CONFIG = {
    "QUOTE_CURRENCY": "EUR",
    "SCAN_TIMEFRAME": 5,
    "SCAN_LOOKBACK": 60,
    "MIN_24H_VOLUME_EUR": 5000,
    "MAX_DAILY_PUMP_PCT": 50.0,
    "RSI_PERIOD": 14,
    "COOLDOWN_MINUTES": 60,
    "MAX_BUYS_PER_RUN": 1,
    "MAX_OHLC_CHECKS_PER_RUN": 20,
    "MIN_TRADE_EUR": 5.0,
    "BUY_SAFETY_MARGIN": 0.997,

    "FGI_BULL_THRESHOLD": 55,
    "FGI_BEAR_THRESHOLD": 35,
    "FGI_EXTREME_FEAR_THRESHOLD": 20,
    "BTC_PAIR": "XXBTZEUR",
    "BTC_SMA_PERIOD": 50,

    "KRAKEN_DRY_RUN": False,
    "STATE_FILE": "state.json",
    "TICKER_BATCH_SIZE": 80,
}

RISK_PROFILES = {
    "BULL_AGGRESSIVE": {"eur_mult": 1.3, "pos_add": 2, "tp_mult": 1.2, "sl_mult": 1.3},
    "BULL_MODERATE":   {"eur_mult": 1.15, "pos_add": 1, "tp_mult": 1.1, "sl_mult": 1.1},
    "NEUTRAL":         {"eur_mult": 1.0, "pos_add": 0, "tp_mult": 1.0, "sl_mult": 1.0},
    "BEAR_DEFENSIVE":  {"eur_mult": 0.7, "pos_add": -1, "tp_mult": 0.8, "sl_mult": 0.85},
    "EXTREME_FEAR":    {"eur_mult": 0.5, "pos_add": -1, "tp_mult": 0.7, "sl_mult": 0.75},
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


class RateLimitError(Exception):
    """Sollevato quando Kraken risponde 429 (rate limit)."""
    pass


# ================== PARAMETRI EFFETTIVI ==================

def get_user_profile(state):
    name = state.get("user_profile", "medio")
    return USER_PROFILES.get(name, USER_PROFILES["medio"]), name


def get_params(regime, state):
    up, _ = get_user_profile(state)
    rp = RISK_PROFILES.get(regime, RISK_PROFILES["NEUTRAL"])
    sl_mult = max(1.0, rp["sl_mult"])
    return {
        "eur": round(up["eur_per_trade"] * rp["eur_mult"], 2),
        "max_pos": max(1, up["max_open_positions"] + rp["pos_add"]),
        "tp_pct": round(up["take_profit_pct"] * rp["tp_mult"], 1),
        "sl_pct": round(up["stop_loss_pct"] * sl_mult, 1),
        "trail_arm": up["trail_arm_pct"],
        "trail_dist": up["trail_distance_pct"],
        "pump_candle_min": up["pump_candle_min_pct"],
        "pump_vol_surge": up["pump_volume_surge"],
        "pump_rsi_max": up["pump_rsi_max"],
        "max_loss": up["max_total_loss_eur"],
        "min_ticker_chg": up["min_ticker_change_pct"],
        "min_hold_min": up.get("min_hold_minutes", 15),
        "max_spread": up.get("max_spread_pct", 2.0),
    }


# ================== TELEGRAM ==================

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
        print(f"[ERR] Telegram: {e}")
        return
    if not data.get("ok"):
        return

    updates = data.get("result", [])
    max_id = offset - 1
    for upd in updates:
        max_id = max(max_id, upd["update_id"])
        msg = upd.get("message") or upd.get("edited_message")
        if msg:
            if str(msg.get("chat", {}).get("id", "")) == str(chat_id):
                handle_command((msg.get("text") or "").strip().lower().lstrip("/"), state)
            continue
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
        telegram_send("⏸ Pausa. Posizioni monitorate.", all_buttons(state))

    elif text in ("riprendi", "resume", "riattiva"):
        state["trading_paused"] = False
        telegram_send("▶️ Riattivato.", all_buttons(state))

    elif text in ("stato", "status"):
        send_status(state)

    elif text in ("vendi_tutto", "venditutto", "panic", "emergenza"):
        state["trading_paused"] = True
        force_close_all(state)

    elif text.startswith("vendi_"):
        force_close_one(state, text[6:].upper())

    elif text.startswith("profilo_"):
        name = text[8:]
        if name in USER_PROFILES:
            state["user_profile"] = name
            up = USER_PROFILES[name]
            telegram_send(
                f"✅ Profilo: {up['label']}\n"
                f"{up['desc']}\n"
                f"TP +{up['take_profit_pct']}% | SL -{up['stop_loss_pct']}% | "
                f"Trail {up['trail_arm_pct']}%/{up['trail_distance_pct']}%\n"
                f"Max pos: {up['max_open_positions']} | Max loss: {up['max_total_loss_eur']}€\n"
                f"Spread max: {up.get('max_spread_pct', 2)}% | Hold: {up.get('min_hold_minutes', 15)}min",
                all_buttons(state),
            )

    elif text in ("profilo", "profile"):
        _, current = get_user_profile(state)
        lines = [f"Profilo attuale: <b>{USER_PROFILES[current]['label']}</b>\n"]
        for name, p in USER_PROFILES.items():
            marker = " ← attivo" if name == current else ""
            lines.append(
                f"{p['label']}: TP +{p['take_profit_pct']}%, SL -{p['stop_loss_pct']}%, "
                f"trail {p['trail_arm_pct']}%, {p['max_open_positions']} pos{marker}"
            )
        telegram_send("\n".join(lines), profile_buttons() + control_buttons())


def send_status(state):
    pos = state.get("open_positions", {})
    regime = state.get("current_regime", "NEUTRAL")
    up, _ = get_user_profile(state)
    fgi = state.get("last_fgi")
    cooldowns = state.get("cooldowns", {})
    lines = [
        f"{'⏸ PAUSA' if state.get('trading_paused') else '▶️ Attivo'} | "
        f"{REGIME_LABEL.get(regime, regime)}",
        f"Profilo: {up['label']} | F&G: {fgi if fgi is not None else '?'}",
        f"P&L: {state.get('cumulative_pnl_eur', 0.0):+.2f}€ / "
        f"max loss: -{up['max_total_loss_eur']}€",
    ]
    btns = []
    if pos:
        lines.append(f"\n<b>Posizioni ({len(pos)}):</b>")
        for base, p in pos.items():
            price = p.get("last_price", p["entry_price"])
            chg = (price - p["entry_price"]) / p["entry_price"] * 100
            hold = minutes_held(p)
            hold_min = p.get("min_hold_min", 15)
            armed = (p.get("highest_price", price) - p["entry_price"]) / p["entry_price"] * 100
            trail_status = "\U0001F7E2 trail" if armed >= p.get("trail_arm", 3) else ""
            sl_status = "SL attivo" if hold >= hold_min else f"SL tra {max(0, int(hold_min - hold))}min"
            lines.append(
                f"• {base}: {chg:+.1f}% | {sl_status} {trail_status}"
            )
            btns.append([{"text": f"Vendi {base}", "callback_data": f"vendi_{base.lower()}"}])
    else:
        lines.append("\nNessuna posizione aperta.")
    if cooldowns:
        active = [f"{b}({int(v)}m)" for b, v in cooldowns.items() if v > 0]
        if active:
            lines.append(f"\nCooldown: {', '.join(active)}")
    btns.extend(profile_buttons())
    btns.extend(control_buttons())
    telegram_send("\n".join(lines), btns)


def profile_buttons():
    return [[
        {"text": "\U0001F6E1 Sicuro", "callback_data": "profilo_sicuro"},
        {"text": "⚖️ Medio", "callback_data": "profilo_medio"},
        {"text": "\U0001F525 Aggressivo", "callback_data": "profilo_aggressivo"},
    ]]


def control_buttons():
    return [
        [{"text": "⏸ Pausa", "callback_data": "pausa"},
         {"text": "▶️ Riprendi", "callback_data": "riprendi"}],
        [{"text": "\U0001F4CA Stato", "callback_data": "stato"},
         {"text": "\U0001F6D1 Vendi tutto", "callback_data": "vendi_tutto"}],
    ]


def all_buttons(state):
    pos = state.get("open_positions", {})
    btns = []
    for base in pos:
        btns.append([{"text": f"Vendi {base}", "callback_data": f"vendi_{base.lower()}"}])
    btns.extend(profile_buttons())
    btns.extend(control_buttons())
    return btns


# ================== UTILITA' ==================

def minutes_held(pos):
    entry = pos.get("entry_time")
    if not entry:
        return 9999
    try:
        t = datetime.fromisoformat(entry)
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    except Exception:
        return 9999


def fp(p):
    return f"{p:.6f}".rstrip("0").rstrip(".")


def round_vol(v, dec):
    f = 10 ** dec
    return math.floor(v * f) / f


def update_cooldowns(state):
    """Decrementa cooldown e rimuovi quelli scaduti."""
    cooldowns = state.get("cooldowns", {})
    to_remove = [b for b, mins in cooldowns.items() if mins <= 0]
    for b in to_remove:
        del cooldowns[b]
    state["cooldowns"] = cooldowns


def add_cooldown(state, base):
    """Aggiunge cooldown per una coin dopo SL."""
    cooldowns = state.get("cooldowns", {})
    cooldowns[base] = CONFIG["COOLDOWN_MINUTES"]
    state["cooldowns"] = cooldowns


def is_on_cooldown(state, base):
    """Controlla se una coin e' in cooldown."""
    return state.get("cooldowns", {}).get(base, 0) > 0


def tick_cooldowns(state, elapsed_minutes):
    """Scala i cooldown del tempo trascorso."""
    cooldowns = state.get("cooldowns", {})
    for base in list(cooldowns.keys()):
        cooldowns[base] = max(0, cooldowns[base] - elapsed_minutes)
    state["cooldowns"] = cooldowns


# ================== CHIUSURA POSIZIONI ==================

def force_close_all(state):
    positions = state.get("open_positions", {})
    if not positions:
        telegram_send("\U0001F6D1 Pausa. Nessuna posizione da chiudere.", all_buttons(state))
        return
    telegram_send(f"\U0001F6D1 Chiusura {len(positions)} posizioni...")
    balances = get_balances()
    for base in list(positions.keys()):
        force_close_one(state, base, balances)
    telegram_send("✅ Tutto chiuso. /riprendi per riattivare.", all_buttons(state))


def force_close_one(state, base, balances=None):
    positions = state.get("open_positions", {})
    pos = positions.get(base)
    if not pos:
        telegram_send(f"Nessuna posizione per {base}.")
        return
    if balances is None:
        balances = get_balances()
    price = pos.get("last_price", pos["entry_price"])
    sell_vol = pos["volume"]
    order_note = ""
    if trading_enabled():
        asset_code = pos.get("asset_code")
        if asset_code and asset_code in balances:
            real_bal = balances[asset_code]
            if real_bal <= 0:
                telegram_send(
                    f"⚠️ {base}: saldo reale 0, rimuovo la posizione senza ordine "
                    f"(probabilmente gia' venduta manualmente).",
                )
                del positions[base]
                return
            sell_vol = min(sell_vol, real_bal)
        try:
            result = place_order(pos["pair"], "sell", sell_vol)
            txid = result.get("txid", ["(ok)"])[0]
            order_note = f"\nOrdine: {txid} ({mode_label()})"
        except Exception as e:
            telegram_send(f"⚠️ Errore vendita {base}: {e}")
            return
    entry = pos["entry_price"]
    chg = (price - entry) / entry * 100
    pnl = (price - entry) * sell_vol
    state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + pnl
    telegram_send(
        f"\U0001F534 VENDUTO {base}\n{fp(entry)} → {fp(price)} ({chg:+.1f}%)\n"
        f"P&L: {pnl:+.2f}€ (cum: {state['cumulative_pnl_eur']:+.2f}€){order_note}",
        all_buttons(state),
    )
    del positions[base]


# ================== STATO ==================

def load_state():
    if os.path.exists(CONFIG["STATE_FILE"]):
        with open(CONFIG["STATE_FILE"], "r") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("open_positions", {})
    state.setdefault("last_heartbeat_date", None)
    state.setdefault("cumulative_pnl_eur", 0.0)
    state.setdefault("trading_paused", False)
    state.setdefault("telegram_update_offset", 0)
    state.setdefault("current_regime", "NEUTRAL")
    state.setdefault("last_fgi", None)
    state.setdefault("user_profile", "medio")
    state.setdefault("cooldowns", {})
    state.setdefault("last_run_time", None)
    return state


def save_state(state):
    tmp = CONFIG["STATE_FILE"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, CONFIG["STATE_FILE"])


# ================== REGIME ==================

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
        print(f"[WARN] BTC: {e}")
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


# ================== KRAKEN API ==================

def get_ohlc(pair, interval=None, lookback=None):
    if interval is None:
        interval = CONFIG["SCAN_TIMEFRAME"]
    if lookback is None:
        lookback = CONFIG["SCAN_LOOKBACK"]
    r = requests.get(f"{KRAKEN_PUBLIC}/OHLC",
                     params={"pair": pair, "interval": interval}, timeout=20)
    if r.status_code == 429:
        raise RateLimitError(f"OHLC {pair}: HTTP 429")
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
                "asset_code": info.get("base"),
                "ordermin": float(info.get("ordermin", 0) or 0),
                "lot_decimals": int(info.get("lot_decimals", 8)),
            }
    return pairs


def get_ticker_batch(pair_names):
    all_tickers = {}
    names = list(pair_names)
    batch = CONFIG["TICKER_BATCH_SIZE"]
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        try:
            r = requests.get(f"{KRAKEN_PUBLIC}/Ticker",
                             params={"pair": ",".join(chunk)}, timeout=20)
            if r.status_code == 429:
                print(f"[RATE LIMIT] Kraken 429 su Ticker batch {i}, fermo la scansione")
                break
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
    if r.status_code == 429:
        raise RateLimitError(f"{path}: HTTP 429")
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


def get_balances():
    """Ritorna un dict {asset_code: saldo_float}. Vuoto se non in modalita' live/trading."""
    if not trading_enabled():
        return {}
    try:
        res = kraken_private("/0/private/Balance", {})
        return {k: float(v) for k, v in res.items()}
    except Exception as e:
        print(f"[WARN] Balance: {e}")
        return {}


def get_eur_balance(balances):
    for key in ("ZEUR", "EUR"):
        if key in balances:
            return balances[key]
    return None


def get_order_fill(txid):
    """Interroga Kraken per prezzo medio e volume eseguito di un ordine chiuso."""
    try:
        res = kraken_private("/0/private/QueryOrders", {"txid": txid})
        info = res.get(txid)
        if info:
            price = float(info.get("price", 0) or 0)
            vol_exec = float(info.get("vol_exec", 0) or 0)
            if price > 0 and vol_exec > 0:
                return price, vol_exec
    except Exception as e:
        print(f"[WARN] QueryOrders {txid}: {e}")
    return None, None


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


# ================== SCAN ==================

def scan_pumping(all_pairs, tickers, params, state):
    pumping = []
    min_chg = params["min_ticker_chg"]
    max_chg = CONFIG["MAX_DAILY_PUMP_PCT"]
    max_spread = params["max_spread"]
    skipped_vol = 0
    skipped_spread = 0
    skipped_cooldown = 0

    for pair_name, info in all_pairs.items():
        tick = tickers.get(pair_name)
        if not tick:
            continue

        last_price = float(tick["c"][0])
        open_price = float(tick["o"])
        if open_price <= 0 or last_price <= 0:
            continue

        # Volume 24h in EUR
        vol_eur = float(tick["v"][1]) * last_price
        if vol_eur < CONFIG["MIN_24H_VOLUME_EUR"]:
            skipped_vol += 1
            continue

        # Variazione giornaliera
        chg = (last_price - open_price) / open_price * 100
        if chg < min_chg or chg > max_chg:
            continue

        # Spread bid-ask
        ask = float(tick["a"][0])
        bid = float(tick["b"][0])
        if bid > 0:
            spread = (ask - bid) / bid * 100
            if spread > max_spread:
                skipped_spread += 1
                continue
        else:
            continue

        # Cooldown
        base = info["base"]
        if is_on_cooldown(state, base):
            skipped_cooldown += 1
            continue

        pumping.append({
            "pair": pair_name, "base": base, "asset_code": info.get("asset_code"),
            "ordermin": info["ordermin"], "lot_decimals": info["lot_decimals"],
            "last_price": last_price, "change_today_pct": chg,
            "vol_eur": vol_eur, "spread_pct": spread,
        })

    pumping.sort(key=lambda x: x["change_today_pct"], reverse=True)
    if skipped_vol or skipped_spread or skipped_cooldown:
        print(f"  Filtrati: {skipped_vol} vol basso, {skipped_spread} spread alto, "
              f"{skipped_cooldown} cooldown")
    return pumping


# ================== SELL ==================

def check_sells(positions, tickers, state, params, balances):
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

        sl_price = pos.get("sl_price", entry * (1 - pos.get("sl_pct", params["sl_pct"]) / 100))
        tp_price = pos.get("tp_price", entry * (1 + pos.get("tp_pct", params["tp_pct"]) / 100))
        t_arm = pos.get("trail_arm", params["trail_arm"])
        t_dist = pos.get("trail_dist", params["trail_dist"])

        hold = minutes_held(pos)
        hold_min = pos.get("min_hold_min", params["min_hold_min"])

        reason = None
        is_loss = False

        # TP e trailing attivi sempre
        if price >= tp_price:
            reason = f"Take profit ({chg:+.1f}%)"
        elif (peak - entry) / entry * 100 >= t_arm:
            drop = (peak - price) / peak * 100
            if drop >= t_dist:
                reason = f"Trailing stop (picco {fp(peak)}, {chg:+.1f}%)"

        # SL attivo solo dopo min_hold_minutes
        if not reason and hold >= hold_min and price <= sl_price:
            reason = f"Stop loss ({chg:+.1f}%)"
            is_loss = True

        if not reason:
            continue

        sell_vol = pos["volume"]
        order_note = ""
        if trading_enabled():
            asset_code = pos.get("asset_code")
            if asset_code and asset_code in balances:
                real_bal = balances[asset_code]
                if real_bal <= 0:
                    telegram_send(
                        f"⚠️ {base}: saldo reale 0, rimuovo la posizione senza ordine "
                        f"(probabile vendita manuale o dust)."
                    )
                    del positions[base]
                    continue
                sell_vol = min(sell_vol, real_bal)
            try:
                result = place_order(pos["pair"], "sell", sell_vol)
                txid = result.get("txid", ["(ok)"])[0]
                order_note = f"\nOrdine: {txid}"
            except Exception as e:
                telegram_send(f"⚠️ Errore vendita {base}: {e}")
                continue

        pnl = (price - entry) * sell_vol
        state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + pnl

        telegram_send(
            f"\U0001F534 <b>VENDI {base}</b>\n{reason}\n"
            f"{fp(entry)} → {fp(price)} ({chg:+.1f}%)\n"
            f"P&L: {pnl:+.2f}€ | Cum: {state['cumulative_pnl_eur']:+.2f}€\n"
            f"Hold: {hold:.0f}min | {mode_label()}{order_note}",
            all_buttons(state),
        )
        del positions[base]

        # Cooldown dopo SL
        if is_loss:
            add_cooldown(state, base)
            print(f"  Cooldown {base}: {CONFIG['COOLDOWN_MINUTES']}min")

        if state["cumulative_pnl_eur"] <= -params["max_loss"]:
            state["trading_paused"] = True
            telegram_send(
                f"\U0001F6D1 KILL-SWITCH: {state['cumulative_pnl_eur']:.2f}€ "
                f"oltre -{params['max_loss']}€\n/riprendi per riattivare.",
                all_buttons(state),
            )


# ================== BUY ==================

def check_buys(positions, pumping, state, params, balances):
    bought = 0
    max_buys = CONFIG["MAX_BUYS_PER_RUN"]
    ohlc_checks = 0
    max_ohlc = CONFIG["MAX_OHLC_CHECKS_PER_RUN"]

    available_eur = get_eur_balance(balances) if trading_enabled() else None
    if trading_enabled() and available_eur is not None:
        print(f"Balance EUR disponibile: {available_eur:.2f}€")

    for c in pumping:
        if bought >= max_buys:
            break
        if len(positions) >= params["max_pos"]:
            break
        if ohlc_checks >= max_ohlc:
            print(f"  Raggiunto tetto {max_ohlc} controlli OHLC per questo run")
            break
        if trading_enabled() and available_eur is not None and available_eur < CONFIG["MIN_TRADE_EUR"]:
            print(f"  Capitale EUR insufficiente ({available_eur:.2f}€), stop acquisti")
            break

        base = c["base"]
        if base in positions:
            continue

        try:
            ohlc_checks += 1
            ohlc = get_ohlc(c["pair"])
        except RateLimitError as e:
            print(f"[RATE LIMIT] {e} — interrompo la scansione acquisti per questo run")
            break
        except Exception as e:
            print(f"[ERR] OHLC {c['pair']}: {e}")
            continue

        closes = ohlc["closes"]
        opens = ohlc["opens"]
        volumes = ohlc["volumes"]

        if len(closes) < CONFIG["RSI_PERIOD"] + 5:
            continue

        # Pump candle: penultima (l'ultima e' in formazione)
        idx = -2 if len(closes) > 2 else -1
        c_open = opens[idx]
        c_close = closes[idx]
        if c_open <= 0:
            continue
        candle_chg = (c_close - c_open) / c_open * 100
        if candle_chg < params["pump_candle_min"]:
            time.sleep(0.3)
            continue

        # Volume surge
        avg_n = min(20, len(volumes) - 3)
        if avg_n < 3:
            continue
        avg_vol = sum(volumes[-avg_n - 3:-3]) / avg_n
        vol_ratio = volumes[idx] / avg_vol if avg_vol > 0 else 0
        if vol_ratio < params["pump_vol_surge"]:
            time.sleep(0.3)
            continue

        # RSI (solo candele chiuse)
        rsi_closes = closes[:-1] if len(closes) > CONFIG["RSI_PERIOD"] + 3 else closes
        rsis = compute_rsi(rsi_closes, CONFIG["RSI_PERIOD"])
        if not rsis:
            continue
        rsi = rsis[-1]
        if rsi > params["pump_rsi_max"]:
            time.sleep(0.3)
            continue

        # Dimensiona l'ordine, capato al capitale reale disponibile
        price = c["last_price"]
        eur = params["eur"]
        reduced_for_capital = False
        if trading_enabled() and available_eur is not None:
            capped = max(0.0, available_eur - 1.0)  # margine di sicurezza per fee
            if capped < eur:
                eur = capped
                reduced_for_capital = True
            if eur < CONFIG["MIN_TRADE_EUR"]:
                continue

        vol = round_vol((eur * CONFIG["BUY_SAFETY_MARGIN"]) / price, c["lot_decimals"])
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
                time.sleep(0.3)
                continue

            # Prezzo/volume di riempimento reale (solo LIVE, non dry-run)
            if not CONFIG["KRAKEN_DRY_RUN"]:
                time.sleep(1.5)
                fill_price, fill_vol = get_order_fill(txid)
                if fill_price and fill_vol:
                    price = fill_price
                    vol = fill_vol
                available_eur = max(0.0, available_eur - eur) if available_eur is not None else None

        regime = state.get("current_regime", "NEUTRAL")
        up, _ = get_user_profile(state)
        tp = params["tp_pct"]
        sl = params["sl_pct"]

        sl_price = price * (1 - sl / 100)
        tp_price = price * (1 + tp / 100)

        cap_note = " (ridotto per capitale disponibile)" if reduced_for_capital else ""

        telegram_send(
            f"\U0001F680 <b>COMPRA {base}/{CONFIG['QUOTE_CURRENCY']}</b>\n"
            f"{REGIME_LABEL.get(regime, '')} | {up['label']}\n"
            f"Prezzo: {fp(price)} | {eur:.0f}€{cap_note}\n"
            f"Pump: +{c['change_today_pct']:.1f}% | Candela: +{candle_chg:.1f}% | "
            f"Spread: {c['spread_pct']:.1f}%\n"
            f"Vol 24h: {c['vol_eur']:.0f}€ | Surge: {vol_ratio:.1f}x | RSI: {rsi:.0f}\n"
            f"TP: {fp(tp_price)} (+{tp:.1f}%) | SL: {fp(sl_price)} (-{sl:.1f}%)\n"
            f"Trail: {params['trail_arm']}%/{params['trail_dist']}% | "
            f"SL attivo tra {params['min_hold_min']}min\n"
            f"{mode_label()}{order_note}",
            [[{"text": f"\U0001F534 Vendi {base}", "callback_data": f"vendi_{base.lower()}"}],
             *control_buttons()],
        )

        positions[base] = {
            "pair": c["pair"], "asset_code": c.get("asset_code"),
            "entry_price": price, "volume": vol,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "highest_price": price, "last_price": price,
            "tp_pct": tp, "sl_pct": sl,
            "tp_price": tp_price, "sl_price": sl_price,
            "trail_arm": params["trail_arm"], "trail_dist": params["trail_dist"],
            "min_hold_min": params["min_hold_min"],
            "strategy": "pump",
        }
        bought += 1
        time.sleep(0.3)
    return bought


# ================== MAIN ==================

def _run_body(state):
    positions = state["open_positions"]

    # Calcola tempo trascorso per cooldown
    now_iso = datetime.now(timezone.utc).isoformat()
    last_run = state.get("last_run_time")
    if last_run:
        try:
            elapsed = (datetime.fromisoformat(now_iso) -
                       datetime.fromisoformat(last_run)).total_seconds() / 60.0
            tick_cooldowns(state, elapsed)
        except Exception:
            pass
    state["last_run_time"] = now_iso
    update_cooldowns(state)

    print("Telegram...")
    check_telegram_commands(state)

    regime, fgi, btc = detect_regime()
    params = get_params(regime, state)
    state["current_regime"] = regime
    state["last_fgi"] = fgi

    up, _ = get_user_profile(state)
    print(f"Profilo: {up['label']} | Regime: {REGIME_LABEL.get(regime)}")
    print(f"Params: {params['eur']}€, max {params['max_pos']} pos, "
          f"TP +{params['tp_pct']}%, SL -{params['sl_pct']}%, "
          f"hold {params['min_hold_min']}min, spread <{params['max_spread']}%")
    print(f"Mode: {mode_label()}")

    balances = get_balances()
    if balances:
        eur_bal = get_eur_balance(balances)
        if eur_bal is not None:
            print(f"Balance EUR: {eur_bal:.2f}€")

    all_pairs = get_all_eur_pairs()
    print(f"{len(all_pairs)} coppie EUR")

    tickers = get_ticker_batch(all_pairs.keys())
    print(f"Ticker: {len(tickers)} coppie")

    pumping = scan_pumping(all_pairs, tickers, params, state)
    print(f"{len(pumping)} pump qualificati "
          f"({params['min_ticker_chg']}%-{CONFIG['MAX_DAILY_PUMP_PCT']}%, "
          f"vol>{CONFIG['MIN_24H_VOLUME_EUR']}€, spread<{params['max_spread']}%)")
    if pumping:
        top = ", ".join(
            f"{p['base']}(+{p['change_today_pct']:.0f}%,{p['vol_eur']:.0f}€,"
            f"sp{p['spread_pct']:.1f}%)"
            for p in pumping[:5]
        )
        print(f"  Top: {top}")

    check_sells(positions, tickers, state, params, balances)

    if state.get("trading_paused"):
        print("In pausa.")
    elif len(positions) < params["max_pos"]:
        n = check_buys(positions, pumping, state, params, balances)
        print(f"Aperte {n} posizioni" if n else "Nessun entry")

    # Heartbeat giornaliero
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_heartbeat_date") != today:
        btc_arr = "↑" if btc else "↓" if btc is False else "?"
        pos_lines = ""
        if positions:
            pos_lines = "\n" + "\n".join(
                f"• {b}: "
                f"{((p.get('last_price', p['entry_price']) - p['entry_price']) / p['entry_price'] * 100):+.1f}%"
                for b, p in positions.items()
            )
        cooldowns = state.get("cooldowns", {})
        cd_info = ""
        if cooldowns:
            cd_info = f"\nCooldown: {len(cooldowns)} coin"

        telegram_send(
            f"✅ <b>Bot v5.1 ({mode_label()})</b>\n"
            f"{REGIME_LABEL.get(regime, regime)} | {up['label']}\n"
            f"F&G: {fgi if fgi is not None else '?'} | BTC: {btc_arr}\n"
            f"{params['eur']:.0f}€/trade | max {params['max_pos']} pos | "
            f"TP +{params['tp_pct']:.0f}% SL -{params['sl_pct']:.0f}%\n"
            f"Trail: {params['trail_arm']}%/{params['trail_dist']}% | "
            f"Hold: {params['min_hold_min']}min | Spread <{params['max_spread']}%\n"
            f"Vol min: {CONFIG['MIN_24H_VOLUME_EUR']}€ | "
            f"Anti-FOMO: <{CONFIG['MAX_DAILY_PUMP_PCT']:.0f}% | "
            f"Max buy/run: {CONFIG['MAX_BUYS_PER_RUN']}\n"
            f"Pump: {len(pumping)} | Pos: {len(positions)} | "
            f"P&L: {state.get('cumulative_pnl_eur', 0.0):+.2f}€{pos_lines}{cd_info}"
            + ("\n⚠️ PAUSA" if state.get("trading_paused") else ""),
            all_buttons(state),
        )
        state["last_heartbeat_date"] = today


def run():
    state = load_state()
    try:
        _run_body(state)
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        telegram_send(
            f"⚠️ <b>Errore nel bot</b>: {e}\n"
            f"Lo stato viene comunque salvato. Controlla i log GitHub Actions."
        )
        save_state(state)
        print("Done (con errore).")
        raise
    else:
        save_state(state)
        print("Done.")


if __name__ == "__main__":
    run()
