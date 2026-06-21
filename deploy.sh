#!/bin/bash
set -e
export BUILD_VERSION=$(date +"%y.%m%d.%H%M")
echo "→ Build $BUILD_VERSION"

# Auto-commit et push si un remote est configuré
if git remote get-url origin &>/dev/null; then
  if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit -m "deploy: $BUILD_VERSION" --allow-empty-message 2>/dev/null || true
  fi
  git push origin main 2>/dev/null && echo "✓ Pushé sur GitHub" || echo "⚠ Push GitHub échoué (continuer quand même)"
fi

docker compose build --no-cache
docker compose up -d
echo "✓ Déployé en $BUILD_VERSION"
