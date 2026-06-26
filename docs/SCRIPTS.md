# Scripts Python — Bibliodech

Vue d'ensemble de chaque fichier Python du projet (hors venv et migrations).

---

## Couche centrale (`app/`)

### [app/main.py](../app/main.py)
Point d'entrée FastAPI. Monte les fichiers statiques (`/static`, `/covers`, `/avatars`), enregistre tous les routers, définit les routes des pages HTML (login, bibliothèque, scanner, localisations, emprunts, séries, paramètres, tâches, logs…), expose l'export CSV, et démarre le scheduler au démarrage de l'application.

### [app/models.py](../app/models.py)
Modèles SQLAlchemy. Définit toutes les tables :

| Modèle | Table | Rôle |
|---|---|---|
| `Book` | `books` | Livre avec ISBN, titre, auteurs, série, localisation, statut d'enrichissement |
| `Series` | `series` | Série (nom unique, source) |
| `User` | `users` | Utilisateur avec rôle (`admin`, `contributor`, `viewer`) |
| `Site` / `Room` / `Shelf` | `sites` / `rooms` / `shelves` | Hiérarchie de localisation |
| `Loan` / `Borrower` | `loans` / `borrowers` | Système d'emprunt |
| `Setting` | `settings` | Paramètres clé/valeur JSON |
| `AuditLog` | `audit_logs` | Historique des actions sur les livres |
| `AppLog` | `app_logs` | Logs applicatifs (erreurs, tâches) |
| `SeriesProposal` | `series_proposals` | Propositions de rattachement à une série (à valider manuellement) |

### [app/database.py](../app/database.py)
Connexion à la base de données. Crée le moteur SQLAlchemy (SQLite par défaut, configurable via `DATABASE_URL`), active `PRAGMA foreign_keys=ON`. `init_db()` crée les tables au démarrage et applique les colonnes manquantes par ALTER TABLE (migrations légères, sans Alembic en runtime).

### [app/schemas.py](../app/schemas.py)
Schémas Pydantic pour la validation des entrées et la sérialisation des sorties API : `BookCreate`, `BookOut`, `SeriesOut`, `UserCreate`, `UserOut`, `LoanCreate`, `BorrowerCreate`.

### [app/settings.py](../app/settings.py)
Accès aux paramètres persistants stockés en DB (table `settings`, clé/valeur JSON). Expose `get(db, key)` et `set_(db, key, value)`. Définit les valeurs par défaut : sources de lookup actives (BnF, SUDOC, Decitre, Google Books…), clés API, config SMTP, options mail, URL du site.

### [app/auth.py](../app/auth.py)
Authentification par cookie signé (itsdangerous). Fonctions principales :
- `hash_password` / `verify_password` — bcrypt
- `create_session` / `clear_session` — cookie `bibliodech_session`
- `get_current_user(request, db)` — décode le cookie, retourne l'utilisateur ou redirige vers `/login`
- `require_admin` / `require_contributor` — vérifie le rôle, lève HTTP 403 si insuffisant
- `bootstrap_admin(db)` — crée le compte `admin` au premier démarrage si aucun utilisateur n'existe

### [app/scheduler.py](../app/scheduler.py)
Planificateur de tâches basé sur APScheduler (mode AsyncIO). Les 11 tâches disponibles :

| ID | Label |
|---|---|
| `reenrich` | Compléter les livres manquants |
| `fetch-covers` | Rechercher les couvertures manquantes |
| `refresh-covers` | Re-télécharger toutes les couvertures (HD) |
| `clean-authors` | Normaliser les auteurs |
| `detect-series` | Détecter les séries |
| `sudoc-series` | Chercher les séries dans le catalogue (SUDOC) |
| `ocr-series` | Lire les séries sur les couvertures (OCR) |
| `clean-series` | Normaliser les noms de séries |
| `bnf-series` | Compléter les séries via la BnF |
| `enrich-genres` | Récupérer les genres des livres |
| `backup` | Sauvegarder la base de données |

La config (intervalles, activé/désactivé) est stockée en DB sous la clé `task_schedules`. `_execute_task(task_id, db)` est appelé aussi bien par le planificateur que par le bouton "Lancer" de l'interface. La progression des tâches longues est suivie dans `_running`.

### [app/lookup.py](../app/lookup.py)
Cœur du système d'enrichissement ISBN. Interroge en parallèle plusieurs sources :

| Source | Données récupérées |
|---|---|
| BnF (SRU Dublin Core + UNIMARC) | Titre, auteurs, éditeur, série (champ 225/461), numéro de tome |
| SUDOC | Titre, auteurs, série |
| Decitre | Titre, auteurs, couverture HD |
| Google Books | Titre, auteurs, description, genre, série (`seriesInfo`) |
| Open Library | Titre, auteurs, description |
| ISBNdb | Titre, auteurs, éditeur |

Logique de fusion : chaque source retourne un dict, le résultat final prend le meilleur de chaque champ selon une priorité configurable. La série est préférentiellement issue de la BnF (champ UNIMARC 225). Pour les livres avec plusieurs champs 225 (série + collection éditeur), le code préfère celui dont `$v` est un entier pur (numéro de tome) plutôt qu'un code de collection type `J2599`.

Contient aussi `_extract_series_position(title, subtitle)` qui détecte le numéro de tome depuis le titre ("T.3", "tome 3", etc.) et `debug_isbn` pour l'endpoint de débogage.

### [app/covers.py](../app/covers.py)
Téléchargement et traitement des couvertures. `fetch_and_save(isbn, url)` télécharge l'image, la redimensionne à 600px max (Pillow / LANCZOS), la sauvegarde en JPEG optimisé (qualité 82) dans `/data/covers/{isbn}.jpg`, et retourne l'URL locale `/covers/{isbn}.jpg`. Ignore les réponses HTML ou les images trop petites (< 1 Ko).

### [app/book_utils.py](../app/book_utils.py)
Sérialisation des livres. `book_to_dict(book)` convertit un objet `Book` SQLAlchemy en dict JSON complet avec :
- localisation résolue (site → pièce → étagère)
- auteurs parsés depuis le JSON stocké en base
- série et numéro de tome
- infos d'emprunt actif (emprunteur, date de retour)

### [app/applog.py](../app/applog.py)
Logs applicatifs persistés en DB (table `app_logs`). Fonctions : `log()`, `log_task()`, `log_error()`. Visible dans l'interface Paramètres > Logs.

### [app/audit.py](../app/audit.py)
Journal d'audit des actions sur les livres (table `audit_logs`). `audit_log(db, book_id, action, detail)` trace les enrichissements, modifications, séries appliquées. Pas de commit propre — l'appelant commite avec le reste de la transaction.

### [app/email.py](../app/email.py)
Envoi de mails HTML via SMTP (config issue de `app/settings.py`). Expose `send_mail(db, to, subject, html)` et des helpers par scénario : création de compte, réinitialisation de mot de passe, relance emprunt en retard.

---

## Routers (`app/routers/`)

### [app/routers/books.py](../app/routers/books.py)
CRUD livres et tâches associées.

**Endpoints API :**
- `GET /api/books` — liste filtrée (série, genre, source, localisation, couverture, texte libre)
- `GET /api/books/{id}` — détail d'un livre
- `POST /api/books` — création manuelle
- `PUT /api/books/{id}` — mise à jour complète
- `PATCH /api/books/{id}` — mise à jour partielle
- `DELETE /api/books/{id}` — suppression
- `GET /api/books/genres` — liste des genres disponibles
- `GET /api/books/sources` — liste des sources
- `POST /api/books/bulk` — création en masse (import)

**Logiques de tâches :**
- `_clean_authors_logic` — normalise les noms d'auteurs (supprime les doublons, uniformise la casse)
- `_fetch_covers_logic` — télécharge les couvertures manquantes
- `_refresh_covers_logic` — re-télécharge toutes les couvertures en HD
- `_enrich_genres_logic` — récupère les genres depuis Google Books / Open Library

### [app/routers/scan.py](../app/routers/scan.py)
Scanner ISBN et enrichissement.

**Endpoints API :**
- `POST /api/scan` — reçoit un ISBN, crée le livre en DB, lance `_enrich_book` en background
- `GET /api/scan/status/{book_id}` — état du polling (pending / ok / not_found)
- `POST /api/books/{id}/re-enrich` — ré-enrichit un livre existant
- `POST /api/books/{id}/re-enrich-source/{source_id}` — ré-enrichit depuis une source spécifique
- `POST /api/books/{id}/apply-source/{source_id}` — applique les données d'une source
- `GET /api/books/{id}/lookup-series` — cherche la série d'un livre sans l'appliquer
- `POST /api/books/{id}/apply-series` — applique manuellement une série
- `POST /api/books/re-enrich-all` — ré-enrichit tous les livres en échec

**Logiques internes :**
- `_enrich_book(book_id, isbn)` — tâche background : appelle `lookup_isbn`, applique titre / auteurs / éditeur / couverture / **série + numéro de tome** en DB
- `_reenrich_missing(db)` — ré-enrichit séquentiellement les livres `pending` ou `not_found`
- `_resolve_cover(isbn, info)` — chaîne de fallback pour trouver la meilleure couverture

### [app/routers/series.py](../app/routers/series.py)
Gestion des séries et détection automatique.

**Endpoints API :**
- `GET /api/series` — liste des séries avec comptage de livres
- `GET /api/series/missing` — tomes manquants dans chaque série (détection par position)
- `GET /api/series/{id}/check-bnf` — vérifie les tomes via la BnF pour une série donnée
- `GET /api/series/proposals` — propositions de rattachement à valider
- `POST /api/series/proposals/{id}/accept` — accepte une proposition
- `POST /api/series/detect` — lance la détection automatique
- `POST /api/series/sudoc-lookup` — détection via SUDOC

**Logiques de tâches :**
- `_detect(db)` — détection par correspondance titre/ISBN entre livres sans série
- `_sudoc_detect(db)` — interroge le catalogue SUDOC pour chaque livre sans série
- `_ocr_detect(db)` — lit les couvertures par OCR pour détecter les noms de séries
- `_bnf_series_logic(db)` — pour chaque série, interroge la BnF et rattache les tomes trouvés (3 passes : position → ISBN 10↔13 → titre normalisé)
- `_clean_series_names_logic(db)` — normalise les noms (ponctuation, casse) et fusionne les doublons
- `_bnf_check_one_series(series, db)` — vérifie une série individuelle via la BnF

### [app/routers/locations.py](../app/routers/locations.py)
CRUD de la hiérarchie de localisation (Site → Pièce → Étagère).

- `GET /api/locations/tree` — arbre complet avec nombre de livres par étagère
- `GET /api/locations` — liste plate des étagères
- CRUD complet pour `/api/sites`, `/api/rooms`, `/api/shelves`

### [app/routers/loans.py](../app/routers/loans.py)
Système d'emprunt.

- CRUD emprunteurs (`/api/borrowers`)
- CRUD emprunts (`/api/loans`) avec filtres (en cours, en retard, par emprunteur)
- `POST /api/loans/{id}/return` — enregistre le retour
- `GET /api/loans/overdue` — liste des emprunts en retard
- `POST /api/loans/send-overdue` — envoie les mails de relance

### [app/routers/users.py](../app/routers/users.py)
Gestion des comptes utilisateurs.

- CRUD utilisateurs (`/api/users`) — réservé admin
- `POST /api/users/{id}/reset-password` — génère un nouveau mot de passe, l'envoie par mail
- `GET /api/users/{id}/permissions` / `PUT` — permissions par pièce
- `GET /api/me` / `PUT` — profil personnel
- `POST /api/me/change-password` — changement de mot de passe
- `POST /api/me/avatar` — upload d'avatar

### [app/routers/backup.py](../app/routers/backup.py)
Sauvegardes de la base de données.

- `GET /api/backups` — liste des sauvegardes disponibles
- `POST /api/backups` — crée une sauvegarde (copie atomique du fichier SQLite)
- `GET /api/backups/{filename}/download` — télécharge une sauvegarde
- `POST /api/backups/{filename}/restore` — restaure une sauvegarde
- `DELETE /api/backups/{filename}` — supprime une sauvegarde
- `POST /api/backups/upload` — importe une sauvegarde externe

Rotation automatique : supprime les plus anciennes au-delà de `max_backups` (configurable).

### [app/routers/settings.py](../app/routers/settings.py)
Lecture/écriture des paramètres applicatifs.

- `GET /api/settings` — tous les paramètres
- `PUT /api/settings/{key}` — met à jour un paramètre
- `GET /api/settings/sources` — liste des sources de lookup et leur état
- `POST /api/settings/email/test` — envoie un mail de test SMTP

---

## Tests (`tests/`)

### [tests/conftest.py](../tests/conftest.py)
Fixtures pytest : base de données SQLite en mémoire, `TestClient` FastAPI, utilisateur admin de test.

### [tests/test_api_books.py](../tests/test_api_books.py)
Tests d'intégration sur l'API livres : création, lecture, mise à jour, suppression, filtres.

### [tests/test_book_utils.py](../tests/test_book_utils.py)
Tests unitaires de `book_to_dict` : sérialisation des auteurs, localisation, série, emprunt actif.

### [tests/test_series_detection.py](../tests/test_series_detection.py)
Tests unitaires de la détection de série depuis les titres (`_extract_series_position`, `_parse_title`).
