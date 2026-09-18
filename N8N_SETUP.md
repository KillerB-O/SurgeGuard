# n8n Fresh Setup Guide

Complete guide to set up n8n with PostgreSQL using Docker.

## Prerequisites

- Docker & Docker Compose installed
- Terminal/Command line access
- Optional: curl for API calls

## Quick Start (One Command)

```bash
# From project root
chmod +x setup-n8n.sh && ./setup-n8n.sh
```

This single script will:
- Clean up old containers
- Start PostgreSQL and n8n
- Wait for services to be healthy
- Display credentials and next steps

## Step-by-Step Manual Setup

### 1. Stop and Clean Old Containers

```bash
docker-compose down --remove-orphans
docker volume rm surgekeeper_postgres_data surgekeeper_n8n_data 2>/dev/null || true
```

### 2. Start Services

```bash
docker-compose up -d
```

### 3. Verify Services Are Running

```bash
# Check container status
docker-compose ps

# Check PostgreSQL health
docker-compose logs postgres | grep -i "ready\|healthy"

# Check n8n logs
docker-compose logs n8n | tail -20
```

### 4. Wait for n8n to Be Ready

```bash
# Keep running until you see a successful response
curl http://localhost:5678/api/v1/health
```

### 5. Access n8n

Open in browser: **http://localhost:5678**

**Default Credentials:**
- Username: `admin`
- Password: `n8n_admin_password`

## Importing Workflows

### Option 1: Using Script

```bash
chmod +x import-workflows.sh
./import-workflows.sh
```

### Option 2: Manual Import via UI

1. Open http://localhost:5678
2. Login with admin credentials
3. Click "Import" (top menu)
4. Select each JSON file from `n8n/workflows/` directory
5. For each workflow:
   - Review the workflow
   - Configure any required credentials
   - Activate it

### Option 3: Manual Import via API

```bash
# Get auth token
TOKEN=$(curl -s -X POST \
  -H "Content-Type: application/json" \
  -d '{"email": "admin", "password": "n8n_admin_password"}' \
  http://localhost:5678/api/v1/login | grep -o '"token":"[^"]*' | cut -d'"' -f4)

# Import single workflow
curl -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d @n8n/workflows/commerce-orders-to-backend.json \
  http://localhost:5678/api/v1/workflows

# Import all workflows
for file in n8n/workflows/*.json; do
  curl -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d @"$file" \
    http://localhost:5678/api/v1/workflows
done
```

## Available Workflows

Your workflows are located in `n8n/workflows/`:

1. **commerce-order-revision-to-backend.json** - Order revision sync
2. **commerce-orders-to-backend.json** - Orders sync
3. **fulfillment-snapshot-to-backend.json** - Fulfillment snapshot
4. **fulfillment-status-to-backend.json** - Fulfillment status updates
5. **recovery-action-executor.json** - Recovery action execution

## Configuration

### Database Configuration (docker-compose.yml)

```yaml
PostgreSQL:
  Host: postgres
  Port: 5432
  Database: n8n
  User: n8n
  Password: n8n_password_change_me
```

### n8n Configuration

Basic Auth:
- Username: `admin`
- Password: `n8n_admin_password`

Change these in `docker-compose.yml` under n8n environment variables before first run.

### Backend Service Integration

If your backend API is running on a different host, update the workflow credentials:

1. In n8n UI, go to **Credentials**
2. Create/update your backend API connection
3. Set the correct backend URL (e.g., http://host.docker.internal:3000 for local backend)

## Verification Checklist

- [ ] Docker containers are running: `docker-compose ps`
- [ ] PostgreSQL is healthy: `docker exec surgekeeper_postgres pg_isready -U n8n`
- [ ] n8n is accessible: `curl http://localhost:5678/api/v1/health`
- [ ] Can login with admin credentials
- [ ] All workflows are imported
- [ ] Workflows can connect to backend API
- [ ] Test workflow execution

## Useful Commands

```bash
# View container logs
docker-compose logs -f n8n          # n8n logs
docker-compose logs -f postgres     # PostgreSQL logs

# Execute commands in containers
docker exec surgekeeper_n8n ps aux
docker exec surgekeeper_postgres psql -U n8n -d n8n -c "SELECT * FROM workflows;"

# Stop services
docker-compose down

# Stop and remove all data
docker-compose down -v

# Restart services
docker-compose restart n8n
docker-compose restart postgres

# Remove everything and start fresh
docker system prune -a
docker-compose up -d
```

## Troubleshooting

### PostgreSQL Connection Error

```bash
# Check PostgreSQL logs
docker-compose logs postgres

# Verify connection
docker exec surgekeeper_postgres psql -U n8n -d n8n -c "\l"
```

### n8n Not Starting

```bash
# Check detailed logs
docker-compose logs n8n

# Restart service
docker-compose restart n8n

# If still failing, rebuild
docker-compose down -v
docker-compose up -d
```

### Workflows Not Importing

```bash
# Check if n8n API is working
curl http://localhost:5678/api/v1/health

# Verify auth token
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"email": "admin", "password": "n8n_admin_password"}' \
  http://localhost:5678/api/v1/login

# Check workflow file syntax
cat n8n/workflows/commerce-orders-to-backend.json | jq .
```

### Port Already in Use

```bash
# Find process using port 5678
lsof -i :5678

# Or change port in docker-compose.yml
# Change "5678:5678" to "5679:5678"
```

## Environment Variables

Can be set in `docker-compose.yml` for n8n service:

```yaml
N8N_HOST: localhost
N8N_PORT: 5678
N8N_PROTOCOL: http
WEBHOOK_URL: http://localhost:5678/
NODE_ENV: production
```

## Testing Workflows

Once imported:

1. Open workflow in n8n
2. Click "Execute Workflow"
3. Check execution logs
4. Verify data in backend API
5. Check PostgreSQL for n8n logs

## Cleaning Up

To completely reset (removes all data):

```bash
docker-compose down -v
docker system prune -a
```

Then run `./setup-n8n.sh` again for fresh start.
