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

# Strategia "dip scalp" (v6): niente piu' caccia ai pump con stop-loss a
# mercato. Si compra un piccolo ribasso su coin liquide e si piazza SUBITO
# un ordine take-profit a ORDINE LIMITE (non uno stop): un limite sopra il
# prezzo di entrata non puo' mai eseguire peggio del prezzo impostato,
# a differenza di uno stop-loss che scatta a mercato e puo' subire forte
# slippage su un book sottile (la causa esatta della perdita su PRCL). La
# rete di sicurezza contro un crollo vero e proprio (rug pull/delisting) e'
# "catastrophic_pct": molto larga, quasi mai toccata, e NON e' un secondo
# ordine resting in parallelo al take-profit (Kraken vincolerebbe due volte
# lo stesso saldo) — e' un controllo locale ad ogni run: solo se sfondata,
# il bot cancella il TP e vende a mercato in emergenza.
USER_PROFILES = {
    "sicuro": {
        "label": "\U0001F6E1 SICURO",
        "eur_pct_per_trade": 50.0,
        "max_open_positions": 2,
        "take_profit_pct": 2.2,       # target lordo; netto ~1.0% dopo fee (0.80% buy taker + 0.40% sell maker)
        "catastrophic_pct": 25.0,     # rete di sicurezza larga, ultima istanza
        "dip_min_pct": 1.0,           # ribasso minimo di oggi per essere considerato
        "dip_max_pct": 5.0,           # oltre questo e' piu' probabile un trend reale che un pullback
        "dip_rsi_min": 35,
        "dip_rsi_max": 55,
        "max_total_loss_eur": 25.0,
        "max_spread_pct": 0.8,
        "min_volume_eur": 40000,
        "desc": "2 posizioni da 50%, coin molto liquide",
    },
    "medio": {
        "label": "⚖️ MEDIO",
        "eur_pct_per_trade": 50.0,
        "max_open_positions": 2,
        "take_profit_pct": 2.5,
        "catastrophic_pct": 32.0,
        "dip_min_pct": 1.0,
        "dip_max_pct": 7.0,
        "dip_rsi_min": 30,
        "dip_rsi_max": 55,
        "max_total_loss_eur": 50.0,
        "max_spread_pct": 1.0,
        "min_volume_eur": 25000,
        "desc": "2 posizioni da 50%, bilanciato",
    },
    "aggressivo": {
        "label": "\U0001F525 AGGRESSIVO",
        "eur_pct_per_trade": 50.0,
        "max_open_positions": 2,
        "take_profit_pct": 3.0,
        "catastrophic_pct": 40.0,
        "dip_min_pct": 0.8,
        "dip_max_pct": 9.0,
        "dip_rsi_min": 25,
        "dip_rsi_max": 58,
        "max_total_loss_eur": 80.0,
        "max_spread_pct": 1.5,
        "min_volume_eur": 12000,
        "desc": "2 posizioni da 50%, coin meno liquide ammesse",
    },
}

# ================== CONFIG ==================

CONFIG = {
    "QUOTE_CURRENCY": "EUR",
    "SCAN_TIMEFRAME": 5,
    "SCAN_LOOKBACK": 60,
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
}


# eur_mult e' fisso a 1.0 in ogni regime: l'utente vuole impegnare SEMPRE
# tutto il capitale presente (eur_pct_per_trade * max_open_positions = 100%,
# vedi USER_PROFILES), quindi qui non c'e' piu' spazio per una riduzione
# difensiva in bear/paura estrema — ridurrebbe l'importo per trade e
# lascerebbe capitale libero inutilizzato, l'esatto contrario di quanto
# richiesto. La prudenza nei regimi rischiosi passa solo da tp_mult (target
# piu' piccolo, piu' veloce da centrare) e sl_mult (rete di sicurezza
# catastrofica piu' stretta), non piu' dalla size. pos_add resta a 0
# ovunque: il NUMERO di posizioni e' fisso, non si muove col regime.
RISK_PROFILES = {
    "BULL_AGGRESSIVE": {"eur_mult": 1.0, "pos_add": 0, "tp_mult": 1.15, "sl_mult": 1.2},
    "BULL_MODERATE":   {"eur_mult": 1.0, "pos_add": 0, "tp_mult": 1.05, "sl_mult": 1.1},
    "NEUTRAL":         {"eur_mult": 1.0, "pos_add": 0, "tp_mult": 1.0, "sl_mult": 1.0},
    "BEAR_DEFENSIVE":  {"eur_mult": 1.0, "pos_add": 0, "tp_mult": 0.9, "sl_mult": 0.8},
    "EXTREME_FEAR":    {"eur_mult": 1.0, "pos_add": 0, "tp_mult": 0.85, "sl_mult": 0.7},
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
    return {
        "eur_pct": round(up["eur_pct_per_trade"] * rp["eur_mult"], 2),
        "max_pos": max(1, up["max_open_positions"] + rp["pos_add"]),
        "tp_pct": round(up["take_profit_pct"] * rp["tp_mult"], 2),
        "catastrophic_pct": round(up["catastrophic_pct"] * rp["sl_mult"], 1),
        "dip_min_pct": up["dip_min_pct"],
        "dip_max_pct": up["dip_max_pct"],
        "dip_rsi_min": up["dip_rsi_min"],
        "dip_rsi_max": up["dip_rsi_max"],
        "max_loss": up["max_total_loss_eur"],
        "max_spread": up.get("max_spread_pct", 1.0),
        "min_vol_eur": up.get("min_volume_eur", 20000),
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
                f"Target +{up['take_profit_pct']}% | Stop catastrofico -{up['catastrophic_pct']}%\n"
                f"Ribasso richiesto: -{up['dip_min_pct']}%..-{up['dip_max_pct']}% | "
                f"RSI {up['dip_rsi_min']}-{up['dip_rsi_max']}\n"
                f"Max pos: {up['max_open_positions']} | Max loss: {up['max_total_loss_eur']}€\n"
                f"Spread max: {up.get('max_spread_pct', 1)}% | Vol min: {up.get('min_volume_eur', 20000)}€",
                all_buttons(state),
            )

    elif text in ("profilo", "profile"):
        _, current = get_user_profile(state)
        lines = [f"Profilo attuale: <b>{USER_PROFILES[current]['label']}</b>\n"]
        for name, p in USER_PROFILES.items():
            marker = " ← attivo" if name == current else ""
            lines.append(
                f"{p['label']}: target +{p['take_profit_pct']}%, "
                f"catastrofico -{p['catastrophic_pct']}%, {p['max_open_positions']} pos{marker}"
            )
        telegram_send("\n".join(lines), profile_buttons() + control_buttons())

    elif text.startswith("rsi "):
        up, current = get_user_profile(state)
        try:
            val = float(text.split()[1].replace(",", "."))
        except (IndexError, ValueError):
            telegram_send("Uso: /rsi 55 (imposta il tetto RSI per considerare un ribasso ancora 'sano')")
        else:
            overrides = state.setdefault("profile_overrides", {})
            overrides.setdefault(current, {})["dip_rsi_max"] = val
            telegram_send(
                f"✅ Tetto RSI per {USER_PROFILES[current]['label']} impostato a {val:.0f} "
                f"(era {USER_PROFILES[current]['dip_rsi_max']:.0f}). "
                f"Sopra questa soglia il ribasso non viene piu' considerato un pullback "
                f"sano e il bot non compra.",
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
            tp_status = "✅ TP piazzato" if p.get("server_tp_txid") else "⚠️ TP non piazzato"
            lines.append(
                f"• {base}: {chg:+.1f}% (target +{p.get('tp_pct', 0):.1f}%) | {tp_status}"
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
                server_txid = pos.get("server_tp_txid")
                if server_txid and cancel_order(server_txid) == CANCEL_FAILED:
                    # Il saldo e' comunque quasi a zero (per questo siamo
                    # in questo ramo): la posizione va rimossa dal
                    # tracciamento a prescindere, ma se la cancellazione
                    # e' fallita per un motivo transitorio (non "gia'
                    # sparito") il take-profit potrebbe restare vivo e orfano.
                    telegram_send(
                        f"⚠️ {base}: rimosso dal tracciamento ma la cancellazione "
                        f"dell'ordine take-profit ({server_txid}) potrebbe non essere "
                        f"andata a buon fine — verifica manualmente su Kraken."
                    )
                del positions[base]
                return
            sell_vol = min(sell_vol, real_bal)
        # Arrotonda ai decimali di volume ammessi dal pair PRIMA di
        # ordinare: real_bal viene da Kraken con precisione piena, non
        # allineata a lot_decimals (pos["volume"] lo era gia' dal buy, ma
        # min() con real_bal puo' aver introdotto piu' cifre di quante il
        # pair ne accetti) — senza, Kraken puo' rifiutare l'ordine.
        sell_vol = round_vol(sell_vol, pos.get("lot_decimals", 8))
        # Cancella l'ordine take-profit sul server PRIMA di vendere noi:
        # altrimenti resta un ordine limite orfano su Kraken. Se poi il bot
        # ricompra la stessa coin, quell'ordine vecchio potrebbe vendere la
        # posizione nuova a un prezzo calcolato sulla vecchia entry.
        server_txid = pos.get("server_tp_txid")
        cancel_status = CANCEL_OK
        if server_txid:
            # cancel_order() ora ritorna tre stati, non solo un bool: se
            # l'ordine e' gia' sparito da solo (ALREADY_GONE — riempito,
            # scaduto, cancellato altrove) equivale a successo, va trattato
            # come "non c'e' piu' nulla da cancellare" e si procede. Solo
            # un fallimento transitorio VERO (FAILED — timeout/rate limit/
            # nonce) significa "l'ordine potrebbe essere ancora vivo",
            # quindi solo in quel caso non tentiamo la vendita.
            cancel_status = cancel_order(server_txid)
            if cancel_status != CANCEL_FAILED:
                pos["server_tp_txid"] = None
                pos["server_tp_price"] = None
                if cancel_status == CANCEL_OK:
                    # Il saldo che il TP teneva vincolato non si libera
                    # sempre nello stesso istante in cui la cancellazione
                    # viene accettata: una vendita immediata sullo stesso
                    # volume puo' trovare ancora "occupato" quello che
                    # stiamo per rivendicare. Se era gia' ALREADY_GONE non
                    # c'e' nessun vincolo da aspettare che si liberi.
                    time.sleep(1.5)
        if cancel_status == CANCEL_FAILED:
            telegram_send(
                f"⚠️ {base}: impossibile cancellare l'ordine take-profit esistente "
                f"({server_txid}, errore transitorio) — non tento la vendita, "
                f"il saldo potrebbe restare vincolato li'. Riprovo al prossimo run."
            )
            return
        try:
            result, sell_vol = sell_with_balance_retry(
                pos["pair"], asset_code, sell_vol, pos.get("lot_decimals", 8)
            )
            txid = result.get("txid", ["(ok)"])[0]
            order_note = f"\nOrdine: {txid} ({mode_label()})"
        except Exception as e:
            telegram_send(f"⚠️ Errore vendita {base}: {e}")
            # A questo punto il TP era stato davvero cancellato (altrimenti
            # saremmo usciti sopra), quindi la posizione e' scoperta per
            # davvero: ripiazza subito qualcosa invece di lasciarla cosi'.
            try:
                fallback_tp = pos.get("tp_price", pos["entry_price"] * 1.02)
                emerg_vol = round_vol(pos["volume"] * 0.99, pos.get("lot_decimals", 8))
                r2, dec2 = place_limit_order_safe(
                    pos["pair"], "sell", emerg_vol, fallback_tp, pos.get("pair_decimals", 8)
                )
                pos["server_tp_txid"] = r2.get("txid", [None])[0]
                pos["server_tp_price"] = fallback_tp
                pos["pair_decimals"] = dec2
                telegram_send(f"↩️ {base}: ripiazzato l'ordine take-profit a {fp(fallback_tp)}.")
            except Exception as e2:
                telegram_send(
                    f"⚠️⚠️ {base}: nessun ordine ripiazzato dopo la vendita fallita ({e2}). "
                    f"Posizione senza protezione reale sul server, serve intervento manuale."
                )
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
    """Ticker in batch da CONFIG['TICKER_BATCH_SIZE'] pair per richiesta.
    ATTENZIONE (verificato contro l'API reale): se anche un solo pair nel
    batch e' sconosciuto/delistato, Kraken fa fallire l'INTERA richiesta —
    nessun risultato parziale per i pair ancora validi nello stesso batch.
    Va bene per la scansione di mercato (i pair vengono presi da
    AssetPairs, sempre validi in quel momento), ma NON va usato per le
    posizioni aperte: un solo pair rotto azzererebbe il monitoraggio di
    tutte le altre. Per quelle vedi get_ticker_individually.
    """
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
            elif data.get("error"):
                print(f"[WARN] Ticker batch {i}: Kraken error {data['error']}")
        except Exception as e:
            print(f"[WARN] Ticker batch {i}: {e}")
        time.sleep(0.3)
    return all_tickers


def get_ticker_individually(pair_names):
    """Come get_ticker_batch ma un pair per richiesta: piu' lento ma molto
    piu' resiliente. Usato solo per le posizioni aperte, che sono poche
    (2-6), quindi il costo extra e' trascurabile — e una coin delistata o
    rinominata (il caso piu' probabile proprio tra le micro-cap che questo
    bot compra) non deve azzerare il monitoraggio di TUTTO il resto del
    portafoglio: SL/TP/trailing/verifica saldo reale sulle altre posizioni
    devono continuare a funzionare anche se una singola coin e' rotta.
    """
    tickers = {}
    for pair in pair_names:
        try:
            r = requests.get(f"{KRAKEN_PUBLIC}/Ticker",
                             params={"pair": pair}, timeout=20)
            if r.status_code == 429:
                print(f"[RATE LIMIT] Kraken 429 su Ticker {pair}")
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("result"):
                tickers.update(data["result"])
            else:
                print(f"[WARN] Ticker {pair}: Kraken error {data.get('error')}")
        except Exception as e:
            print(f"[WARN] Ticker {pair}: {e}")
        time.sleep(0.2)
    return tickers


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


def sell_with_balance_retry(pair, asset_code, sell_vol, lot_decimals):
    """Piazza una vendita a mercato. Se Kraken rifiuta per 'Insufficient
    funds' (il saldo reale in questo istante e' inferiore a sell_vol —
    puo' succedere se e' cambiato dopo lo snapshot di balances preso a
    inizio run, o se asset_code non era nella risposta di Balance quel
    run), rilegge il saldo VERO adesso e riprova una volta con un margine
    dello 0.1% sotto. Senza questo self-heal, un fallimento del genere si
    ripete identico ad ogni run finche' non interviene un umano — e nel
    frattempo, dato che lo stop server viene cancellato prima di tentare
    la vendita (per non avere due ordini vivi), la posizione resta senza
    NESSUNA protezione reale sul server per tutto quel tempo.
    Ritorna (risultato_ordine, volume_effettivamente_venduto).
    """
    try:
        return place_order(pair, "sell", sell_vol), sell_vol
    except Exception as e:
        if "Insufficient funds" not in str(e):
            raise
        # Da' tempo a Kraken di rilasciare il saldo che era vincolato
        # dall'ordine stop appena cancellato (il ledger interno non e'
        # sempre istantaneo): un retry immediato puo' vedere ancora il
        # vecchio saldo "occupato" e fallire di nuovo per lo stesso motivo.
        time.sleep(2.0)
        fresh = get_balances()
        real_bal = fresh.get(asset_code, 0.0) if fresh else 0.0
        retry_vol = round_vol(real_bal * 0.999, lot_decimals)
        diag = (
            f"saldo rifetchato {asset_code}: {real_bal} "
            f"(balances {'non disponibile' if fresh is None else ('assente' if asset_code not in fresh else 'presente')})"
        )
        if retry_vol <= 0:
            raise RuntimeError(f"{e} — {diag}, niente da vendere") from e
        try:
            return place_order(pair, "sell", retry_vol), retry_vol
        except Exception as e2:
            raise RuntimeError(f"{e2} (dopo retry, {diag})") from e2


# Ancorata a "price" (non solo "up to N decimals"): Kraken usa la stessa
# frase anche per l'errore sui decimali di VOLUME ("volume can only be
# specified up to N decimals"). Senza l'ancoraggio, un errore di volume
# verrebbe letto come se fosse sui decimali di prezzo e la correzione
# verrebbe applicata al parametro sbagliato, senza risolvere il problema
# reale (e mascherando l'errore vero nel log).
_DECIMALS_ERROR_RE = re.compile(r"price can only be specified up to (\d+) decimals", re.IGNORECASE)


# Nota: questa strategia non piazza piu' ordini "stop-loss" su Kraken (che
# scattano A MERCATO e possono subire slippage forte su un book sottile — la
# causa esatta della perdita su PRCL che ha portato al pivot di strategia).
# L'unico ordine resting e' il take-profit a ordine limite (place_limit_order
# sotto); la rete di sicurezza catastrofica e' un controllo locale che vende
# a mercato solo in caso di crollo estremo (vedi check_sells).
def place_limit_order(pair, side, volume, price, price_decimals):
    """Piazza un ordine a ORDINE LIMITE (non a mercato). E' il cuore della
    nuova strategia: un limite di vendita sopra il prezzo di ingresso non
    puo' MAI eseguire a un prezzo peggiore di quello impostato (nessun
    compratore accetta di pagare piu' del mercato), quindi resta in attesa
    sul book come ordine maker finche' (se) il prezzo lo raggiunge — a
    differenza di uno stop-loss, che scatta A MERCATO quando toccato e puo'
    eseguire ben sotto il trigger su un book sottile (la causa esatta della
    perdita su PRCL). In cambio non ha garanzia di esecuzione: se il prezzo
    non arriva mai al livello, l'ordine resta li' semplicemente in attesa.
    """
    price_str = f"{price:.{max(0, price_decimals)}f}"
    data = {
        "pair": pair, "type": side, "ordertype": "limit",
        "price": price_str, "volume": f"{volume}",
    }
    if CONFIG["KRAKEN_DRY_RUN"]:
        data["validate"] = "true"
    return kraken_private("/0/private/AddOrder", data)


def place_limit_order_safe(pair, side, volume, price, price_decimals):
    """Come place_limit_order, ma si autocorregge se i decimali di prezzo
    sono sbagliati (vedi place_stop_order_safe, stessa logica)."""
    try:
        return place_limit_order(pair, side, volume, price, price_decimals), price_decimals
    except Exception as e:
        m = _DECIMALS_ERROR_RE.search(str(e))
        if m:
            corrected = int(m.group(1))
            if corrected != price_decimals:
                result = place_limit_order(pair, side, volume, price, corrected)
                return result, corrected
        raise


# Stati di ritorno di cancel_order(): un fallimento puo' voler dire due
# cose opposte, e trattarle allo stesso modo e' esattamente il bug che ha
# causato l'incidente PUMP (in una direzione) e il suo fix originale (nella
# direzione opposta, vedi CANCEL_ALREADY_GONE sotto).
CANCEL_OK = "CANCELLED"            # cancellazione riuscita adesso
CANCEL_ALREADY_GONE = "ALREADY_GONE"  # l'ordine non esiste piu' (gia' cancellato/riempito/scaduto altrove) — equivale a successo: non c'e' piu' nulla da cancellare
CANCEL_FAILED = "FAILED"           # fallimento transitorio reale (timeout/429/nonce/manutenzione) — l'ordine potrebbe essere ancora vivo


def cancel_order(txid):
    """Cancella un ordine sul server Kraken. Ritorna uno dei tre stati
    sopra invece di un semplice bool: un CancelOrder che fallisce con
    'Unknown order'/'Invalid order' significa che l'ordine e' gia' sparito
    (eseguito, cancellato altrove, scaduto) — se lo trattiamo come
    'fallito, quindi forse ancora vivo' il codice puo' bloccarsi per
    sempre convinto che una protezione esista quando non c'e' piu' nulla
    da proteggere ne' da vendere."""
    try:
        kraken_private("/0/private/CancelOrder", {"txid": txid})
        return CANCEL_OK
    except Exception as e:
        msg = str(e)
        if "Unknown order" in msg or "Invalid order" in msg:
            return CANCEL_ALREADY_GONE
        print(f"[WARN] CancelOrder {txid}: {e}")
        return CANCEL_FAILED


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

def scan_dips(all_pairs, tickers, params, state):
    """Cerca coin LIQUIDE (volume/spread sani) con un piccolo ribasso di
    oggi — non un pump, non un crollo. Il contrario esatto della vecchia
    scan_pumping: li' si cercava la salita piu' estrema per cavalcarla con
    uno stop-loss stretto dietro; qui si cerca un pullback modesto su una
    coin sana da rivendere con un piccolo margine (vedi check_buys), su piu'
    posizioni diversificate invece di concentrarsi su 3-4 pump."""
    dips = []
    dip_min = params["dip_min_pct"]
    dip_max = params["dip_max_pct"]
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

        # Volume 24h in EUR: soglia molto piu' alta che nella vecchia
        # scan_pumping, e' il cuore della diversificazione "molte coin
        # liquide" — niente micro-cap da 1200€/24h dove uno stop (o qui
        # un ordine limite) puo' muovere il book da solo.
        vol_eur = float(tick["v"][1]) * last_price
        if vol_eur < params["min_vol_eur"]:
            skipped_vol += 1
            continue

        # Variazione giornaliera: deve essere un ribasso MODESTO, non un
        # crollo. Fuori dalla fascia -dip_max..-dip_min in entrambe le
        # direzioni scartiamo: sopra -dip_min il calo e' troppo piccolo per
        # lasciare margine fino al target; sotto -dip_max e' piu' probabile
        # un vero trend ribassista che un pullback fisiologico.
        chg = (last_price - open_price) / open_price * 100
        if not (-dip_max <= chg <= -dip_min):
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

        dips.append({
            "pair": pair_name, "base": base, "asset_code": info.get("asset_code"),
            "ordermin": info["ordermin"], "lot_decimals": info["lot_decimals"],
            "pair_decimals": info.get("pair_decimals", 8),
            "last_price": last_price, "change_today_pct": chg,
            "vol_eur": vol_eur, "spread_pct": spread,
        })

    # Preferiamo i ribassi piu' lievi (piu' vicini a 0%): il caso piu'
    # vicino al normale rumore di mercato su una coin liquida, e
    # statisticamente il piu' facile da recuperare fino al target.
    dips.sort(key=lambda x: x["change_today_pct"], reverse=True)
    if skipped_vol or skipped_spread or skipped_cooldown:
        print(f"  Filtrati: {skipped_vol} vol basso, {skipped_spread} spread alto, "
              f"{skipped_cooldown} cooldown")
    return dips


# ================== SELL ==================

def check_sells(positions, tickers, state, params, balances):
    """Molto piu' semplice della vecchia versione (niente trailing/pavimento
    breakeven/SL locale): il take-profit e' un ordine A LIMITE gia' piazzato
    su Kraken al momento dell'acquisto, quindi non serve nessuna logica
    locale per deciderlo o eseguirlo — Kraken lo fa da solo anche se il bot
    e' offline. Qui il bot deve solo: (1) accorgersi quando quell'ordine si
    e' riempito (via saldo reale, la fonte di verita'), e (2) sorvegliare la
    rete di sicurezza catastrofica, l'unica cosa che puo' far vendere il bot
    stesso invece di aspettare Kraken."""
    for base, pos in list(positions.items()):
        tick = tickers.get(pos["pair"])
        if not tick:
            continue
        price = float(tick["c"][0])
        pos["last_price"] = price
        entry = pos["entry_price"]
        chg = (price - entry) / entry * 100

        # Il saldo reale e' la fonte di verita': se e' gia' a zero (o quasi),
        # l'ordine take-profit piazzato all'acquisto si e' riempito da solo
        # su Kraken (o la posizione e' stata chiusa manualmente) — va
        # controllato ad ogni run per OGNI posizione, non solo quando la
        # logica sotto decide anche lei di vendere, altrimenti un fill non
        # verrebbe mai scoperto e la posizione resterebbe "fantasma" in
        # state.json per sempre.
        if trading_enabled() and balances is None:
            print(f"[WARN] Saldo reale non disponibile ({base}): salto verifica questo run")
            continue

        if trading_enabled():
            asset_code = pos.get("asset_code")
            if asset_code and balances.get(asset_code, 0.0) < pos["volume"] * 0.05:
                real_price, real_vol, real_fee = None, None, 0.0
                server_txid = pos.get("server_tp_txid")
                if server_txid:
                    real_price, real_vol, real_fee = get_order_fill(server_txid)
                if real_price and real_vol:
                    close_pnl = (real_price - entry) * real_vol - pos.get("entry_fee_eur", 0.0) - real_fee
                    state["cumulative_pnl_eur"] = state.get("cumulative_pnl_eur", 0.0) + close_pnl
                    record_trade_stat(state, close_pnl, manual=False)
                    close_chg = (real_price - entry) / entry * 100
                    icon = "\U0001F7E2" if close_pnl >= 0 else "\U0001F534"
                    telegram_send(
                        f"{icon} <b>VENDUTO {base}</b>\nTake profit riempito\n"
                        f"{fp(entry)} → {fp(real_price)} ({close_chg:+.1f}%)\n"
                        f"P&L: {close_pnl:+.2f}€ | Cum: {state['cumulative_pnl_eur']:+.2f}€",
                        all_buttons(state),
                    )
                    if close_pnl < 0:
                        add_cooldown(state, base)
                else:
                    telegram_send(
                        f"⚠️ {base}: saldo reale ~0, rimuovo la posizione senza ordine "
                        f"tracciato (probabile vendita manuale)."
                    )
                # Se la chiusura non e' passata dal nostro TP (es. vendita
                # manuale sul sito Kraken), l'ordine resta vivo sul server
                # senza piu' una posizione dietro — orfano, stesso rischio
                # gia' corretto in force_close_one. Se invece e' stato lui a
                # chiudere, cancel_order ritorna ALREADY_GONE (l'ordine e'
                # gia' concluso), che va bene cosi'.
                if server_txid and cancel_order(server_txid) == CANCEL_FAILED:
                    telegram_send(
                        f"⚠️ {base}: rimosso dal tracciamento ma la cancellazione "
                        f"dell'ordine take-profit ({server_txid}) potrebbe non essere "
                        f"andata a buon fine — verifica manualmente su Kraken."
                    )
                del positions[base]
                check_kill_switch(state)
                continue

        # --- Nessun fill: la posizione e' ancora aperta. L'unica cosa che
        # puo' farci vendere di iniziativa e' un crollo oltre la soglia
        # catastrofica — rete di sicurezza di ultima istanza (rug pull,
        # delisting, flash crash), non un meccanismo di presa profitto.
        catastrophic_price = pos.get("catastrophic_price")
        if catastrophic_price is not None and price <= catastrophic_price:
            sell_vol = pos["volume"]
            exit_fee_eur = 0.0
            order_note = ""
            if trading_enabled():
                asset_code = pos.get("asset_code")
                if balances is not None and asset_code and asset_code in balances:
                    sell_vol = min(sell_vol, balances[asset_code])
                sell_vol = round_vol(sell_vol, pos.get("lot_decimals", 8))
                # Cancella l'ordine take-profit prima di vendere a mercato:
                # altrimenti restano due ordini di vendita vivi sulla stessa
                # posizione, rischio di vendere due volte o che uno dei due
                # fallisca a vuoto per saldo insufficiente.
                server_txid = pos.get("server_tp_txid")
                cancel_status = CANCEL_OK
                if server_txid:
                    cancel_status = cancel_order(server_txid)
                    if cancel_status != CANCEL_FAILED:
                        pos["server_tp_txid"] = None
                        pos["server_tp_price"] = None
                        if cancel_status == CANCEL_OK:
                            time.sleep(1.5)
                if cancel_status == CANCEL_FAILED:
                    telegram_send(
                        f"⚠️ {base}: crollo oltre la soglia catastrofica ({chg:+.1f}%) ma "
                        f"non riesco a cancellare l'ordine take-profit esistente "
                        f"({server_txid}, errore transitorio) — non tento la vendita "
                        f"di emergenza, il saldo potrebbe restare vincolato li'. "
                        f"Riprovo al prossimo run."
                    )
                    continue
                try:
                    result, sell_vol = sell_with_balance_retry(
                        pos["pair"], asset_code, sell_vol, pos.get("lot_decimals", 8)
                    )
                    txid = result.get("txid", ["(ok)"])[0]
                    order_note = f"\nOrdine: {txid}"
                except Exception as e:
                    telegram_send(f"⚠️⚠️ Errore vendita di emergenza {base}: {e}")
                    # A questo punto il TP era stato davvero cancellato
                    # (altrimenti saremmo usciti sopra): la posizione e'
                    # scoperta per davvero. Ripiazza il take-profit invece di
                    # lasciarla senza nessun ordine.
                    try:
                        emerg_vol = round_vol(pos["volume"] * 0.99, pos.get("lot_decimals", 8))
                        r2, dec2 = place_limit_order_safe(
                            pos["pair"], "sell", emerg_vol, pos["tp_price"], pos.get("pair_decimals", 8)
                        )
                        pos["server_tp_txid"] = r2.get("txid", [None])[0]
                        pos["server_tp_price"] = pos["tp_price"]
                        pos["pair_decimals"] = dec2
                        telegram_send(f"↩️ {base}: ripiazzato l'ordine take-profit a {fp(pos['tp_price'])}.")
                    except Exception as e2:
                        telegram_send(
                            f"⚠️⚠️ {base}: nessun ordine ripiazzato dopo il fallimento della "
                            f"vendita di emergenza ({e2}). Posizione senza protezione reale "
                            f"sul server, serve intervento manuale."
                        )
                    continue

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
                f"\U0001F534 <b>VENDITA DI EMERGENZA {base}</b>\nStop catastrofico ({chg:+.1f}%)\n"
                f"{fp(entry)} → {fp(price)} ({chg:+.1f}%)\n"
                f"P&L: {pnl:+.2f}€ | Cum: {state['cumulative_pnl_eur']:+.2f}€{order_note}",
                all_buttons(state),
            )
            del positions[base]
            add_cooldown(state, base)
            check_kill_switch(state)
            continue

        # --- Nessun crollo: la posizione aspetta semplicemente che l'ordine
        # take-profit si riempia da solo. Se per qualche motivo non e' mai
        # stato piazzato (fallimento al momento dell'acquisto), ritentiamo
        # qui invece di lasciarla scoperta a tempo indeterminato.
        if trading_enabled() and not CONFIG["KRAKEN_DRY_RUN"] and not pos.get("server_tp_txid"):
            try:
                tp_vol = pos["volume"]
                asset_code_tp = pos.get("asset_code")
                if balances is not None and asset_code_tp and asset_code_tp in balances:
                    tp_vol = min(tp_vol, balances[asset_code_tp])
                tp_vol = round_vol(tp_vol, pos.get("lot_decimals", 8))
                r, used_dec = place_limit_order_safe(
                    pos["pair"], "sell", tp_vol, pos["tp_price"], pos.get("pair_decimals", 8)
                )
                pos["server_tp_txid"] = r.get("txid", [None])[0]
                pos["server_tp_price"] = pos["tp_price"]
                pos["pair_decimals"] = used_dec
            except Exception as e:
                print(f"[WARN] Ripiazzamento take-profit {base}: {e}")
                telegram_send(
                    f"⚠️ {base}: ordine take-profit non piazzato ({e}). "
                    f"Posizione senza target reale sul server fino al prossimo run."
                )


# ================== BUY ==================

def check_buys(positions, candidates, state, params, balances):
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
        "gia_posseduto": 0, "saldo_reale": 0, "correlazione": 0,
        "rsi": 0, "trend_1h": 0, "capitale": 0, "altro": 0,
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

    for c in candidates:
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

        if len(closes) < CONFIG["RSI_PERIOD"] + 5:
            continue

        # Correlazione con posizioni gia' aperte: evita di concentrare il
        # rischio su coin che si muovono insieme (proxy di diversificazione
        # settoriale, dato che Kraken non espone una categoria via API) —
        # ancora piu' rilevante ora che l'obiettivo esplicito e' diversificare
        # su tante posizioni piccole invece di 3-4 concentrate.
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

        # RSI a fascia (non piu' solo un tetto): un ribasso "sano" da
        # comprare deve avere un RSI ne' troppo alto (non sarebbe piu' un
        # vero pullback) ne' troppo basso (piu' probabile un vero crollo/
        # capitolazione che un rimbalzo).
        rsi_closes = closes[:-1] if len(closes) > CONFIG["RSI_PERIOD"] + 3 else closes
        rsis = compute_rsi(rsi_closes, CONFIG["RSI_PERIOD"])
        if not rsis:
            continue
        rsi = rsis[-1]
        if rsi < params["dip_rsi_min"] or rsi > params["dip_rsi_max"]:
            reasons["rsi"] += 1
            time.sleep(0.3)
            continue

        # Conferma di trend a 1h: compriamo il ribasso solo se il trend
        # orario di fondo e' ancora sostanzialmente sano (prezzo sopra la
        # sua media mobile) — un "buy the dip" classico, non un tentativo di
        # prendere un coltello che cade dentro un trend gia' rotto.
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
                # Entrata a mercato (taker): garantisce l'esecuzione subito,
                # non c'e' motivo di rischiare di non riempirsi su un
                # ordine limite in entrata quando il vero vantaggio della
                # nuova strategia (evitare lo slippage) sta nell'USCITA a
                # limite, non nell'entrata.
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
        catastrophic_pct = params["catastrophic_pct"]

        tp_price = price * (1 + tp / 100)
        catastrophic_price = price * (1 - catastrophic_pct / 100)

        cap_note = " (ridotto per capitale disponibile)" if reduced_for_capital else ""

        telegram_send(
            f"\U0001F4C9 <b>COMPRA {base}/{CONFIG['QUOTE_CURRENCY']}</b>\n"
            f"{REGIME_LABEL.get(regime, '')} | {up['label']}\n"
            f"Prezzo: {fp(price)} | {eur:.0f}€{cap_note}\n"
            f"Ribasso: {c['change_today_pct']:.1f}% | Spread: {c['spread_pct']:.1f}% | RSI: {rsi:.0f}\n"
            f"Vol 24h: {c['vol_eur']:.0f}€\n"
            f"Target: {fp(tp_price)} (+{tp:.1f}%) | Stop catastrofico: {fp(catastrophic_price)} (-{catastrophic_pct:.0f}%)\n"
            f"{mode_label()}{order_note}",
            [[{"text": f"\U0001F534 Vendi {base}", "callback_data": f"vendi_{base.lower()}"}],
             *control_buttons()],
        )

        # Piazza SUBITO l'ordine take-profit a ordine limite: e' la
        # protezione/target primaria di questa strategia, non uno stop. Un
        # limite sopra il prezzo corrente non puo' MAI eseguire subito
        # (nessun compratore accetterebbe di pagare piu' del mercato), quindi
        # riposa sul book come maker finche' (se) il prezzo lo raggiunge — a
        # differenza di uno stop-loss, che scatta A MERCATO e puo' eseguire
        # ben sotto il trigger su un book sottile (la causa esatta della
        # perdita su PRCL). La rete di sicurezza catastrofica (vedi
        # check_sells) NON e' un secondo ordine resting in parallelo: Kraken
        # vincolerebbe due volte lo stesso saldo. E' un controllo locale ad
        # ogni run.
        server_tp_txid = None
        used_pair_decimals = c.get("pair_decimals", 8)
        if trading_enabled() and not CONFIG["KRAKEN_DRY_RUN"]:
            try:
                tp_result, used_pair_decimals = place_limit_order_safe(
                    c["pair"], "sell", vol, tp_price, c.get("pair_decimals", 8)
                )
                server_tp_txid = tp_result.get("txid", [None])[0]
            except Exception as e:
                telegram_send(f"⚠️ {base}: ordine take-profit non piazzato ({e}). "
                               f"Verra' ritentato ai prossimi run.")

        positions[base] = {
            "pair": c["pair"], "asset_code": c.get("asset_code"),
            "entry_price": price, "volume": vol,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "last_price": price,
            "tp_pct": tp, "tp_price": tp_price,
            "catastrophic_pct": catastrophic_pct, "catastrophic_price": catastrophic_price,
            "strategy": "dip_scalp",
            "server_tp_txid": server_tp_txid,
            "server_tp_price": tp_price if server_tp_txid else None,
            "pair_decimals": used_pair_decimals,
            "lot_decimals": c.get("lot_decimals", 8),
            "entry_fee_eur": entry_fee_eur,
        }
        bought += 1
        time.sleep(0.3)

    if bought == 0 and candidates:
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
    print(f"Params: {params['eur_pct']}% capitale/trade, max {params['max_pos']} pos, "
          f"target +{params['tp_pct']}%, catastrofico -{params['catastrophic_pct']}%, "
          f"RSI {params['dip_rsi_min']}-{params['dip_rsi_max']}, "
          f"ribasso -{params['dip_min_pct']}%..-{params['dip_max_pct']}%, "
          f"spread <{params['max_spread']}%")
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
    # Un pair per richiesta, non in batch: un batch unico fallisce per
    # intero se anche solo una coin e' delistata/rinominata (verificato
    # contro l'API), il che azzererebbe il monitoraggio di TUTTE le
    # posizioni aperte per quel run, non solo di quella rotta.
    position_pairs = [pos["pair"] for pos in positions.values() if pos.get("pair")]
    tickers = get_ticker_individually(position_pairs) if position_pairs else {}

    scan_pairs = [p for p in all_pairs.keys() if p not in tickers]
    tickers.update(get_ticker_batch(scan_pairs))
    print(f"Ticker: {len(tickers)} coppie")
    if position_pairs and any(p not in tickers for p in position_pairs):
        missing = [p for p in position_pairs if p not in tickers]
        print(f"[WARN] Ticker mancante per posizioni aperte: {missing}")

    dips = scan_dips(all_pairs, tickers, params, state)
    print(f"{len(dips)} ribassi qualificati "
          f"(-{params['dip_max_pct']}%..-{params['dip_min_pct']}%, "
          f"vol>{params['min_vol_eur']}€, spread<{params['max_spread']}%)")
    if dips:
        top = ", ".join(
            f"{p['base']}({p['change_today_pct']:.1f}%,{p['vol_eur']:.0f}€,"
            f"sp{p['spread_pct']:.1f}%)"
            for p in dips[:5]
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
        n = check_buys(positions, dips, state, params, balances)
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
            f"✅ <b>Bot v6.0 ({mode_label()})</b>\n"
            f"{REGIME_LABEL.get(regime, regime)} | {up['label']}\n"
            f"F&G: {fgi if fgi is not None else '?'} | BTC: {btc_arr}\n"
            f"{params['eur_pct']:.0f}% capitale/trade | max {params['max_pos']} pos | "
            f"Target +{params['tp_pct']:.1f}% | Catastrofico -{params['catastrophic_pct']:.0f}%\n"
            f"Ribasso: -{params['dip_min_pct']:.1f}%..-{params['dip_max_pct']:.1f}% | "
            f"RSI {params['dip_rsi_min']:.0f}-{params['dip_rsi_max']:.0f}\n"
            f"Spread <{params['max_spread']}% | Vol min: {params['min_vol_eur']}€ | "
            f"Max buy/run: {CONFIG['MAX_BUYS_PER_RUN']}\n"
            f"Ribassi: {len(dips)} | Pos: {len(positions)} | "
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
