#!/bin/bash
# Genuvia Edge — Railway infrastructure setup
# Usage: bash infrastructure/setup.sh
# Requires: Railway CLI installed + railway login completed

set -e

echo "Setting up Genuvia Edge infrastructure on Railway..."

# Link to existing project or create new
railway status || railway init

echo "Creating Nexara scanner service..."
railway service create nexara-scanner || echo "Service already exists"

echo "Creating Sbonelo service..."
railway service create nexara-sbonelo || echo "Service already exists"

echo ""
echo "Infrastructure created. Next steps:"
echo "1. Set environment variables in Railway dashboard for each service"
echo "2. Link each service to the GitHub repo"
echo "3. Set start commands per railway.toml and railway.sbonelo.toml"
echo ""
echo "Reference: infrastructure/env.template for all required variables"
