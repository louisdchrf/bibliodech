# Bibliodech — Roadmap

## Bugs connus

- **BNF lookup intermittent** : la recherche BnF échoue parfois à l'import (timeout, réponse vide). Piste : retry plus robuste, détection d'erreur silencieuse, fallback automatique.
- **Couvertures manquantes** : certaines parutions n'ont pas d'image de couverture. Fallback chain en place (Decitre → OL → GB) mais incomplet.
- **iOS Safari** : scan live `getUserMedia` non fiable → fallback photo uniquement.

---

## Fonctionnalités à ajouter

### Scanner
- **Flash torche** : bouton pour activer le flash lors du scan (API `torch` via `applyConstraints`). Android Chrome uniquement, pas iOS Safari.

### Bibliothèque / Séries
- **Changer de source dans la fiche ne met pas à jour la série** : quand on applique une source (`apply-source`), `series_id`/`series_position` ne sont pas mis à jour si la source retourne un `series_name`.
- **Lien "Voir →" sur les tomes détectés** n'ouvre pas la modale — à corriger.

### Statistiques
- **Ajouts par jour** : la courbe affiche les 30 derniers jours mais le libellé dit "par mois" — à harmoniser.

### Affichage
- **Langue BnF** : convertir les codes ISO 639-2 (`fre`, `eng`…) en libellés lisibles (`Français`, `Anglais`…).
- **Menu Localisation** : les boutons d'ajout (pièce, étagère…) n'apparaissent qu'au hover — les rendre toujours visibles sur mobile.
- **Filtres multi-sélection** dans toutes les vues (bibliothèque, séries…) — noté en roadmap longue.

### Nouvelles sources
- **Alertes nouvelles parutions** : surveiller les nouvelles sorties pour les séries présentes et notifier (email ou bandeau). Nécessite une source de données (Babelio, scraping éditeur…).

---

## Améliorations envisagées (backlog)

- **Détection de séries par recherche web** : lancer une recherche (`"[titre] [auteur] série"`) et extraire nom + position via LLM. Sources : Babelio, Wikipedia, Goodreads, BDGest.
- Support Rakuten Books pour les codes JAN japonais.
- Export vers Calibre ou autres formats standard.
- Mode hors-ligne pour le scanner mobile (PWA / cache).

---

## Chantier : Médiathèque (CDs, vinyles, cassettes…)

### Contexte

Étendre Bibliodech au-delà des livres pour gérer une collection musicale physique. Les supports (CD, vinyle, cassette…) partagent le scanner et la localisation existants, mais ont leur propre page, leurs propres sources de données et leurs propres champs.

**Bases de données retenues** :
- **MusicBrainz** — primaire, gratuit, open source, lookup EAN natif, couvertures via Cover Art Archive. Validé sur `5099706493525` → *Miles Davis — Kind of Blue* (score 100, cover OK).
- **Discogs** — fallback, clé API gratuite (25 req/min), meilleure couverture des pressages locaux et obscurs.

Les deux couvrent CD, vinyle, cassette, SACD, DVD audio — le champ `format` dans la réponse distingue les formats.

### Plan d'implémentation

#### Étape 1 — Modèle & migration
- Nouvelle table `discs` : `barcode`, `title`, `artist`, `label`, `catalog_number`, `year`, `format` (CD/Vinyl/Cassette…), `genre`, `track_count`, `language`, `country`, `cover_url`, `mbid`, `room_id`, `location_id`, `enrichment_status`, `source_data`, `added_at`, `added_by`
- Migration Alembic
- Modèle SQLAlchemy `Disc` dans `app/models.py`

#### Étape 2 — Lookup musical
- `app/lookup_music.py` : `lookup_barcode(barcode)` → MusicBrainz d'abord, Discogs en fallback
- MusicBrainz : `GET /ws/2/release?query=barcode:{ean}&inc=artist-credits+labels+release-groups`
- Cover Art Archive : `GET coverartarchive.org/release/{mbid}/front`
- Discogs : `GET /database/search?barcode={ean}&type=release` (clé configurable dans Paramètres)
- Throttling MusicBrainz : 1 req/s max (User-Agent obligatoire)

#### Étape 3 — Scanner étendu
- Lever la restriction 978/979 dans `isValidBarcode()` : tout EAN-13 valide (checksum) est accepté
- `POST /api/scan` : si le barcode est 978/979 → flux livre existant ; sinon → tenter `lookup_barcode()` → créer un `Disc` si trouvé, proposer un choix si non trouvé
- Toast scanner : distinguer visuellement livre vs disque (icône 💿 vs 📚)

#### Étape 4 — Page Médiathèque (`/music`)
- Vue grille/liste sur le même pattern que `/library`
- Modale disque : onglets Infos (titre, artiste, label, format, année, pistes), Historique des prêts
- Filtres : format (CD/Vinyl…), genre, artiste, localisation
- Tri : titre, artiste, année d'ajout

#### Étape 5 — Sources dans Paramètres
- Section **Sources — Livres** (existant, renommé)
- Section **Sources — Musique** : MusicBrainz (actif par défaut, timeout configurable) + Discogs (clé API à saisir)
- Même UI que les sources livres : activer/désactiver, ordre, timeout

#### Étape 6 — Intégration transversale
- **Localisation** : les disques utilisent le même système Site → Salle → Étagère, localisations indépendantes des livres
- **Stats** : KPI disques, répartition par format, top artistes
- **Aide** : documentation `/aide` mise à jour
- **Prêts** : à évaluer (prêter un disque comme un livre ?)
