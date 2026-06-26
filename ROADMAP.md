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
