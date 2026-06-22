# Bibliodech

Application web auto-hébergée de gestion de bibliothèque personnelle. Scan ISBN par douchette, enrichissement automatique des métadonnées, détection de séries, localisation physique des livres et suivi des prêts.

## Fonctionnalités

- **Scan à la chaîne** — douchette USB/HID ou caméra (QR/EAN), chaque ISBN s'enchaîne sans confirmation manuelle
- **Enrichissement automatique** — SUDOC, BNF, Decitre, Google Books, Open Library, ISBNdb (optionnel) ; toutes les sources interrogées en parallèle, résultats fusionnés
- **Détection de séries multi-signaux** — parsing du titre (formats BnF/SUDOC), catalogue SUDOC par ISBN (champ UNIMARC 225), heuristiques auteur+éditeur, OCR Tesseract sur les couvertures
- **Localisation hiérarchique** — Site › Salle › Étagère, assignée au scan ou modifiable en masse
- **Prêts** — suivi emprunteur, date de retour prévue, vue "prêts en cours", historique
- **Bibliothèque** — recherche, filtres (série, localisation), tri, vue grille ou liste, édition individuelle et en masse, export CSV
- **Multi-utilisateurs** — rôles Admin et Contributeur, gestion des comptes depuis les paramètres
- **Tâches planifiables** — enrichissement, couvertures, normalisation auteurs, détection séries (SUDOC + OCR), planification horaire configurable avec suivi de progression en temps réel

## Prérequis

- Docker et Docker Compose

## Installation

```bash
git clone <repo>
cd bibliodech
```

Éditer `docker-compose.yml` et changer ces valeurs **avant le premier démarrage** :

```yaml
environment:
  SECRET_KEY: <chaîne aléatoire longue>
  ADMIN_USERNAME: <votre login admin>
  ADMIN_PASSWORD: <votre mot de passe>
```

Puis démarrer :

```bash
docker compose up -d
```

L'application est accessible sur **http://\<ip-machine\>:8000**.

## Mise à jour

```bash
docker compose build --no-cache
docker compose up -d
```

Les données sont persistées dans le volume Docker `bibliodech_data` et survivent aux mises à jour. Les migrations de schéma sont appliquées automatiquement au démarrage.

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `SECRET_KEY` | `change-me-in-production` | Clé de signature des cookies de session — **à changer** |
| `ADMIN_USERNAME` | `admin` | Login du compte admin créé au premier démarrage |
| `ADMIN_PASSWORD` | `admin123` | Mot de passe initial — **à changer** |
| `DATABASE_URL` | `sqlite:////app/data/bibliodech.db` | URL de la base de données |

## Sources ISBN

Configurables dans **Paramètres › Sources ISBN**. Activées par défaut : SUDOC, BNF, Decitre, Google Books.

**ISBNdb** (optionnel, ~15 $/mois) améliore la couverture des éditions étrangères. La clé API se configure dans Paramètres › Sources ISBN › Clés API.

## Détection des séries

La détection est un pipeline en 5 signaux, du plus fiable au moins fiable :

| Priorité | Signal | Source | Mode |
|---|---|---|---|
| 1 | **Parsing du titre** | Titre du livre | Auto-assignation |
| 2 | **Catalogue SUDOC** | UNIMARC 225 par ISBN | Auto-assignation |
| 3 | **Préfixe + éditeur** | Métadonnées | Proposition |
| 4 | **Auteur + éditeur** | Métadonnées | Proposition |
| 5 | **OCR couverture** | Tesseract (local) | Proposition |

Les signaux 1 et 2 s'exécutent automatiquement à chaque enrichissement. Les signaux 3 à 5 sont des tâches planifiables depuis **Paramètres › Tâches**. Les propositions générées sont à valider dans **Bibliothèque › Détection**.

Documentation complète : [`docs/FONCTIONNALITES.md`](docs/FONCTIONNALITES.md)

## Développement local (sans Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL="sqlite:///./bibliodech.db"
export SECRET_KEY="dev-secret"
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="admin123"

uvicorn app.main:app --reload
```

## Accès réseau

Conçu pour un usage **réseau local (LAN)**. Ne pas exposer directement sur Internet sans reverse proxy avec TLS.

## Stack technique

- Backend : Python 3.12 / FastAPI / SQLAlchemy / SQLite
- Frontend : HTML / CSS / JavaScript vanilla (aucun build step)
- OCR : Tesseract (`tesseract-ocr` + `tesseract-ocr-fra`) via `pytesseract`
- Conteneur : `python:3.12-slim`, un seul service + volume nommé (`/app/data`)
