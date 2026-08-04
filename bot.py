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
import re
import time
import traceback
import urllib.parse
from datetime import datetime, timezone

import requests

# ================== PROFILI UTENTE ==================

USER_PROFILES = {
    "sicuro": {
        "label": "\U0001F6E1 SICURO",
        "eur_pct_per_trade": 15.0,
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
        "min_volume_eur": 5000,
        "desc": "Pochi trade, protezione capitale",
    },
    "medio": {
        "label": "⚖️ MEDIO",
        "eur_pct_per_trade": 25.0,
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
        "min_volume_eur": 3000,
        "desc": "Bilanciato rischio/rendimento",
    },
    "aggressivo": {
        "label": "\U0001F525 AGGRESSIVO",
        "eur_pct_per_trade": 30.0,
        "max_open_positions": 4,
        # TP molto alto: e' un backstop di sicurezza, non un tetto reale.
        # Decide il trailing a fasce (col pavimento breakeven) fin dove far
        # correre il guadagno.
        "take_profit_pct": 300.0,
        "stop_loss_pct": 12.0,
        "trail_arm_pct": 4.0,
        "trail_distance_pct": 3.0,
        # Trailing a fasce: (guadagno dal picco raggiunto, distanza trailing).
        # Sotto +8% resta stretto (3%) come prima, cosi' i guadagni piccoli
        # vengono comunque protetti. Sopra, si allarga per lasciar respirare
        # i pump veri, che ritracciano parecchio mentre salgono.
        "trail_tiers": [[0, 3.0], [8, 8.0], [20, 12.0]],
        "pump_candle_min_pct": 0.6,
        "pump_volume_surge": 1.1,
        "pump_rsi_max": 96,
        "max_total_loss_eur": 80.0,
        "min_ticker_change_pct": 0.5,
        "min_hold_minutes": 10,
        "max_spread_pct": 5.0,
        "min_volume_eur": 1200,
        "desc": "Entra su tutto, massima esposizione",
    },
}

# ================== CONFIG ==================

CONFIG = {
    "QUOTE_CURRENCY": "EUR",
    "SCAN_TIMEFRAME": 5,
    "SCAN_LOOKBACK": 60,
    "MAX_DAILY_PUMP_PCT": 50.0,
    "RSI_PERIOD": 14,
    "COOLDOWN_MINUTES": 60,
    "MAX_BUYS_PER_RUN": 1,
    "MAX_OHLC_CHECKS_PER_RUN": 60,
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

    # Conferma di trend a 1h: filtra i pump che avvengono dentro un trend
    # orario ancora chiaramente ribassista (rimbalzi morti). Se la coin ha
    # meno storia oraria di TREND_CONFIRM_MIN_CANDLES (listing recente), il
    # filtro non si applica: non vogliamo perdere i listing nuovi, spesso i
    # pump migliori per questo bot.
    "TREND_CONFIRM_INTERVAL": 60,
    "TREND_CONFIRM_SMA_PERIOD": 10,
    "TREND_CONFIRM_MIN_CANDLES": 12,

    # Correlazione tra posizioni aperte: Kraken non espone una "categoria"
    # via API pubblica (quella dello screenshot e' solo nel sito), quindi
    # usiamo una proxy piu' solida e senza dipendenze esterne: la
    # correlazione dei rendimenti a 5min tra il candidato e ogni posizione
    # gia' aperta. Se si muovono troppo insieme, e' rischio concentrato
    # anche se sono coin "diverse".
    "MAX_POSITION_CORRELATION": 0.75,
    "CORRELATION_LOOKBACK": 30,

    # Pavimento del trailing: una volta armato (guadagno >= trail_arm), lo
    # stop non scende mai sotto entry + questo margine. Copre le fee di
    # andata e ritorno su Kraken (taker ~0.4-0.5% a tratta, quindi fino a
    # ~1%+ di round trip nella fascia di volume tipica del bot, piu' un
    # cuscinetto per lo slippage sull'esecuzione dello stop). Un margine
    # troppo stretto qui non e' un pavimento reale: la posizione puo'
    # comunque chiudersi in perdita netta a fee pagate anche se il prezzo
    # sta sopra questa soglia.
    "BREAKEVEN_BUFFER_PCT": 1.5,

    # Stop "catastrofico" piazzato sul server Kraken durante la finestra di
    # min_hold_minutes, al posto dello stop reale (sl_price). La logica
    # locale ignora deliberatamente lo SL prima di min_hold (per assorbire
    # rumore/dip temporanei sul prezzo di ingresso), ma senza questo lo
    # stop *reale* sul server resterebbe comunque piazzato stretto a
    # sl_price fin dal minuto zero e potrebbe eseguire la vendita da solo
    # su Kraken proprio durante la finestra che min_hold dovrebbe
    # proteggere. Lo stop di grazia e' piu' largo (sl_pct moltiplicato,
    # con un tetto) cosi' assorbe lo stesso rumore ma resta comunque una
    # vera rete di sicurezza contro un crollo reale (flash crash/rug pull)
    # durante l'attesa.
    "HOLD_GRACE_SL_WIDEN_MULT": 2.0,
    "HOLD_GRACE_SL_CAP_PCT": 35.0,
}


def catastrophic_sl_pct(sl_pct):
    """Distanza percentuale dello stop di grazia (vedi HOLD_GRACE_SL_*)."""
    return min(CONFIG["HOLD_GRACE_SL_CAP_PCT"], sl_pct * CONFIG["HOLD_GRACE_SL_WIDEN_MULT"])


# rsi_add: quanto il tetto RSI si sposta rispetto alla base del profilo
# utente, in base al regime di mercato. In bull i pump restano ipercomprati
# a lungo mentre il trend continua (RSI alto e' normale, non un segnale di
# inversione) quindi allarghiamo il tetto per non tagliare fuori i
# continuation pump. In bear/paura estrema i pump ipercomprati sono piu'
# spesso rimbalzi morti/dead cat bounce che si sgonfiano in fretta, quindi
# stringiamo il tetto per essere piu' selettivi sull'ingresso.
RISK_PROFILES = {
    "BULL_AGGRESSIVE": {"eur_mult": 1.3, "pos_add": 2, "tp_mult": 1.2, "sl_mult": 1.3, "rsi_add": 3},
    "BULL_MODERATE":   {"eur_mult": 1.15, "pos_add": 1, "tp_mult": 1.1, "sl_mult": 1.1, "rsi_add": 1},
    "NEUTRAL":         {"eur_mult": 1.0, "pos_add": 0, "tp_mult": 1.0, "sl_mult": 1.0, "rsi_add": 0},
    "BEAR_DEFENSIVE":  {"eur_mult": 0.7, "pos_add": -1, "tp_mult": 0.8, "sl_mult": 0.85, "rsi_add": -8},
    "EXTREME_FEAR":    {"eur_mult": 0.5, "pos_add": -1, "tp_mult": 0.7, "sl_mult": 0.75, "rsi_add": -15},
}
RSI_MAX_FLOOR = 50
RSI_MAX_CEILING = 99

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
    base = USER_PROFILES.get(name, USER_PROFILES["medio"])
    overrides = state.get("profile_overrides", {}).get(name)
    if overrides:
        merged = dict(base)
        merged.update(overrides)
        return merged, name
    return base, name


def get_params(regime, state):
    up, _ = get_user_profile(state)
    rp = RISK_PROFILES.get(regime, RISK_PROFILES["NEUTRAL"])
    sl_mult = max(1.0, rp["sl_mult"])
    rsi_max = up["pump_rsi_max"] + rp.get("rsi_add", 0)
    rsi_max = max(RSI_MAX_FLOOR, min(RSI_MAX_CEILING, rsi_max))
    return {
        "eur_pct": round(up["eur_pct_per_trade"] * rp["eur_mult"], 2),
        "max_pos": max(1, up["max_open_positions"] + rp["pos_add"]),
        "tp_pct": round(up["take_profit_pct"] * rp["tp_mult"], 1),
        "sl_pct": round(up["stop_loss_pct"] * sl_mult, 1),
        "trail_arm": up["trail_arm_pct"],
        "trail_dist": up["trail_distance_pct"],
        "trail_tiers": up.get("trail_tiers"),
        "pump_candle_min": up["pump_candle_min_pct"],
        "pump_vol_surge": up["pump_volume_surge"],
        "pump_rsi_max": rsi_max,
        "pump_rsi_base": up["pump_rsi_max"],
        "max_loss": up["max_total_loss_eur"],
        "min_ticker_chg": up["min_ticker_change_pct"],
        "min_hold_min": up.get("min_hold_minutes", 15),
        "max_spread": up.get("max_spread_pct", 2.0),
        "min_vol_eur": up.get("min_volume_eur", 5000),
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
        telegram_send(
            "⚠️ Confermi la vendita di TUTTE le posizioni?",
            [[{"text": "✅ Conferma vendi tutto", "callback_data": "okvenditutto"}],
             [{"text": "❌ Annulla", "callback_data": "annulla"}]],
        )

    elif text == "okvenditutto":
        state["trading_paused"] = True
        force_close_all(state)

    elif text.startswith("okvendi_"):
        force_close_one(state, text[8:].upper())

    elif text.startswith("vendi_"):
        base = text[6:].upper()
        telegram_send(
            f"⚠️ Confermi la vendita di {base}?",
            [[{"text": f"✅ Conferma vendita {base}", "callback_data": f"okvendi_{base.lower()}"}],
             [{"text": "❌ Annulla", "callback_data": "annulla"}]],
        )

    elif text == "annulla":
        telegram_send("Annullato, nessuna vendita eseguita.", all_buttons(state))

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

    elif text.startswith("rsi "):
        up, current = get_user_profile(state)
        try:
            val = float(text.split()[1].replace(",", "."))
        except (IndexError, ValueError):
            telegram_send("Uso: /rsi 90 (imposta il tetto RSI per il profilo attivo)")
        else:
            overrides = state.setdefault("profile_overrides", {})
            overrides.setdefault(current, {})["pump_rsi_max"] = val
            telegram_send(
                f"✅ Tetto RSI base per {USER_PROFILES[current]['label']} impostato a {val:.0f} "
                f"(era {USER_PROFILES[current]['pump_rsi_max']:.0f}). "
                f"Il regime di mercato lo sposta ancora in automatico "
                f"(es. bear -8, paura estrema -15, bull +1/+3) — guarda "
                f"\"RSI<\" nel log di ogni run per il valore effettivo.",
                all_buttons(state),
            )


def send_status(state):
    pos = state.get("open_positions", {})
    regime = state.get("current_regime", "NEUTRAL")
    up, _ = get_user_profile(state)
    params = get_params(regime, state)
    fgi = state.get("last_fgi")
    cooldowns = state.get("cooldowns", {})
    lines = [
        f"{'⏸ PAUSA' if state.get('trading_paused') else '▶️ Attivo'} | "
        f"{REGIME_LABEL.get(regime, regime)}",
        f"Profilo: {up['label']} | F&G: {fgi if fgi is not None else '?'}",
        f"P&L: {state.get('cumulative_pnl_eur', 0.0):+.2f}€ / "
        f"max loss: -{up['max_total_loss_eur']}€",
    ]
    ts = state.get("trade_stats", {})
    auto_n = ts.get("auto_wins", 0) + ts.get("auto_losses", 0)
    if auto_n:
        wr = ts.get("auto_wins", 0) / auto_n * 100
        lines.append(
            f"Bot: {auto_n} trade ({wr:.0f}% win) | P&L bot: {ts.get('auto_pnl', 0.0):+.2f}€"
        )
        if ts.get("manual_count"):
            lines.append(
                f"Manuali: {ts['manual_count']} ({ts.get('manual_pnl', 0.0):+.2f}€)"
            )
    btns = []
    if pos:
        lines.append(f"\n<b>Posizioni ({len(pos)}/{params['max_pos']}):</b>")
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

def trail_distance(peak_gain_pct, pos, params):
    """
    Distanza di trailing in funzione di quanto e' salita la posizione.
    Piccoli guadagni -> trailing stretto (li protegge, come prima).
    Pump grossi -> trailing largo (li lascia respirare invece di
    troncarli al primo ritracciamento fisiologico).
    Se non ci sono fasce definite, torna al comportamento a distanza fissa.
    """
    tiers = pos.get("trail_tiers") or params.get("trail_tiers")
    if not tiers:
        return pos.get("trail_dist", params["trail_dist"])
    dist = tiers[0][1]
    for gain, d in tiers:
        if peak_gain_pct >= gain:
            dist = d
    return dist


def record_trade_stat(state, pnl, manual):
    """
    Tiene traccia separata delle chiusure automatiche (TP/SL/trailing) da
    quelle manuali (bottone/comando), cosi' il winrate reale del bot non
    viene sporcato da vendite decise a mano (o click sbagliati).
    """
    stats = state.setdefault("trade_stats", {
        "auto_wins": 0, "auto_losses": 0, "auto_pnl": 0.0,
        "manual_count": 0, "manual_pnl": 0.0,
    })
    if manual:
        stats["manual_count"] += 1
        stats["manual_pnl"] += pnl
    else:
        if pnl >= 0:
            stats["auto_wins"] += 1
        else:
            stats["auto_losses"] += 1
        stats["auto_pnl"] += pnl


def check_kill_switch(state):
    """
    Va chiamata dopo OGNI chiusura di posizione, qualunque sia la strada
    (vendita normale decisa dal bot, stop reale gia' scattato sul server
    scoperto al run successivo, o chiusura manuale) — prima il controllo
    viveva solo in un punto, quindi la maggior parte delle uscite (incluse
    quelle piu' probabili ora che lo stop e' reale sul server) potevano
    superare la soglia senza che il bot si fermasse mai.
    """
    up, _ = get_user_profile(state)
    max_loss = up["max_total_loss_eur"]
    if state.get("trading_paused"):
        return
    if state.get("cumulative_pnl_eur", 0.0) <= -max_loss:
        state["trading_paused"] = True
        telegram_send(
            f"\U0001F6D1 KILL-SWITCH: {state['cumulative_pnl_eur']:.2f}€ "
            f"oltre -{max_loss}€\n/riprendi per riattivare.",
            all_buttons(state),
        )


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


_UNSET = object()  # sentinel: distingue "balances non passato" da "passato esplicitamente None"


def force_close_one(state, base, balances=_UNSET):
    positions = state.get("open_positions", {})
    pos = positions.get(base)
    if not pos:
        telegram_send(f"Nessuna posizione per {base}.")
        return
    if balances is _UNSET:
        # Chiamata diretta (es. /vendi_X da Telegram), nessun balances gia'
        # recuperato da chi chiama: lo prendiamo qui.
        balances = get_balances()
    # Se invece balances e' None perche' force_close_all l'ha gia' provato e
    # l'API ha fallito, NON riproviamo qui: rifarlo per ogni posizione
    # martellerebbe di chiamate private un'API gia' in difficolta', senza
    # ragione di aspettarsi un risultato diverso a distanza di secondi.
    price = pos.get("last_price", pos["entry_price"])
    sell_vol = pos["volume"]
    order_note = ""
    exit_fee_eur = 0.0
    if trading_enabled():
        asset_code = pos.get("asset_code")
        if balances is None:
            # Balance API in errore/timeout anche dopo il refetch: non
            # possiamo verificare quanto e' rimasto davvero. Non cancelliamo
            # la posizione alla cieca — proviamo a vendere il volume nominale
            # tracciato, come se il saldo reale coincidesse (degradante ma
            # sicuro: nel peggiore dei casi Kraken rifiuta l'ordine).
            print(f"[WARN] Saldo reale non disponibile ({base}): vendo il volume tracciato")
        elif asset_code:
            # Quantita' reale rimasta rispetto al volume comprato, non il
            # valore in EUR: un vero calo di prezzo lascia comunque il
            # volume originale sul conto. Solo se e' rimasto quasi nulla
            # della QUANTITA' la posizione e' stata chiusa altrove.
            real_bal = balances.get(asset_code, 0.0)
            if real_bal < pos["volume"] * 0.05:
                telegram_send(
                    f"⚠️ {base}: saldo reale ~0, rimuovo la posizione senza ordine "
                    f"(probabilmente gia' venduta manualmente)."
                )
                server_txid = pos.get("server_sl_txid")
                if server_txid:
                    cancel_order(server_txid)
                del positions[base]
                return
            sell_vol = min(sell_vol, real_bal)
        # Arrotonda ai decimali di volume ammessi dal pair PRIMA di
        # ordinare: real_bal viene da Kraken con precisione piena, non
        # allineata a lot_decimals (pos["volume"] lo era gia' dal buy, ma
        # min() con real_bal puo' aver introdotto piu' cifre di quante il
        # pair ne accetti) — senza, Kraken puo' rifiutare l'ordine.
        sell_vol = round_vol(sell_vol, pos.get("lot_decimals", 8))
        # Cancella lo stop reale sul server PRIMA di vendere noi: altrimenti
        # resta un ordine stop orfano su Kraken. Se poi il bot ricompra la
        # stessa coin, quello stop vecchio potrebbe vendere la posizione
        # nuova a un prezzo calcolato sulla vecchia entry.
        server_txid = pos.get("server_sl_txid")
        if server_txid:
            cancel_order(server_txid)
        try:
            result = place_order(pos["pair"], "sell", sell_vol)
            txid = result.get("txid", ["(ok)"])[0]
            order_note = f"\nOrdine: {txid} ({mode_label()})"
        except Exception as e:
            telegram_send(f"⚠️ Errore vendita {base}: {e}")
            return

        # Prezzo/volume/fee di riempimento reale, non l'ultimo prezzo noto
        # (che puo' essere del run precedente) — altrimenti il P&L
        # registrato e' inventato, sia sul prezzo che sulle fee.
        if not CONFIG["KRAKEN_DRY_RUN"]:
            time.sleep(1.5)
            fill_price, fill_vol, fill_fee = get_order_fill(txid)
            if fill_price and fill_vol:
                price = fill_price
                sell_vol = fill_vol
                exit_fee_eur = fill_fee
    entry = pos["entry_price"]
    chg = (price - entry) / entry * 100
    pnl = (price - entry) * sell_vol - pos.get("entry_fee_eur", 0.0) - exit_fee_eur
    state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + pnl
    record_trade_stat(state, pnl, manual=True)
    telegram_send(
        f"\U0001F534 VENDUTO {base}\n{fp(entry)} → {fp(price)} ({chg:+.1f}%)\n"
        f"P&L: {pnl:+.2f}€ (cum: {state['cumulative_pnl_eur']:+.2f}€){order_note}",
        all_buttons(state),
    )
    del positions[base]
    check_kill_switch(state)


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
    state.setdefault("trade_stats", {
        "auto_wins": 0, "auto_losses": 0, "auto_pnl": 0.0,
        "manual_count": 0, "manual_pnl": 0.0,
    })
    state.setdefault("profile_overrides", {})
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
        "highs": [float(c[2]) for c in candles][-n:],
        "volumes": [float(c[6]) for c in candles][-n:],
        "times": [int(c[0]) for c in candles][-n:],
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
                "pair_decimals": int(info.get("pair_decimals", 8)),
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


def place_stop_order(pair, volume, stop_price, price_decimals):
    """Piazza uno stop-loss VERO sul server Kraken (non solo calcolato dal
    bot). Protegge la posizione anche se il bot va offline (crash, run
    fallita, GitHub Actions giu') — senza questo, tra un run e l'altro
    (o durante un'interruzione) non c'e' nessuna protezione reale sul
    mercato, solo nella logica locale. Scatta a mercato quando il prezzo
    tocca stop_price: niente post-only/limit, per garantire l'esecuzione
    anche su coin illiquide dove un ordine limite potrebbe non riempirsi
    mai mentre il prezzo scappa sotto.
    """
    price_str = f"{stop_price:.{max(0, price_decimals)}f}"
    data = {
        "pair": pair, "type": "sell", "ordertype": "stop-loss",
        "price": price_str, "volume": f"{volume}",
    }
    if CONFIG["KRAKEN_DRY_RUN"]:
        data["validate"] = "true"
    return kraken_private("/0/private/AddOrder", data)


# Ancorata a "price" (non solo "up to N decimals"): Kraken usa la stessa
# frase anche per l'errore sui decimali di VOLUME ("volume can only be
# specified up to N decimals"). Senza l'ancoraggio, un errore di volume
# verrebbe letto come se fosse sui decimali di prezzo e la correzione
# verrebbe applicata al parametro sbagliato, senza risolvere il problema
# reale (e mascherando l'errore vero nel log).
_DECIMALS_ERROR_RE = re.compile(r"price can only be specified up to (\d+) decimals", re.IGNORECASE)


def place_stop_order_safe(pair, volume, stop_price, price_decimals):
    """Come place_stop_order, ma si autocorregge se i decimali sono
    sbagliati. Succede per le posizioni aperte prima che pair_decimals
    venisse letto correttamente: quel valore (sbagliato) resta scritto per
    sempre in state.json, e il codice nuovo lo rilegge comunque fidandosi.
    Invece di fallire di nuovo in eterno, se Kraken risponde con l'errore
    "price can only be specified up to N decimals" estraiamo N dal
    messaggio stesso e riproviamo subito con quello — niente piu' bisogno
    di intervento manuale, e la posizione riprende il valore corretto.
    Ritorna (risultato_ordine, decimali_effettivamente_usati).
    """
    try:
        return place_stop_order(pair, volume, stop_price, price_decimals), price_decimals
    except Exception as e:
        m = _DECIMALS_ERROR_RE.search(str(e))
        if m:
            corrected = int(m.group(1))
            if corrected != price_decimals:
                result = place_stop_order(pair, volume, stop_price, corrected)
                return result, corrected
        raise


def cancel_order(txid):
    try:
        kraken_private("/0/private/CancelOrder", {"txid": txid})
        return True
    except Exception as e:
        print(f"[WARN] CancelOrder {txid}: {e}")
        return False


def get_balances():
    """Ritorna un dict {asset_code: saldo_float}. {} se non in modalita'
    live/trading (stato legittimo: non c'e' nulla da leggere). None se la
    chiamata a Kraken e' fallita (timeout, rate limit, manutenzione, nonce
    invalido, ecc.) — stato DIVERSO da {} e va trattato diversamente da chi
    chiama: un dict vuoto usato per dire "saldi a zero" quando in realta' la
    richiesta e' fallita farebbe scambiare ogni posizione reale per dust e
    portarebbe a cancellarla dallo stato senza averla davvero venduta."""
    if not trading_enabled():
        return {}
    try:
        res = kraken_private("/0/private/Balance", {})
        return {k: float(v) for k, v in res.items()}
    except Exception as e:
        print(f"[WARN] Balance: {e}")
        return None


def get_eur_balance(balances):
    # None = saldo davvero sconosciuto (Balance API in errore). 0.0 = saldo
    # EUR confermato a zero (Kraken omette dalla risposta le valute a saldo
    # esattamente zero, stesso comportamento gia' gestito per gli asset in
    # check_sells/check_buys). Le due cose vanno distinte: se "sconosciuto"
    # tornasse None anche qui, a valle (base_eur, cap su available_eur) il
    # bot tratterebbe un conto con 0€ liberi come "capitale non verificabile"
    # e proverebbe comunque un ordine, che Kraken rifiuterebbe ad ogni run.
    if balances is None:
        return None
    for key in ("ZEUR", "EUR"):
        if key in balances:
            return balances[key]
    return 0.0


def get_order_fill(txid):
    """Interroga Kraken per prezzo medio, volume eseguito e fee pagata di un
    ordine chiuso. La fee (in EUR, valuta quote) va sottratta dal P&L
    lordo: senza, ogni trade risulta ~0.4-0.5% (per lato, ~0.8-1% andata e
    ritorno) migliore nei log/nel kill-switch di quanto arrivi davvero sul
    conto — cumulative_pnl_eur si sarebbe scollato dal saldo reale."""
    try:
        res = kraken_private("/0/private/QueryOrders", {"txid": txid})
        info = res.get(txid)
        if info:
            price = float(info.get("price", 0) or 0)
            vol_exec = float(info.get("vol_exec", 0) or 0)
            fee = float(info.get("fee", 0) or 0)
            if price > 0 and vol_exec > 0:
                return price, vol_exec, fee
    except Exception as e:
        print(f"[WARN] QueryOrders {txid}: {e}")
    return None, None, 0.0


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


def check_trend_1h(pair):
    """
    Conferma di trend a 1h: il prezzo deve stare sopra la sua media mobile
    delle ultime N ore. Se manca storia sufficiente (coin appena listata)
    il filtro non blocca (ritorna True) — meglio un falso negativo raro
    che perdere sistematicamente i listing nuovi.
    """
    try:
        ohlc = get_ohlc(pair, CONFIG["TREND_CONFIRM_INTERVAL"],
                         CONFIG["TREND_CONFIRM_SMA_PERIOD"] + 5)
    except RateLimitError:
        raise
    except Exception as e:
        print(f"[WARN] trend 1h {pair}: {e}")
        return True

    closes = ohlc["closes"]
    period = CONFIG["TREND_CONFIRM_SMA_PERIOD"]
    if len(closes) < CONFIG["TREND_CONFIRM_MIN_CANDLES"]:
        return True
    sma = sum(closes[-period:]) / period
    return closes[-1] > sma


def pct_returns(closes):
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1] > 0]


def correlation(a, b):
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    a, b = a[-n:], b[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_a <= 0 or var_b <= 0:
        return 0.0
    return cov / math.sqrt(var_a * var_b)


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
        if vol_eur < params["min_vol_eur"]:
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
            "pair_decimals": info.get("pair_decimals", 8),
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

        # Il bot legge il ticker solo agli istanti in cui gira (ogni ~1min).
        # Su coin poco liquide uno spike puo' salire e ricrollare tutto tra
        # un poll e l'altro: il trailing basato solo sul ticker non lo vede
        # mai e non protegge nulla di quel guadagno. Le candele OHLC invece
        # registrano il vero massimo (high) di ogni intervallo, indipendente
        # da quando il bot ha interrogato l'API. Guardiamo solo le candele
        # dall'apertura della posizione in poi, per non "vedere" un picco
        # di prima dell'acquisto e armare il trailing su un guadagno mai
        # avuto davvero.
        hold = minutes_held(pos)
        try:
            lookback = min(288, max(20, int(hold / CONFIG["SCAN_TIMEFRAME"]) + 6))
            ohlc = get_ohlc(pos["pair"], CONFIG["SCAN_TIMEFRAME"], lookback)
            entry_epoch = datetime.fromisoformat(pos["entry_time"]).timestamp()
            candle_highs = [
                h for t, h in zip(ohlc["times"], ohlc["highs"]) if t >= entry_epoch
            ]
            if candle_highs:
                pos["highest_price"] = max(pos["highest_price"], max(candle_highs))
        except RateLimitError as e:
            print(f"[RATE LIMIT] {e} — uso solo il ticker per {base} questo run")
        except Exception as e:
            print(f"[WARN] OHLC picco reale {base}: {e}")

        peak = pos["highest_price"]

        # Il saldo reale e' la fonte di verita': se e' gia' a zero, qualcosa
        # ha chiuso la posizione fuori dal ciclo normale del bot — quasi
        # sempre lo stop-loss reale piazzato su Kraken (protegge anche se il
        # bot era offline), a volte una vendita manuale. Va controllato ad
        # ogni run per OGNI posizione, non solo quando la logica locale
        # decide anche lei di vendere: se il prezzo nel frattempo e' tornato
        # sopra sl_price/tp_price locali, la logica sotto non se ne
        # accorgerebbe mai e la posizione resterebbe "fantasma" in
        # state.json per sempre.
        if trading_enabled() and balances is None:
            # Saldo reale non disponibile in questo run (Balance API in
            # errore/timeout/rate limit): NON possiamo distinguere "chiuso
            # fuori dal ciclo normale" da "saldo semplicemente non
            # verificabile ora". Saltiamo la verifica invece di cancellare
            # la posizione — si ricontrollera' al prossimo run.
            print(f"[WARN] Saldo reale non disponibile ({base}): salto verifica ghost-position questo run")
        elif trading_enabled():
            asset_code = pos.get("asset_code")
            # Quantita' reale rimasta rispetto al volume comprato, non il
            # valore in EUR: un vero calo di prezzo (anche -90%) lascia
            # comunque il volume originale sul conto, cambia solo il suo
            # valore. Solo se e' rimasto quasi nulla della QUANTITA' (fee
            # normali tolgono <1%) la posizione e' stata chiusa altrove
            # (stop server, vendita manuale) — non solo svalutata.
            if asset_code and balances.get(asset_code, 0.0) < pos["volume"] * 0.05:
                real_price, real_vol, real_fee = None, None, 0.0
                server_txid = pos.get("server_sl_txid")
                if server_txid:
                    real_price, real_vol, real_fee = get_order_fill(server_txid)
                if real_price and real_vol:
                    close_pnl = (real_price - entry) * real_vol - pos.get("entry_fee_eur", 0.0) - real_fee
                    state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + close_pnl
                    record_trade_stat(state, close_pnl, manual=False)
                    close_chg = (real_price - entry) / entry * 100
                    telegram_send(
                        f"\U0001F534 <b>VENDUTO {base}</b>\nStop loss server (bot offline o run saltata)\n"
                        f"{fp(entry)} → {fp(real_price)} ({close_chg:+.1f}%)\n"
                        f"P&L: {close_pnl:+.2f}€ | Cum: {state['cumulative_pnl_eur']:+.2f}€",
                        all_buttons(state),
                    )
                    if close_pnl < 0:
                        add_cooldown(state, base)
                else:
                    telegram_send(
                        f"⚠️ {base}: saldo reale 0, rimuovo la posizione senza ordine "
                        f"(probabile vendita manuale o dust)."
                    )
                # Se la chiusura non e' passata dal nostro stop (es. vendita
                # manuale sul sito Kraken), lo stop resta vivo sul server
                # senza piu' una posizione dietro — orfano, stesso rischio
                # gia' corretto in force_close_one. Se invece e' stato lui a
                # chiudere, e' gia' un ordine concluso e cancel_order() e'
                # un no-op innocuo.
                if server_txid:
                    cancel_order(server_txid)
                del positions[base]
                check_kill_switch(state)
                continue

        sl_price = pos.get("sl_price", entry * (1 - pos.get("sl_pct", params["sl_pct"]) / 100))
        tp_price = pos.get("tp_price", entry * (1 + pos.get("tp_pct", params["tp_pct"]) / 100))
        t_arm = pos.get("trail_arm", params["trail_arm"])
        peak_gain = (peak - entry) / entry * 100
        t_dist = trail_distance(peak_gain, pos, params)

        hold = minutes_held(pos)
        hold_min = pos.get("min_hold_min", params["min_hold_min"])

        reason = None
        is_loss = False

        # TP e trailing attivi sempre
        if price >= tp_price:
            reason = f"Take profit ({chg:+.1f}%)"
        elif peak_gain >= t_arm:
            # Pavimento: una volta armato il trailing, il bot punta a non
            # vendere sotto il breakeven (+ margine fee) — il pump puo'
            # rimangiarsi gran parte del guadagno, ma la logica cerca di
            # evitare di portare a casa una perdita netta dopo essere
            # stato in profitto vero. Non e' una garanzia assoluta: questo
            # controllo gira una volta a run (non in continuo), quindi un
            # crollo improvviso tra un run e l'altro puo' far eseguire la
            # vendita sotto questa soglia al prezzo reale trovato al
            # prossimo controllo, e lo stop reale sul server puo' subire
            # slippage rispetto al prezzo impostato.
            trail_stop_price = peak * (1 - t_dist / 100)
            breakeven_price = entry * (1 + CONFIG["BREAKEVEN_BUFFER_PCT"] / 100)
            effective_stop = max(trail_stop_price, breakeven_price)
            # Il picco puo' essere stato scoperto solo in QUESTO run (vedi
            # ripescaggio OHLC sopra): su uno spike intra-candela armato e
            # ricrollato prima che un run precedente potesse sincronizzare
            # lo stop server al nuovo effective_stop, questo controllo
            # locale e' l'UNICA cosa che decide la vendita, non il backstop
            # di un ordine gia' piazzato a quel livello (che semplicemente
            # non esiste ancora). Se il prezzo e' gia' sotto entry (perdita
            # vera, non solo sotto il margine fee), rispettiamo min_hold
            # come farebbe il vero stop-loss: il taglio anticipato del
            # pavimento resta per proteggere un guadagno reale, non per
            # uscire in perdita prima che min_hold sia scaduto.
            if price <= effective_stop and (price > entry or hold >= hold_min):
                if price < entry:
                    reason = f"Trailing stop (protezione tardiva, {chg:+.1f}%)"
                    is_loss = True
                elif effective_stop > trail_stop_price:
                    reason = f"Trailing stop (pavimento breakeven, {chg:+.1f}%)"
                else:
                    reason = (f"Trailing stop {t_dist:.0f}% "
                              f"(picco {fp(peak)} +{peak_gain:.0f}%, {chg:+.1f}%)")

        # SL attivo solo dopo min_hold_minutes
        if not reason and hold >= hold_min and price <= sl_price:
            reason = f"Stop loss ({chg:+.1f}%)"
            is_loss = True

        if not reason:
            # Non vendiamo questo run. Due casi in cui tocca lo stop reale
            # sul server Kraken: (1) manca del tutto (es. un piazzamento
            # fallito al momento dell'acquisto, come NPC con l'errore sui
            # decimali — senza questo ripescaggio resterebbe scoperta finche'
            # non arma il trailing, anche per giorni); (2) il trailing ha
            # alzato lo stop effettivo in modo apprezzabile e va allineato.
            if trading_enabled() and not CONFIG["KRAKEN_DRY_RUN"]:
                if peak_gain >= t_arm:
                    trail_stop_price = peak * (1 - t_dist / 100)
                    breakeven_price = entry * (1 + CONFIG["BREAKEVEN_BUFFER_PCT"] / 100)
                    desired_stop = max(trail_stop_price, breakeven_price)
                elif hold >= hold_min:
                    desired_stop = sl_price
                else:
                    # Ancora in finestra di grazia (min_hold non raggiunto):
                    # tieni sul server lo stop largo "catastrofico", non
                    # stringerlo a sl_price. Altrimenti un ripescaggio qui
                    # (es. dopo un piazzamento fallito al buy) piazzerebbe
                    # comunque lo stop stretto, vanificando min_hold.
                    grace_pct = catastrophic_sl_pct(pos.get("sl_pct", params["sl_pct"]))
                    desired_stop = entry * (1 - grace_pct / 100)
                old_txid = pos.get("server_sl_txid")
                old_stop = pos.get("server_sl_price")
                needs_update = old_txid is None or (
                    old_stop is not None and desired_stop > old_stop * 1.003
                )
                if needs_update:
                    if old_txid:
                        cancel_order(old_txid)
                    # Usa il saldo reale se disponibile, non il volume
                    # nominale registrato all'acquisto: se la fee e' stata
                    # trattenuta nell'asset invece che in EUR, il volume
                    # davvero posseduto e' leggermente inferiore, e Kraken
                    # rifiuta un ordine per piu' di quanto c'e' realmente
                    # ("Insufficient funds") anche se la posizione e' reale.
                    stop_vol = pos["volume"]
                    asset_code_sl = pos.get("asset_code")
                    if balances is not None and asset_code_sl and asset_code_sl in balances:
                        stop_vol = min(stop_vol, balances[asset_code_sl])
                    # Idem check_buys: il saldo reale non e' allineato ai
                    # decimali di volume ammessi dal pair.
                    stop_vol = round_vol(stop_vol, pos.get("lot_decimals", 8))
                    try:
                        r, used_dec = place_stop_order_safe(
                            pos["pair"], stop_vol, desired_stop,
                            pos.get("pair_decimals", 8)
                        )
                        pos["server_sl_txid"] = r.get("txid", [None])[0]
                        pos["server_sl_price"] = desired_stop
                        pos["pair_decimals"] = used_dec
                    except Exception as e:
                        print(f"[WARN] Piazzamento/aggiornamento stop server {base}: {e}")
            continue

        sell_vol = pos["volume"]
        order_note = ""
        exit_fee_eur = 0.0
        if trading_enabled():
            asset_code = pos.get("asset_code")
            if balances is not None and asset_code and asset_code in balances:
                sell_vol = min(sell_vol, balances[asset_code])
            sell_vol = round_vol(sell_vol, pos.get("lot_decimals", 8))
            # Cancella lo stop reale prima di vendere noi: altrimenti
            # restano due ordini di vendita vivi sulla stessa posizione
            # (il nostro market e lo stop sul server), rischio di vendere
            # due volte o che il secondo ordine fallisca a vuoto.
            server_txid = pos.get("server_sl_txid")
            if server_txid:
                cancel_order(server_txid)
            try:
                result = place_order(pos["pair"], "sell", sell_vol)
                txid = result.get("txid", ["(ok)"])[0]
                order_note = f"\nOrdine: {txid}"
            except Exception as e:
                telegram_send(f"⚠️ Errore vendita {base}: {e}")
                continue

            # Prezzo/volume/fee di riempimento reale: su coin poco liquide
            # (spread largo, book sottile) il prezzo del ticker usato per
            # decidere puo' essere ben diverso da dove l'ordine market e'
            # davvero eseguito. Senza questo, il P&L riportato (e la soglia
            # kill-switch) si basano su un prezzo stimato, non reale, e
            # senza la fee su un lordo che non arriva mai per intero.
            if not CONFIG["KRAKEN_DRY_RUN"]:
                time.sleep(1.5)
                fill_price, fill_vol, fill_fee = get_order_fill(txid)
                if fill_price and fill_vol:
                    price = fill_price
                    sell_vol = fill_vol
                    exit_fee_eur = fill_fee
                    chg = (price - entry) / entry * 100

        pnl = (price - entry) * sell_vol - pos.get("entry_fee_eur", 0.0) - exit_fee_eur
        state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + pnl
        record_trade_stat(state, pnl, manual=False)

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

        check_kill_switch(state)


# ================== BUY ==================

def check_buys(positions, pumping, state, params, balances):
    if trading_enabled() and balances is None:
        # Balance API in errore/timeout: senza saldo reale non possiamo ne'
        # calcolare il capitale libero ne' verificare se possediamo gia'
        # (fuori state.json) l'asset che stiamo per comprare. Meglio saltare
        # gli acquisti per questo run che comprare alla cieca.
        print("[WARN] Saldo reale non disponibile: salto tutti gli acquisti questo run")
        return 0
    bought = 0
    max_buys = CONFIG["MAX_BUYS_PER_RUN"]
    ohlc_checks = 0
    max_ohlc = CONFIG["MAX_OHLC_CHECKS_PER_RUN"]
    # Contatori diagnostici: perche' i candidati vengono scartati. Servono a
    # rispondere da log a "perche' oggi non ha comprato nulla?" senza dover
    # indovinare quale filtro e' stato il collo di bottiglia.
    reasons = {
        "gia_posseduto": 0, "saldo_reale": 0, "candela": 0, "correlazione": 0,
        "volume": 0, "rsi": 0, "trend_1h": 0, "capitale": 0, "altro": 0,
    }

    available_eur = get_eur_balance(balances) if trading_enabled() else None
    if trading_enabled() and available_eur is not None:
        print(f"Balance EUR disponibile: {available_eur:.2f}€")

    # Size per trade proporzionale al capitale TOTALE (EUR liberi + valore
    # a mercato delle posizioni gia' aperte), non un euro fisso — cosi' se
    # il capitale scende (o sale) il size si adatta da solo, invece di
    # restare fisso a 30€ anche quando il conto e' sceso a 80€. Il valore
    # delle posizioni aperte usa "last_price", gia' aggiornato da
    # check_sells poco prima nello stesso ciclo — nessuna chiamata API in
    # piu' necessaria.
    positions_value = sum(
        pos.get("last_price", pos.get("entry_price", 0)) * pos.get("volume", 0)
        for pos in positions.values()
    )
    if available_eur is not None:
        total_capital = available_eur + positions_value
    else:
        # Non in trading live (segnale/dry-run senza saldo reale): usa il
        # capitale "virtuale" tracciato solo dalle posizioni note.
        total_capital = positions_value if positions_value > 0 else None
    base_eur = (
        round(total_capital * params["eur_pct"] / 100, 2)
        if total_capital else round(100 * params["eur_pct"] / 100, 2)
    )

    # Rendimenti recenti delle posizioni gia' aperte, per il filtro di
    # correlazione (vedi CONFIG["MAX_POSITION_CORRELATION"]). Calcolati una
    # sola volta per run, non per candidato.
    open_returns = {}
    if positions:
        try:
            for pbase, pos in positions.items():
                pohlc = get_ohlc(pos["pair"], CONFIG["SCAN_TIMEFRAME"],
                                  CONFIG["CORRELATION_LOOKBACK"])
                open_returns[pbase] = pct_returns(pohlc["closes"])
        except RateLimitError as e:
            print(f"[RATE LIMIT] {e} — salto acquisti per questo run")
            return 0
        except Exception as e:
            print(f"[WARN] OHLC posizioni aperte (correlazione): {e}")

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
            reasons["gia_posseduto"] += 1
            continue

        # Guardia sul saldo reale: se possediamo gia' un importo non
        # trascurabile di questo asset su Kraken, non ricompriamo, anche se
        # lo stato locale non lo traccia (es. un push su git fallito ha
        # perso la posizione dal state.json). Il saldo Kraken e' la fonte
        # di verita' definitiva, non lo e' positions.
        asset_code = c.get("asset_code")
        if trading_enabled() and asset_code and asset_code in balances:
            real_bal = balances[asset_code]
            if real_bal * c["last_price"] >= CONFIG["MIN_TRADE_EUR"]:
                print(f"  Skip {base}: saldo reale gia' presente "
                      f"({real_bal:.6f}, non tracciato in state.json)")
                reasons["saldo_reale"] += 1
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
            reasons["candela"] += 1
            time.sleep(0.3)
            continue

        # Correlazione con posizioni gia' aperte: evita di concentrare il
        # rischio su coin che si muovono insieme (proxy di diversificazione
        # settoriale, dato che Kraken non espone una categoria via API).
        if open_returns:
            cand_returns = pct_returns(closes)
            skip_corr = False
            for pbase, pret in open_returns.items():
                r = correlation(cand_returns, pret)
                if r >= CONFIG["MAX_POSITION_CORRELATION"]:
                    print(f"  Skip {base}: correlato con {pbase} (r={r:.2f})")
                    skip_corr = True
                    break
            if skip_corr:
                reasons["correlazione"] += 1
                time.sleep(0.3)
                continue

        # Volume surge
        avg_n = min(20, len(volumes) - 3)
        if avg_n < 3:
            continue
        avg_vol = sum(volumes[-avg_n - 3:-3]) / avg_n
        vol_ratio = volumes[idx] / avg_vol if avg_vol > 0 else 0
        if vol_ratio < params["pump_vol_surge"]:
            reasons["volume"] += 1
            time.sleep(0.3)
            continue

        # RSI (solo candele chiuse)
        rsi_closes = closes[:-1] if len(closes) > CONFIG["RSI_PERIOD"] + 3 else closes
        rsis = compute_rsi(rsi_closes, CONFIG["RSI_PERIOD"])
        if not rsis:
            continue
        rsi = rsis[-1]
        if rsi > params["pump_rsi_max"]:
            reasons["rsi"] += 1
            time.sleep(0.3)
            continue

        # Conferma di trend a 1h: scarta i pump dentro un trend orario
        # ancora ribassista (rimbalzi morti). Ultimo filtro prima di
        # comprare, cosi' la chiamata OHLC extra si paga solo per i
        # candidati che hanno gia' passato tutto il resto.
        try:
            if not check_trend_1h(c["pair"]):
                reasons["trend_1h"] += 1
                time.sleep(0.3)
                continue
        except RateLimitError as e:
            print(f"[RATE LIMIT] {e} — interrompo la scansione acquisti per questo run")
            break

        # Dimensiona l'ordine come % del capitale totale, capato al capitale
        # EUR reale disponibile in questo momento
        price = c["last_price"]
        eur = base_eur
        reduced_for_capital = False
        if trading_enabled() and available_eur is not None:
            capped = max(0.0, available_eur - 1.0)  # margine di sicurezza per fee
            if capped < eur:
                eur = capped
                reduced_for_capital = True
            if eur < CONFIG["MIN_TRADE_EUR"]:
                reasons["capitale"] += 1
                continue

        vol = round_vol((eur * CONFIG["BUY_SAFETY_MARGIN"]) / price, c["lot_decimals"])
        if vol <= 0 or vol < c["ordermin"]:
            reasons["altro"] += 1
            continue

        order_note = ""
        entry_fee_eur = 0.0
        if trading_enabled():
            try:
                result = place_order(c["pair"], "buy", vol)
                txid = result.get("txid", ["(ok)"])[0]
                order_note = f"\nOrdine: {txid}"
            except Exception as e:
                telegram_send(f"⚠️ Errore acquisto {base}: {e}")
                time.sleep(0.3)
                continue

            # Prezzo/volume/fee di riempimento reale (solo LIVE, non dry-run).
            # La fee d'ingresso va tenuta a mente per il P&L netto in uscita.
            if not CONFIG["KRAKEN_DRY_RUN"]:
                time.sleep(1.5)
                fill_price, fill_vol, fill_fee = get_order_fill(txid)
                if fill_price and fill_vol:
                    price = fill_price
                    vol = fill_vol
                    entry_fee_eur = fill_fee
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

        # Lo stop reale sul server parte piu' largo di sl_price ("catastrofico",
        # vedi HOLD_GRACE_SL_*): la logica locale non applica lo SL prima di
        # min_hold_minutes per assorbire rumore/dip temporanei, quindi anche
        # lo stop lato Kraken deve restare largo in quella finestra, altrimenti
        # eseguirebbe la vendita da solo su un dip che il bot sta ignorando
        # apposta. Si stringe a sl_price dopo min_hold (vedi check_sells).
        grace_sl_pct = catastrophic_sl_pct(sl)
        grace_sl_price = price * (1 - grace_sl_pct / 100)

        server_sl_txid = None
        used_pair_decimals = c.get("pair_decimals", 8)
        if trading_enabled() and not CONFIG["KRAKEN_DRY_RUN"]:
            try:
                sl_result, used_pair_decimals = place_stop_order_safe(
                    c["pair"], vol, grace_sl_price, c.get("pair_decimals", 8)
                )
                server_sl_txid = sl_result.get("txid", [None])[0]
            except Exception as e:
                telegram_send(f"⚠️ {base}: stop loss reale su Kraken non piazzato ({e}). "
                               f"Protetto solo dalla logica del bot, non dal server.")

        positions[base] = {
            "pair": c["pair"], "asset_code": c.get("asset_code"),
            "entry_price": price, "volume": vol,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "highest_price": price, "last_price": price,
            "tp_pct": tp, "sl_pct": sl,
            "tp_price": tp_price, "sl_price": sl_price,
            "trail_arm": params["trail_arm"], "trail_dist": params["trail_dist"],
            "trail_tiers": params.get("trail_tiers"),
            "min_hold_min": params["min_hold_min"],
            "strategy": "pump",
            "server_sl_txid": server_sl_txid,
            "server_sl_price": grace_sl_price if server_sl_txid else None,
            "pair_decimals": used_pair_decimals,
            "lot_decimals": c.get("lot_decimals", 8),
            "entry_fee_eur": entry_fee_eur,
        }
        bought += 1
        time.sleep(0.3)

    if bought == 0 and pumping:
        scarti = ", ".join(f"{k} {v}" for k, v in reasons.items() if v)
        print(f"  Scarti candidati: {scarti or 'nessuno (limiti run raggiunti prima)'}")
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
    rsi_note = (f" (base {params['pump_rsi_base']:.0f})"
                if params["pump_rsi_max"] != params["pump_rsi_base"] else "")
    print(f"Params: {params['eur_pct']}% capitale/trade, max {params['max_pos']} pos, "
          f"TP +{params['tp_pct']}%, SL -{params['sl_pct']}%, "
          f"RSI<{params['pump_rsi_max']:.0f}{rsi_note}, "
          f"hold {params['min_hold_min']}min, spread <{params['max_spread']}%")
    print(f"Mode: {mode_label()}")

    balances = get_balances()
    if balances:
        eur_bal = get_eur_balance(balances)
        if eur_bal is not None:
            print(f"Balance EUR: {eur_bal:.2f}€")

    all_pairs = get_all_eur_pairs()
    print(f"{len(all_pairs)} coppie EUR")

    # Le posizioni aperte hanno priorita' sul ticker, in una chiamata a
    # parte prima della scansione dell'intero mercato: se Kraken risponde
    # 429 a meta' della scansione (che puo' essere diversi batch), un pair
    # gia' comprato potrebbe finire in un batch successivo mai eseguito —
    # check_sells lo salterebbe del tutto quel run (niente SL, niente TP,
    # niente verifica saldo reale) solo perche' la sua coin non era in
    # cima alla lista di scansione. Il capitale gia' investito viene prima
    # della ricerca di nuove opportunita'.
    position_pairs = [pos["pair"] for pos in positions.values() if pos.get("pair")]
    tickers = get_ticker_batch(position_pairs) if position_pairs else {}

    scan_pairs = [p for p in all_pairs.keys() if p not in tickers]
    tickers.update(get_ticker_batch(scan_pairs))
    print(f"Ticker: {len(tickers)} coppie")
    if position_pairs and any(p not in tickers for p in position_pairs):
        missing = [p for p in position_pairs if p not in tickers]
        print(f"[WARN] Ticker mancante per posizioni aperte: {missing}")

    pumping = scan_pumping(all_pairs, tickers, params, state)
    print(f"{len(pumping)} pump qualificati "
          f"({params['min_ticker_chg']}%-{CONFIG['MAX_DAILY_PUMP_PCT']}%, "
          f"vol>{params['min_vol_eur']}€, spread<{params['max_spread']}%)")
    if pumping:
        top = ", ".join(
            f"{p['base']}(+{p['change_today_pct']:.0f}%,{p['vol_eur']:.0f}€,"
            f"sp{p['spread_pct']:.1f}%)"
            for p in pumping[:5]
        )
        print(f"  Top: {top}")

    check_sells(positions, tickers, state, params, balances)

    # Valutato anche qui, non solo dopo ogni vendita: check_kill_switch()
    # nei call-site sopra scatta solo quando UNA posizione si chiude in
    # questo run. Se cumulative_pnl_eur e' gia' oltre soglia ma
    # trading_paused e' ancora False per qualche motivo (stato importato a
    # mano, corsa del kill-switch mai scattata), senza questa chiamata il
    # bot continuerebbe a comprare finche' non si chiude una posizione.
    check_kill_switch(state)

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
            f"{params['eur_pct']:.0f}% capitale/trade | max {params['max_pos']} pos | "
            f"TP +{params['tp_pct']:.0f}% SL -{params['sl_pct']:.0f}%\n"
            f"Trail: {params['trail_arm']}%/{params['trail_dist']}% | "
            f"Hold: {params['min_hold_min']}min | Spread <{params['max_spread']}%\n"
            f"Vol min: {params['min_vol_eur']}€ | "
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
