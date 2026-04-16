#!/usr/bin/env bash
# ============================================================================
# sync-public.sh — Synchronise la source canonique privée -> cellule-public
#
# Principe:
# 1. Whitelist stricte de paths copiés vers une copie fraîche
# 2. Scan anti-fuite regex (tokens, IPs privées, emails, passwords)
# 3. Abort dur si un pattern sensible est détecté
# 4. Rsync vers /tmp/cellule-public si clean
# 5. Commit + push (optionnel via --push)
#
# Usage:
#   ./sync-public.sh              # dry-run: prepare + scan + diff, PAS de commit
#   ./sync-public.sh --commit     # commit les changements, pas de push
#   ./sync-public.sh --push       # commit + push origin main
#
# Exit codes:
#   0 = OK (changes staged/committed/pushed selon mode)
#   1 = leak détecté (abort)
#   2 = erreur I/O / whitelist invalide
#   3 = pas de changement à publier
# ============================================================================

set -euo pipefail

SRC="/home/harpersatrage/iamine"
DST="/tmp/cellule-public"
STAGING="/tmp/cellule-public-staging-$$"
MODE="dry-run"

for arg in "$@"; do
  case "$arg" in
    --commit) MODE="commit" ;;
    --push)   MODE="push"   ;;
    --help|-h)
      grep '^#' "$0" | head -25
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

log() { echo "[sync-public] $*" >&2; }

# ---------------------------------------------------------------------------
# 1. Prépare le staging (copie fraîche de la whitelist)
# ---------------------------------------------------------------------------
log "Staging: $STAGING"
rm -rf "$STAGING"
mkdir -p "$STAGING"

# Whitelist: paths autorisées à être copiées depuis SRC
# Format: chemin relatif (glob rsync), un par ligne
WHITELIST=(
  # Code iamine (package Python)
  "iamine/__init__.py"
  "iamine/__main__.py"
  "iamine/*.py"
  "iamine/core/*.py"

  # Assets statiques publics
  "iamine/static/*.html"
  "iamine/static/*.css"
  "iamine/static/*.js"
  "iamine/static/docs/*.html"
  "iamine/static/docs/*.css"
  "iamine/static/docs/*.js"
  "iamine/static/icons/*"
  "iamine/cellule.ico"
  "iamine/iamine_icon.ico"

  # Migrations SQL (schéma, pas de données)
  "iamine/migrations/*.sql"
  "migrations/*.sql"

  # Build & packaging
  "pyproject.toml"
  "requirements.txt"
  "README.md"
  "LICENSE"

  # CI/CD workflows are maintained DIRECTLY in cellule-public
  # (not in canonical VPS) — exclude them from sync to avoid overwrite.
  # ".github/workflows/*.yml"  # DO NOT UNCOMMENT
  ".pyinstaller-hooks/*"

  # Scripts d'installation public (rien d'admin)
  "install.sh"
  "install.ps1"
  "install-worker.sh"
  "install-worker.ps1"

  # Docker (si public)
  "Dockerfile"
  "docker-compose.yml"
  "docker-entrypoint.sh"

  # Config deploy public
  "fly.toml"
  "render.yaml"

  # Images icônes (non sensibles)
  "iamine_icon_256.png"
  "iamine_icon_512.png"
)

# Blacklist: patterns GLOB qui sont EXCLUS même s'ils matchent une whitelist
# (ceinture + bretelles)
BLACKLIST_GLOBS=(
  "iamine/admin_pool*.py"          # routes admin serveur
  "iamine/red_*.py"                # RED admin agent
  "iamine/RED/*"                   # RED workspace
  "iamine/reports/*"               # rapports internes
  "iamine/log/*"                   # logs
  "iamine/models/*"                # modèles GGUF
  "iamine/dist/*"                  # wheels
  "iamine/venv/*"                  # virtualenv
  "iamine/__pycache__/*"           # bytecode
  "iamine/clef/*"                  # clés
  "iamine/.ssh/*"                  # SSH keys
  "iamine/.env*"                   # env files
  "iamine/backups/*"               # backups DB
  "iamine/snapshots/*"             # snapshots VPS
  "iamine/.git/*"                  # git metadata privé
  "iamine/scripts/admin-*"         # scripts admin
  "iamine/scripts/snapshot.sh"     # script snapshot privé
  "iamine/scripts/migrate-vps.sh"  # script migration VPS
  "iamine/CLAUDE*.md"              # notes Claude internes
  "iamine/RED/*"                   # RED notes
  "iamine/static/admin*.html"      # UI admin serveur (pas d'intérêt public)
  "iamine/static/admin*.js"        # JS admin
  "iamine/settings.py"             # connection strings par défaut (dev)
)

log "Copying whitelist paths..."
for pattern in "${WHITELIST[@]}"; do
  # resolve glob in SRC
  for f in $SRC/$pattern; do
    [ -e "$f" ] || continue
    rel="${f#$SRC/}"

    # check blacklist
    skip=0
    for bl in "${BLACKLIST_GLOBS[@]}"; do
      case "$rel" in
        $bl) skip=1; break ;;
      esac
    done
    [ $skip -eq 1 ] && { log "  SKIP blacklist: $rel"; continue; }

    # create parent dir + copy
    mkdir -p "$(dirname "$STAGING/$rel")"
    cp -a "$f" "$STAGING/$rel"
  done
done

FILE_COUNT=$(find "$STAGING" -type f | wc -l)
log "Staged $FILE_COUNT files"
[ "$FILE_COUNT" -eq 0 ] && { log "ERROR: 0 files staged, whitelist broken"; exit 2; }

# ---------------------------------------------------------------------------
# 2. Scan anti-fuite
# ---------------------------------------------------------------------------
log "Scanning for sensitive patterns..."

# Patterns regex à refuser (si match = abort)
SCAN_PATTERNS=(
  # Secrets & tokens
  'iam_[0-9a-f]{16,}'                              # worker api token
  'ghp_[A-Za-z0-9]{30,}'                           # github PAT
  'sk-ant-[A-Za-z0-9_-]{20,}'                      # claude API key
  'hf_[A-Za-z0-9]{30,}'                            # huggingface token
  'HF_TOKEN\s*=\s*["'"'"'][^"'"'"'\s]{20,}'        # huggingface env var
  'ADMIN_TOKEN\s*=\s*["'"'"'][^"'"'"'\s]{10,}'     # admin token with value
  'ADMIN_PASSWORD\s*=\s*["'"'"'][^"'"'"'\s]+["'"'"']'  # admin password
  'postgres://[^@\s]+:[^@\s]+@'                    # postgres with creds
  'postgresql://[^@\s]+:[^@\s]+@'
  'password\s*=\s*["'"'"'][^"'"'"'\s]+["'"'"']'   # password literal (hors placeholder)

  # IPs privées et publiques connues
  '192\.168\.[0-9]+\.[0-9]+'                       # LAN privée
  '10\.[0-9]+\.[0-9]+\.[0-9]+'                     # LAN privée
  '172\.(1[6-9]|2[0-9]|3[0-1])\.[0-9]+\.[0-9]+'    # LAN privée
  '109\.123\.240\.151'                             # VPS public IP (doit utiliser le hostname)
  '80\.14\.168\.158'                               # Gladiator public IP

  # Hostnames LAN personnels
  'harper-satrage'
  'harpersat-System'
  'harpersat@192'

  # Emails personnels
  '[a-zA-Z0-9._+-]+@gmail\.com'
  '[a-zA-Z0-9._+-]+@hotmail\.(com|fr)'
  '[a-zA-Z0-9._+-]+@yahoo\.(com|fr)'
  'david\.mourgues'
  'sgc\.sic61'

  # Clés SSH privées
  'BEGIN OPENSSH PRIVATE KEY'
  'BEGIN RSA PRIVATE KEY'
  'BEGIN EC PRIVATE KEY'
)

# Allowlist: patterns attendus qui matcheraient à tort (faux positifs)
ALLOWLIST_REGEX=(
  '192\.168\.1\.1 # TEMPLATE'          # doc template placeholder
  'YOUR_PASSWORD'
  'YOUR_TOKEN'
  'iam_YOUR_TOKEN'
  'change-me-in-production'
)

LEAK=0
LEAK_REPORT="/tmp/sync-public-leaks-$$.txt"
: > "$LEAK_REPORT"

for pattern in "${SCAN_PATTERNS[@]}"; do
  matches=$(grep -rEn "$pattern" "$STAGING" 2>/dev/null || true)
  [ -z "$matches" ] && continue

  # filter allowlist
  while IFS= read -r match; do
    [ -z "$match" ] && continue
    allowed=0
    for allow in "${ALLOWLIST_REGEX[@]}"; do
      if echo "$match" | grep -qE "$allow"; then
        allowed=1
        break
      fi
    done
    if [ $allowed -eq 0 ]; then
      echo "LEAK [$pattern]: $match" >> "$LEAK_REPORT"
      LEAK=1
    fi
  done <<< "$matches"
done

if [ $LEAK -eq 1 ]; then
  log "!!! LEAK DETECTED — sync aborted !!!"
  log "Report: $LEAK_REPORT"
  echo "" >&2
  cat "$LEAK_REPORT" | head -50 >&2
  echo "" >&2
  log "Fix the source files, then re-run. DO NOT modify staging directly."
  exit 1
fi

log "Scan clean — no leaks detected"
rm -f "$LEAK_REPORT"

# ---------------------------------------------------------------------------
# 3. Sync vers cellule-public avec rsync (delete for clean mirror)
# ---------------------------------------------------------------------------
[ ! -d "$DST" ] && { log "ERROR: $DST missing — clone cellule-public first"; exit 2; }
[ ! -d "$DST/.git" ] && { log "ERROR: $DST not a git repo"; exit 2; }

log "Rsync staging -> $DST (excluding .git/)"

# We only rsync files that are in the staging (new/changed)
# We do NOT delete files outside the whitelist from DST — we just add/update
# DST's non-whitelisted files (e.g. pre-existing public workflow overrides)
# are preserved. If you want full mirror deletion, add --delete below.
rsync -a --no-owner --no-group \
  --exclude='.git' \
  "$STAGING/" "$DST/"

cd "$DST"

# Show diff summary
DIFF_STATS=$(git diff --stat HEAD 2>/dev/null | tail -1 || echo "")
UNTRACKED=$(git ls-files --others --exclude-standard | wc -l)

if [ -z "$DIFF_STATS" ] && [ "$UNTRACKED" -eq 0 ]; then
  log "No changes vs cellule-public HEAD — nothing to publish"
  rm -rf "$STAGING"
  exit 3
fi

log "Changes detected:"
git status -s | head -20 >&2
echo "" >&2
log "Diff stat: $DIFF_STATS"

# ---------------------------------------------------------------------------
# 4. Commit + push selon mode
# ---------------------------------------------------------------------------
if [ "$MODE" = "dry-run" ]; then
  log "DRY-RUN mode: changes staged in $DST, not committed"
  log "Run with --commit or --push to persist"
  rm -rf "$STAGING"
  exit 0
fi

# Gather version + SHA for commit message
VERSION=$(grep -E '^version' "$SRC/pyproject.toml" | head -1 | cut -d'"' -f2 || echo "unknown")
SRC_SHA=$(cd "$SRC" && git rev-parse --short HEAD 2>/dev/null || echo "no-git")

git add -A

COMMIT_MSG="chore(sync): public mirror v${VERSION} from ${SRC_SHA}

Synchronized via scripts/sync-public.sh:
- $FILE_COUNT whitelisted files copied
- Leak scan clean (no tokens/IPs/credentials detected)
"

git -c user.email=david.mourgues@gmail.com -c user.name='David Mourgues' \
  commit -m "$COMMIT_MSG" >&2 || {
  log "Commit failed (maybe nothing to commit)"
  rm -rf "$STAGING"
  exit 3
}

log "Committed: $(git log --oneline -1)"

if [ "$MODE" = "push" ]; then
  if [ -z "${GH_TOKEN:-}" ]; then
    log "ERROR: GH_TOKEN not set in env — cannot push"
    log "Run: GH_TOKEN=ghp_xxx ./sync-public.sh --push"
    rm -rf "$STAGING"
    exit 2
  fi
  log "Pushing to origin main..."
  git push "https://${GH_TOKEN}@github.com/CELLULEAI/POOL.git" main >&2
  log "Push done"
fi

rm -rf "$STAGING"
log "Sync complete"
