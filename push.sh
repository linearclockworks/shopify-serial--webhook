#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

# Use provided argument as commit message, or fall back to a default
COMMIT_MSG="${1:-Update Shopify webhook handler and Bryan Crider sled automation}"

echo "🚀 Staging changes..."
git add .

echo "✍️ Committing with message: \"$COMMIT_MSG\""
git commit -m "$COMMIT_MSG"

echo "⬆️ Pushing to GitHub..."
git push origin main

echo "✅ Pushed to GitHub! Vercel will automatically trigger a new deployment."
