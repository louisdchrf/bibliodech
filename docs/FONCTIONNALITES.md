# Bibliodech — Documentation des fonctionnalités

## Architecture générale

```
FastAPI (Python)
├── app/main.py          — Routes HTML (pages Jinja2)
├── app/routers/
│   ├── scan.py          — Scan ISBN, enrichissement, re-enrichissement
│   ├── books.py         — CRUD livres, bulk actions
│   ├── series.py        — Gestion des séries
│   ├── loans.py         — Prêts
│   ├── locations.py     — Sites / salles / étagères
│   ├── users.py         — Comptes utilisateurs
│   └── settings.py      — Paramètres (sources, clés API, SMTP)
├── app/lookup.py        — Logique de recherche multi-sources
├── app/audit.py         — Journal d'activité (audit log)
├── app/models.py        — Modèles SQLAlchemy
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

- Bibliothèque : **Quagga2** (`static/js/quagga2.min.js`, 143 KB), décodage EAN-13/EAN-8/UPC
- Requiert **HTTPS** (`getUserMedia`) — fourni par Pangolin en production
- La caméra reste ouverte après chaque scan (scan en continu)
- Les résultats apparaissent sous forme de **toasts** (pending → ok/warn/err)
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

## Séries

- Détection automatique à l'enrichissement (titre contenant "Tome X", champs dédiés BNF/Google Books)
- Gestion manuelle dans `/series` : créer, renommer, associer des livres
- Position dans la série stockée en float (permet des positions comme 2.5 pour un hors-série)

---

## Prêts

- Associer un livre à un emprunteur (compte utilisateur ou contact externe)
- Date de retour prévisionnelle, alerte si en retard
- Historique des prêts par livre

---

## Utilisateurs et rôles

| Rôle | Droits |
|---|---|
| **admin** | Tout (suppression, gestion utilisateurs) |
| **contributeur** | Scan, ajout, modification |

Changement de mot de passe obligatoire à la première connexion si `must_change_password = true`.

---

## Paramètres

- **Sources** : activer/désactiver chaque source, ordre de priorité, timeout
- **Clés API** : Google Books, ISBNdb
- **SMTP** : configuration email pour les alertes (retards de prêt, nouveau compte)
- **Import CSV** : importer une liste de livres en masse
- **Export** : export CSV de la bibliothèque complète

---

## Audit log

Chaque action sur un livre est tracée dans `audit_logs` :

| Action | Déclencheur |
|---|---|
| `created` | Scan ou saisie manuelle |
| `enriched` | Enrichissement automatique ou re-enrichissement |
| `updated` | Modification manuelle d'un champ (champs modifiés enregistrés) |

Visible dans l'onglet **Historique** de chaque fiche.
