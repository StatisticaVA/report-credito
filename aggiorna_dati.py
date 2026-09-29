#!/usr/bin/env python3
"""Scarica le tavole della Banca d'Italia e salva in dati/ solo le righe che servono alla pagina.

Lo usa l'aggiornamento automatico su GitHub (.github/workflows/aggiorna-dati.yml), una volta al mese,
ma si può lanciare anche a mano:  python3 aggiorna_dati.py
Solo libreria standard di Python (niente da installare).

Per ogni tavola di TAVOLE:
  1. scarica lo ZIP dal servizio A2A della Base Dati Statistica (tutta la tavola, ~5 MB);
  2. tiene le righe dei territori e degli enti indicati (le colonne dipendono dalla tavola: il territorio è LOC_CTP
     per depositi e impieghi, SEDELEG_SOGG per le sofferenze);
  3. scrive dati/<CODICE>.csv (stesse colonne del CSV della Banca d'Italia) e aggiorna dati/aggiornamento.json.
Se il download non riesce o i dati sembrano sbagliati (vuoti, più vecchi di quelli già salvati)
i file esistenti NON vengono toccati e lo script termina con errore (GitHub manda un'e-mail).
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

URL = "https://a2a.bancaditalia.it/infostat/dataservices/export/IT/CSV/DATA/CUBE/BANKITALIA/DIFF/{codice}"
CARTELLA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dati")

# Varese, Lombardia, Italia
TERRITORI = ["ITC41", "ITC4", "IT"]
TAVOLE = [
    {"codice": "TDB10290", "nome": "Depositi (esclusi PCT) - per provincia, settore e sottosettore della clientela",
     "filtri": {"LOC_CTP": TERRITORI, "ENTE_SEGN": ["1070001"]}},
    {"codice": "TDB10295", "nome": "Prestiti (esclusi PCT) - per provincia, settore e sottosettore della clientela",
     "filtri": {"LOC_CTP": TERRITORI, "ENTE_SEGN": ["1070001"]}},
    {"codice": "TRI30401", "nome": "Quota delle sofferenze (al lordo delle svalutazioni e al netto dei passaggi a perdita) "
                                   "di pertinenza dei maggiori affidati - per provincia della clientela",
     "filtri": {"SEDELEG_SOGG": TERRITORI}},
    # sportelli (dati annuali; territorio = LOC_SPORT, nessun settore)
    {"codice": "TDB20207", "nome": "Banche e sportelli - per provincia e gruppo istituzionale di banche",
     "filtri": {"LOC_SPORT": TERRITORI, "ENTE_SEGN": ["1100010"]}},
    {"codice": "TDB20220", "nome": "Numero sportelli per 100.000 abitanti - per provincia",
     "filtri": {"LOC_SPORT": TERRITORI}},
    {"codice": "TDB10227", "nome": "Dipendenti - per provincia",
     "filtri": {"LOC_SPORT": TERRITORI}},
    # tavola a serie storiche: una colonna per serie «SDP_LOCATM.A.<ente>.<fenomeno>.<territorio>», date «2024/12/31»
    {"codice": "TSPAG110", "nome": "ATM e POS - per provincia di sportello",
     "serie": TERRITORI},
]
# colonne che devono esserci in ogni tavola (oltre a quelle dei filtri)
OBBLIGATORIE = ["DATA_OSS", "ENTE_SEGN", "FENEC", "VALORE"]


def scarica(url, tentativi=4):
    errore = None
    for n in range(tentativi):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "report-credito (Camera di Commercio di Varese)"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:  # rete, 5xx, timeout
            errore = e
            print(f"  tentativo {n + 1} non riuscito: {e}", flush=True)
            time.sleep(30 * (n + 1))
    raise RuntimeError(f"download non riuscito: {errore}")


def leggi_csv_zip(dati):
    z = zipfile.ZipFile(io.BytesIO(dati))
    for nome in z.namelist():
        if nome.lower().endswith(".csv"):
            testo = z.read(nome).decode("utf-8-sig")
            if "DATA_OSS" in testo.split("\n", 1)[0]:
                return nome, testo
    raise RuntimeError("nello ZIP non c'è il CSV dei dati (colonna DATA_OSS)")


def filtra(testo, filtri):
    """Restituisce (intestazione, righe tenute): tutte le colonne della tavola, nell'ordine del CSV originale."""
    righe = csv.reader(io.StringIO(testo), delimiter=";")
    testa = next(righe)
    mancanti = [c for c in OBBLIGATORIE + list(filtri) if c not in testa]
    if mancanti:
        raise RuntimeError(f"colonne mancanti nel CSV: {mancanti}")
    pos = {c: testa.index(c) for c in filtri}
    tenute = []
    for r in righe:
        if len(r) < len(testa):
            continue
        if all(r[pos[c]] in valori for c, valori in filtri.items()):
            tenute.append(r[:len(testa)])
    return testa, tenute


def filtra_serie(testo, territori):
    """Tavola a serie storiche: tiene DATA_OSS e le colonne dei territori indicati, e le righe con almeno un valore."""
    righe = csv.reader(io.StringIO(testo), delimiter=";")
    testa = next(righe)
    if "DATA_OSS" not in testa:
        raise RuntimeError("colonna DATA_OSS mancante nel CSV")
    tieni = [testa.index("DATA_OSS")] + [j for j, c in enumerate(testa) if c.count(".") >= 4 and c.rsplit(".", 1)[1] in territori]
    if len(tieni) == 1:
        raise RuntimeError("nessuna serie dei territori richiesti")
    tenute = [[r[j] for j in tieni] for r in righe if len(r) >= len(testa) and any(r[j].strip() for j in tieni[1:])]
    return [testa[j] for j in tieni], tenute


def scrivi_csv(percorso, testa, righe):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writerow(testa)
    w.writerows(righe)
    tmp = percorso + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    os.replace(tmp, percorso)


def main():
    os.makedirs(CARTELLA, exist_ok=True)
    p_info = os.path.join(CARTELLA, "aggiornamento.json")
    info = {}
    if os.path.exists(p_info):
        with open(p_info, encoding="utf-8") as f:
            info = json.load(f)
    adesso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    errori = []
    for t in TAVOLE:
        cod = t["codice"]
        print(f"{cod}: scarico…", flush=True)
        try:
            nome, testo = leggi_csv_zip(scarica(URL.format(codice=cod)))
            testa, righe = filtra_serie(testo, t["serie"]) if "serie" in t else filtra(testo, t["filtri"])
            if not righe:
                raise RuntimeError("nessuna riga per i territori richiesti")
            i_data = testa.index("DATA_OSS")
            ultimo = max(r[i_data] for r in righe).replace("/", "-")   # le serie storiche scrivono 2024/12/31
            prima = info.get(cod, {}).get("ultimo_dato")
            if prima and ultimo < prima:
                raise RuntimeError(f"i dati scaricati arrivano al {ultimo}, quelli salvati al {prima}: tengo i vecchi")
            scrivi_csv(os.path.join(CARTELLA, f"{cod}.csv"), testa, righe)
            info[cod] = {"nome": t["nome"], "file_banca_italia": nome, "scaricato": adesso,
                         "ultimo_dato": ultimo, "righe": len(righe), "filtri": t.get("filtri") or {"territori": t["serie"]}}
            print(f"{cod}: {len(righe)} righe, ultimo dato {ultimo}", flush=True)
        except Exception as e:
            errori.append(f"{cod}: {e}")
            print(f"{cod}: ERRORE {e}", flush=True)
    info["controllato"] = adesso
    with open(p_info, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
        f.write("\n")
    if errori:
        print("\n".join(errori), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
