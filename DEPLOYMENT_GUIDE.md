# Production Deployment Guide: Forum Content Moderation

This document contains everything the DevOps / SysAdmin team needs to deploy the Forum Content Moderation system to production.

---

## 1. System Architecture

The solution consists of two components:
1. **Odoo 19 Backend with Custom Modules**:
   - `forum_content_moderation`: Handles text moderation (toxic, insult, profanity via Detoxify; illegal keyword + semantic zero-shot classification via BART-large-MNLI) and image moderation coordination.
   - `custom_forum_api`: Handles user-facing redirect notifications on the forum frontend when posts are placed in `pending` (Waiting Validation).
2. **NSFW Image Detection Microservice** (`nsfw-service`):
   - Node.js Express service running TensorFlow.js + NSFWJS on port `5001`.
   - Endpoint: `POST /check-image` (multipart/form-data with field `image`).

---

## 2. Dependencies & Build Requirements

### A. Python Dependencies (Odoo Environment)
Add these to your production Odoo Python environment / Dockerfile:
```bash
pip install torch transformers detoxify requests
```

### B. Pre-caching HuggingFace Model Weights (Critical for Docker / CI/CD)
To prevent the container from downloading weights (~2GB) on the first user post or failing in air-gapped networks, pre-warm the HuggingFace cache during Docker build:
```dockerfile
RUN python3 -c "from detoxify import Detoxify; Detoxify('original'); from transformers import pipeline; pipeline('zero-shot-classification', model='facebook/bart-large-mnli')"
```
This ensures model weights are baked into `/root/.cache/huggingface` or the application user's home directory.

### C. Node.js Dependencies (NSFW Service)
In `/path/to/nsfw-service`:
```bash
npm install
```
Dependencies defined in `package.json`:
- `express`
- `multer`
- `@tensorflow/tfjs`
- `nsfwjs`
- `sharp`

---

## 3. Microservice Process Management (PM2 or Docker)

The NSFW microservice must run as a managed daemon.

### Option A: Using PM2
```bash
npm install -g pm2
cd /path/to/nsfw-service
pm2 start server.js --name "nsfw-service"
pm2 startup
pm2 save
```

### Option B: Docker Compose
```yaml
version: '3.8'

services:
  nsfw-service:
    build: ./nsfw-service
    restart: always
    ports:
      - "127.0.0.1:5001:5001"
    environment:
      - PORT=5001
```

---

## 4. Odoo Configuration (`odoo.conf`)

Add the custom modules to your `addons_path` and configure the memory/CPU limits:

```ini
[options]
; Database credentials (do NOT use postgres user in production)
db_host = 127.0.0.1
db_port = 5432
db_user = odoo
db_password = <SECURE_PASSWORD>
db_name = <PRODUCTION_DB_NAME>

; Addons paths
addons_path = /path/to/odoo/addons,/path/to/forum-moderation-project,/path/to/custom_forum_api

; Reverse proxy & SSL
proxy_mode = True

; Memory & Timeout Configuration for AI Models
; Disable or increase memory ceiling so workers do not cycle on transformer inference
limit_memory_hard = 0
limit_memory_soft = 0
limit_time_cpu = 120
limit_time_real = 240
```

---

## 5. Nginx Reverse Proxy Configuration

Ensure client upload limits accommodate image attachments:
```nginx
server {
    listen 443 ssl http2;
    server_name forum.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    client_max_body_size 25M;

    location / {
        proxy_pass http://127.0.0.1:8069;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
```

---

## 6. Odoo System Parameters (Configurable via Settings > Technical > Parameters)

| Parameter Key | Default Value | Purpose |
| :--- | :--- | :--- |
| `forum_content_moderation.nsfw_service_url` | `http://localhost:5001/check-image` | URL of the NSFW microservice (change if hosted on separate container/host) |
| `forum_content_moderation.odoo_base_url` | `http://localhost:8069` | Internal base URL for resolving external image links |
| `forum_content_moderation.text_block_threshold` | `0.85` | Toxicity score above which post is held in pending |
| `forum_content_moderation.text_review_threshold` | `0.70` | Borderline toxicity threshold |
| `forum_content_moderation.image_block_threshold` | `0.60` | NSFW score above which post is held in pending |
| `forum_content_moderation.image_review_threshold` | `0.40` | Borderline NSFW threshold |
