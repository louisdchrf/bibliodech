# Bibliodech — Documentation des fonctionnalités

## Architecture générale

```
FastAPI (Python)
├── app/main.py          — Routes HTML (pages Jinja2)
├── app/routers/
│   ├── scan.py          — Scan ISBN, enrichissement, re-enrichissement
│   ├── books.py         — CRUD livres, bulk actions
│   ├── music.py         — Scan, CRUD et tâches disques (CDs, vinyles…)
│   ├── series.py        — Gestion des séries
│   ├── loans.py         — Prêts
│   ├── locations.py     — Sites / salles / étagères
│   ├── users.py         — Comptes utilisateurs
│   └── settings.py      — Paramètres (sources, clés API, SMTP)
├── app/lookup.py        — Logique de recherche multi-sources (livres)
├── app/lookup_music.py  — Logique de recherche musicale (MusicBrainz, Discogs)
├── app/audit.py         — Journal d'activité (audit log)
├── app/models.py        — Modèles SQLAlchemy (Book, Disc, Series, Loan…)
└── app/database.py      — Init DB + migrations

templates/               — Pages Jinja2 (HTML + JS vanilla)
static/css/style.css     — CSS custom (variables, dark/light)
static/js/quagga2.min.js — Bibliothèque de décodage code-barres
```

**Base de données** : SQLite, fichier persisté dans un volume Docker (`/app/data/bibliodech.db`).  
**Déploiement** : `./deploy.sh` → génère un `BUILD_VERSION`, build Docker, push GitHub, relance le container.

---

## Scanner ISBN

### Comment ça marche

1. L'utilisateur ouvre `/scanner`
2. Il scanne via **caméra live** (bouton 📷) ou **saisie manuelle** (champ texte)
3. L'ISBN est envoyé à `POST /api/scan`
4. Un livre est créé immédiatement en base avec `enrichment_status = "pending"`
5. L'enrichissement se fait en **arrière-plan** via `_enrich_book()`
6. Le client poll `GET /api/scan/status/{id}` jusqu'à `ok` ou `not_found`

### Caméra live (Quagga2)

- Bibliothèque : **Quagga2** (`static/js/quagga2.min.js`, 143 KB), décodage EAN-13 uniquement (ISBN)
- Requiert **HTTPS** (`getUserMedia`) — fourni par Pangolin en production
- La caméra reste ouverte après chaque scan (scan en continu)
- Les résultats apparaissent sous forme de **toasts** (pending → ok/warn/err)
- **Perf mobile** : résolution cible 480p, `halfSample: true`, fréquence 20 fps — réduit la charge CPU
- Premier poll de statut à 500 ms (au lieu de 1500 ms) pour réduire l'attente du toast bleu
- **Flash** : bouton 🔦 visible uniquement si l'appareil supporte `torch` (Android Chrome)
- **iOS Safari** : live non disponible → fallback automatique sur sélection de photo

### Localisation au scan

Sélecteurs **Site → Salle → Étagère** toujours visibles. L'étagère choisie est transmise avec chaque scan (`location_id`, `room_id`).

### Codes non-ISBN

Les codes EAN dont le préfixe n'est pas 978/979 (ex : 878, 411, 413) sont détectés et signalés par un avertissement orange — ils ne sont pas dans les bases de données livres mondiales.

---

## Enrichissement multi-sources

### Sources disponibles (configurables dans Paramètres)

| Source | Type | Clé requise | Points forts |
|---|---|---|---|
| **BNF** | Gratuit | Non | Livres français, séries |
| **SUDOC** | Gratuit | Non | Bibliothèques universitaires |
| **Decitre** | Gratuit | Non | BD, mangas, couvertures |
| **Open Library** | Gratuit | Non | Lookup ISBN direct |
| **OL Search** | Gratuit | Non | Fallback large (non-ISBN inclus) |
| **Google Books** | Gratuit limité | Recommandée | 1 000 req/j avec clé (100 sans) |
| **ISBNdb** | Payant (~15$/mois) | Oui | Excellente couverture FR/EN |

### Priorité et fusion

Dans `lookup_isbn()` (`app/lookup.py`) :
1. Toutes les sources actives sont interrogées **en parallèle** pour chaque variante ISBN
2. Les résultats sont fusionnés : la source prioritaire (ordre dans les paramètres) fournit les champs de base, les autres complètent les champs manquants
3. Les résultats bruts de chaque source sont stockés dans `book.source_data` (JSON) — visible dans l'onglet **Sources** de chaque fiche

### Variantes ISBN

Chaque ISBN est converti en variantes (ISBN-10 ↔ ISBN-13) pour maximiser les chances de trouver un résultat.

### Nettoyage des auteurs BNF

La BNF renvoie les auteurs en format UNIMARC (`Prénom. Auteur du texte Nom`). La fonction `_bnf_clean_creators()` normalise vers `Prénom Nom` et gère :
- Mentions de responsabilité (`[dessin de] X ; [scénario de] Y`)
- Multi-auteurs en ISBD (`Nom1, Prénom1, Nom2, Prénom2`)
- Entrées fantômes (rôle sans nom)
- Institutions (`Musée du Louvre (Paris). Auteur du texte` → `Musée du Louvre (Paris)`)

---

## Médiathèque musicale

### Scanner CDs, vinyles et cassettes

Le scanner détecte automatiquement si un code-barres est un **ISBN livre** (préfixe 978/979) ou un **EAN musical** et route vers le bon flux :

- ISBN → flux livre existant (`POST /api/scan`)
- EAN musical → flux disque (`POST /api/scan/music`)

Les codes de 10 à 14 chiffres déclenchent la soumission automatique. Si le code est ambiguë, l'interface propose une fiche "CD/Vinyle" au lieu d'une fiche livre.

### Sources musicales

| Source | Type | Clé requise | Points forts |
|---|---|---|---|
| **MusicBrainz** | Gratuit, open source | Non | Lookup EAN direct, couvertures via Cover Art Archive. Max 1 req/s (User-Agent obligatoire). |
| **Discogs** | Gratuit limité | Oui (25 req/min) | Meilleure couverture des pressages régionaux et rares |

**Waterfall de pochettes** : MusicBrainz (via MBID + Cover Art Archive) → Discogs (via code-barres) → recherche MusicBrainz par titre+artiste.

La clé Discogs se configure dans **Paramètres → Sources → Musique** (clé API personnelle, gratuite).

### Page Médiathèque (`/music`)

- Vue **grille** (pochettes) ou **liste** (tableau)
- Filtres : format (CD, Vinyl, Cassette…), artiste, localisation
- Tri : artiste, titre, date d'ajout
- **Sélection en masse** : modifier la localisation, supprimer

### Fiche disque

Accessible depuis la grille ou la liste. Contient :

- Pochette, titre, artiste, label, format, année
- Métadonnées : N° catalogue, pistes, pays, langue
- Liens externes : MusicBrainz, Discogs
- Onglet **Infos** : tous les champs
- Onglet **Édition** : modification manuelle de tous les champs + localisation

Si le disque est `Non trouvé` après l'enrichissement, un bouton **Relancer la recherche** ré-interroge MusicBrainz et Discogs sans attendre la tâche planifiée.

### Enrichissement et statuts

| Statut | Signification |
|---|---|
| `pending` | En cours d'enrichissement (fond bleu, polling actif) |
| `ok` | Enrichi avec succès |
| `not_found` | Aucune source n'a trouvé ce code-barres |

### Tâches planifiables — Musique

Accessibles dans **Tâches → Musique** :

| Tâche | Rôle |
|---|---|
| `reenrich-discs` | Re-enrichit les disques `not_found` ou `pending` via MusicBrainz et Discogs |
| `fetch-disc-covers` | Télécharge les pochettes manquantes pour les disques enrichis |
| `refresh-disc-covers` | Re-télécharge toutes les pochettes (qualité maximale) |

---

## Recherche globale

Icône loupe dans la barre de navigation (raccourci **⌘K** sur Mac). Cherche en temps réel dans **livres et disques simultanément** dès 2 caractères saisis.

Les résultats sont groupés par type et cliquables :
- Si on est déjà sur la page Bibliothèque ou Musique, le clic ouvre directement la modale de la fiche
- Sinon, navigation vers la page correspondante

---

## Bibliothèque

### Affichage et filtres

- Tri : titre, auteur, date d'ajout
- Filtres : série, localisation
- Recherche texte : titre, auteur, ISBN

### Fiche livre — onglets

**Édition** : modification des champs (titre, auteurs, éditeur, série, position, localisation)  
**Sources** : tableau comparatif de toutes les sources configurées — colonnes = sources, lignes = champs. La valeur retenue est surlignée en vert. Bouton ↺ par source pour recharger depuis cette source uniquement.  
**Historique** : journal des événements (création, enrichissement, modification) avec auteur et date.

### Sélection en masse (bulk)

Cocher plusieurs livres → barre d'actions : modifier auteurs, attribuer série, localisation, 🔄 Recharger (re-enrichissement), supprimer.

### Re-enrichissement

- **Par livre** : bouton 🔄 Relancer dans la fiche (toutes les sources)
- **Par source** : bouton ↺ dans l'onglet Sources (une source spécifique)
- **Sélection** : bouton 🔄 Recharger dans la barre bulk
- **Tout recharger** : bouton ♻️ dans la barre bibliothèque (tous les livres avec ISBN)
- **Manquants seulement** : bouton 🔄 Compléter (livres `not_found` ou sans titre)

---

## Localisation

Hiérarchie à 3 niveaux : **Site** → **Salle** → **Étagère**

Gérée dans `/locations`. Chaque livre peut être associé à une étagère. La localisation est sélectionnable au scan (dropdowns en cascade) et modifiable dans la fiche.

---

## Détection des séries

La détection des séries est un pipeline multi-signaux qui s'exécute à l'enrichissement et via des tâches planifiables. Les sources sont consultées dans l'ordre de fiabilité décroissante.

---

### Signal 0 — Parsing du titre (haute confiance)

**Fichier** : `app/routers/series.py` → `_parse_title()` et `_detect()`  
**Déclencheur** : tâche "Détecter les séries" + à chaque enrichissement via SUDOC

Reconnaît les formats de titres structurés courants en bibliothèques françaises :

| Pattern | Exemple |
|---|---|
| `Série. N, sous-titre` (BnF/SUDOC) | `Blake et Mortimer. 25, Le testament de William S.` |
| `Série. Tome N` | `Astérix. Tome 1` |
| `Série Tome N` | `Astérix Tome 1` |
| `Série – Tome N` | `Tintin – Tome 7` |
| `Série Volume N` / `Vol. N` | `One Piece Volume 12` |
| `Série T. N` | `Astérix T. 3` |
| `Série n° N` | `Lucky Luke n° 42` |

**Comportement** : auto-crée la série et assigne le livre avec sa position. Aucune proposition générée, assignation directe.

---

### Signal SUDOC — Catalogue universitaire (haute confiance)

**Fichier** : `app/routers/scan.py` → `_lookup_series_sudoc()`  
**Déclencheur** : à chaque enrichissement (`_enrich_book`) + tâche "Chercher les séries dans le catalogue"

Flux en 2 appels HTTP :
1. `GET https://www.sudoc.fr/services/isbn2ppn/{isbn}` → PPN (identifiant SUDOC)
2. `GET https://www.sudoc.fr/{ppn}.xml` → notice UNIMARC

Extrait le champ **UNIMARC 225** :
- `225$a` → nom de la série (ex : `Ralph Azham`)
- `225$v` → numéro de volume (ex : `2`)

Très efficace pour les BDs françaises (Dupuis, Dargaud, Casterman, Lombard…).  
En fallback : `lookup_isbn()` → champ `series_name` retourné par Open Library ou Google Books.

---

### Signal 1 — Préfixe + éditeur (confiance moyenne)

**Fichier** : `app/routers/series.py` → `_detect()`, groupe `prefix_groups`

Regroupe les livres orphelins qui partagent le **même premier mot significatif de titre** et le **même éditeur** (normalisé). Si au moins 2 livres correspondent et qu'un groupe similaire existe déjà en série, assignation directe. Sinon : proposition dans la page Détection.

---

### Signal 2 — Auteur + éditeur (confiance faible)

**Fichier** : `app/routers/series.py` → `_detect()`, groupe `author_groups`

Regroupe les livres orphelins du **même auteur** chez le **même éditeur**. Génère uniquement des propositions (jamais d'auto-assignation) car un auteur peut publier des œuvres indépendantes chez le même éditeur.

---

### Signal OCR — Lecture des couvertures (confiance variable)

**Fichier** : `app/routers/series.py` → `_ocr_series_from_cover()` et `_ocr_detect()`  
**Déclencheur** : tâche "Lire les séries sur les couvertures" + bouton 🔍 dans la page Détection

Utilise **Tesseract OCR** (local, sans clé API) via `pytesseract`. La couverture locale est préprocessée avant la lecture (niveaux de gris, upscale ×1.5–2, contraste ×2, netteté ×2).

**Extraction en 3 passes** sur 4 bandes verticales (1/4, 1/3, 1/2, pleine image) :

| Passe | Méthode |
|---|---|
| **1. Formules** | Regex `"LES AVENTURES DE X"`, `"UNE AVENTURE DE X"` |
| **2. Séries connues** | Matching exact ou par mots-clés contre les séries en base |
| **2b. Fuzzy** | Distance de Levenshtein ≤ 30% pour erreurs OCR |
| **3. Heuristique** | Ligne en majuscules, 1–2 mots, ni auteur ni éditeur connu |

**Filtres anti-bruit** :
- Noms d'auteurs du livre exclus (comparaison par préfixe de mot)
- Éditeurs connus exclus (Dupuis, Dargaud, Casterman, Lombard, Glénat…)
- Lignes "Prénom Nom" à 2–3 mots exclues si non connues comme séries

**Résolution** : les couvertures sont stockées en **600px de large** depuis juin 2026 (précédemment 300px). La tâche "Re-télécharger toutes les couvertures (HD)" permet de mettre à jour les couvertures existantes.

---

### Page Détection (`/series/proposals`)

Interface de validation des propositions générées par les signaux 1 et 2.

**Par proposition :**
- Badge(s) indiquant la/les source(s) détection (cumulables : `Auteur + éditeur` + `🔍 OCR couverture` + `📚 SUDOC`)
- Champ nom de la série (pré-rempli si détecté)
- Bouton 🔍 : lance l'OCR sur les couvertures du groupe (1 par 1 avec barre de progression)
- Dropdown : relier à une série existante plutôt que d'en créer une nouvelle
- Boutons : **Accepter** (crée/assigne la série) ou **Rejeter**

---

### Ordre d'exécution recommandé

Pour une bibliothèque neuve ou après un import massif :

1. **Scanner** les livres (enrichissement automatique déclenche SUDOC en parallèle)
2. Tâche **"Détecter les séries"** → Signal 0 (titres) + Signaux 1/2 (heuristiques)
3. Tâche **"Chercher les séries dans le catalogue"** → SUDOC sur les orphelins restants
4. Valider les propositions dans **Détection → Propositions** (utiliser 🔍 OCR si le nom est vide)
5. Tâche **"Lire les séries sur les couvertures (OCR)"** → dernier recours pour les livres sans ISBN exploitable

---

### Normalisation des noms

Toutes les comparaisons de noms de séries utilisent `_norm()` :
```python
unicodedata.normalize("NFD", s.lower())  # minuscules + suppression des accents
```
Exemples : `"Tintin"`, `"TINTIN"`, `"tïntïn"` → tous équivalents à `"tintin"`.

---

### Gestion manuelle

Page **Bibliothèque → Séries** : vue en grille ou liste de toutes les séries, avec couverture du premier tome, nom et nombre de volumes.

Clic sur une série → **modale à 3 onglets** :

| Onglet | Contenu |
|---|---|
| **Infos** | Formulaire : nom, auteurs, éditeur, localisation (pièce + étagère) — modifications appliquées à tous les livres de la série. Bouton Supprimer (admin). |
| **Tomes** | Liste triée des volumes avec couverture et numéro de tome. Clic → ouvre la modale livre correspondante. |
| **Historique** | Tous les emprunts liés aux livres de la série (emprunteur, date, statut). |

#### Filtres

- **Recherche texte** : filtre par nom de série
- **Localisation** : filtre par pièce (une série apparaît si au moins un livre y est rangé)
- **Taille** : chips Toutes / ≥2 / ≥5 / ≥10 volumes

#### Sélection en masse

Bouton **Sélectionner** → mode sélection : clic sur une carte la coche. Barre flottante avec :
- Compteur + "Tout sélectionner"
- **Modifier…** → modale d'édition en masse (auteurs, éditeur, localisation) — seuls les champs renseignés sont appliqués

---

## Prêts

- Associer un livre à un emprunteur (compte utilisateur ou contact externe)
- Date de retour prévisionnelle, alerte si en retard
- Historique des prêts par livre

---

## Statistiques

Page `/stats` — KPIs + graphiques :

| Section | Contenu |
|---|---|
| **KPIs livres** | Livres, Auteurs, Séries, Prêts en cours |
| **Ajouts par jour** | Courbe sur 30 jours |
| **Langues** | Donut chart avec légende |
| **Par localisation** | Barres horizontales |
| **Top auteurs** | Barres horizontales |
| **Sources** | Répartition par source d'enrichissement |
| **Prêts** | Total, en cours, % retournés, top emprunteurs, livres les plus prêtés |
| **Séries** | Total séries, livres en série, % catalogués, séries vides |
| **Top séries** | Barres des séries les plus fournies |
| **KPIs musique** | Disques, Artistes, Formats |
| **Formats** | Donut chart CD / Vinyl / Cassette… |
| **Top artistes** | Barres des artistes les plus représentés |

---

## Utilisateurs et rôles

| Rôle | Droits |
|---|---|
| **admin** | Tout (suppression, gestion utilisateurs, tâches, paramètres) |
| **contributeur** | Scan, ajout, modification des livres et séries |
| **lecteur** | Consultation uniquement |

**Droits par localisation** : l'admin peut restreindre un contributeur à des salles spécifiques (`UserRoomPermission`). Le scanner pré-sélectionne automatiquement les salles autorisées.

Changement de mot de passe obligatoire à la première connexion si `must_change_password = true`.

---

## Paramètres

- **Sources — Livres** : activer/désactiver chaque source, ordre de priorité, timeout
- **Sources — Musique** : MusicBrainz (toujours actif), Discogs (clé API à saisir)
- **Clés API** : Google Books, ISBNdb
- **SMTP** : configuration email pour les alertes (retards de prêt, nouveau compte)
- **Import CSV** : importer une liste de livres en masse
- **Export CSV — Bibliothèque** : livres (titre, auteurs, ISBN, éditeur, localisation…)
- **Export CSV — Musique** : discothèque (titre, artiste, label, format, localisation…)
- **Sauvegardes** : créer, télécharger, restaurer, supprimer des sauvegardes de la base SQLite. Rétention configurable. Planifiable via les tâches.

### Tâches planifiables

Accessibles dans **Paramètres → Tâches** :

| Tâche | Rôle |
|---|---|
| `bnf-series` | Trouve tous les tomes BnF pour chaque série et rattache ceux en bibliothèque |
| `detect-series` | Comparaison titre/auteurs entre livres sans série (signaux 0, 1, 2) |
| `sudoc-series` | Interroge le SUDOC pour chaque livre sans série |
| `ocr-series` | Lecture OCR des couvertures pour détecter les noms de séries |
| `reenrich` | Re-enrichit tous les livres `not_found` ou sans titre |
| `reenrich-all` | Re-enrichit tous les livres avec ISBN |
| `fetch-covers` | Télécharge les couvertures manquantes |
| `refresh-covers` | Re-télécharge toutes les couvertures en 600px |
| `clean-series` | Nettoie les noms de séries (casse, accents, doublons) |
| `backup` | Crée une sauvegarde de la base de données |
| `reenrich-discs` | Re-enrichit les disques `not_found` ou `pending` |
| `fetch-disc-covers` | Télécharge les pochettes manquantes des disques |
| `refresh-disc-covers` | Re-télécharge toutes les pochettes de disques |

---

## Audit log

Chaque action sur un livre est tracée dans `audit_logs` :

| Action | Déclencheur |
|---|---|
| `created` | Scan ou saisie manuelle |
| `enriched` | Enrichissement automatique ou re-enrichissement |
| `updated` | Modification manuelle d'un champ (champs modifiés enregistrés) |

Visible dans l'onglet **Historique** de chaque fiche.
