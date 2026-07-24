"""
Bot di trading v4.1 — Pump Catcher con profili rischio

Scansiona TUTTE le coppie EUR su Kraken via Ticker, individua pump,
entra e esce con TP/trailing/SL. Tre profili selezionabili da Telegram:
  🛡 SICURO — pochi trade, TP stretto, SL stretto, protegge il capitale
  ⚖️ MEDIO — bilanciato, il profilo di default
  🔥 AGGRESSIVO — entra su tutto, TP largo, SL largo, massima esposizione

Il regime di mercato (Fear & Greed + BTC) scala i parametri del profilo
scelto: in bull amplifica, in bear riduce.
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

# ================== PROFILI UTENTE (switchabili da Telegram) ==================

USER_PROFILES = {
    "sicuro": {
        "label": "\U0001F6E1 SICURO",
        "eur_per_trade": 15.0,
        "max_open_positions": 2,
        "take_profit_pct": 5.0,
        "stop_loss_pct": 4.0,
        "trail_arm_pct": 3.0,
        "trail_distance_pct": 2.0,
        "pump_candle_min_pct": 1.5,
        "pump_volume_surge": 1.5,
        "pump_rsi_max": 80,
        "max_total_loss_eur": 25.0,
        "min_ticker_change_pct": 1.5,
        "desc": "Pochi trade, protezione capitale",
    },
    "medio": {
        "label": "⚖️ MEDIO",
        "eur_per_trade": 25.0,
        "max_open_positions": 4,
        "take_profit_pct": 10.0,
        "stop_loss_pct": 8.0,
        "trail_arm_pct": 5.0,
        "trail_distance_pct": 3.0,
        "pump_candle_min_pct": 1.0,
        "pump_volume_surge": 1.2,
        "pump_rsi_max": 90,
        "max_total_loss_eur": 50.0,
        "min_ticker_change_pct": 1.0,
        "desc": "Bilanciato rischio/rendimento",
    },
    "aggressivo": {
        "label": "\U0001F525 AGGRESSIVO",
        "eur_per_trade": 30.0,
        "max_open_positions": 5,
        "take_profit_pct": 15.0,
        "stop_loss_pct": 12.0,
        "trail_arm_pct": 4.0,
        "trail_distance_pct": 3.5,
        "pump_candle_min_pct": 0.5,
        "pump_volume_surge": 1.0,
        "pump_rsi_max": 95,
        "max_total_loss_eur": 80.0,
        "min_ticker_change_pct": 0.5,
        "desc": "Entra su tutto, massima esposizione",
    },
}

# ================== CONFIG (parametri non legati al profilo) ==================

CONFIG = {
    "QUOTE_CURRENCY": "EUR",
    "SCAN_TIMEFRAME": 5,
    "SCAN_LOOKBACK": 60,
    "MIN_24H_VOLUME_EUR": 100,
    "RSI_PERIOD": 14,

    "FGI_BULL_THRESHOLD": 55,
    "FGI_BEAR_THRESHOLD": 35,
    "FGI_EXTREME_FEAR_THRESHOLD": 20,
    "BTC_PAIR": "XXBTZEUR",
    "BTC_SMA_PERIOD": 50,

    "KRAKEN_DRY_RUN": False,
    "STATE_FILE": "state.json",
    "TICKER_BATCH_SIZE": 80,
}

# Regime: moltiplica i valori del profilo scelto
RISK_PROFILES = {
    "BULL_AGGRESSIVE": {"eur_mult": 1.3, "pos_add": 2, "tp_mult": 1.2, "sl_mult": 1.3},
    "BULL_MODERATE":   {"eur_mult": 1.15, "pos_add": 1, "tp_mult": 1.1, "sl_mult": 1.1},
    "NEUTRAL":         {"eur_mult": 1.0, "pos_add": 0, "tp_mult": 1.0, "sl_mult": 1.0},
    "BEAR_DEFENSIVE":  {"eur_mult": 0.7, "pos_add": -1, "tp_mult": 0.8, "sl_mult": 0.7},
    "EXTREME_FEAR":    {"eur_mult": 0.5, "pos_add": -1, "tp_mult": 0.7, "sl_mult": 0.6},
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


# ================== PARAMETRI EFFETTIVI ==================

def get_user_profile(state):
    name = state.get("user_profile", "medio")
    return USER_PROFILES.get(name, USER_PROFILES["medio"]), name


def get_params(regime, state):
    up, _ = get_user_profile(state)
    rp = RISK_PROFILES.get(regime, RISK_PROFILES["NEUTRAL"])
    return {
        "eur": round(up["eur_per_trade"] * rp["eur_mult"], 2),
        "max_pos": max(1, up["max_open_positions"] + rp["pos_add"]),
        "tp_pct": round(up["take_profit_pct"] * rp["tp_mult"], 1),
        "sl_pct": round(up["stop_loss_pct"] * rp["sl_mult"], 1),
        "trail_arm": up["trail_arm_pct"],
        "trail_dist": up["trail_distance_pct"],
        "pump_candle_min": up["pump_candle_min_pct"],
        "pump_vol_surge": up["pump_volume_surge"],
        "pump_rsi_max": up["pump_rsi_max"],
        "max_loss": up["max_total_loss_eur"],
        "min_ticker_chg": up["min_ticker_change_pct"],
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
                f"✅ Profilo cambiato: {up['label']}\n"
                f"{up['desc']}\n"
                f"TP: +{up['take_profit_pct']}% | SL: -{up['stop_loss_pct']}% | "
                f"Max pos: {up['max_open_positions']} | Max loss: {up['max_total_loss_eur']}€\n"
                f"(il regime di mercato scala questi valori automaticamente)",
                all_buttons(state),
            )

    elif text in ("profilo", "profile"):
        _, current = get_user_profile(state)
        lines = [f"Profilo attuale: <b>{USER_PROFILES[current]['label']}</b>\n"]
        for name, p in USER_PROFILES.items():
            marker = " ← attivo" if name == current else ""
            lines.append(
                f"{p['label']}: TP +{p['take_profit_pct']}%, SL -{p['stop_loss_pct']}%, "
                f"{p['max_open_positions']} pos, loss max {p['max_total_loss_eur']}€{marker}"
            )
        telegram_send("\n".join(lines), profile_buttons() + control_buttons())


def send_status(state):
    pos = state.get("open_positions", {})
    regime = state.get("current_regime", "NEUTRAL")
    up, pname = get_user_profile(state)
    fgi = state.get("last_fgi")
    lines = [
        f"{'⏸ PAUSA' if state.get('trading_paused') else '▶️ Attivo'} | {REGIME_LABEL.get(regime, regime)}",
        f"Profilo: {up['label']} | F&G: {fgi if fgi is not None else '?'}",
        f"P&L: {state.get('cumulative_pnl_eur', 0.0):+.2f}€ / max loss: -{up['max_total_loss_eur']}€",
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
        [{"text": "📊 Stato", "callback_data": "stato"},
         {"text": "🛑 Vendi tutto", "callback_data": "vendi_tutto"}],
    ]


def all_buttons(state):
    pos = state.get("open_positions", {})
    btns = []
    for base in pos:
        btns.append([{"text": f"Vendi {base}", "callback_data": f"vendi_{base.lower()}"}])
    btns.extend(profile_buttons())
    btns.extend(control_buttons())
    return btns


# ================== CHIUSURA POSIZIONI ==================

def force_close_all(state):
    positions = state.get("open_positions", {})
    if not positions:
        telegram_send("🛑 Pausa. Nessuna posizione da chiudere.", all_buttons(state))
        return
    telegram_send(f"🛑 Chiusura {len(positions)} posizioni...")
    for base in list(positions.keys()):
        force_close_one(state, base)
    telegram_send("✅ Tutto chiuso. /riprendi per riattivare.", all_buttons(state))


def force_close_one(state, base):
    positions = state.get("open_positions", {})
    pos = positions.get(base)
    if not pos:
        telegram_send(f"Nessuna posizione per {base}.")
        return
    price = pos.get("last_price", pos["entry_price"])
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
    chg = (price - entry) / entry * 100
    pnl = (price - entry) * pos["volume"]
    state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + pnl
    telegram_send(
        f"🔴 VENDUTO {base}\n{fp(entry)} → {fp(price)} ({chg:+.1f}%)\n"
        f"P&L: {pnl:+.2f}€ (cum: {state['cumulative_pnl_eur']:+.2f}€){order_note}",
        all_buttons(state),
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
        "telegram_update_offset": 0, "current_regime": "NEUTRAL",
        "last_fgi": None, "user_profile": "medio",
    }


def save_state(state):
    with open(CONFIG["STATE_FILE"], "w") as f:
        json.dump(state, f, indent=2)


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


# ================== SCAN ==================

def scan_pumping(all_pairs, tickers, params):
    pumping = []
    min_chg = params["min_ticker_chg"]
    for pair_name, info in all_pairs.items():
        tick = tickers.get(pair_name)
        if not tick:
            continue
        last_price = float(tick["c"][0])
        open_price = float(tick["o"])
        if open_price <= 0:
            continue
        vol_eur = float(tick["v"][1]) * last_price
        if vol_eur < CONFIG["MIN_24H_VOLUME_EUR"]:
            continue
        chg = (last_price - open_price) / open_price * 100
        if chg < min_chg:
            continue
        pumping.append({
            "pair": pair_name, "base": info["base"],
            "ordermin": info["ordermin"], "lot_decimals": info["lot_decimals"],
            "last_price": last_price, "change_today_pct": chg, "vol_eur": vol_eur,
        })
    pumping.sort(key=lambda x: x["change_today_pct"], reverse=True)
    return pumping


# ================== SELL ==================

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

        tp = pos.get("tp_pct", params["tp_pct"])
        sl = pos.get("sl_pct", params["sl_pct"])
        t_arm = pos.get("trail_arm", params["trail_arm"])
        t_dist = pos.get("trail_dist", params["trail_dist"])

        reason = None
        if chg <= -sl:
            reason = f"Stop loss ({chg:+.1f}%)"
        elif chg >= tp:
            reason = f"Take profit ({chg:+.1f}%)"
        elif (peak - entry) / entry * 100 >= t_arm:
            drop = (peak - price) / peak * 100
            if drop >= t_dist:
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
            f"🔴 <b>VENDI {base}</b>\n{reason}\n"
            f"{fp(entry)} → {fp(price)} ({chg:+.1f}%)\n"
            f"P&L: {pnl:+.2f}€ | Cum: {state['cumulative_pnl_eur']:+.2f}€\n"
            f"{mode_label()}{order_note}",
            all_buttons(state),
        )
        del positions[base]

        if state["cumulative_pnl_eur"] <= -params["max_loss"]:
            state["trading_paused"] = True
            telegram_send(
                f"🛑 KILL-SWITCH: {state['cumulative_pnl_eur']:.2f}€ oltre -{params['max_loss']}€\n"
                f"/riprendi per riattivare.",
                all_buttons(state),
            )


# ================== BUY ==================

def check_buys(positions, pumping, state, params):
    bought = 0
    for c in pumping:
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

        if len(closes) < CONFIG["RSI_PERIOD"] + 5:
            continue

        # Pump candle (penultima — l'ultima e' in formazione)
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

        # RSI
        rsis = compute_rsi(closes, CONFIG["RSI_PERIOD"])
        if not rsis:
            continue
        rsi = rsis[-1]
        if rsi > params["pump_rsi_max"]:
            time.sleep(0.3)
            continue

        # Ordine
        price = c["last_price"]
        eur = params["eur"]
        vol = round_vol(eur / price, c["lot_decimals"])
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

        regime = state.get("current_regime", "NEUTRAL")
        up, _ = get_user_profile(state)
        tp = params["tp_pct"]
        sl = params["sl_pct"]

        telegram_send(
            f"🚀 <b>COMPRA {base}/{CONFIG['QUOTE_CURRENCY']}</b>\n"
            f"{REGIME_LABEL.get(regime, '')} | {up['label']}\n"
            f"Prezzo: {fp(price)} | {eur:.0f}€\n"
            f"Pump: +{c['change_today_pct']:.1f}% oggi | Candela: +{candle_chg:.1f}%\n"
            f"Vol: {vol_ratio:.1f}x | RSI: {rsi:.0f}\n"
            f"TP: +{tp:.1f}% | SL: -{sl:.1f}%\n"
            f"{mode_label()}{order_note}",
            [[{"text": f"🔴 Vendi {base}", "callback_data": f"vendi_{base.lower()}"}],
             *control_buttons()],
        )

        positions[base] = {
            "pair": c["pair"], "entry_price": price, "volume": vol,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "highest_price": price, "last_price": price,
            "tp_pct": tp, "sl_pct": sl,
            "trail_arm": params["trail_arm"], "trail_dist": params["trail_dist"],
            "strategy": "pump",
        }
        bought += 1
        time.sleep(0.3)
    return bought


# ================== MAIN ==================

def run():
    state = load_state()
    positions = state.get("open_positions", {})

    print("Telegram...")
    check_telegram_commands(state)

    regime, fgi, btc = detect_regime()
    params = get_params(regime, state)
    state["current_regime"] = regime
    state["last_fgi"] = fgi

    up, pname = get_user_profile(state)
    print(f"Profilo: {up['label']} | Regime: {REGIME_LABEL.get(regime)}")
    print(f"Params: {params['eur']}€, max {params['max_pos']} pos, TP +{params['tp_pct']}%, SL -{params['sl_pct']}%")
    print(f"Mode: {mode_label()}")

    all_pairs = get_all_eur_pairs()
    print(f"{len(all_pairs)} coppie EUR")

    tickers = get_ticker_batch(all_pairs.keys())
    print(f"Ticker: {len(tickers)} coppie")

    pumping = scan_pumping(all_pairs, tickers, params)
    print(f"{len(pumping)} pump attivi (>{params['min_ticker_chg']}%)")
    if pumping:
        top = ", ".join(f"{p['base']}(+{p['change_today_pct']:.0f}%)" for p in pumping[:5])
        print(f"  Top: {top}")

    check_sells(positions, tickers, state, params)

    if state.get("trading_paused"):
        print("In pausa.")
    elif len(positions) < params["max_pos"]:
        n = check_buys(positions, pumping, state, params)
        print(f"Aperte {n} posizioni" if n else "Nessun entry")

    # Heartbeat
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_heartbeat_date") != today:
        btc_arr = "↑" if btc else "↓" if btc is False else "?"
        pos_lines = ""
        if positions:
            pos_lines = "\n" + "\n".join(
                f"• {b}: {((p.get('last_price',p['entry_price'])-p['entry_price'])/p['entry_price']*100):+.1f}%"
                for b, p in positions.items()
            )
        telegram_send(
            f"✅ <b>Bot v4.1 ({mode_label()})</b>\n"
            f"{REGIME_LABEL.get(regime, regime)} | {up['label']}\n"
            f"F&G: {fgi if fgi is not None else '?'} | BTC: {btc_arr}\n"
            f"{params['eur']:.0f}€/trade | max {params['max_pos']} pos | "
            f"TP +{params['tp_pct']:.0f}% SL -{params['sl_pct']:.0f}%\n"
            f"Pump: {len(pumping)} | Pos: {len(positions)} | "
            f"P&L: {state.get('cumulative_pnl_eur', 0.0):+.2f}€{pos_lines}"
            + ("\n⚠️ PAUSA" if state.get("trading_paused") else ""),
            all_buttons(state),
        )
        state["last_heartbeat_date"] = today

    state["open_positions"] = positions
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    run()
