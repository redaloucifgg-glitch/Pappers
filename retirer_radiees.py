"""Retire du tier A les agences radiées du RCS (d'après les pages Pappers) et remplace
les noms du CSV par ceux présents dans les pages Pappers.

Lit pappers/<nom>__<siren>.jsonl, repère les entreprises dont le statut RCS est
RADIÉ (même règle que analyser_pappers.py), puis écrit :
    export/agences_tier_A_actives.csv   le tier A sans les radiées
    export/agences_tier_A_radiees.csv   les lignes retirées, pour contrôle

Nom : le titre de la page Pappers (« # ... » dans raw_content) remplace la colonne `nom`,
tel quel, enseigne entre parenthèses comprise (aucune séparation).
    ex. INFINE CONSEILS (CENTURY 21 RASPAIL)

Une agence absente de pappers/ ou sans statut / titre lisible est GARDÉE telle quelle
(son nom d'origine est conservé).

Usage : python retirer_radiees.py
Uniquement la bibliothèque standard (+ analyser_pappers.py à côté).
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

from analyser_pappers import extraire

TITRE = re.compile(r"^# (.+?)\s*$", re.M)  # « # » + espace : ne capte pas « ### Pappers IA »


def norm(siren):
    """SIREN sur 9 chiffres (zéros de tête restaurés)."""
    chiffres = re.sub(r"\D", "", str(siren or ""))
    return chiffres.zfill(9) if chiffres else ""


def nom_pappers(contenu):
    """Titre de la page Pappers, ou None s'il est introuvable."""
    m = TITRE.search(contenu)
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pappers", default="pappers")
    ap.add_argument("--csv", default="export/agences_tier_A.csv")
    ap.add_argument("--sortie", default="export")
    ap.add_argument("--colonne-nom", default="nom")
    a = ap.parse_args()

    if not Path(a.csv).exists():
        sys.exit(f"Fichier introuvable : {a.csv}")
    radiees, noms, avec_page, lus, illisibles = set(), {}, set(), 0, 0
    for p in Path(a.pappers).glob("*.jsonl"):
        m = re.search(r"_(\d{9})\.jsonl$", p.name)
        if not m:
            continue
        avec_page.add(m.group(1))
        try:
            d = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
            siren, contenu = norm(d["siren"]), d["raw_content"]
        except (ValueError, KeyError, IndexError):
            illisibles += 1
            continue
        lus += 1
        if extraire(contenu)["radiee"]:
            radiees.add(siren)
        nom = nom_pappers(contenu)
        if nom:
            noms[siren] = nom

    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        lecteur = csv.DictReader(f)
        colonnes = list(lecteur.fieldnames or [])
        lignes = list(lecteur)
    if a.colonne_nom not in colonnes:
        sys.exit(f"Colonne « {a.colonne_nom} » absente de {a.csv} (colonnes : {', '.join(colonnes)})")

    renommees = avec_parenthese = 0
    for l in lignes:
        nom = noms.get(norm(l["siren"]))
        if nom:
            if l[a.colonne_nom] != nom:
                renommees += 1
            l[a.colonne_nom] = nom
            if nom.endswith(")"):
                avec_parenthese += 1

    gardees = [l for l in lignes if norm(l["siren"]) not in radiees]
    retirees = [l for l in lignes if norm(l["siren"]) in radiees]
    sans_page = len({norm(l["siren"]) for l in lignes} - avec_page)

    sortie = Path(a.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    for nom_f, rows in (("agences_tier_A_actives.csv", gardees),
                        ("agences_tier_A_radiees.csv", retirees)):
        with open(sortie / nom_f, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=colonnes)
            w.writeheader()
            w.writerows(rows)
    print(f"Pages Pappers lues : {lus} (illisibles : {illisibles})")
    print(f"Tier A : {len(lignes)} agences")
    print(f"  retirées (radiées) : {len(retirees)}")
    print(f"  gardées            : {len(gardees)}  (dont {sans_page} sans page Pappers)")
    print(f"Noms remplacés par ceux de Pappers : {renommees}")
    print(f"  dont avec une parenthèse (enseigne) : {avec_parenthese}")
    print(f"Écrit : {sortie}/agences_tier_A_actives.csv et agences_tier_A_radiees.csv")


if __name__ == "__main__":
    main()
