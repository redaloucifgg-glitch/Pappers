"""Analyse les JSONL Pappers (un fichier par agence) et en tire un CSV structuré.

Lit pappers/<nom>__<siren>.jsonl (sortie de pappers_extract.py) et écrit dans
pappers_analyse/ :
    rapport_pappers.md      couverture de chaque champ, cohérence, répartitions,
                            échantillon de lignes financières à vérifier
    agences_enrichies.csv   (seulement avec --csv) une ligne par agence : colonnes du
                            tier A + les infos extraites de la page Pappers

Infos extraites (quand elles sont présentes dans les 5 chunks) :
    forme juridique, capital, NAF, activité déclarée, effectif (texte + min/max +
    année), date de création, dirigeant, adresse, TVA, statut RCS, radiée,
    procédure collective, comptes confidentiels ou non, CA / résultat (meilleur
    effort, avec la ligne brute pour vérifier), contacts (téléphone, e-mail, site)
    ou "réservé aux utilisateurs connectés".

Contrôles de cohérence : le SIREN écrit dans la page doit être celui demandé, et la
date de création Pappers doit être celle de la base SIRENE.

Usage : python analyser_pappers.py            (rapport seul)
        python analyser_pappers.py --csv      (rapport + CSV enrichi)
        python analyser_pappers.py --voir 888640059   (affiche l'extraction d'un SIREN)

Uniquement la bibliothèque standard.
"""
import argparse
import csv
import json
import re
import statistics
import sys
import unicodedata
from collections import Counter
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path


# ------------------------------------------------------------------ utilitaires
def n(texte):
    """minuscules sans accents, pour comparer."""
    t = unicodedata.normalize("NFKD", texte or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


CELLULE = re.compile(r"\|\s*([^|\n]+?)\s*:\s*\|\s*([^|\n]*?)\s*(?=\||\n|$)")


def champs_tableaux(contenu):
    """Toutes les lignes '| Libellé : | valeur |' -> {libellé normalisé: valeur}."""
    champs = {}
    for m in CELLULE.finditer(contenu):
        cle = n(m.group(1)).strip(" :")
        val = m.group(2).strip()
        if cle not in champs or (not champs[cle] and val):
            champs[cle] = val
    return champs


def get(champs, *noms):
    for nom in noms:
        if champs.get(nom):
            return champs[nom]
    return ""


def vers_iso(texte):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", texte or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


MONTANT = re.compile(r"(-?\s*\d[\d\s]*(?:[.,]\d+)?)\s*(k\s?€|m\s?€|md\s?€|€)", re.I)


def vers_euros(texte, debut=0):
    """Premier montant d'un texte (à partir de `debut`), en euros (k€, M€, Md€). None si absent."""
    m = MONTANT.search(texte or "", debut)
    if not m:
        return None
    nombre = re.sub(r"\s", "", m.group(1)).replace(",", ".")
    try:
        valeur = float(nombre)
    except ValueError:
        return None
    unite = re.sub(r"\s", "", m.group(2).lower())
    return valeur * {"k€": 1e3, "m€": 1e6, "md€": 1e9}.get(unite, 1)


def parse_effectif(texte):
    """-> (min, max, année de la donnée). max None = 'et plus'."""
    t = n(texte)
    annee = re.search(r"donnee\s*(\d{4})", t)
    annee = int(annee.group(1)) if annee else None
    if re.search(r"non employeur|\b0\s*salari", t):
        return 0, 0, annee
    m = re.search(r"(\d[\d\s]*?)\s*(?:a|ou)\s*(\d[\d\s]*?)\s*salari", t)
    if m:
        return int(re.sub(r"\D", "", m.group(1))), int(re.sub(r"\D", "", m.group(2))), annee
    m = re.search(r"(\d[\d\s]*?)\s*salaries?\s*et\s*plus|plus\s*de\s*(\d[\d\s]*?)\s*salari", t)
    if m:
        return int(re.sub(r"\D", "", m.group(1) or m.group(2))), None, annee
    m = re.search(r"(\d[\d\s]*?)\s*salari", t)
    if m:
        v = int(re.sub(r"\D", "", m.group(1)))
        return v, v, annee
    return None, None, annee


EXCLURE_FINANCE = re.compile(r"inferieur|superieur|seuil|confidentialit|condition|article l", re.I)


def ligne_finance(contenu, libelles):
    """Première ligne qui parle de `libelles` et donne un montant juste après (hors texte
    légal du type 'chiffre d'affaires inférieur à 700 000 €'). Les chunks sont recollés par
    '[...]' : on coupe aussi à cet endroit."""
    for ligne in re.split(r"\n|\s\[\.\.\.\]\s", contenu):
        low = n(ligne)
        pos = [low.find(l) for l in libelles if l in low]
        if not pos:
            continue
        m = MONTANT.search(ligne, min(pos))
        if not m or EXCLURE_FINANCE.search(n(ligne[:m.end()])):
            continue
        return ligne.strip(), vers_euros(ligne, min(pos))
    return "", None


PROCEDURES = ("liquidation judiciaire", "redressement judiciaire", "procedure de sauvegarde",
              "procedure collective", "dissolution anticipee", "cessation des paiements")
RE_TEL = re.compile(r"(?:\+33|0)\s?[1-9](?:[\s.]?\d{2}){4}")
RE_MAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def extraire(contenu):
    c = champs_tableaux(contenu)
    low = n(contenu)
    forme = get(c, "forme juridique")
    eff_txt = get(c, "effectif")
    emin, emax, eannee = parse_effectif(eff_txt)
    naf = re.search(r"\b(\d{2}\.\d{2}[A-Z])\b", get(c, "code naf ou ape"))
    rcs = get(c, "inscription au rcs")
    siren_page = re.sub(r"\D", "", get(c, "siren"))

    ca_ligne, ca = ligne_finance(contenu, ("chiffre d'affaires", "chiffre d’affaires"))
    res_ligne, res = ligne_finance(contenu, ("resultat net", "resultat de l'exercice", "resultat"))
    annees = re.findall(r"\b(20\d{2})\b", ca_ligne)
    if ca is not None:
        comptes = "chiffres trouvés"
    elif "confidentialite totale" in low:
        comptes = "confidentialité totale"
    elif "confidentialite partielle" in low:
        comptes = "confidentialité partielle"
    else:
        comptes = "inconnu"

    tel_brut = get(c, "telephone")
    mail_brut = get(c, "email")
    site_brut = get(c, "sites internet", "site internet")
    reserve = any("reserve" in n(x) for x in (tel_brut, mail_brut, site_brut))
    tel = RE_TEL.search(tel_brut)
    mail = RE_MAIL.search(mail_brut)
    site = "" if "reserve" in n(site_brut) else site_brut

    return {
        "forme_juridique": forme,
        "capital_eur": vers_euros(get(c, "capital social")),
        "naf": naf.group(1) if naf else "",
        "activite_declaree": get(c, "activite principale declaree", "activite"),
        "effectif_texte": eff_txt, "effectif_min": emin, "effectif_max": emax,
        "effectif_annee": eannee,
        "pappers_date_creation": vers_iso(get(c, "creation")),
        "pappers_dirigeant": get(c, "dirigeant", "dirigeants"),
        "pappers_adresse": get(c, "adresse", "adresse complete"),
        "tva": get(c, "numero de tva"),
        "statut_rcs": rcs.split("(")[0].strip(),
        "radiee": bool(n(rcs).startswith("radi") or re.search(r"(entreprise|societe) radiee|radiee le", low)),
        "en_activite": bool(re.search(r"\ben activite\b", low)),
        "procedure": " / ".join(p for p in PROCEDURES if p in low),
        "comptes": comptes,
        "ca_eur": ca, "ca_annee": annees[0] if annees else "", "ca_ligne": ca_ligne[:150],
        "resultat_eur": res, "resultat_ligne": res_ligne[:150],
        "contact_reserve": reserve,
        "telephone": tel.group(0) if tel else "", "email": mail.group(0) if mail else "",
        "site_web": site,
        "siren_page": siren_page,
    }


# ------------------------------------------------------------------- traitement
COLONNES_EXTRAITES = ["forme_juridique", "capital_eur", "naf", "activite_declaree",
                      "effectif_texte", "effectif_min", "effectif_max", "effectif_annee",
                      "pappers_date_creation", "pappers_dirigeant", "pappers_adresse", "tva",
                      "statut_rcs", "radiee", "en_activite", "procedure", "comptes",
                      "ca_eur", "ca_annee", "ca_ligne", "resultat_eur", "resultat_ligne",
                      "contact_reserve", "telephone", "email", "site_web"]
COLONNES_CONTROLE = ["siren_coherent", "date_creation_coherente", "chars", "date_extraction"]


def tranche(emin, emax):
    if emin is None:
        return "inconnu"
    v = emax if emax is not None else emin
    if v == 0:
        return "0 salarié"
    for borne, nom in ((2, "1-2"), (5, "3-5"), (9, "6-9"), (19, "10-19"), (49, "20-49")):
        if v <= borne:
            return nom
    return "50 et plus"


def pct(x, total):
    return f"{100 * x / total:.1f} %" if total else "-"


def tableau(entetes, rows):
    return (["| " + " | ".join(entetes) + " |", "|" + "|".join("---" for _ in entetes) + "|"]
            + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows] + [""])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dossier", default="pappers")
    ap.add_argument("--sortie", default="pappers_analyse")
    ap.add_argument("--csv", action="store_true", help="écrit aussi agences_enrichies.csv")
    ap.add_argument("--voir", default="", help="SIREN à afficher (debug)")
    a = ap.parse_args()

    dossier = Path(a.dossier)
    fichiers = sorted(p for p in dossier.glob("*.jsonl") if re.search(r"_(\d{9})\.jsonl$", p.name))
    if not fichiers:
        sys.exit(f"Aucun fichier <nom>__<siren>.jsonl dans {dossier}/")

    if a.voir:
        for p in fichiers:
            if p.name.endswith(f"_{a.voir}.jsonl"):
                d = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
                print(json.dumps(extraire(d["raw_content"]), ensure_ascii=False, indent=1))
                return
        sys.exit("SIREN introuvable.")

    sortie = Path(a.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    total = illisibles = 0
    tailles, rempli = [], Counter()
    tr_eff, formes, statuts, comptes, annees_eff = Counter(), Counter(), Counter(), Counter(), Counter()
    incoherents_siren, incoherents_date, comparees = [], 0, 0
    radiees = procedures = contact_reserve = avec_contact = sans_info = eff3 = 0
    lignes_fin = []
    entetes = None

    sortie_csv = (open(sortie / "agences_enrichies.csv", "w", newline="", encoding="utf-8")
                  if a.csv else nullcontext())
    with sortie_csv as f:
        w = None
        for p in fichiers:
            try:
                d = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
                contenu = d["raw_content"]
            except (ValueError, KeyError, IndexError):
                illisibles += 1
                continue
            total += 1
            x = extraire(contenu)
            ag = d.get("agence") or {}
            siren = d.get("siren", "")
            coh = (x["siren_page"] == siren) if x["siren_page"] else None
            dc = ag.get("date_creation", "")
            dcoh = (x["pappers_date_creation"] == dc) if (x["pappers_date_creation"] and dc) else None
            ligne = dict(ag)
            ligne.update(x)
            ligne.update({"siren_coherent": coh, "date_creation_coherente": dcoh,
                          "chars": d.get("chars", len(contenu)),
                          "date_extraction": d.get("extracted_at", "")})
            if f is not None:
                if w is None:
                    entetes = list(ag.keys()) + [c for c in COLONNES_EXTRAITES if c not in ag] \
                        + COLONNES_CONTROLE
                    w = csv.DictWriter(f, fieldnames=entetes, extrasaction="ignore")
                    w.writeheader()
                w.writerow(ligne)

            # statistiques
            tailles.append(len(contenu))
            for champ in ("forme_juridique", "capital_eur", "naf", "activite_declaree",
                          "effectif_texte", "pappers_date_creation", "pappers_dirigeant",
                          "pappers_adresse", "tva", "statut_rcs"):
                if x[champ] not in ("", None):
                    rempli[champ] += 1
            if x["effectif_min"] is not None:
                rempli["effectif (chiffré)"] += 1
            if x["ca_eur"] is not None:
                rempli["chiffre d'affaires"] += 1
            if x["resultat_eur"] is not None:
                rempli["résultat"] += 1
            if not (x["forme_juridique"] or x["effectif_texte"] or x["pappers_date_creation"]):
                sans_info += 1
            t = tranche(x["effectif_min"], x["effectif_max"])
            tr_eff[t] += 1
            if t not in ("inconnu", "0 salarié", "1-2"):
                eff3 += 1
            if x["effectif_annee"]:
                annees_eff[x["effectif_annee"]] += 1
            formes[x["forme_juridique"].split(",")[0] or "(vide)"] += 1
            statuts[x["statut_rcs"] or "(vide)"] += 1
            comptes[x["comptes"]] += 1
            radiees += x["radiee"]
            procedures += bool(x["procedure"])
            if x["contact_reserve"]:
                contact_reserve += 1
            if x["telephone"] or x["email"] or x["site_web"]:
                avec_contact += 1
            if coh is False:
                incoherents_siren.append(siren)
            if dcoh is not None:
                comparees += 1
                incoherents_date += (dcoh is False)
            for l in (x["ca_ligne"], x["resultat_ligne"]):
                if l and len(lignes_fin) < 15 and l not in lignes_fin:
                    lignes_fin.append(l)

    R = ["# Analyse des pages Pappers", f"_Généré le {datetime.now().isoformat(timespec='seconds')}_", "",
         "## Fichiers", ""]
    R += tableau(["", "Nombre"], [
        ("Fichiers lus", total), ("Illisibles", illisibles),
        ("Sans aucune info exploitable (ni forme, ni effectif, ni création)", sans_info),
        ("Contenu : min / médiane / max (caractères)",
         f"{min(tailles)} / {int(statistics.median(tailles))} / {max(tailles)}" if tailles else "-"),
        ("Contenu < 1 000 caractères", sum(1 for t in tailles if t < 1000))])
    R += ["## Cohérence", ""]
    R += tableau(["Contrôle", "Résultat"], [
        ("SIREN écrit dans la page différent de celui demandé", len(incoherents_siren)),
        (f"Date de création Pappers différente de SIRENE (sur {comparees} comparées)", incoherents_date)])
    if incoherents_siren:
        R += ["Exemples de SIREN incohérents : " + ", ".join(incoherents_siren[:10]), ""]
    R += ["## Couverture des champs", ""]
    R += tableau(["Champ", "Renseigné", "Part"],
                 [(c, rempli[c], pct(rempli[c], total)) for c in (
                     "forme_juridique", "capital_eur", "naf", "activite_declaree", "effectif_texte",
                     "effectif (chiffré)", "pappers_date_creation", "pappers_dirigeant",
                     "pappers_adresse", "tva", "statut_rcs", "chiffre d'affaires", "résultat")])
    R += ["## Effectif", ""]
    ordre = ["0 salarié", "1-2", "3-5", "6-9", "10-19", "20-49", "50 et plus", "inconnu"]
    R += tableau(["Tranche", "Agences", "Part"], [(t, tr_eff[t], pct(tr_eff[t], total)) for t in ordre])
    R += [f"Agences à **3 salariés ou plus** : **{eff3}** ({pct(eff3, total)}). "
          "Rappel : l'effectif vient de l'INSEE, 0 ou « inconnu » ne veut pas dire zéro salarié.", ""]
    if annees_eff:
        R += ["Année de la donnée d'effectif : " + ", ".join(
            f"{a} ({n_})" for a, n_ in sorted(annees_eff.items(), reverse=True)[:4]), ""]
    R += ["## Forme juridique (top 8)", ""]
    R += tableau(["Forme", "Agences"], formes.most_common(8))
    R += ["## Statut et alertes", ""]
    R += tableau(["", "Agences"], [("Statut RCS : " + s, c) for s, c in statuts.most_common(4)]
                 + [("Marquées radiées", radiees), ("Mention de procédure collective / liquidation", procedures)])
    R += ["## Comptes", ""]
    R += tableau(["", "Agences", "Part"], [(c, k, pct(k, total)) for c, k in comptes.most_common()])
    R += ["## Contacts", ""]
    R += tableau(["", "Agences"], [("« Réservé aux utilisateurs connectés »", contact_reserve),
                                    ("Téléphone, e-mail ou site trouvé", avec_contact)])
    R += ["## Lignes financières trouvées (à vérifier)", "",
          "Extraction faite au mieux : le format des pages avec comptes publiés n'a pas été "
          "validé sur beaucoup d'exemples. Contrôlez que le montant retenu est le bon.", ""]
    R += [f"- `{l}`" for l in lignes_fin] if lignes_fin else ["Aucune ligne de chiffre d'affaires trouvée.", ""]
    texte = "\n".join(R)
    (sortie / "rapport_pappers.md").write_text(texte + "\n", encoding="utf-8")
    print(texte)
    print(f"\nÉcrit : {sortie}/rapport_pappers.md ({total} fichiers analysés)"
          + (f" et {sortie}/agences_enrichies.csv" if a.csv else ""))


if __name__ == "__main__":
    main()
