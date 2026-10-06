#!/usr/bin/env python3
"""Scarica le tavole della Banca d'Italia e salva in dati/ solo le righe che servono alla pagina.

Lo usa l'aggiornamento automatico su GitHub (.github/workflows/aggiorna-dati.yml, giorni 28-31 e 2 di ogni mese),
ma si può lanciare anche a mano:  python3 aggiorna_dati.py
Solo libreria standard di Python (niente da installare).

Per ogni tavola di TAVOLE:
  1. scarica lo ZIP dal servizio A2A della Base Dati Statistica (tutta la tavola, ~5 MB);
  2. tiene le righe dei territori e degli enti indicati (le colonne dipendono dalla tavola: il territorio è LOC_CTP
     per depositi e impieghi, SEDELEG_SOGG per le sofferenze);
  3. scrive dati/<CODICE>.csv (stesse colonne del CSV della Banca d'Italia) e aggiorna dati/aggiornamento.json.
Se per una tavola il download non riesce o i dati sembrano sbagliati (tavola cambiata, vuota, più vecchia di quella
già salvata) il suo file NON viene toccato e il problema viene scritto nel file di esito (--esito): le altre tavole
si aggiornano lo stesso. Il workflow legge l'esito e, solo se c'è un problema, apre una segnalazione chiara nel
repository (GitHub la manda per e-mail): vedi segnala_problemi.py. Se la Banca d'Italia non ha pubblicato dati nuovi
non è un problema: nessuna segnalazione.
Con la variabile d'ambiente PROVA_ERRORE=true si simula un problema (per provare l'e-mail).
"""
import argparse
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
    # (dal 06/10/2026 TSDPT140 al posto di TSPAG110: stesse colonne e stessi valori, ma TSPAG110 è ferma al 2024)
    {"codice": "TSDPT140", "nome": "ATM e POS - per provincia di sportello",
     "serie": TERRITORI},
]
# colonne che devono esserci in ogni tavola (oltre a quelle dei filtri)
OBBLIGATORIE = ["DATA_OSS", "ENTE_SEGN", "FENEC", "VALORE"]


class Problema(Exception):
    """Problema di una tavola. tipo: "rete" (la Banca d'Italia non risponde), "risposta" (risponde ma non con lo ZIP
    dei dati), "formato" (la tavola è cambiata: colonne o territori mancanti), "anomalia" (dati più vecchi di quelli
    salvati), "prova" (simulato con PROVA_ERRORE)."""
    def __init__(self, tipo, dettaglio):
        super().__init__(dettaglio)
        self.tipo = tipo


# pause fra i tentativi (secondi): un disservizio breve della Banca d'Italia non deve far partire l'e-mail.
# Se una tavola non si scarica nemmeno così, per le successive si fa un tentativo solo (il sito è probabilmente fermo).
PAUSE = [60, 180, 300]
TIMEOUT = 90


def scarica(url, pause):
    errore = None
    for n in range(len(pause) + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "report-credito (Camera di Commercio di Varese)"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # rete, 5xx, timeout
            errore = e
            print(f"  tentativo {n + 1} non riuscito: {e}", flush=True)
            if n < len(pause):
                time.sleep(pause[n])
    n = len(pause) + 1
    raise Problema("rete", f"il sito della Banca d'Italia non ha risposto ({n} {'tentativo' if n == 1 else 'tentativi'}): {errore}")


def leggi_csv_zip(dati):
    try:
        z = zipfile.ZipFile(io.BytesIO(dati))
    except zipfile.BadZipFile:
        inizio = dati[:200].decode("utf-8", "replace").strip().replace("\n", " ")
        raise Problema("risposta", f"la Banca d'Italia ha risposto, ma non con il file ZIP dei dati (inizio della risposta: «{inizio[:120]}»)")
    for nome in z.namelist():
        if nome.lower().endswith(".csv"):
            testo = z.read(nome).decode("utf-8-sig")
            if "DATA_OSS" in testo.split("\n", 1)[0]:
                return nome, testo
    raise Problema("formato", "nello ZIP non c'è il CSV dei dati (colonna DATA_OSS)")


def filtra(testo, filtri):
    """Restituisce (intestazione, righe tenute): tutte le colonne della tavola, nell'ordine del CSV originale."""
    righe = csv.reader(io.StringIO(testo), delimiter=";")
    testa = next(righe)
    mancanti = [c for c in OBBLIGATORIE + list(filtri) if c not in testa]
    if mancanti:
        raise Problema("formato", f"nel CSV mancano le colonne {', '.join(mancanti)} (la Banca d'Italia ha cambiato la tavola)")
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
        raise Problema("formato", "nel CSV manca la colonna DATA_OSS (la Banca d'Italia ha cambiato la tavola)")
    tieni = [testa.index("DATA_OSS")] + [j for j, c in enumerate(testa) if c.count(".") >= 4 and c.rsplit(".", 1)[1] in territori]
    if len(tieni) == 1:
        raise Problema("formato", "nella tavola non ci sono più le serie di Varese, Lombardia e Italia")
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
    arg = argparse.ArgumentParser(description="Scarica e filtra le tavole della Banca d'Italia")
    arg.add_argument("--esito", help="file JSON in cui scrivere l'esito (lo legge segnala_problemi.py)")
    esito_path = arg.parse_args().esito
    prova = os.environ.get("PROVA_ERRORE", "").lower() == "true"
    os.makedirs(CARTELLA, exist_ok=True)
    p_info = os.path.join(CARTELLA, "aggiornamento.json")
    info = {}
    if os.path.exists(p_info):
        with open(p_info, encoding="utf-8") as f:
            info = json.load(f)
    adesso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    problemi, tavole = [], []
    pause = PAUSE
    for t in TAVOLE:
        cod = t["codice"]
        prima = info.get(cod, {}).get("ultimo_dato")
        print(f"{cod}: scarico…", flush=True)
        try:
            if prova and cod == TAVOLE[-1]["codice"]:
                raise Problema("prova", "errore simulato per provare l'e-mail di avviso (nessun problema reale)")
            try:
                dati = scarica(URL.format(codice=cod), pause)
            except Problema:
                pause = []
                raise
            nome, testo = leggi_csv_zip(dati)
            testa, righe = filtra_serie(testo, t["serie"]) if "serie" in t else filtra(testo, t["filtri"])
            if not righe:
                raise Problema("formato", "nella tavola non ci sono più righe di Varese, Lombardia o Italia")
            i_data = testa.index("DATA_OSS")
            ultimo = max(r[i_data] for r in righe).replace("/", "-")   # le serie storiche scrivono 2024/12/31
            if prima and ultimo < prima:
                raise Problema("anomalia", f"i dati scaricati arrivano al {ultimo}, quelli già salvati al {prima}: tenuti quelli salvati")
            scrivi_csv(os.path.join(CARTELLA, f"{cod}.csv"), testa, righe)
            info[cod] = {"nome": t["nome"], "file_banca_italia": nome, "scaricato": adesso,
                         "ultimo_dato": ultimo, "righe": len(righe), "filtri": t.get("filtri") or {"territori": t["serie"]}}
            tavole.append({"tavola": cod, "nome": t["nome"], "ultimo_dato": ultimo, "prima": prima})
            print(f"{cod}: {len(righe)} righe, ultimo dato {ultimo}", flush=True)
        except Problema as e:
            problemi.append({"tavola": cod, "nome": t["nome"], "tipo": e.tipo, "dettaglio": str(e), "ultimo_dato_salvato": prima})
            print(f"{cod}: PROBLEMA ({e.tipo}) {e}", flush=True)
        except Exception as e:  # imprevisto: lo si segnala come tale
            problemi.append({"tavola": cod, "nome": t["nome"], "tipo": "imprevisto", "dettaglio": f"{type(e).__name__}: {e}", "ultimo_dato_salvato": prima})
            print(f"{cod}: ERRORE IMPREVISTO {type(e).__name__}: {e}", flush=True)
    info["controllato"] = adesso
    with open(p_info, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
        f.write("\n")
    if esito_path:
        with open(esito_path, "w", encoding="utf-8") as f:
            json.dump({"quando": adesso, "prova": prova, "problemi": problemi, "tavole": tavole}, f, ensure_ascii=False, indent=1)
    if problemi:
        print("\n".join(f"{p['tavola']}: {p['dettaglio']}" for p in problemi), file=sys.stderr)
        # senza file di esito (uso a mano) si termina con errore; con l'esito ci pensa segnala_problemi.py
        if not esito_path:
            sys.exit(1)


if __name__ == "__main__":
    main()
