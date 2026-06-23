# Stack technique — Bibliodech

## Framework web

| Package | Version | Rôle |
|---|---|---|
| **fastapi** | 0.111.0 | Framework HTTP : définit les routes API (`@router.get`, `@router.post`...) et gère la validation des paramètres via Pydantic. C'est le cœur de l'application. |
| **uvicorn[standard]** | 0.29.0 | Serveur ASGI qui fait tourner FastAPI. Écoute sur le port 8000 et passe les requêtes à l'application. Le flag `[standard]` ajoute le support WebSocket et la recharge automatique. |

## Base de données

| Package | Version | Rôle |
|---|---|---|
| **sqlalchemy** | 2.0.30 | ORM : permet d'écrire `db.query(Book).filter(...)` au lieu de SQL brut. Définit les modèles (`Book`, `Series`, `User`...) et gère les sessions. |
| **aiosqlite** | 0.20.0 | Driver asynchrone pour SQLite. Nécessaire pour que SQLAlchemy puisse interroger la base sans bloquer l'event loop asyncio. |
| **alembic** | ≥ 1.13 | Versioning des migrations de schéma. Historise les changements de structure de la DB et les applique dans l'ordre au démarrage via `startup.sh`. Les nouvelles colonnes doivent passer par `alembic revision --autogenerate -m "description"`. |

## Requêtes HTTP sortantes

| Package | Version | Rôle |
|---|---|---|
| **httpx** | 0.27.0 | Client HTTP asynchrone utilisé pour appeler les APIs externes : BnF SRU, SUDOC, Google Books, Open Library, Decitre, ISBNdb. Remplace `requests` dans un contexte async. |

## Authentification & sécurité

| Package | Version | Rôle |
|---|---|---|
| **passlib[bcrypt]** | 1.7.4 | Hashage des mots de passe. `passlib` est l'abstraction de haut niveau, `[bcrypt]` est l'algorithme utilisé dans `hash_password()` et `verify_password()`. |
| **bcrypt** | 3.2.2 | Implémentation C de l'algorithme bcrypt, utilisée par passlib en dessous. |
| **itsdangerous** | 2.2.0 | Signe et vérifie les tokens de session stockés dans le cookie `bibliodech_session`. Garantit qu'un cookie ne peut pas être falsifié sans la `SECRET_KEY`. |

## Templates & formulaires

| Package | Version | Rôle |
|---|---|---|
| **jinja2** | 3.1.4 | Moteur de templates HTML côté serveur. Génère les pages (`library.html`, `base.html`...) avec `{% for %}`, `{{ variable }}`, `{% include %}`. |
| **python-multipart** | 0.0.9 | Nécessaire pour que FastAPI puisse lire les formulaires HTML (`Content-Type: multipart/form-data`), notamment le formulaire de login et l'upload de couvertures. |

## Traitement d'images & OCR

| Package | Version | Rôle |
|---|---|---|
| **Pillow** | 10.3.0 | Traitement d'images : redimensionne les couvertures téléchargées, les convertit en JPEG optimisé, et prépare les images pour l'OCR (niveaux de gris, upscale, contraste). |
| **pytesseract** | 0.3.13 | Binding Python pour Tesseract OCR. Extrait le texte des couvertures pour détecter les noms de séries. Nécessite que `tesseract-ocr` et `tesseract-ocr-fra` soient installés dans le conteneur (voir `Dockerfile`). |

## Tâches planifiées

| Package | Version | Rôle |
|---|---|---|
| **apscheduler** | ≥ 3.10 | Planificateur de tâches interne à l'app. Lance automatiquement les tâches (`detect-series`, `reenrich`, `fetch-covers`...) à intervalles configurables depuis l'onglet Tâches des Paramètres. Fonctionne comme un cron embarqué. |

## Dépendances système (Dockerfile)

Ces dépendances ne sont pas dans `requirements.txt` mais sont installées dans le conteneur Docker :

| Package | Rôle |
|---|---|
| **tesseract-ocr** | Moteur OCR utilisé par pytesseract pour lire le texte sur les couvertures. |
| **tesseract-ocr-fra** | Modèle de langue française pour Tesseract, améliore la reconnaissance des titres en français. |

## Dépendances de développement (`requirements-dev.txt`)

| Package | Rôle |
|---|---|
| **pytest** | Framework de tests. Lancer avec `python3 -m pytest tests/ -v`. |
| **pytest-asyncio** | Support des fonctions async dans pytest. |
| **httpx** | Déjà en prod, utilisé aussi par `TestClient` FastAPI pour les tests d'intégration. |
