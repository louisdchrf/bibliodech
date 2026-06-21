# Bibliodech — Roadmap

## Bugs connus

- **BNF lookup intermittent** : la recherche BNF échoue parfois à l'import (timeout, réponse vide). Investiguer : retry plus robuste, détection d'erreur silencieuse, fallback automatique vers une autre source si BNF renvoie 0 résultat alors qu'un autre source trouve.

- **Couvertures manquantes** : certaines parutions n'ont pas d'image de couverture. Pistes :
  - Élargir les sources de couvertures (Google Books, BNF Gallica, Open Library covers)
  - Ajouter une image placeholder cohérente quand aucune couverture n'est disponible
  - Permettre l'upload manuel d'une couverture depuis la fiche

## Fonctionnalités à ajouter

### Scanner
- **Flash torche** : ajouter un bouton pour activer/désactiver le flash de l'appareil photo lors du scan live (API `MediaStreamTrack.applyConstraints({ torch: true })`). Disponible sur Android Chrome, pas sur iOS Safari.

### Sources de données
- **Couvertures** : enrichir la logique de fallback pour les couvertures — essayer dans l'ordre : source principale → Open Library covers by ISBN → Google Books thumbnail → placeholder.

### Documentation
- Rédiger une documentation structurée des fonctionnalités existantes :
  - Architecture générale (FastAPI + SQLAlchemy + Jinja2 + Docker)
  - Sources de données et leur priorité (BNF, SUDOC, Decitre, Google Books, OL, OL Search)
  - Système de scan (Quagga2, live + photo, iOS/Android)
  - Gestion des localisations (site → salle → étagère)
  - Système de prêts
  - Import/export CSV
  - Gestion des séries

### Infrastructure & CI/CD
- **GitHub** : uploader le projet sur GitHub (repo privé ou public)
  - `.gitignore` propre (exclure `data/`, secrets, `__pycache__`)
  - Push initial du code existant
- **Auto-push sur deploy** : modifier `deploy.sh` pour qu'il commite et pousse les modifications de code automatiquement avant (ou après) le build Docker
  - `git add -A && git commit -m "deploy: $BUILD_VERSION" && git push`
  - Ne pas versionner la base de données ni les volumes

## Améliorations envisagées (backlog)

- **Alertes nouvelles parutions** : pour les séries présentes en bibliothèque, surveiller les nouvelles sorties et notifier (email ou bandeau dans l'app). Nécessite une source de données de sorties (ex: Babelio, Goodreads, ou scraping éditeur).

- **Logs globaux** : onglet dans Paramètres listant tous les événements récents (créations, modifications, enrichissements) sur tous les livres — pour un aperçu d'activité sans devoir ouvrir chaque fiche.



- **Détection de séries par recherche web** : l'analyse des titres OL fonctionne mais est limitée à une seule source. Piste : lancer une recherche web (`"[titre] [auteur] série"`) pour identifier la série depuis plusieurs sources (Babelio, Wikipedia, Goodreads, BDGest...). À coupler avec un LLM pour extraire nom de série + position depuis le texte retourné.

- Compilation/fusion des métadonnées entre sources (ex : prendre le titre BNF + la couverture Google + la série SUDOC)
- Support Rakuten Books pour les codes JAN japonais (nécessite clé API)
- Recherche textuelle dans la bibliothèque (titre partiel, auteur)
- Export vers Calibre ou d'autres formats standard
- Statistiques avancées (livres par série, par langue, par éditeur)
- Mode hors-ligne pour le scanner mobile (PWA / cache)
