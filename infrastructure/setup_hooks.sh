#!/usr/bin/env bash
# Run once after cloning: bash infrastructure/setup_hooks.sh

set -e

echo "→ Installing pre-commit..."
pip install pre-commit --quiet

echo "→ Installing hooks..."
pre-commit install                  # pre-push and pre-commit stages
pre-commit install --hook-type commit-msg   # conventional commits

echo "→ Generating secrets baseline (first run only)..."
if [ ! -f .secrets.baseline ]; then
  detect-secrets scan \
    --exclude-files '.*\.jsonl' \
    --exclude-files '.*\.db' \
    --exclude-files 'infrastructure/env\.template' \
    > .secrets.baseline
  echo "   .secrets.baseline created — commit this file."
else
  echo "   .secrets.baseline already exists, skipping."
fi

echo ""
echo "✓ Done. Hooks are active."
echo ""
echo "Commit format required: <type>(<scope>): <description>"
echo "Types: feat | fix | docs | style | refactor | perf | test | build | ci | chore | revert"
echo "Example: feat(risk-guard): add daily drawdown hard stop"
