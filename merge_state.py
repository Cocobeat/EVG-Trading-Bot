#!/usr/bin/env python3
"""
Merge a 3 vie per state.json.

Usato quando due run del bot modificano state.json quasi in contemporanea
e git non riesce a fare un merge testuale automatico. Invece di confrontare
righe di testo, questo script capisce il significato dei campi e li
riconcilia in modo sicuro:

  - open_positions: unione intelligente. Se questa run ha aggiunto una
    posizione (nuovo acquisto), la mantiene. Se questa run ne ha rimossa
    una (vendita), la toglie. Per il resto prende la versione piu' fresca
    dal remoto (l'altra run potrebbe aver aggiornato prezzo/trailing).
  - cumulative_pnl_eur: somma il DELTA introdotto da questa run sopra il
    valore piu' recente del remoto, cosi' nessun guadagno/perdita si perde
    ne' si conta due volte.
  - telegram_update_offset: prende sempre il massimo, non si torna mai
    indietro (eviterebbe di rileggere comandi gia' processati).
  - cooldowns: unione, con i valori di questa run che vincono sui duplicati.
  - Campi scalari singoli (pausa, profilo, regime, ecc.): se questa run li
    ha cambiati rispetto alla base, vince questa run; altrimenti vince il
    remoto (che e' piu' fresco).

Uso: python3 merge_state.py base.json local.json remote.json output.json
"""

import json
import sys


def load(path):
    with open(path) as f:
        return json.load(f)


def merge_positions(base, local, remote):
    keys = set(base) | set(local) | set(remote)
    result = {}
    for k in keys:
        in_base = k in base
        in_local = k in local
        in_remote = k in remote

        if in_base and not (in_local and in_remote):
            # La posizione esisteva prima di questa run, ma almeno uno dei
            # due lati l'ha rimossa (venduta): la vendita e' autoritativa,
            # a prescindere da chi dei due l'ha fatta.
            continue

        if not in_base:
            # Posizione nuova (non esisteva prima di questa run)
            if in_local and in_remote:
                # Caso raro: comprata da entrambe le run quasi in contemporanea.
                # Tieni la versione remota, e' quella gia' pubblicata.
                result[k] = remote[k]
            elif in_local:
                result[k] = local[k]
            else:
                result[k] = remote[k]
            continue

        # Presente in base, local e remote: nessuno l'ha venduta.
        if local[k] == base[k]:
            result[k] = remote[k]  # non toccata da noi -> versione piu' fresca
        else:
            result[k] = local[k]  # aggiornata da noi (prezzo/trailing) -> vince questa run
    return result


def merge(base, local, remote):
    out = dict(remote)  # parti dalla versione piu' fresca

    out["open_positions"] = merge_positions(
        base.get("open_positions", {}) or {},
        local.get("open_positions", {}) or {},
        remote.get("open_positions", {}) or {},
    )

    delta_pnl = local.get("cumulative_pnl_eur", 0.0) - base.get("cumulative_pnl_eur", 0.0)
    out["cumulative_pnl_eur"] = remote.get("cumulative_pnl_eur", 0.0) + delta_pnl

    out["telegram_update_offset"] = max(
        local.get("telegram_update_offset", 0) or 0,
        remote.get("telegram_update_offset", 0) or 0,
    )

    cooldowns = dict(remote.get("cooldowns", {}) or {})
    cooldowns.update(local.get("cooldowns", {}) or {})
    out["cooldowns"] = cooldowns

    for field in ("trading_paused", "user_profile", "current_regime",
                  "last_fgi", "last_heartbeat_date", "last_run_time"):
        if local.get(field) != base.get(field):
            out[field] = local.get(field)
        # altrimenti resta gia' il valore remoto (piu' fresco) impostato sopra

    return out


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Uso: python3 merge_state.py base.json local.json remote.json output.json")
        sys.exit(2)
    base_p, local_p, remote_p, out_p = sys.argv[1:5]
    base = load(base_p)
    local = load(local_p)
    remote = load(remote_p)
    merged = merge(base, local, remote)
    with open(out_p, "w") as f:
        json.dump(merged, f, indent=2)
    print("Merge completato.")
