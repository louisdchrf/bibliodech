#!/bin/bash
export BUILD_VERSION=$(date +"%y.%m%d.%H%M")
echo "→ Build $BUILD_VERSION"
docker compose build --no-cache
docker compose up -d
echo "✓ Déployé en $BUILD_VERSION"
