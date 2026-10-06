"""Compte les agences dont le titre de la page Pappers est de la forme :

    # NOM (ENSEIGNE)        ex. # INFINE CONSEILS (CENTURY 21 RASPAIL)

Lit pappers/<nom>__<siren>.jsonl (titre « # ... » dans raw_content) et écrit :
    export/agences_avec_enseigne.csv   siren, nom, enseigne, titre_pappers

Par défaut, ne compte que les SIREN présents dans export/agences_tier_A.csv.
Si ce fichier est absent (ou avec --tout), toutes les pages Pappers sont comptées.

Usage : python compter_enseignes.py
Uniquement la bibliothèque standard.
"""
import argparse
import csv
import json
import re
from pathlib import Path

TITRE = re.compile(r"^# (.+?)\s*$", re.M)  # « # » + espace : ne capte pas « ### Pappers IA »
ENSEIGNE = re.compile(r"^(?P<nom>.+?)\s*\((?P<enseigne>[^()]+)\)\s*$")


def norm(siren):
    chiffres = re.sub(r"\D", "", str(siren or ""))
    return chiffres.zfill(9) if chiffres else ""


def sirens_csv(chemin):
    with open(chemin, newline="", encoding="utf-8-sig") as f:
        return {norm(l["siren"]) for l in csv.DictReader(f)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pappers", default="pappers")
    ap.add_argument("--csv", default="export/agences_tier_A.csv")
    ap.add_argument("--sortie", default="export")
    ap.add_argument("--tout", action="store_true", help="compter toutes les pages, pas seulement le tier A")
    a = ap.parse_args()

    filtre = None
    if not a.tout and Path(a.csv).exists():
        filtre = sirens_csv(a.csv)
        print(f"Périmètre : {len(filtre)} agences de {a.csv}")
    else:
        print("Périmètre : toutes les pages Pappers")

    lus = sans_titre = illisibles = 0
    trouvees = []
    for p in Path(a.pappers).glob("*.jsonl"):
        if not re.search(r"_(\d{9})\.jsonl$", p.name):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.loads(f.readline())
            siren, contenu = norm(d["siren"]), d["raw_content"]
        except (ValueError, KeyError, OSError):
            illisibles += 1
            continue
        if filtre is not None and siren not in filtre:
            continue
        lus += 1
        m = TITRE.search(contenu)
        if not m:
            sans_titre += 1
            continue
        titre = m.group(1)
        e = ENSEIGNE.match(titre)
        if e:
            trouvees.append({"siren": siren, "nom": e["nom"], "enseigne": e["enseigne"],
                             "titre_pappers": titre})

    sortie = Path(a.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    dest = sortie / "agences_avec_enseigne.csv"
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["siren", "nom", "enseigne", "titre_pappers"])
        w.writeheader()
        w.writerows(trouvees)

    part = 100 * len(trouvees) / lus if lus else 0
    print(f"Pages lues : {lus} (illisibles : {illisibles}, sans titre repérable : {sans_titre})")
    print(f"Avec enseigne entre parenthèses : {len(trouvees)} ({part:.1f} %)")
    print(f"Écrit : {dest}")


if __name__ == "__main__":
    main()
