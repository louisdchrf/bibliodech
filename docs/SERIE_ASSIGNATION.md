# Assignation d'un livre à une série — Flux complet

## Vue d'ensemble

Quand un ISBN est scanné, l'assignation à une série se fait en deux temps :
1. **Immédiatement** lors de l'enrichissement en background (~1–5s après le scan)
2. **En différé** via des tâches planifiées si l'étape 1 a échoué

---

## Étape 1 — Réponse immédiate (< 100ms)

Le livre est créé en DB avec `title = isbn`, `enrichment_status = "pending"`. L'interface affiche un spinner. L'enrichissement est lancé **en background** — la réponse HTTP revient immédiatement.

---

## Étape 2 — Lookup en parallèle (~1–5 secondes)

`_enrich_book` appelle `lookup_isbn` qui interroge **toutes les sources actives en simultané** :

| Source | Timeout | Ce qu'elle retourne pour la série |
|---|---|---|
| BnF (Dublin Core) | 5s | Titre, auteurs — pas la série directement |
| BnF (UNIMARC) | 15s | **Champ 225** `$a` (nom) + `$v` (numéro de tome) — source la plus fiable |
| SUDOC | 5s | Série si indiquée dans la notice |
| Decitre | 10s | Rarement la série |
| Google Books | 5s | `seriesInfo.shortSeriesBookTitle` + `bookDisplayNumber` — peu renseigné pour les livres FR |
| Open Library | 5s | Rarement |

La BnF fait **deux requêtes** : d'abord Dublin Core (titre/auteurs), puis UNIMARC (série). C'est donc souvent la plus longue mais la plus précise.

---

## Étape 3 — Fusion des résultats

Les sources sont fusionnées avec cette **priorité pour la série** :

```
BnF > Decitre > Open Library > Google Books > SUDOC > ISBNdb
```

Si aucune source n'a trouvé de série, le code tente une **détection depuis le titre** via regex sur des patterns comme `"Lucky Luke T.3"`, `"Astérix tome 3"`, `"Blake et Mortimer, tome 5"`.

---

## Étape 4 — Application en DB

C'est ici que tout se joue. 4 scénarios possibles.

### Scénario A — La BnF retourne une série ✅
Cas normal pour les BD et romans de série français.

Exemple : `9782266245272` → BnF retourne `[225] $a = "Time riders"`, `$v = "2"`

1. `series_name = "Time riders"`, `series_position = 2.0`
2. Recherche en DB d'une série dont `_norm(name) == _norm("Time riders")` (insensible à la casse et aux accents)
3. **Si elle existe** → `book.series_id = series.id`, `book.series_position = 2.0`
4. **Si elle n'existe pas** → création automatique de la série, puis assignation

**Durée totale : ~2–4 secondes**

---

### Scénario B — La BnF a deux champs 225 (série + collection éditeur) ⚠️

Exemple : `9782266245272` avait deux entrées 225 :
- `$a = "Pocket jeunesse"`, `$v = "J2599"` (collection éditeur)
- `$a = "Time riders"`, `$v = "2"` (vraie série)

Le code collecte tous les champs 225 et **préfère celui dont `$v` est un entier pur < 10000** (numéro de tome réel). Si aucun champ n'a d'entier pur, il prend le premier.

**Durée totale : ~2–4 secondes**

---

### Scénario C — Aucune source ne connaît la série, mais elle est dans le titre 🔍

Exemple : `"Lucky Luke T.3 — Billy the Kid"`

Le regex `_extract_series_position` détecte `"Lucky Luke"` comme série et `3` comme position. Moins fiable — peut extraire un faux nom si le titre est ambigu.

**Durée totale : ~1–3 secondes**

---

### Scénario D — Introuvable partout 🚫

Toutes les sources renvoient null. `enrichment_status = "not_found"`. Aucune série assignée. Saisie manuelle possible, ou relance via ré-enrichissement.

**Durée totale : jusqu'à ~15 secondes** (attend l'expiration de tous les timeouts)

---

## Ce qui n'est PAS fait lors du scan

Le scan n'assigne pas la position si le livre a déjà une `series_id` mais pas de `series_position` — corrigé le 2026-06-26 : si `book.series_position is None`, la position trouvée lors de l'enrichissement est désormais appliquée même si la série était déjà assignée.

---

## Les tâches de rattrapage (en différé)

Si un livre passe au travers lors du scan, 3 tâches peuvent corriger ça a posteriori depuis Paramètres > Tâches :

| Tâche | Ce qu'elle fait | Durée estimée |
|---|---|---|
| `bnf-series` | Pour chaque série existante, cherche sur BnF tous les tomes et rattache ceux qui sont en bibliothèque (3 passes : position → ISBN 10↔13 → titre normalisé) | ~1–3 min selon nb de séries |
| `detect-series` | Comparaison titre/auteurs entre livres sans série — détecte les séries par ressemblance | ~10–30s |
| `sudoc-series` | Interroge le SUDOC pour chaque livre sans série | ~1–5 min selon nb de livres |

---

## Résumé visuel

```
Scan ISBN
    │
    ├─ Livre créé en DB (< 100ms) → réponse immédiate "pending"
    │
    └─ Background : lookup en parallèle (~1–5s)
            │
            ├─ BnF UNIMARC retourne série → assignée immédiatement ✅
            ├─ Google Books / SUDOC retourne série → assignée ✅
            ├─ Série détectée dans le titre → assignée (moins fiable) ⚠️
            └─ Rien trouvé → pas de série ❌
                    │
                    └─ Tâches planifiées (bnf-series, detect-series, sudoc-series)
                            → rattrapage depuis Paramètres > Tâches
```

---

## Fichiers concernés

| Fichier | Rôle dans ce flux |
|---|---|
| `app/routers/scan.py` | `scan_isbn` (endpoint), `_enrich_book` (background), application série en DB |
| `app/lookup.py` | `lookup_isbn`, `_lookup_bnf`, fusion multi-sources, priorité série, détection depuis le titre |
| `app/routers/series.py` | `_bnf_series_logic`, `_detect`, `_sudoc_detect` (tâches de rattrapage) |
