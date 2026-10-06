#!/usr/bin/env python3
"""
Extraction Pappers (tier A) via Tavily Extract.

- URL      : https://www.pappers.fr/entreprise/<siren>
- Tavily   : extract_depth=basic, format=markdown, 5 chunks (chunks_per_source=5)
- Sortie   : pappers/<nom-agence>__<siren>.jsonl  (1 fichier / agence)
- Reprise  : pappers/_state/progress.jsonl (+ keys_state.json pour les crédits par clé)
- Rapport  : pappers/_report/analyse.md + echecs.csv

Clés : variable d'env TAVILY_API_KEYS (une clé par ligne, ou séparées par , ou espace).

Codes de sortie :
  0  = terminé (plus rien à traiter) ou --limit atteint
  10 = tranche de temps (--max-runtime) écoulée, il reste des agences
  2  = toutes les clés sont épuisées / invalides, il reste des agences
  1  = erreur de config
"""
import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sys
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = os.environ.get("TAVILY_API_URL", "https://api.tavily.com/extract")
PAPPERS_URL = "https://www.pappers.fr/entreprise/{siren}"
DEFAULT_QUERY = (
    "dirigeants, forme juridique, capital social, date de création, adresse du siège, "
    "activité NAF, effectif, chiffre d'affaires, résultat, établissements"
)
HTTP_RETRIES = 4
BLOCK_MARKERS = (
    "just a moment", "enable javascript and cookies", "attention required",
    "access denied", "captcha", "verify you are human", "checking your browser",
)
MAX_BATCH = 20  # limite Tavily : 20 URLs / requête


class AllKeysExhausted(Exception):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def slugify(name: str, maxlen=80) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return (s[:maxlen].strip("-")) or "agence"


def categorize(msg: str) -> str:
    m = (msg or "").lower()
    if "timeout" in m or "timed out" in m:
        return "timeout_page"
    if any(x in m for x in ("403", "forbidden", "blocked", "denied", "captcha", "cloudflare")):
        return "bloque_anti_bot"
    if "404" in m or "not found" in m:
        return "page_introuvable"
    if "robots" in m:
        return "robots_txt"
    if "429" in m or "rate" in m:
        return "rate_limit_cible"
    if any(x in m for x in ("ssl", "dns", "connect", "resolve")):
        return "erreur_reseau_cible"
    return "autre"


# --------------------------------------------------------------------------- clés
class KeyPool:
    """Rotation des clés + suivi des crédits (persisté, sans stocker les clés)."""

    def __init__(self, keys, state_path: Path, budget: int, max_cost: int, min_interval: float):
        self.path, self.budget, self.max_cost, self.min_interval = state_path, budget, max_cost, min_interval
        saved = {}
        if state_path.exists():
            try:
                saved = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                saved = {}
        self.keys = []
        for k in keys:
            kid = hashlib.sha256(k.encode()).hexdigest()[:8]
            s = saved.get(kid, {})
            self.keys.append({"key": k, "id": kid, "used": s.get("used", 0),
                              "dead": s.get("dead"), "busy": False, "cool": 0.0,
                              "used_run": 0})
        self.cv = threading.Condition()

    def _usable(self, k):
        return not k["dead"] and k["used"] + self.max_cost <= self.budget

    def live_count(self):
        with self.cv:
            return sum(1 for k in self.keys if self._usable(k))

    def acquire(self):
        with self.cv:
            while True:
                live = [k for k in self.keys if self._usable(k)]
                if not live:
                    return None
                now = time.time()
                avail = [k for k in live if not k["busy"] and k["cool"] <= now]
                if avail:
                    k = min(avail, key=lambda x: x["used"])
                    k["busy"] = True
                    return k
                self.cv.wait(timeout=1.0)

    def release(self, k, credits=0, dead=None, cooldown=None):
        with self.cv:
            k["busy"] = False
            k["used"] += credits or 0
            k["used_run"] += credits or 0
            if dead:
                k["dead"] = dead
            k["cool"] = time.time() + (self.min_interval if cooldown is None else cooldown)
            self._save()
            self.cv.notify_all()

    def _save(self):
        data = {k["id"]: {"used": k["used"], "dead": k["dead"], "updated": now_iso()} for k in self.keys}
        atomic_write(self.path, json.dumps(data, indent=1))

    def snapshot(self):
        with self.cv:
            return [(k["id"], k["used"], k["used_run"], k["dead"]) for k in self.keys]


# --------------------------------------------------------------------------- progression
class Progress:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.rec = {}
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        self.rec[r["siren"]] = r
                    except Exception:
                        continue
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("a", encoding="utf-8")

    def record(self, siren, status, attempts, reason=None, category=None, key_id=None):
        r = {"siren": siren, "status": status, "attempts": attempts, "reason": (reason or "")[:300],
             "category": category, "key": key_id, "ts": now_iso()}
        with self.lock:
            self.rec[siren] = r
            self.fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            self.fh.flush()

    def get(self, siren):
        return self.rec.get(siren)


# --------------------------------------------------------------------------- données
def load_rows(csv_path: Path):
    rows, seen = [], set()
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            s = re.sub(r"\D", "", r.get("siren") or "").zfill(9)
            if len(s) != 9 or s in seen:
                continue
            seen.add(s)
            r["siren"] = s
            rows.append(r)
    return rows


def existing_ok_from_files(out: Path):
    found = set()
    for p in out.glob("*__*.jsonl"):
        m = re.search(r"__(\d{9})\.jsonl$", p.name)
        if m:
            found.add(m.group(1))
    return found


def siren_from_url(url: str):
    m = re.search(r"(\d{9})(?:[/?#].*)?$", url or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------------- run
def run(args):
    out = Path(args.out)
    state_dir = out / "_state"
    keys = [k for k in re.split(r"[\s,;]+", os.environ.get("TAVILY_API_KEYS", "")) if k]
    keys = list(dict.fromkeys(keys))
    if not keys:
        log("ERREUR : TAVILY_API_KEYS vide.")
        return 1
    csv_path = Path(args.csv)
    if not csv_path.exists():
        log(f"ERREUR : CSV introuvable : {csv_path}")
        return 1

    rows = load_rows(csv_path)
    by_siren = {r["siren"]: r for r in rows}
    progress = Progress(state_dir / "progress.jsonl")
    ok_files = existing_ok_from_files(out)
    for s in ok_files:  # fichier écrit mais progress non mis à jour (crash)
        if not progress.get(s) or progress.get(s)["status"] != "ok":
            progress.record(s, "ok", (progress.get(s) or {}).get("attempts", 0) + 1, reason="recupere_depuis_fichier")

    bs = min(args.batch_size, MAX_BATCH)
    pool = KeyPool(keys, state_dir / "keys_state.json", args.key_budget, math.ceil(bs / 5), args.min_interval)
    log(f"{len(rows)} agences au CSV | {len(keys)} clés ({pool.live_count()} utilisables) | lot={bs}")

    start = time.time()
    deadline = start + args.max_runtime if args.max_runtime else None
    stop = threading.Event()
    stats = Counter()
    reasons = Counter()
    lock = threading.Lock()
    total_batches = [0, 0]  # [faits, prévus]

    def time_over():
        return deadline is not None and time.time() >= deadline

    def pending_list(first_pass_limit=0):
        pend = []
        for r in rows:
            rec = progress.get(r["siren"])
            if rec and rec["status"] == "ok":
                continue
            att = rec["attempts"] if rec else 0
            if att >= args.max_attempts:
                continue
            pend.append((att, r))
        pend.sort(key=lambda x: x[0])  # nouvelles agences d'abord, réessais ensuite
        pend = [r for _, r in pend]
        return pend[:first_pass_limit] if first_pass_limit else pend

    def write_agency(row, url, content):
        name = row.get("nom") or row.get("enseigne") or "agence"
        path = out / f"{slugify(name)}__{row['siren']}.jsonl"
        rec = {
            "siren": row["siren"],
            "nom": row.get("nom"),
            "url": url,
            "extracted_at": now_iso(),
            "tavily": {"extract_depth": "basic", "format": "markdown",
                       "chunks_per_source": args.chunks if args.query else None,
                       "query": args.query or None},
            "agence": {k: v for k, v in row.items() if v not in (None, "")},
            "chars": len(content),
            "raw_content": content,
        }
        atomic_write(path, json.dumps(rec, ensure_ascii=False) + "\n")

    def fail(siren, reason, category, key_id=None):
        prev = progress.get(siren)
        progress.record(siren, "failed", (prev["attempts"] if prev else 0) + 1, reason, category, key_id)
        with lock:
            stats["failed"] += 1
            reasons[category] += 1

    def process_batch(batch):
        if stop.is_set():
            return
        urls = [PAPPERS_URL.format(siren=r["siren"]) for r in batch]
        payload = {"urls": urls, "extract_depth": "basic", "format": "markdown",
                   "timeout": float(args.timeout), "include_images": False, "include_usage": True}
        if args.query:
            payload["query"] = args.query
            payload["chunks_per_source"] = args.chunks

        data, err, permanent, key_id = None, "inconnu", False, None
        for attempt in range(1, HTTP_RETRIES + 1):
            key = pool.acquire()
            if key is None:
                raise AllKeysExhausted()
            key_id = key["id"]
            credits, dead, cooldown, sc = 0, None, None, None
            try:
                resp = requests.post(API_URL, json=payload,
                                     headers={"Authorization": f"Bearer {key['key']}"},
                                     timeout=(10, args.timeout + 60))
                sc = resp.status_code
                if sc == 200:
                    data = resp.json()
                    credits = (data.get("usage") or {}).get("credits")
                    if credits is None:
                        credits = math.ceil(len(data.get("results", [])) / 5)
                elif sc in (401, 403):
                    dead, err = "cle_invalide", f"http_{sc}"
                elif sc in (432, 433):
                    dead, err = "quota_epuise", f"http_{sc}"
                elif sc == 429:
                    cooldown, err = 15 * attempt, "http_429"
                elif sc == 400:
                    err, permanent = f"http_400 {resp.text[:150]}", True
                else:
                    err = f"http_{sc}"
            except requests.Timeout:
                err = "timeout_http"
            except requests.RequestException as e:
                err = f"erreur_reseau:{type(e).__name__}"
            except ValueError:
                err = "json_invalide"
            finally:
                pool.release(key, credits, dead, cooldown)
            if data is not None or permanent:
                break
            if not dead:
                time.sleep(min(60, 2 ** attempt + random.random()))

        if data is None:
            for r in batch:
                fail(r["siren"], err, "erreur_api", key_id)
            return

        seen = set()
        for item in data.get("results", []):
            s = siren_from_url(item.get("url", ""))
            if s not in by_siren or s in seen:
                continue
            seen.add(s)
            content = (item.get("raw_content") or "").strip()
            low = content[:1500].lower()
            if any(m in low for m in BLOCK_MARKERS):
                fail(s, "page_de_blocage_detectee", "bloque_anti_bot", key_id)
            elif len(content) < args.min_chars:
                fail(s, f"contenu_trop_court({len(content)})", "contenu_vide", key_id)
            else:
                write_agency(by_siren[s], PAPPERS_URL.format(siren=s), content)
                prev = progress.get(s)
                progress.record(s, "ok", (prev["attempts"] if prev else 0) + 1, key_id=key_id)
                with lock:
                    stats["ok"] += 1
        for item in data.get("failed_results", []):
            s = siren_from_url(item.get("url", ""))
            if s in by_siren and s not in seen:
                seen.add(s)
                msg = str(item.get("error", "echec_sans_message"))
                fail(s, msg, categorize(msg), key_id)
        for r in batch:  # URL absente de la réponse
            if r["siren"] not in seen:
                fail(r["siren"], "absente_de_la_reponse", "autre", key_id)

    def run_pass(pending):
        batches = [pending[i:i + bs] for i in range(0, len(pending), bs)]
        total_batches[1] += len(batches)
        it, inflight = iter(batches), set()
        workers = max(1, min(args.workers, pool.live_count()))
        with ThreadPoolExecutor(workers) as ex:
            while True:
                while len(inflight) < workers * 2 and not stop.is_set() and not time_over():
                    b = next(it, None)
                    if b is None:
                        break
                    inflight.add(ex.submit(process_batch, b))
                if not inflight:
                    break
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for f in done:
                    try:
                        f.result()
                    except AllKeysExhausted:
                        stop.set()
                    total_batches[0] += 1
                log(f"lots {total_batches[0]}/{total_batches[1]} | run: ok={stats['ok']} ko={stats['failed']} "
                    f"| crédits run={sum(k[2] for k in pool.snapshot())} | clés actives={pool.live_count()}")

    first = True
    while not stop.is_set() and not time_over():
        pend = pending_list(args.limit if first else 0)
        if not pend:
            break
        log(f"{len(pend)} agences à traiter")
        run_pass(pend)
        if args.limit:
            break
        first = False

    remaining = len(pending_list())
    exhausted = stop.is_set()
    if exhausted and remaining:
        code = 2
    elif args.limit:
        code = 0
    elif remaining and time_over():
        code = 10
    else:
        code = 0
    with (state_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "start": datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds"),
            "end": now_iso(), "duration_s": round(time.time() - start),
            "ok": stats["ok"], "failed": stats["failed"], "batches": total_batches[0],
            "credits": sum(k[2] for k in pool.snapshot()), "remaining": remaining, "exit": code,
            "reasons": dict(reasons)}, ensure_ascii=False) + "\n")
    log(f"fin de run : ok={stats['ok']} ko={stats['failed']} restant={remaining} code={code}")
    return code


# --------------------------------------------------------------------------- rapport
HINTS = {
    "bloque_anti_bot": "Pappers (protection anti-bot) refuse l'accès. Tavily basic ne passe pas : "
                       "tester extract_depth=advanced sur un échantillon, ou passer par l'API officielle Pappers.",
    "timeout_page": "La page n'a pas répondu à temps. Augmenter --timeout (max 60 s) puis relancer (reprise automatique).",
    "page_introuvable": "SIREN absent de Pappers ou URL non résolue (entreprise radiée, etc.).",
    "contenu_vide": "Page récupérée mais quasi vide (rendu JS non terminé). Augmenter --timeout ou tester advanced.",
    "robots_txt": "Bloqué par robots.txt côté cible.",
    "rate_limit_cible": "Pappers limite le débit : baisser --workers, augmenter --min-interval.",
    "erreur_reseau_cible": "Erreur réseau/SSL/DNS côté cible, généralement transitoire.",
    "erreur_api": "Erreur côté API Tavily (HTTP 5xx, 429, réseau, clés). Voir la section clés.",
    "autre": "Cause non classée, voir les exemples de messages.",
}


def build_report(args):
    out = Path(args.out)
    state_dir, rep_dir = out / "_state", out / "_report"
    rows = load_rows(Path(args.csv))
    progress = Progress(state_dir / "progress.jsonl")
    total = len(rows)
    ok = [r for r in rows if (progress.get(r["siren"]) or {}).get("status") == "ok"]
    ko = [r for r in rows if (progress.get(r["siren"]) or {}).get("status") == "failed"]
    todo = total - len(ok) - len(ko)
    given_up = [r for r in ko if progress.get(r["siren"])["attempts"] >= args.max_attempts]

    cat = Counter(progress.get(r["siren"])["category"] for r in ko)
    examples = defaultdict(Counter)
    for r in ko:
        p = progress.get(r["siren"])
        examples[p["category"]][p["reason"]] += 1

    runs = []
    rp = state_dir / "runs.jsonl"
    if rp.exists():
        runs = [json.loads(l) for l in rp.read_text(encoding="utf-8").splitlines() if l.strip()]
    credits = sum(r.get("credits", 0) for r in runs)
    dur = sum(r.get("duration_s", 0) for r in runs)

    keys_state = {}
    kp = state_dir / "keys_state.json"
    if kp.exists():
        keys_state = json.loads(kp.read_text(encoding="utf-8"))

    pct = lambda n: f"{(100 * n / total):.1f} %" if total else "0 %"
    L = ["# Analyse extraction Pappers (Tavily, basic, markdown, 5 chunks)", "",
         f"_Généré le {now_iso()}_", "", "## Synthèse", "",
         "| | Agences | % |", "|---|---:|---:|",
         f"| Total CSV tier A | {total} | 100 % |",
         f"| Réussies | {len(ok)} | {pct(len(ok))} |",
         f"| Échecs | {len(ko)} | {pct(len(ko))} |",
         f"| dont abandonnées (≥ {args.max_attempts} essais) | {len(given_up)} | {pct(len(given_up))} |",
         f"| Pas encore traitées | {todo} | {pct(todo)} |", "",
         f"- Crédits consommés (tous runs) : **{credits}** (≈ {credits * 0.008:.2f} $ au tarif pay-as-you-go)",
         f"- Durée cumulée : {dur // 60} min sur {len(runs)} run(s)"]
    if len(ok):
        L.append(f"- Coût moyen : {credits / len(ok):.3f} crédit / agence réussie")
    L.append(f"- Crédits restants estimés pour finir : ≈ {math.ceil((todo + len(ko)) / 5)}")
    L += ["", "## Raisons des échecs", ""]
    if ko:
        L += ["| Raison | Nb | % des échecs | Exemple de message | Piste |", "|---|---:|---:|---|---|"]
        for c, n in cat.most_common():
            ex = examples[c].most_common(1)[0][0].replace("|", "/")[:80]
            L.append(f"| `{c}` | {n} | {100 * n / len(ko):.1f} % | {ex} | {HINTS.get(c, '')} |")
    else:
        L.append("Aucun échec.")
    L += ["", "## Clés Tavily", "", "| Clé (hash) | Crédits utilisés | État |", "|---|---:|---|"]
    for kid, s in keys_state.items():
        L.append(f"| `{kid}` | {s.get('used', 0)} | {s.get('dead') or 'ok'} |")
    if keys_state:
        L.append(f"\nTotal : {sum(s.get('used', 0) for s in keys_state.values())} crédits "
                 f"sur {len(keys_state)} clé(s) ; {sum(1 for s in keys_state.values() if s.get('dead'))} désactivée(s).")
    if runs:
        L += ["", "## Derniers runs", "", "| Début | Durée | OK | KO | Crédits | Restant |", "|---|---:|---:|---:|---:|---:|"]
        for r in runs[-8:]:
            L.append(f"| {r['start']} | {r['duration_s'] // 60} min | {r['ok']} | {r['failed']} | {r['credits']} | {r['remaining']} |")

    text = "\n".join(L) + "\n"
    atomic_write(rep_dir / "analyse.md", text)
    with (rep_dir / "echecs.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["siren", "nom", "url", "categorie", "raison", "essais"])
        for r in ko:
            p = progress.get(r["siren"])
            w.writerow([r["siren"], r.get("nom"), PAPPERS_URL.format(siren=r["siren"]),
                        p["category"], p["reason"], p["attempts"]])
    print(text)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/agences_tier_A.csv")
    ap.add_argument("--out", default="pappers")
    ap.add_argument("--batch-size", type=int, default=20, help="URLs par requête (max 20)")
    ap.add_argument("--workers", type=int, default=6, help="requêtes parallèles (1 clé / requête)")
    ap.add_argument("--timeout", type=float, default=30, help="timeout Tavily par page, en s (défaut Tavily basic = 10)")
    ap.add_argument("--chunks", type=int, default=5, help="chunks_per_source (1-5)")
    ap.add_argument("--query", default=DEFAULT_QUERY,
                    help="requête qui guide le choix des chunks ('' = pas de chunks, page entière)")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--max-runtime", type=int, default=0, help="secondes avant arrêt propre (0 = illimité)")
    ap.add_argument("--limit", type=int, default=0, help="test : traiter N agences seulement")
    ap.add_argument("--key-budget", type=int, default=1000, help="crédits max par clé")
    ap.add_argument("--min-interval", type=float, default=1.0, help="pause (s) entre 2 requêtes sur une même clé")
    ap.add_argument("--min-chars", type=int, default=150)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    args.chunks = max(1, min(5, args.chunks))
    args.timeout = max(1.0, min(60.0, args.timeout))
    if args.report_only:
        build_report(args)
        return 0
    code = run(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
