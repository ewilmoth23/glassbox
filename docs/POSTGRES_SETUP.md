# PostgreSQL + PostGIS + TimescaleDB + pgvector — Mac Mini setup

**Audience:** Ethan (operator). Copy-paste these commands on the Mac Mini.
**Goal:** Stand up the durable Postgres database that turns Glassbox from a "live globe that forgets" into a real product with a 30-day searchable archive.
**Time:** 30-60 minutes the first time, depending on whether you already have Homebrew + dependencies.
**Time to revert:** <5 minutes (just `brew services stop` and the install is dormant; data is in a directory you can move or delete).

---

## CHOICES MADE FOR YOU (do not deviate without reason)

- **Postgres version:** 16 (latest stable LTS-ish; TimescaleDB officially supported)
- **Install method:** Homebrew (bare-metal, no Docker) — per V2 plan reject of Docker on Mac Mini
- **Extensions:** PostGIS 3.4+, TimescaleDB 2.14+, pgvector 0.7+, pg_trgm, uuid-ossp, btree_gist
- **Data directory:** `/opt/homebrew/var/postgresql@16` (Homebrew default on Apple Silicon)
- **Service management:** `brew services` for v1.0; switch to launchd plist (Phase 0) once stable
- **Database name:** `glassbox`
- **Roles:** `glassbox` (full DDL), `glassbox_writer` (INSERT/UPDATE/SELECT), `glassbox_reader` (SELECT)

If you'd prefer the Docker path (e.g. for clean uninstall), see Appendix A at the bottom — same end-state but different operational pattern.

---

## STEP 0 — PRE-FLIGHT (5 min)

### 0.1 — Confirm Homebrew is installed and current

```bash
brew --version
```

Expected: `Homebrew 4.x.x` or newer. If missing or older:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew update
```

### 0.2 — Snapshot any existing Postgres install

If you already have Postgres on this machine, **stop and back it up** before continuing. Multiple Postgres versions can coexist via Homebrew but you don't want to fight it.

```bash
brew services list | grep postgres
```

If anything shows up: `brew services stop postgresql@<version>` and back up its data dir before proceeding.

### 0.3 — Verify free disk space

```bash
df -h /opt/homebrew
```

Need at least 5 GB free. v1.0 retention (90 days) will use ~30-50 GB at full operating volume per the data estimates in `GLASSBOX_V2_MIGRATION_PLAN.md`.

---

## STEP 1 — INSTALL (10 min)

### 1.1 — Install PostgreSQL 16

```bash
brew install postgresql@16
```

Add to PATH (one time):

```bash
echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Verify:

```bash
psql --version
# Expected: psql (PostgreSQL) 16.x
```

### 1.2 — Install PostGIS

```bash
brew install postgis
```

PostGIS auto-installs against the latest Postgres in your Cellar. If you have multiple versions, force the linkage:

```bash
brew unlink postgis && brew link postgis --force
```

### 1.3 — Install TimescaleDB

```bash
brew tap timescale/tap
brew install timescaledb
```

TimescaleDB needs a one-time Postgres config tune. Run:

```bash
timescaledb_move.sh
# Follow the prompts. Default answers are fine — say "yes" to update postgresql.conf.
```

If `timescaledb_move.sh` isn't on your PATH:

```bash
/opt/homebrew/opt/timescaledb/bin/timescaledb_move.sh
```

### 1.4 — Install pgvector

```bash
brew install pgvector
```

If Homebrew doesn't have it (older versions), build from source:

```bash
cd /tmp
git clone --branch v0.7.4 https://github.com/pgvector/pgvector.git
cd pgvector
make
make install
cd - && rm -rf /tmp/pgvector
```

### 1.5 — Sanity check installs

```bash
ls /opt/homebrew/opt/postgresql@16/share/extension/ | grep -E "(postgis|timescaledb|vector|pg_trgm|uuid-ossp|btree_gist)"
```

Expected output (order may vary):

```
btree_gist--1.7.sql
pg_trgm--1.6.sql
postgis--3.4.x.sql
timescaledb--2.x.x.sql
uuid-ossp--1.1.sql
vector--0.7.x.sql
```

If any are missing, the corresponding `brew install` failed silently — re-run it.

---

## STEP 2 — INITIALIZE THE DATABASE (5 min)

### 2.1 — Start Postgres for the first time

```bash
brew services start postgresql@16
```

Verify it's running:

```bash
brew services list | grep postgresql@16
# Expected: postgresql@16  started  ethan  ~/Library/LaunchAgents/...
```

Connect as the bootstrap user:

```bash
psql postgres
```

If you see the `postgres=#` prompt, you're in. Type `\q` to exit.

### 2.2 — Create the role + database

Save the password somewhere safe — **DO NOT** put it in any committed file. Add to `.env.glassbox` (which is gitignored).

```bash
# Generate a strong password
GLASSBOX_DB_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)
echo "Generated password (save this): $GLASSBOX_DB_PASSWORD"
```

Now create the role + database:

```bash
psql postgres <<EOF
CREATE USER glassbox WITH PASSWORD '$GLASSBOX_DB_PASSWORD';
CREATE DATABASE glassbox OWNER glassbox;
GRANT ALL PRIVILEGES ON DATABASE glassbox TO glassbox;
EOF
```

Add to `.env.glassbox` at MEWR root (NOT committed):

```bash
cat >> "$GLASSBOX_HOME/.env" <<EOF

# Postgres (added $(date +%Y-%m-%d))
GLASSBOX_DB_HOST="127.0.0.1"
GLASSBOX_DB_PORT="5432"
GLASSBOX_DB_NAME="glassbox"
GLASSBOX_DB_USER="glassbox"
GLASSBOX_DB_PASSWORD="$GLASSBOX_DB_PASSWORD"
GLASSBOX_DB_URL="postgresql://glassbox:$GLASSBOX_DB_PASSWORD@127.0.0.1:5432/glassbox"
EOF
```

### 2.3 — Run the init.sql schema

```bash
cd "$GLASSBOX_HOME"

# As the glassbox user (will prompt for password from step 2.2)
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql \
  -h 127.0.0.1 -U glassbox -d glassbox \
  -f infra/postgres/init.sql
```

Expected output: a string of `CREATE EXTENSION`, `CREATE TABLE`, `CREATE INDEX`, `INSERT 0 1` lines, ending with no errors. Any line starting with `ERROR:` means something failed.

If you see `ERROR: extension "timescaledb" must be loaded via shared_preload_libraries`:

```bash
echo "shared_preload_libraries = 'timescaledb'" >> /opt/homebrew/var/postgresql@16/postgresql.conf
brew services restart postgresql@16
# Re-run the init.sql
```

---

## STEP 3 — VERIFY (5 min)

### 3.1 — Schema is correct

```bash
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql -h 127.0.0.1 -U glassbox -d glassbox <<'EOF'
SELECT version();
SELECT PostGIS_Version();
SELECT extversion FROM pg_extension WHERE extname IN
  ('postgis', 'timescaledb', 'vector', 'pg_trgm', 'uuid-ossp', 'btree_gist')
ORDER BY extname;
SELECT * FROM schema_migration;
EOF
```

Expected: PostgreSQL 16.x, PostGIS 3.4+, TimescaleDB 2.x, vector 0.7+. The `schema_migration` table should have one row with version `001-init`.

### 3.2 — Hypertables are correct

```bash
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql -h 127.0.0.1 -U glassbox -d glassbox <<'EOF'
SELECT hypertable_name, num_chunks
FROM timescaledb_information.hypertables;
EOF
```

Expected:

```
   hypertable_name | num_chunks
-------------------+------------
 position_track    | 0
 event             | 0
 api_audit_log     | 0
```

### 3.3 — Health view works

```bash
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql -h 127.0.0.1 -U glassbox -d glassbox \
  -c "SELECT * FROM v_db_health;"
```

Expected: a row of zeros (nothing in the DB yet).

### 3.4 — Insert a test row + read it back

```bash
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql -h 127.0.0.1 -U glassbox -d glassbox <<'EOF'
INSERT INTO source (source_type, fetched_at)
  VALUES ('install_test', NOW());
SELECT id, source_type, fetched_at FROM source WHERE source_type = 'install_test';
DELETE FROM source WHERE source_type = 'install_test';
EOF
```

Expected: 1 row inserted, 1 row returned, 1 row deleted.

If all four checks pass, the database is ready.

---

## STEP 4 — BACKUP SCRIPT (5 min)

Without backups, a disk failure = lose the moat. Set up daily backups now.

### 4.1 — Create the backup script

```bash
mkdir -p ~/mewr-backups/postgres

cat > ~/bin/backup_glassbox_db.sh <<'EOF'
#!/usr/bin/env bash
# Daily Glassbox Postgres backup. Run via launchd (see com.mewr.glassbox-db-backup.plist).
# Keeps last 7 daily, 4 weekly, 12 monthly.

set -euo pipefail
BACKUP_DIR="$HOME/mewr-backups/postgres"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DOW=$(date +%a)  # Mon, Tue, ...
DOM=$(date +%d)
mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly" "$BACKUP_DIR/monthly"

# Source env to get password
set -a
source "$GLASSBOX_HOME/.env"
set +a

# Daily backup (compressed custom format = small + restorable subset of tables)
pg_dump -h 127.0.0.1 -U "$GLASSBOX_DB_USER" -Fc -Z 6 \
  -d "$GLASSBOX_DB_NAME" \
  -f "$BACKUP_DIR/daily/glassbox_${TIMESTAMP}.dump"

# Weekly: copy Sunday's daily into weekly/
if [ "$DOW" = "Sun" ]; then
  cp "$BACKUP_DIR/daily/glassbox_${TIMESTAMP}.dump" \
     "$BACKUP_DIR/weekly/glassbox_${TIMESTAMP}.dump"
fi

# Monthly: copy 1st-of-month's daily into monthly/
if [ "$DOM" = "01" ]; then
  cp "$BACKUP_DIR/daily/glassbox_${TIMESTAMP}.dump" \
     "$BACKUP_DIR/monthly/glassbox_${TIMESTAMP}.dump"
fi

# Prune: keep 7 daily, 4 weekly, 12 monthly
find "$BACKUP_DIR/daily" -name "glassbox_*.dump" -mtime +7 -delete
find "$BACKUP_DIR/weekly" -name "glassbox_*.dump" -mtime +28 -delete
find "$BACKUP_DIR/monthly" -name "glassbox_*.dump" -mtime +366 -delete

echo "$(date) — backup OK — $(du -h "$BACKUP_DIR/daily/glassbox_${TIMESTAMP}.dump" | cut -f1)"
EOF

chmod +x ~/bin/backup_glassbox_db.sh
mkdir -p ~/bin
```

### 4.2 — Test the backup

```bash
~/bin/backup_glassbox_db.sh
ls -la ~/mewr-backups/postgres/daily/
```

Expected: a `.dump` file <1 MB (database is empty).

### 4.3 — Schedule the backup

Save this as `~/Library/LaunchAgents/com.mewr.glassbox-db-backup.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mewr.glassbox-db-backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>~/bin/backup_glassbox_db.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>15</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>__HOME__/mewr-logs/glassbox-db-backup.log</string>
  <key>StandardErrorPath</key>
  <string>__HOME__/mewr-logs/glassbox-db-backup.log</string>
</dict>
</plist>
```

Replace `__HOME__` with your actual home, then load:

```bash
launchctl load ~/Library/LaunchAgents/com.mewr.glassbox-db-backup.plist
```

Backup will run nightly at 3:15 AM.

### 4.4 — Test a restore (DO THIS BEFORE YOU NEED IT)

```bash
# Create a test database
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql postgres -c "CREATE DATABASE glassbox_restore_test OWNER glassbox;"

# Restore the backup into it
LATEST_BACKUP=$(ls -t ~/mewr-backups/postgres/daily/*.dump | head -1)
PGPASSWORD="$GLASSBOX_DB_PASSWORD" pg_restore \
  -h 127.0.0.1 -U glassbox \
  -d glassbox_restore_test \
  --no-owner --no-acl \
  "$LATEST_BACKUP"

# Verify it has the schema
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql -h 127.0.0.1 -U glassbox -d glassbox_restore_test \
  -c "SELECT * FROM schema_migration;"

# Drop the test DB
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql postgres -c "DROP DATABASE glassbox_restore_test;"
```

If the restore worked, your backup process is verified. **Do not skip this test.** Untested backups are not backups.

---

## STEP 5 — INTEGRATE WITH HEALTHCHECK (2 min)

The existing `CHECK_GLASSBOX_PRODUCTION.sh` should now probe Postgres too. Append a new section by editing the file or run this snippet to verify the DB manually:

```bash
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql -h 127.0.0.1 -U glassbox -d glassbox \
  -c "SELECT * FROM v_db_health;" -t
```

Expected: row of stats. Wire this into the healthcheck in Phase 0.8.

---

## STEP 6 — REVERT (if you need to remove everything)

If anything goes wrong and you want to start over:

```bash
# Stop service
brew services stop postgresql@16

# Drop the database (irreversible — back up first if you have data!)
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql postgres -c "DROP DATABASE IF EXISTS glassbox;"
PGPASSWORD="$GLASSBOX_DB_PASSWORD" psql postgres -c "DROP USER IF EXISTS glassbox;"

# Remove the data directory
rm -rf /opt/homebrew/var/postgresql@16

# Uninstall (optional — only if you want Postgres entirely gone)
brew uninstall postgresql@16 postgis pgvector
brew untap timescale/tap

# Remove backup launchd
launchctl unload ~/Library/LaunchAgents/com.mewr.glassbox-db-backup.plist
rm ~/Library/LaunchAgents/com.mewr.glassbox-db-backup.plist
```

The Glassbox server code that hasn't been migrated to dual-write yet will keep working as before — it doesn't depend on Postgres. This is the revert safety net per V2 plan Rule 9.

---

## OPERATIONAL NOTES

### Daily commands you'll actually use

| Command | What it does |
|---|---|
| `brew services list` | See if Postgres is running |
| `brew services restart postgresql@16` | Restart Postgres (e.g. after config change) |
| `psql -h 127.0.0.1 -U glassbox -d glassbox` | Connect to the DB (will prompt for password unless PGPASSWORD env set) |
| `\dt` (inside psql) | List all tables |
| `\d entity` (inside psql) | Describe the entity table |
| `tail -f ~/mewr-logs/glassbox-db-backup.log` | Watch backup logs |
| `tail -f /opt/homebrew/var/log/postgresql@16.log` | Watch Postgres server log |

### When the DB starts feeling slow

```bash
# Top 10 slowest queries (last hour) — requires pg_stat_statements
psql -h 127.0.0.1 -U glassbox -d glassbox <<'EOF'
SELECT
  substring(query, 1, 80) AS query_short,
  calls,
  round(mean_exec_time::numeric, 2) AS mean_ms,
  round(total_exec_time::numeric, 2) AS total_ms
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;
EOF
```

If `pg_stat_statements` isn't installed, add it: `CREATE EXTENSION pg_stat_statements;` then add `pg_stat_statements` to `shared_preload_libraries` in postgresql.conf and restart.

### Memory tuning

Default Postgres uses ~128 MB shared buffers — fine for v1.0. Tune later if needed:

```bash
# In /opt/homebrew/var/postgresql@16/postgresql.conf:
# shared_buffers = 2GB         # 25% of available RAM
# effective_cache_size = 6GB   # 75% of available RAM
# work_mem = 32MB              # per query operation
# maintenance_work_mem = 512MB # for VACUUM, CREATE INDEX
```

For a 24GB Mac Mini that's also running Ollama (~9GB) + ingesters (~1GB) + macOS (~4GB), ~10GB is comfortable for Postgres.

---

## APPENDIX A — ALTERNATIVE: Docker install path

If you'd prefer Docker (cleaner uninstall, isolation), use this instead of Steps 1-3:

```bash
brew install --cask docker
open -a Docker  # let Docker Desktop start

mkdir -p ~/glassbox-postgres-data

docker run -d \
  --name glassbox-postgres \
  --restart unless-stopped \
  -p 127.0.0.1:5432:5432 \
  -v ~/glassbox-postgres-data:/var/lib/postgresql/data \
  -v "$GLASSBOX_HOME/infra/postgres:/docker-entrypoint-initdb.d:ro" \
  -e POSTGRES_DB=glassbox \
  -e POSTGRES_USER=glassbox \
  -e POSTGRES_PASSWORD="$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)" \
  timescale/timescaledb-ha:pg16

# Then add pgvector inside the container
docker exec -it glassbox-postgres bash
apt-get update && apt-get install -y postgresql-16-pgvector
exit
```

Trade-offs:
- ✅ Easy to uninstall (`docker rm -f glassbox-postgres`)
- ✅ One image has Postgres + PostGIS + TimescaleDB pre-bundled
- ❌ Adds ~200 MB Docker Desktop overhead always running
- ❌ Volume mount semantics on macOS can be slow (VirtIOFS helps)
- ❌ Per the V2 plan reject list, we said no Docker on Mac Mini — only use this if Homebrew install is failing for some specific reason

---

## WHAT'S NEXT

After this guide is complete and verified:

1. **Phase 0.8** — wire DB checks into `CHECK_GLASSBOX_PRODUCTION.sh`
2. **Phase 1.1** — modify `planes.py` ingester to dual-write (KV hot cache + Postgres durable)
3. **Phase 1.2** — new viewport REST endpoint that queries Postgres spatially
4. **Phase 1.3-1.6** — entity detail, first algorithm, LLM brief, frontend wiring

Each subsequent phase builds on this DB. If this step isn't solid, everything downstream is fragile.

**Document any deviations from this guide in `GLASSBOX_BIBLE.md` so future-you knows what's actually installed.**
