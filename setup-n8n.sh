#!/bin/bash

# n8n Docker Setup Script for SurgeKeeper
# This script sets up a fresh n8n environment with Postgres

set -e

echo "🚀 Starting n8n Fresh Setup for SurgeKeeper"
echo "=============================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if Docker is running
echo "📋 Checking Docker installation..."
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker is not installed. Please install Docker first.${NC}"
    exit 1
fi

if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}❌ Docker daemon is not running. Please start Docker.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker is running${NC}"
echo ""

# Stop and remove existing containers (optional)
echo "🧹 Cleaning up old containers (if any)..."
docker-compose down --remove-orphans 2>/dev/null || true
echo -e "${GREEN}✓ Cleanup complete${NC}"
echo ""

# Start services
echo "🐳 Starting PostgreSQL and n8n services..."
docker-compose up -d
echo ""

# Wait for services to be ready
echo "⏳ Waiting for services to be healthy..."
sleep 5

max_retries=30
retry_count=0

while [ $retry_count -lt $max_retries ]; do
    if docker-compose ps | grep -q "postgres.*healthy"; then
        echo -e "${GREEN}✓ PostgreSQL is healthy${NC}"
        break
    fi
    echo "   Waiting for PostgreSQL... (${retry_count}/${max_retries})"
    sleep 2
    retry_count=$((retry_count + 1))
done

if [ $retry_count -eq $max_retries ]; then
    echo -e "${RED}❌ PostgreSQL failed to start. Check logs with: docker-compose logs postgres${NC}"
    exit 1
fi

echo ""
retry_count=0
while [ $retry_count -lt $max_retries ]; do
    if curl -s http://localhost:5678/api/v1/health > /dev/null 2>&1; then
        echo -e "${GREEN}✓ n8n is ready${NC}"
        break
    fi
    echo "   Waiting for n8n... (${retry_count}/${max_retries})"
    sleep 2
    retry_count=$((retry_count + 1))
done

if [ $retry_count -eq $max_retries ]; then
    echo -e "${RED}❌ n8n failed to start. Check logs with: docker-compose logs n8n${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}✓ All services are healthy!${NC}"
echo ""

# Display credentials
echo "📝 n8n Setup Complete!"
echo "=============================================="
echo -e "${YELLOW}Access n8n:${NC}"
echo "  URL: http://localhost:5678"
echo "  Username: admin"
echo "  Password: n8n_admin_password"
echo ""
echo -e "${YELLOW}PostgreSQL:${NC}"
echo "  Host: localhost"
echo "  Port: 5432"
echo "  Username: n8n"
echo "  Password: n8n_password_change_me"
echo ""
echo -e "${YELLOW}Useful Commands:${NC}"
echo "  View logs: docker-compose logs -f n8n"
echo "  Stop services: docker-compose down"
echo "  Remove all data: docker-compose down -v"
echo ""
