"""Nettoie la base d'agences (data/agences_*.csv) et la classe en tiers A / B / C.

Étapes (dans cet ordre, le premier motif trouvé exclut la ligne) :
  1. doublons de SIREN
  2. NON DIFFUSIBLE et sociétés en liquidation (un dirigeant "Liquidateur")
  3. catégories juridiques non retenues (on garde uniquement 1xxx = entrepreneur
     individuel et 5xxx = sociétés commerciales ; 1700 = agent commercial exclu)
  4. réseaux et franchises (mots-clés sur nom + enseigne)
  5. mandataires (mots-clés sur nom + enseigne)
  6. domiciliation : société dont l'adresse est partagée par 3 sociétés ou plus

Tiers des lignes gardées :
  A = sociétés (SARL, SAS, SA, SNC...)           -> cible prioritaire
  B = entrepreneurs individuels AVEC enseigne     -> à vérifier
  C = entrepreneurs individuels sans enseigne     -> mis de côté

Sorties :
  export/agences_tier_A.csv      sociétés (cible prioritaire), 
  export/agences_tier_B.csv      entrepreneurs individuels avec enseigne
  export/agences_tier_C.csv      entrepreneurs individuels sans enseigne (de côté)
                                 (chaque ligne : tier, requete_places)
  export/agences_exclues.csv     lignes exclues + motif_exclusion (pour auditer)
  export/noms_a_verifier.csv     noms répétés non détectés : réseaux possibles
  export/echantillon_test.csv    (si --deps) échantillon tier A pour le test
  rapports/nettoyage.md          bilan chiffré

Usage : python nettoyer_agences.py
        python nettoyer_agences.py --deps 75,06,13 --n 1000
        python nettoyer_agences.py --seuil 8 --seuil-adresse 4

Les listes de mots-clés viennent de analyse_agences.py ; ajoutez les vôtres dans
RESEAUX_EXTRA / MANDATAIRES_EXTRA ci-dessous.
"""
import argparse
import csv
import os
import random
from collections import Counter, defaultdict
from datetime import date

from analyse_agences import (MANDATAIRES, RESEAUX, charger, cle_nom, compiler,
                             norm, pct, tableau, trouver)

RESEAUX_EXTRA = ["SOLVIMO", "COTE PARTICULIERS", "SIXIEME AVENUE"]
MANDATAIRES_EXTRA = []

DOSSIER_EXPORT = "export"
RAPPORT = "rapports/nettoyage.md"

RX_RESEAUX = compiler(RESEAUX + RESEAUX_EXTRA)
RX_MANDATAIRES = compiler(MANDATAIRES + MANDATAIRES_EXTRA)


def motif_exclusion(l):
    texte = norm(f"{l.get('nom', '')} {l.get('enseigne', '')}")
    if "NON DIFFUSIBLE" in texte or (l.get("nom") or "").strip() == "[ND]":
        return "non diffusible"
    if "Liquidateur" in (l.get("gerants") or ""):
        return "en liquidation"
    cat = (l.get("categorie_juridique") or "").strip()
    if cat == "1700":
        return "agent commercial (cat. 1700)"
    if cat[:1] not in ("1", "5"):
        return f"catégorie non retenue : {cat or 'vide'}"
    mot = trouver(texte, RX_RESEAUX)
    if mot:
        return f"réseau : {mot}"
    mot = trouver(texte, RX_MANDATAIRES)
    if mot:
        return f"mandataire : {mot}"
    return ""


def tier(l):
    if (l.get("categorie_juridique") or "").strip()[:1] == "5":
        return "A"
    return "B" if (l.get("enseigne") or "").strip() else "C"


def cle_adresse(l):
    adr = norm(l.get("adresse") or "")
    if not adr or "NON DIFFUSIBLE" in adr:
        return ""
    return norm(f"{adr} {l.get('code_postal') or ''}")


def requete_places(l):
    base = (l.get("enseigne") or "").split("|")[0].strip() or (l.get("nom") or "").strip()
    lieu = " ".join(x for x in (l.get("code_postal"), l.get("ville")) if x)
    return f"{base} {lieu} agence immobilière".strip()


def ecrire(chemin, colonnes, lignes):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=colonnes, extrasaction="ignore")
        w.writeheader()
        w.writerows(lignes)
    print(f"  {chemin} : {len(lignes)} lignes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seuil", type=int, default=5,
                    help="un nom présent N fois ou plus est listé à vérifier")
    ap.add_argument("--seuil-adresse", type=int, default=3,
                    help="société exclue si son adresse est partagée par N sociétés ou plus")
    ap.add_argument("--deps", default="",
                    help="départements de l'échantillon test, ex. 75,06,13")
    ap.add_argument("--n", type=int, default=1000, help="taille de l'échantillon")
    a = ap.parse_args()

    lignes, colonnes = charger()
    if "categorie_juridique" not in colonnes:
        raise SystemExit("Colonne categorie_juridique absente : relancez "
                         "fetch_sirene.py avec --force.")
    lus = len(lignes)

    # 1. doublons de SIREN
    vus, uniques, doublons = set(), [], 0
    for l in lignes:
        if l["siren"] in vus:
            doublons += 1
            continue
        vus.add(l["siren"])
        uniques.append(l)

    # 2-5. exclusions
    gardees, exclues, motifs = [], [], Counter()
    for l in uniques:
        m = motif_exclusion(l)
        if m:
            l["motif_exclusion"] = m
            exclues.append(l)
            motifs[m.split(" : ")[0]] += 1
        else:
            l["tier"] = tier(l)
            l["requete_places"] = requete_places(l)
            gardees.append(l)

    # 6. domiciliation : adresse partagée par plusieurs sociétés
    adresses = Counter(cle_adresse(l) for l in gardees if l["tier"] == "A")
    apres = []
    for l in gardees:
        k = cle_adresse(l)
        if l["tier"] == "A" and k and adresses[k] >= a.seuil_adresse:
            l["motif_exclusion"] = "domiciliation : adresse partagée"
            exclues.append(l)
            motifs["domiciliation"] += 1
        else:
            apres.append(l)
    gardees = sorted(apres, key=lambda l: l["tier"])

    # noms répétés non détectés par les mots-clés
    noms, exemples, deps = Counter(), defaultdict(list), defaultdict(set)
    for l in gardees:
        k = cle_nom(l)
        if k:
            noms[k] += 1
            deps[k].add(l["_dep"])
            if len(exemples[k]) < 2:
                exemples[k].append(l["nom"])
    a_verifier = [{"nom": k, "occurrences": n, "nb_departements": len(deps[k]),
                   "exemples": " / ".join(exemples[k])}
                  for k, n in noms.most_common() if n >= a.seuil]

    # sorties
    print("Écriture :")
    sortie = colonnes + ["tier", "requete_places"]
    for t in "ABC":
        ecrire(f"{DOSSIER_EXPORT}/agences_tier_{t}.csv", sortie,
               [l for l in gardees if l["tier"] == t])
    ecrire(f"{DOSSIER_EXPORT}/agences_exclues.csv", colonnes + ["motif_exclusion"], exclues)
    ecrire(f"{DOSSIER_EXPORT}/noms_a_verifier.csv",
           ["nom", "occurrences", "nb_departements", "exemples"], a_verifier)

    echantillon = []
    if a.deps:
        liste = [d.strip().upper() for d in a.deps.split(",") if d.strip()]
        part = max(1, a.n // len(liste))
        rnd = random.Random(42)
        for d in liste:
            cand = [l for l in gardees if l["tier"] == "A" and l["_dep"] == d]
            rnd.shuffle(cand)                       # tirage aléatoire reproductible
            echantillon += cand[:part]
        ecrire(f"{DOSSIER_EXPORT}/echantillon_test.csv", sortie, echantillon)

    # rapport
    par_tier = Counter(l["tier"] for l in gardees)
    R = ["# Nettoyage des agences", f"_Généré le {date.today().isoformat()}_", "",
         "## Bilan", ""]
    rows = [("Lignes lues", lus, "")]
    rows.append(("Doublons de SIREN", f"-{doublons}", ""))
    for m, n in motifs.most_common():
        rows.append((m, f"-{n}", pct(n, lus)))
    rows.append(("**Lignes gardées**", f"**{len(gardees)}**", pct(len(gardees), lus)))
    R += tableau(["Étape", "Lignes", "Part"], rows) + [""]
    R += ["## Tiers", ""]
    R += tableau(["Tier", "Lignes", "Part"],
                 [(t, par_tier[t], pct(par_tier[t], len(gardees))) for t in "ABC"]) + [""]
    for titre, prefixe, nb in (("Catégories exclues (top 15)", "catégorie non retenue", 15),
                               ("Réseaux exclus (top 15)", "réseau", 15),
                               ("Mandataires exclus (top 10)", "mandataire", 10)):
        c = Counter(l["motif_exclusion"] for l in exclues
                    if l["motif_exclusion"].startswith(prefixe))
        if c:
            R += [f"## {titre}", ""]
            R += tableau(["Motif", "Lignes"], c.most_common(nb)) + [""]
    top_dep = Counter(l["_dep"] for l in gardees if l["tier"] == "A").most_common(15)
    R += ["## Tier A : top 15 départements", ""]
    R += tableau(["Dép.", "Lignes"], top_dep) + [""]
    R += [f"**{len(a_verifier)}** noms répétés >= {a.seuil} fois dans "
          "`export/noms_a_verifier.csv` : les vrais réseaux sont à ajouter à "
          "RESEAUX_EXTRA puis relancer.", ""]
    if echantillon:
        R += [f"Échantillon test : **{len(echantillon)}** lignes "
              f"({a.deps}) dans `export/echantillon_test.csv`.", ""]
    texte = "\n".join(R)
    os.makedirs(os.path.dirname(RAPPORT), exist_ok=True)
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print("\n" + texte)


if __name__ == "__main__":
    main()
  
