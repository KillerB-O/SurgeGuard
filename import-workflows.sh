#!/bin/bash

# Import n8n Workflows Script
# This script imports all workflow JSON files into running n8n instance

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
N8N_URL="http://localhost:5678"
N8N_USER="admin"
N8N_PASSWORD="n8n_admin_password"
WORKFLOWS_DIR="./n8n/workflows"

echo -e "${BLUE}🔧 n8n Workflow Importer${NC}"
echo "=============================================="
echo ""

# Check if n8n is reachable
echo "🔍 Checking n8n connection..."
if ! curl -s "$N8N_URL/api/v1/health" > /dev/null; then
    echo -e "${RED}❌ Cannot reach n8n at $N8N_URL${NC}"
    echo "Make sure n8n is running: docker-compose up -d"
    exit 1
fi
echo -e "${GREEN}✓ n8n is reachable${NC}"
echo ""

# Get auth token
echo "🔐 Authenticating with n8n..."
AUTH_RESPONSE=$(curl -s -X POST \
    -H "Content-Type: application/json" \
    -d "{\"email\": \"$N8N_USER\", \"password\": \"$N8N_PASSWORD\"}" \
    "$N8N_URL/api/v1/login")

TOKEN=$(echo "$AUTH_RESPONSE" | grep -o '"token":"[^"]*' | cut -d'"' -f4)

if [ -z "$TOKEN" ]; then
    echo -e "${YELLOW}⚠️  Could not auto-authenticate. This might be first-time setup.${NC}"
    echo "Please login manually at $N8N_URL and then run this script again."
    echo ""
    echo "Or export token manually:"
    echo "  export N8N_TOKEN=<your_n8n_api_token>"
    echo ""
    exit 1
fi
echo -e "${GREEN}✓ Authenticated successfully${NC}"
echo ""

# Import workflows
echo "📦 Importing workflows..."
echo ""

if [ ! -d "$WORKFLOWS_DIR" ]; then
    echo -e "${RED}❌ Workflows directory not found: $WORKFLOWS_DIR${NC}"
    exit 1
fi

import_count=0
success_count=0

for workflow_file in "$WORKFLOWS_DIR"/*.json; do
    if [ -f "$workflow_file" ]; then
        workflow_name=$(basename "$workflow_file" .json)
        import_count=$((import_count + 1))

        echo -ne "  Importing: ${BLUE}$workflow_name${NC}... "

        # Read and prepare workflow
        workflow_json=$(cat "$workflow_file")

        # Import workflow
        IMPORT_RESPONSE=$(curl -s -X POST \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer $TOKEN" \
            -d "$workflow_json" \
            "$N8N_URL/api/v1/workflows")

        if echo "$IMPORT_RESPONSE" | grep -q "id"; then
            echo -e "${GREEN}✓${NC}"
            success_count=$((success_count + 1))
        else
            echo -e "${RED}✗${NC}"
            echo "    Response: $IMPORT_RESPONSE"
        fi
    fi
done

echo ""
echo "=============================================="
echo -e "${GREEN}✓ Import Summary: $success_count/$import_count workflows imported${NC}"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "  1. Open http://localhost:5678 in your browser"
echo "  2. Login with: admin / n8n_admin_password"
echo "  3. Review and activate each workflow"
echo "  4. Configure any external service credentials"
echo ""
