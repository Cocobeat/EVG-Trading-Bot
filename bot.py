"""
Bot di trading automatico per Kraken (segnali + esecuzione ordini reali).

Analizza monete a bassa/media capitalizzazione quotate in EUR su Kraken,
cerca segnali di ingresso/uscita, ti avvisa su Telegram e (se configurato)
PIAZZA DAVVERO gli ordini di acquisto/vendita sul tuo conto Kraken.

MODALITA':
- Se le variabili KRAKEN_API_KEY / KRAKEN_API_SECRET non sono impostate:
  il bot funziona in modalita' SOLO SEGNALE (nessun ordine, solo notifiche
  Telegram). Utile per osservare la strategia prima di rischiare soldi veri.
- Se sono impostate e CONFIG["KRAKEN_DRY_RUN"] = True:
  il bot chiede a Kraken di VALIDARE l'ordine (parametro 'validate') senza
  eseguirlo davvero. Nessun soldo si muove, ma verifichi che tutto il
  collegamento funzioni.
- Se CONFIG["KRAKEN_DRY_RUN"] = False: il bot piazza ordini di mercato VERI.
  Soldi reali si muovono. Passa a questa modalita' solo dopo aver testato
  a lungo le altre due.

STRATEGIA (v2 - con trend filter e trailing stop):
- Ingresso: RSI(14) che risale sopra 35 da sotto (rimbalzo da ipervenduto),
  SOLO se il prezzo e' sopra la media mobile a 50 periodi (TREND_SMA_PERIOD):
  evita di comprare rimbalzi in monete che sono in un trend discendente di
  fondo ("catching a falling knife").
- Uscita:
  * Stop loss dinamico: proporzionale alla volatilita' della moneta al
    momento dell'ingresso (non piu' una percentuale fissa uguale per tutte).
  * Trailing stop: una volta raggiunto un profitto minimo (TRAIL_ARM_PROFIT_PCT),
    il bot smette di avere un target fisso e lascia correre il prezzo,
    vendendo solo se scende di una certa percentuale dal massimo toccato.
    Questo evita di tagliare i trend forti troppo presto.
  * RSI in ipercomprato (>= 70) come uscita di sicurezza aggiuntiva.

SICUREZZA:
- La chiave API Kraken deve avere SOLO i permessi "Query Funds" e
  "Create & Modify Orders". MAI il permesso "Withdraw Funds".
- Kill-switch: se la perdita cumulata (stimata) supera
  CONFIG["MAX_TOTAL_LOSS_EUR"], il bot si mette in pausa automaticamente
  e smette di aprire nuove posizioni finche' non lo riattivi tu a mano
  (vedi GUIDA.md).

Rischio: le monete a bassa capitalizzazione sono molto volatili, possono avere
scarsa liquidita' su Kraken (spread ampi, slippage) e la strategia qui sotto e'
un esempio didattico, NON una garanzia di profitto. Investi solo cio' che sei
disposto a perdere.
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

# ================== CONFIGURAZIONE (modificabile) ==================
CONFIG = {
    "QUOTE_CURRENCY": "EUR",
    "TIMEFRAME_MINUTES": 60,        # candele orarie
    "LOOKBACK_CANDLES": 90,         # candele scaricate (deve coprire almeno TREND_SMA_PERIOD)
    "RSI_PERIOD": 14,
    "RSI_BUY_THRESHOLD": 35.0,      # segnale di acquisto: RSI risale sopra questa soglia
    "RSI_SELL_THRESHOLD": 70.0,     # uscita di sicurezza per ipercomprato

    # ---- Filtro di trend (evita di comprare in un downtrend di fondo) ----
    "TREND_SMA_PERIOD": 50,         # media mobile (candele orarie) usata come filtro di trend
    "TREND_FILTER_ENABLED": True,   # compra solo se il prezzo e' sopra questa media mobile

    # ---- Uscita: stop loss dinamico + trailing stop (invece di TP/SL fissi) ----
    "STOP_LOSS_ATR_MULT": 1.5,      # stop loss = volatilita' della moneta * questo moltiplicatore
    "MIN_STOP_LOSS_PCT": 3.0,       # ma non scendere mai sotto questa percentuale...
    "MAX_STOP_LOSS_PCT": 10.0,      # ...ne' salire mai sopra questa
    "TRAIL_ARM_PROFIT_PCT": 3.0,    # il trailing stop si attiva solo dopo questo guadagno minimo
    "TRAIL_ATR_MULT": 2.0,          # distanza del trailing stop dal massimo = volatilita' * moltiplicatore
    "MIN_TRAIL_PCT": 4.0,
    "MAX_TRAIL_PCT": 15.0,

    "MIN_MARKET_CAP_RANK": 30,      # esclude le prime 30 monete per market cap (BTC, ETH, ecc.)
    "MAX_MARKET_CAP_RANK": 250,     # esclude micro-cap troppo illiquide/rischiose
    "MAX_24H_CHANGE_PCT": 20.0,     # scarta monete gia' salite troppo nelle ultime 24h
    "MIN_VOLATILITY_PCT": 3.0,      # volatilita' minima richiesta (deviazione standard dei rendimenti, %)
    "MAX_OPEN_POSITIONS": 3,        # con 100 euro, meglio non frammentare troppo
    "MAX_PAIRS_TO_SCAN": 35,        # limite di sicurezza per tempo di esecuzione / rate limit
    "STATE_FILE": "state.json",

    # ---- Esecuzione ordini reali ----
    "EUR_PER_TRADE": 30.0,          # quanti euro investire per ogni segnale di acquisto
    "KRAKEN_DRY_RUN": False,         # True = valida l'ordine senza eseguirlo. Rimesso a True apposta:
                                     # testa la nuova strategia prima di tornare a False (vedi GUIDA.md).
    "MAX_TOTAL_LOSS_EUR": 30.0,     # kill-switch: perdita cumulata oltre la quale il bot si ferma da solo
}

# Kraken usa ticker diversi da CoinGecko per alcune monete storiche
ALIAS_KRAKEN_TO_COINGECKO = {
    "XBT": "btc",
    "XDG": "doge",
}

KRAKEN_PUBLIC_API = "https://api.kraken.com/0/public"
KRAKEN_PRIVATE_BASE = "https://api.kraken.com"
COINGECKO_API = "https://api.coingecko.com/api/v3"


# ================== TELEGRAM ==================

def telegram_send(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[ATTENZIONE] TELEGRAM_TOKEN o TELEGRAM_CHAT_ID mancanti. Messaggio non inviato:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
        if r.status_code != 200:
            print(f"[ERRORE TELEGRAM] {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[ERRORE TELEGRAM] {e}")


def check_telegram_commands(state):
    """Legge eventuali comandi mandati dall'utente al bot Telegram (/pausa, /riprendi,
    /stato, /vendi_tutto) da quando e' stato controllato l'ultima volta. Va chiamata
    all'inizio di ogni esecuzione, quindi con un ritardo massimo pari alla frequenza
    del workflow (default: 30 minuti), a meno di lanciare "Run workflow" a mano."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    offset = state.get("telegram_update_offset", 0)
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 0},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[ERRORE] lettura comandi Telegram: {e}")
        return
    if not data.get("ok"):
        return

    updates = data.get("result", [])
    max_update_id = offset - 1
    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"])
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        msg_chat_id = str(msg.get("chat", {}).get("id", ""))
        if msg_chat_id != str(chat_id):
            continue  # ignora messaggi da chat diverse dalla tua
        text = (msg.get("text") or "").strip().lower().lstrip("/")
        handle_telegram_command(text, state)

    if updates:
        state["telegram_update_offset"] = max_update_id + 1


def handle_telegram_command(text, state):
    if text in ("pausa", "stop", "ferma"):
        state["trading_paused"] = True
        telegram_send(
            "⏸ Trading in pausa su tuo comando.\n"
            "Non verranno aperte nuove posizioni. Le posizioni già aperte restano "
            "monitorate normalmente (stop loss e trailing stop continuano a funzionare).\n"
            "Scrivi /riprendi per riattivare, oppure /vendi_tutto per chiudere subito tutto."
        )
    elif text in ("riprendi", "resume", "riattiva"):
        state["trading_paused"] = False
        telegram_send("▶️ Trading riattivato: il bot torna a cercare nuovi segnali di acquisto.")
    elif text in ("stato", "status"):
        positions = state.get("open_positions", {})
        lines = [f"Stato: {'⏸ IN PAUSA' if state.get('trading_paused') else '▶️ attivo'}"]
        lines.append(f"P&L cumulato stimato: {state.get('cumulative_pnl_eur', 0.0):+.2f} EUR")
        if positions:
            lines.append(f"Posizioni aperte ({len(positions)}):")
            for base, pos in positions.items():
                lines.append(
                    f"- {base}: entrata {format_price(pos['entry_price'])}, "
                    f"massimo {format_price(pos.get('highest_price', pos['entry_price']))}"
                )
        else:
            lines.append("Nessuna posizione aperta.")
        telegram_send("\n".join(lines))
    elif text in ("vendi_tutto", "venditutto", "panic", "emergenza"):
        state["trading_paused"] = True
        force_close_all_positions(state)


def force_close_all_positions(state):
    open_positions = state.get("open_positions", {})
    if not open_positions:
        telegram_send("🛑 Trading messo in pausa. Non c'erano posizioni aperte da chiudere.")
        return
    telegram_send(f"🛑 Chiusura di emergenza di {len(open_positions)} posizioni in corso...")
    for base, pos in list(open_positions.items()):
        try:
            closes = get_closes(pos["pair"])
            current_price = closes[-1] if closes else pos["entry_price"]
        except Exception:
            current_price = pos["entry_price"]

        order_note = ""
        if trading_enabled():
            try:
                result = place_market_order(pos["pair"], "sell", pos["volume"])
                txid = result.get("txid", ["(validato, nessun txid)"])[0]
                order_note = f"\nOrdine: {txid} ({mode_label()})"
            except Exception as e:
                telegram_send(f"⚠️ ERRORE chiudendo {base}: {e}. Verifica manualmente su Kraken.")
                continue

        entry_price = pos["entry_price"]
        change_pct = (current_price - entry_price) / entry_price * 100
        pnl_eur = (current_price - entry_price) * pos["volume"]
        state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + pnl_eur

        telegram_send(
            f"\U0001F534 VENDI (emergenza) {base}/{CONFIG['QUOTE_CURRENCY']}\n"
            f"Prezzo vendita (stimato): {format_price(current_price)}\n"
            f"Variazione: {change_pct:+.2f}%\n"
            f"P&L stimato: {pnl_eur:+.2f} EUR"
            f"{order_note}"
        )
        del open_positions[base]

    state["open_positions"] = open_positions
    telegram_send("✅ Chiusura di emergenza completata. Trading in pausa (scrivi /riprendi per riattivare).")


# ================== STATO (persistito in state.json) ==================

def load_state():
    if os.path.exists(CONFIG["STATE_FILE"]):
        with open(CONFIG["STATE_FILE"], "r") as f:
            return json.load(f)
    return {
        "open_positions": {},
        "last_heartbeat_date": None,
        "cumulative_pnl_eur": 0.0,
        "trading_paused": False,
        "telegram_update_offset": 0,
    }


def save_state(state):
    with open(CONFIG["STATE_FILE"], "w") as f:
        json.dump(state, f, indent=2)


# ================== KRAKEN - DATI PUBBLICI ==================

def get_kraken_eur_pairs():
    """Ritorna { 'ADAEUR': {'base','wsname','ordermin','lot_decimals'}, ... } solo per le coppie in EUR."""
    r = requests.get(f"{KRAKEN_PUBLIC_API}/AssetPairs", timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Errore Kraken AssetPairs: {data['error']}")
    pairs = {}
    for name, info in data["result"].items():
        wsname = info.get("wsname", "") or ""
        if wsname.endswith(f"/{CONFIG['QUOTE_CURRENCY']}"):
            base = wsname.split("/")[0]
            pairs[name] = {
                "base": base,
                "wsname": wsname,
                "ordermin": float(info.get("ordermin", 0) or 0),
                "lot_decimals": int(info.get("lot_decimals", 8)),
            }
    return pairs


def get_coingecko_market_data():
    """Ritorna { 'ada': {'rank': 9, 'change_24h': 2.3}, ... } per le prime 250 monete per market cap."""
    r = requests.get(
        f"{COINGECKO_API}/coins/markets",
        params={
            "vs_currency": CONFIG["QUOTE_CURRENCY"].lower(),
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
            "price_change_percentage": "24h",
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    out = {}
    for coin in data:
        symbol = (coin.get("symbol") or "").lower()
        out[symbol] = {
            "rank": coin.get("market_cap_rank"),
            "change_24h": coin.get("price_change_percentage_24h"),
        }
    return out


def get_closes(pair_name):
    """Scarica le candele OHLC da Kraken. Ritorna i prezzi di chiusura dal piu' vecchio al piu' recente."""
    r = requests.get(
        f"{KRAKEN_PUBLIC_API}/OHLC",
        params={"pair": pair_name, "interval": CONFIG["TIMEFRAME_MINUTES"]},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Errore Kraken OHLC {pair_name}: {data['error']}")
    result = data["result"]
    key = [k for k in result.keys() if k != "last"][0]
    candles = result[key]
    closes = [float(c[4]) for c in candles]
    return closes[-CONFIG["LOOKBACK_CANDLES"]:]


# ================== KRAKEN - API PRIVATA (esecuzione ordini) ==================

def trading_enabled():
    return bool(os.environ.get("KRAKEN_API_KEY")) and bool(os.environ.get("KRAKEN_API_SECRET"))


def _kraken_signature(urlpath, data, secret):
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def kraken_private_request(uri_path, data):
    api_key = os.environ.get("KRAKEN_API_KEY")
    api_secret = os.environ.get("KRAKEN_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("KRAKEN_API_KEY / KRAKEN_API_SECRET mancanti")
    payload = dict(data)
    payload["nonce"] = str(int(time.time() * 1000))
    headers = {
        "API-Key": api_key,
        "API-Sign": _kraken_signature(uri_path, payload, api_secret),
    }
    r = requests.post(KRAKEN_PRIVATE_BASE + uri_path, headers=headers, data=payload, timeout=20)
    r.raise_for_status()
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"Errore Kraken privato {uri_path}: {result['error']}")
    return result["result"]


def round_volume(volume, lot_decimals):
    factor = 10 ** lot_decimals
    return math.floor(volume * factor) / factor


def place_market_order(pair_name, side, volume):
    """side = 'buy' o 'sell'. Rispetta CONFIG['KRAKEN_DRY_RUN'] (validate=True/False)."""
    data = {
        "pair": pair_name,
        "type": side,
        "ordertype": "market",
        "volume": f"{volume}",
        "validate": "true" if CONFIG["KRAKEN_DRY_RUN"] else "false",
    }
    return kraken_private_request("/0/private/AddOrder", data)


# ================== INDICATORI ==================

def compute_rsi(closes, period):
    """RSI (Wilder). Ritorna una lista di valori RSI allineata alle candele (dalla piu' vecchia alla piu' recente)."""
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    rsis = []
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis.append(100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        rsis.append(rsi)
    return rsis


def compute_volatility_pct(closes):
    """Deviazione standard dei rendimenti percentuali, come proxy semplice di volatilita'."""
    if len(closes) < 2:
        return 0.0
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        for i in range(1, len(closes))
        if closes[i - 1] != 0
    ]
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((x - mean) ** 2 for x in returns) / len(returns)
    return math.sqrt(variance)


def compute_sma(closes, period):
    """Media mobile semplice sugli ultimi 'period' valori di chiusura."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def clamp(value, min_value, max_value):
    return max(min_value, min(value, max_value))


def build_candidate_universe(kraken_pairs, market_data):
    candidates = []
    for pair_name, info in kraken_pairs.items():
        base = info["base"]
        cg_symbol = ALIAS_KRAKEN_TO_COINGECKO.get(base, base.lower())
        md = market_data.get(cg_symbol)
        if not md or md.get("rank") is None:
            continue
        if not (CONFIG["MIN_MARKET_CAP_RANK"] <= md["rank"] <= CONFIG["MAX_MARKET_CAP_RANK"]):
            continue
        change_24h = md.get("change_24h")
        if change_24h is not None and change_24h > CONFIG["MAX_24H_CHANGE_PCT"]:
            continue
        candidates.append({
            "pair": pair_name,
            "base": base,
            "rank": md["rank"],
            "change_24h": change_24h,
            "ordermin": info["ordermin"],
            "lot_decimals": info["lot_decimals"],
        })
    candidates.sort(key=lambda c: c["rank"])
    return candidates[: CONFIG["MAX_PAIRS_TO_SCAN"]]


def format_price(p):
    return f"{p:.6f}".rstrip("0").rstrip(".")


def mode_label():
    if not trading_enabled():
        return "SOLO SEGNALE (nessuna chiave Kraken configurata)"
    return "DRY-RUN (nessun ordine reale)" if CONFIG["KRAKEN_DRY_RUN"] else "LIVE (ordini reali!)"


# ================== LOGICA DI VENDITA ==================

def check_sell_signals(open_positions, state):
    for base, pos in list(open_positions.items()):
        pair_name = pos["pair"]
        try:
            closes = get_closes(pair_name)
        except Exception as e:
            print(f"[ERRORE] {pair_name}: {e}")
            continue
        if not closes:
            continue

        current_price = closes[-1]
        entry_price = pos["entry_price"]
        change_pct = (current_price - entry_price) / entry_price * 100

        # aggiorna il massimo storico raggiunto da quando e' aperta la posizione
        pos["highest_price"] = max(pos.get("highest_price", entry_price), current_price)
        highest_price = pos["highest_price"]

        entry_volatility = pos.get("volatility_pct", CONFIG["MIN_VOLATILITY_PCT"])
        stop_loss_price = pos.get(
            "stop_loss_price",
            entry_price * (1 - CONFIG["MIN_STOP_LOSS_PCT"] / 100),
        )

        rsis = compute_rsi(closes, CONFIG["RSI_PERIOD"])
        current_rsi = rsis[-1] if rsis else None

        reason = None

        if current_price <= stop_loss_price:
            reason = f"Stop loss dinamico raggiunto ({change_pct:+.2f}%)"
        else:
            profit_from_entry_pct = (highest_price - entry_price) / entry_price * 100
            if profit_from_entry_pct >= CONFIG["TRAIL_ARM_PROFIT_PCT"]:
                trail_pct = clamp(
                    entry_volatility * CONFIG["TRAIL_ATR_MULT"],
                    CONFIG["MIN_TRAIL_PCT"],
                    CONFIG["MAX_TRAIL_PCT"],
                )
                trailing_stop_price = highest_price * (1 - trail_pct / 100)
                if current_price <= trailing_stop_price:
                    reason = (
                        f"Trailing stop attivato (picco {format_price(highest_price)}, "
                        f"{change_pct:+.2f}% dall'ingresso)"
                    )
            if reason is None and current_rsi is not None and current_rsi >= CONFIG["RSI_SELL_THRESHOLD"]:
                reason = f"RSI in ipercomprato ({current_rsi:.1f})"

        if not reason:
            time.sleep(1)
            continue

        order_note = ""
        if trading_enabled():
            try:
                result = place_market_order(pair_name, "sell", pos["volume"])
                txid = result.get("txid", ["(validato, nessun txid)"])[0]
                order_note = f"\nOrdine: {txid} ({mode_label()})"
            except Exception as e:
                telegram_send(
                    f"⚠️ ERRORE nell'ordine di VENDITA per {base}: {e}\n"
                    f"La posizione resta aperta nello stato: verificala manualmente su Kraken."
                )
                time.sleep(1)
                continue

        pnl_eur = (current_price - entry_price) * pos["volume"]
        state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + pnl_eur

        msg = (
            f"\U0001F534 VENDI {base}/{CONFIG['QUOTE_CURRENCY']}\n"
            f"Motivo: {reason}\n"
            f"Prezzo acquisto: {format_price(entry_price)}\n"
            f"Prezzo vendita (stimato): {format_price(current_price)}\n"
            f"Massimo toccato: {format_price(highest_price)}\n"
            f"Variazione: {change_pct:+.2f}%\n"
            f"P&L stimato: {pnl_eur:+.2f} EUR"
            f"{order_note}"
        )
        telegram_send(msg)
        del open_positions[base]

        if state["cumulative_pnl_eur"] <= -CONFIG["MAX_TOTAL_LOSS_EUR"] and not state.get("trading_paused"):
            state["trading_paused"] = True
            telegram_send(
                f"\U0001F6D1 KILL-SWITCH ATTIVATO: perdita cumulata stimata "
                f"{state['cumulative_pnl_eur']:.2f} EUR ha superato il limite di "
                f"{CONFIG['MAX_TOTAL_LOSS_EUR']} EUR.\n"
                f"Il bot NON aprira' nuove posizioni finche' non imposti manualmente "
                f"\"trading_paused\": false in state.json su GitHub."
            )
        time.sleep(1)


# ================== LOGICA DI ACQUISTO ==================

def check_buy_signals(open_positions, candidates, state):
    for c in candidates:
        if len(open_positions) >= CONFIG["MAX_OPEN_POSITIONS"]:
            break
        base = c["base"]
        if base in open_positions:
            continue
        try:
            closes = get_closes(c["pair"])
        except Exception as e:
            print(f"[ERRORE] {c['pair']}: {e}")
            continue

        min_len_needed = max(CONFIG["RSI_PERIOD"] + 5, CONFIG["TREND_SMA_PERIOD"])
        if len(closes) < min_len_needed:
            continue

        volatility = compute_volatility_pct(closes)
        if volatility < CONFIG["MIN_VOLATILITY_PCT"]:
            continue

        current_price = closes[-1]

        if CONFIG["TREND_FILTER_ENABLED"]:
            sma = compute_sma(closes, CONFIG["TREND_SMA_PERIOD"])
            if sma is None or current_price < sma:
                # la moneta e' sotto la sua media mobile di fondo: probabile downtrend
                # strutturale, salta per evitare di "comprare un coltello che cade"
                time.sleep(1)
                continue

        rsis = compute_rsi(closes, CONFIG["RSI_PERIOD"])
        if not rsis or len(rsis) < 2:
            continue
        prev_rsi, curr_rsi = rsis[-2], rsis[-1]

        if not (prev_rsi < CONFIG["RSI_BUY_THRESHOLD"] <= curr_rsi):
            time.sleep(1)
            continue

        raw_volume = CONFIG["EUR_PER_TRADE"] / current_price
        volume = round_volume(raw_volume, c["lot_decimals"])

        if volume <= 0 or volume < c["ordermin"]:
            print(
                f"[SKIP] {base}: volume {volume} sotto il minimo Kraken "
                f"({c['ordermin']}) per {CONFIG['EUR_PER_TRADE']} EUR allocati."
            )
            time.sleep(1)
            continue

        order_note = ""
        if trading_enabled():
            try:
                result = place_market_order(c["pair"], "buy", volume)
                txid = result.get("txid", ["(validato, nessun txid)"])[0]
                order_note = f"\nOrdine: {txid} ({mode_label()})"
            except Exception as e:
                telegram_send(f"⚠️ ERRORE nell'ordine di ACQUISTO per {base}: {e}")
                time.sleep(1)
                continue

        stop_loss_pct = clamp(
            volatility * CONFIG["STOP_LOSS_ATR_MULT"],
            CONFIG["MIN_STOP_LOSS_PCT"],
            CONFIG["MAX_STOP_LOSS_PCT"],
        )
        stop_loss_price = current_price * (1 - stop_loss_pct / 100)

        change_line = (
            f"Variazione 24h: {c['change_24h']:+.2f}%\n" if c["change_24h"] is not None else ""
        )
        msg = (
            f"\U0001F7E2 COMPRA {base}/{CONFIG['QUOTE_CURRENCY']}\n"
            f"Prezzo attuale: {format_price(current_price)}\n"
            f"Volume: {volume} {base} (~{CONFIG['EUR_PER_TRADE']:.0f} EUR)\n"
            f"RSI: {curr_rsi:.1f} (risalito da {prev_rsi:.1f})\n"
            f"Volatilita' recente: {volatility:.1f}%\n"
            f"Stop loss dinamico: {format_price(stop_loss_price)} (-{stop_loss_pct:.1f}%)\n"
            f"Rank market cap: #{c['rank']}\n"
            f"{change_line}"
            f"Modalita': {mode_label()}"
            f"{order_note}"
        )
        telegram_send(msg)
        open_positions[base] = {
            "pair": c["pair"],
            "entry_price": current_price,
            "volume": volume,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "volatility_pct": volatility,
            "highest_price": current_price,
            "stop_loss_price": stop_loss_price,
        }
        time.sleep(1)


# ================== CICLO PRINCIPALE ==================

def run():
    state = load_state()
    open_positions = state.get("open_positions", {})

    print("Controllo comandi Telegram (/pausa, /riprendi, /stato, /vendi_tutto)...")
    check_telegram_commands(state)

    print(f"Modalita' attuale: {mode_label()}")
    print("Recupero elenco coppie Kraken in EUR...")
    kraken_pairs = get_kraken_eur_pairs()
    print(f"Trovate {len(kraken_pairs)} coppie {CONFIG['QUOTE_CURRENCY']} su Kraken.")

    print("Recupero dati di mercato da CoinGecko...")
    market_data = get_coingecko_market_data()

    candidates = build_candidate_universe(kraken_pairs, market_data)
    print(f"{len(candidates)} candidati dopo i filtri di rank/pump.")

    check_sell_signals(open_positions, state)

    if state.get("trading_paused"):
        print("Trading in pausa (kill-switch attivo): nessuna nuova posizione verra' aperta.")
    elif len(open_positions) < CONFIG["MAX_OPEN_POSITIONS"]:
        check_buy_signals(open_positions, candidates, state)

    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_heartbeat_date") != today:
        telegram_send(
            f"✅ Bot attivo ({mode_label()}) — {len(open_positions)} posizioni aperte, "
            f"{len(candidates)} candidati monitorati oggi, "
            f"P&L cumulato stimato: {state.get('cumulative_pnl_eur', 0.0):+.2f} EUR."
            + (" IN PAUSA (kill-switch)." if state.get("trading_paused") else "")
        )
        state["last_heartbeat_date"] = today

    state["open_positions"] = open_positions
    save_state(state)
    print("Esecuzione completata.")


if __name__ == "__main__":
    run()
