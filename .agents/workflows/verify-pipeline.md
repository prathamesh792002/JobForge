# JobForge — Pipeline Verification Workflow

## Phase Verification Commands

### Phase 1: Infrastructure Foundation
```bash
docker-compose up -d && sleep 5 && alembic upgrade head && echo "Phase 1 PASS"
```

### Phase 2: Telegram Gateway
Start the bot in dev mode and send `/start`. Confirm response received.

### Phase 3: Extraction + Classification
```bash
python -m tests.test_extractor
python -m tests.test_classifier
```

### Phase 4: Tailoring + Compilation
```bash
python -m tests.test_tailor
python -m tests.test_compiler
```

### Phase 5: Full E2E
Full end-to-end pipeline test: link → PDF → email draft → approve → email sent → open alert → /stats
