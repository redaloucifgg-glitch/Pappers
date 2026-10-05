"""Compte et analyse les CSV produits par fetch_sirene.py (data/agences_XX.csv).

Génère un rapport Markdown (rapports/analyse.md) avec :
  - le total et la répartition par département (+ départements manquants)
  - la structure des données : taux de remplissage de chaque colonne, doublons
  - la répartition par catégorie juridique
  - la détection des réseaux et des mandataires (mots-clés + catégorie 1000)
  - les noms répétés (réseaux probables absents de la liste de mots-clés)
  - l'ancienneté et le nombre de dirigeants
  - la segmentation finale : réseau / mandataire / indépendant

Usage : python analyse_agences.py
        python analyse_agences.py --seuil 8      (nom répété >= 8 fois = réseau probable)
        python analyse_agences.py --export       (écrit aussi export/agences_segmentees.csv)

Uniquement la bibliothèque standard : aucune installation nécessaire.
"""
import csv
import glob
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date

DOSSIER = "data"
RAPPORT = "rapports/analyse.md"
EXPORT = "export/agences_segmentees.csv"
SEUIL = 5

DEPARTEMENTS = (
    [f"{i:02d}" for i in range(1, 96) if i != 20]
    + ["2A", "2B", "971", "972", "973", "974", "976"]
)

# Libellés des catégories juridiques les plus courantes (code INSEE)
CATEGORIES = {
    "1000": "Entrepreneur individuel",
    "5202": "Société en nom collectif (SNC)",
    "5306": "Société en commandite simple",
    "5385": "Société d'exercice libéral (SELARL)",
    "5410": "SARL unipersonnelle (ancien code)",
    "5498": "EURL",
    "5499": "SARL",
    "5505": "SA à conseil d'administration",
    "5510": "SA à conseil d'administration",
    "5599": "SA (autre)",
    "5710": "SAS",
    "5720": "SASU",
    "5800": "Société coopérative",
    "6540": "Société civile immobilière (SCI)",
    "6599": "Société civile (autre)",
    "9220": "Association déclarée",
    "9210": "Association non déclarée",
    "9900": "Autre personne morale de droit privé",
}

# Réseaux / franchises : à exclure de la cible
RESEAUX = [
    "CENTURY 21", "ORPI", "LAFORET", "GUY HOQUET", "STEPHANE PLAZA", "PLAZA",
    "ERA IMMOBILIER", "ERA", "FONCIA", "NEXITY", "SQUARE HABITAT", "CITYA",
    "AVIS IMMOBILIER", "HUMAN IMMOBILIER", "L ADRESSE", "TEMPLIERS", "BARNES",
    "SOTHEBY", "ENGEL", "KELLER WILLIAMS", "NESTENN", "IMMO DE FRANCE",
    "SERGIC", "ACTION LOGEMENT", "ARTHURIMMO", "ARTHUR IMMO", "CABINET BEDIN",
    "ORALIA", "LOISELET", "DAIGREMONT", "CENTURY", "STOP IMMOBILIER",
    "AGENCE EN DIRECT", "LES AGENCES DU PAYS", "CAFPI", "CAMPUS IMMOBILIER",
]

# Mandataires / réseaux de mandataires : à exclure de la cible
MANDATAIRES = [
    "IAD", "SAFTI", "CAPIFRANCE", "OPTIMHOME", "BSK", "MEGAGENCE",
    "PROPRIETES PRIVEES", "EFFICITY", "3G IMMO", "EXPERTIMO", "KW FRANCE",
    "MANDATAIRE", "AGENT COMMERCIAL", "REZOXPERT", "IMMO FACILE",
    "MON COURTIER EN IMMOBILIER", "PROPRIETES PARISIENNES", "ECOMIAM",
    "LA GARANTIE IMMOBILIERE", "SWISS LIFE IMMOBILIER",
]

FORMES = {"SARL", "SAS", "SASU", "EURL", "SA", "SCI", "SNC", "SELARL", "AGENCE",
          "IMMOBILIER", "IMMOBILIERE", "IMMO", "CABINET", "GROUPE", "LE", "LA",
          "LES", "DE", "DU", "DES", "ET"}


def norm(texte):
    t = unicodedata.normalize("NFKD", texte or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", " ", t.upper()).strip()


def compiler(mots):
    return [(m, re.compile(r"(?<![A-Z0-9])" + re.escape(norm(m)) + r"(?![A-Z0-9])"))
            for m in mots]


RE_RESEAUX = compiler(RESEAUX)
RE_MANDATAIRES = compiler(MANDATAIRES)


def trouver(texte, regexes):
    for mot, rx in regexes:
        if rx.search(texte):
            return mot
    return None


def cle_nom(ligne):
    """Nom 'canonique' pour repérer les noms qui se répètent."""
    base = (ligne.get("enseigne") or "").split("|")[0] or ligne.get("nom") or ""
    mots = [m for m in norm(base).split() if m not in FORMES]
    return " ".join(mots)


def charger():
    fichiers = sorted(glob.glob(os.path.join(DOSSIER, "agences_*.csv")))
    if not fichiers:
        sys.exit(f"Aucun fichier {DOSSIER}/agences_*.csv trouvé. "
                 "Lancez d'abord fetch_sirene.py.")
    lignes, colonnes = [], []
    for f in fichiers:
        dep = os.path.basename(f)[len("agences_"):-len(".csv")].upper()
        with open(f, newline="", encoding="utf-8") as fh:
            lecteur = csv.DictReader(fh)
            colonnes = lecteur.fieldnames or colonnes
            for row in lecteur:
                row["_dep"] = dep
                lignes.append(row)
    return lignes, colonnes


def tableau(entetes, rows):
    out = ["| " + " | ".join(entetes) + " |",
           "|" + "|".join("---" for _ in entetes) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def pct(n, total):
    return f"{100 * n / total:.1f} %" if total else "-"


def segmenter(ligne, noms_repetes):
    texte = norm(f"{ligne.get('nom', '')} {ligne.get('enseigne', '')}")
    mot = trouver(texte, RE_RESEAUX)
    if mot:
        return "reseau", f"mot-clé : {mot}"
    mot = trouver(texte, RE_MANDATAIRES)
    if mot:
        return "mandataire", f"mot-clé : {mot}"
    if cle_nom(ligne) in noms_repetes:
        return "reseau_probable", f"nom répété : {cle_nom(ligne)}"
    if (ligne.get("categorie_juridique") or "").strip() == "1000":
        return "mandataire_probable", "entrepreneur individuel"
    return "independant", ""


def main():
    global SEUIL
    args = sys.argv[1:]
    if "--seuil" in args:
        SEUIL = int(args[args.index("--seuil") + 1])
    export = "--export" in args

    lignes, colonnes = charger()
    total = len(lignes)
    R = [f"# Analyse des agences immobilières",
         f"_Généré le {date.today().isoformat()} - {total} lignes lues_", ""]

    # 1. Départements
    par_dep = Counter(l["_dep"] for l in lignes)
    manquants = [d for d in DEPARTEMENTS if d not in par_dep]
    R += ["## 1. Répartition par département", "",
          f"**{total}** agences dans **{len(par_dep)}** départements.", ""]
    if manquants:
        R += [f"Départements sans fichier : {', '.join(manquants)}", ""]
    rows = [(d, par_dep[d], pct(par_dep[d], total))
            for d in sorted(par_dep, key=lambda x: -par_dep[x])]
    R += tableau(["Dép.", "Agences", "Part"], rows) + [""]

    # 2. Structure des données
    R += ["## 2. Structure des données", "",
          f"Colonnes : {', '.join(colonnes)}", ""]
    rows = []
    for c in colonnes:
        vides = sum(1 for l in lignes if not (l.get(c) or "").strip())
        rows.append((c, total - vides, pct(total - vides, total), vides))
    R += tableau(["Colonne", "Remplis", "Taux", "Vides"], rows) + [""]
    sirens = Counter(l.get("siren") for l in lignes if l.get("siren"))
    doublons = sum(1 for n in sirens.values() if n > 1)
    R += [f"SIREN en doublon : **{doublons}**", ""]
    if "categorie_juridique" not in colonnes:
        R += ["> La colonne `categorie_juridique` est absente : relancez "
              "fetch_sirene.py avec --force.", ""]

    # 3. Catégories juridiques
    cats = Counter((l.get("categorie_juridique") or "").strip() or "(vide)"
                   for l in lignes)
    R += ["## 3. Catégories juridiques", ""]
    rows = [(c, CATEGORIES.get(c, "autre"), n, pct(n, total))
            for c, n in cats.most_common(15)]
    R += tableau(["Code", "Libellé", "Agences", "Part"], rows) + [""]

    # 4. Noms répétés
    noms = Counter(cle_nom(l) for l in lignes)
    noms_repetes = {n for n, c in noms.items() if n and c >= SEUIL}
    R += [f"## 4. Noms qui se répètent (>= {SEUIL} fois)", "",
          "Réseaux probables : à vérifier puis à ajouter aux listes de mots-clés.", ""]
    rows = [(n, noms[n]) for n in sorted(noms_repetes, key=lambda x: -noms[x])[:40]]
    R += (tableau(["Nom", "Occurrences"], rows) if rows else ["Aucun."]) + [""]

    # 5. Segmentation
    segments, motifs = Counter(), defaultdict(Counter)
    for l in lignes:
        seg, motif = segmenter(l, noms_repetes)
        l["_segment"], l["_motif"] = seg, motif
        segments[seg] += 1
        if motif:
            motifs[seg][motif] += 1
    R += ["## 5. Segmentation", ""]
    ordre = ["independant", "mandataire_probable", "mandataire",
             "reseau_probable", "reseau"]
    rows = [(s, segments[s], pct(segments[s], total)) for s in ordre]
    R += tableau(["Segment", "Agences", "Part"], rows) + [""]
    R += [f"**Cible potentielle : {segments['independant']} agences indépendantes** "
          f"(+ {segments['mandataire_probable']} entrepreneurs individuels à "
          "examiner).", ""]
    for s in ("reseau", "mandataire"):
        if motifs[s]:
            R += [f"### Détail : {s}", ""]
            R += tableau(["Motif", "Agences"], motifs[s].most_common(25)) + [""]

    # 6. Ancienneté et dirigeants
    annee = date.today().year
    tranches = Counter()
    for l in lignes:
        a = (l.get("date_creation") or "")[:4]
        if not a.isdigit():
            tranches["inconnue"] += 1
        elif int(a) >= annee - 1:
            tranches["moins de 2 ans"] += 1
        elif int(a) >= annee - 5:
            tranches["2 à 5 ans"] += 1
        elif int(a) >= annee - 15:
            tranches["5 à 15 ans"] += 1
        else:
            tranches["plus de 15 ans"] += 1
    R += ["## 6. Ancienneté", ""]
    R += tableau(["Tranche", "Agences", "Part"],
                 [(t, n, pct(n, total)) for t, n in tranches.most_common()]) + [""]
    nb_dir = Counter()
    for l in lignes:
        g = (l.get("gerants") or "").strip()
        nb_dir[min(len(g.split(" | ")), 4) if g else 0] += 1
    R += ["## 7. Nombre de dirigeants renseignés", ""]
    R += tableau(["Dirigeants", "Agences"],
                 [("4 ou plus" if k == 4 else k, nb_dir[k]) for k in sorted(nb_dir)]) + [""]

    texte = "\n".join(R)
    os.makedirs(os.path.dirname(RAPPORT), exist_ok=True)
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print(texte)
    print(f"\nRapport écrit dans {RAPPORT}")

    if export:
        os.makedirs(os.path.dirname(EXPORT), exist_ok=True)
        sortie = colonnes + ["segment", "motif_segment"]
        with open(EXPORT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=sortie, extrasaction="ignore")
            w.writeheader()
            for l in lignes:
                l["segment"], l["motif_segment"] = l["_segment"], l["_motif"]
                w.writerow(l)
        print(f"Export écrit dans {EXPORT}")


if __name__ == "__main__":
    main()
