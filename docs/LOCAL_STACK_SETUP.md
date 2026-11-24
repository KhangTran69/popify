# Disposable local reviewer stack and cleanup

For a fresh local end-to-end review, use a **fresh clone or worktree** with no existing `.env` or application data. Compose names its PostgreSQL and n8n volumes from the project name, so a unique name keeps this review's database and workflow state separate from the default project. The API and n8n host ports are fixed at `8001` and `5678`; if either port is already in use, stop here rather than stopping another service.

Create the local-only configuration in the fresh checkout. Set unique values for the required secrets; keep `LLM_MODE=mock` and leave Shopify/OpenAI credentials unset.

```sh
# Run this check first. If it fails, stop; never overwrite an existing .env.
test ! -e .env
```

Only after the check succeeds, create the file exclusively and configure it. The shell's no-clobber mode makes creation fail if another process creates `.env` after the preflight:

```sh
(set -C; cat .env.example > .env) || { echo '.env appeared during setup; stop and inspect it.' >&2; exit 1; }
```

In one terminal, confirm Docker is already running and the ports are free. If Docker is unavailable, lsof is missing, or any listener appears, stop without changing the existing service.

```sh
docker info || { echo 'Docker is unavailable; stop before creating project resources.' >&2; exit 1; }
command -v lsof >/dev/null || { echo 'lsof is unavailable; stop before checking ports.' >&2; exit 1; }
PORT_CHECK_OUTPUT=$(lsof -nP -iTCP:8001,5678 -sTCP:LISTEN 2>&1)
PORT_CHECK_STATUS=$?
if [ "$PORT_CHECK_STATUS" -eq 0 ]; then
  if [ -n "$PORT_CHECK_OUTPUT" ]; then
    printf '%s\n' "$PORT_CHECK_OUTPUT" >&2
    echo 'A required port is in use; stop before starting the project.' >&2
    exit 1
  fi
elif [ "$PORT_CHECK_STATUS" -eq 1 ] && [ -z "$PORT_CHECK_OUTPUT" ]; then
  : # lsof found no listener on either port
else
  if [ -n "$PORT_CHECK_OUTPUT" ]; then printf '%s\n' "$PORT_CHECK_OUTPUT" >&2; fi
  echo 'Could not verify required ports; stop before starting the project.' >&2
  exit 1
fi
```

Only after the checks succeed, choose a new project name and verify that no matching resources exist. If any command prints a value, choose another name and repeat these checks.

```sh
# Keep this variable in the same terminal for setup, verification, and cleanup.
POPI_UUID=$(uuidgen) || { echo 'UUID generation failed; stop before creating project resources.' >&2; exit 1; }
POPI_UUID=$(printf '%s' "$POPI_UUID" | tr '[:upper:]' '[:lower:]') || { echo 'UUID normalization failed; stop.' >&2; exit 1; }
case "$POPI_UUID" in
  ????????-????-????-????-????????????) ;;
  *) echo 'UUID format is invalid; stop before creating project resources.' >&2; exit 1 ;;
esac
case "$POPI_UUID" in
  *[!0123456789abcdef-]*) echo 'UUID contains invalid characters; stop.' >&2; exit 1 ;;
esac
POPI_PROJECT="popify-review-$POPI_UUID"
export POPI_PROJECT
docker ps -aq --filter "label=com.docker.compose.project=$POPI_PROJECT" || { echo 'Docker container preflight failed; stop.' >&2; exit 1; }
docker volume ls -q --filter "label=com.docker.compose.project=$POPI_PROJECT" || { echo 'Docker volume preflight failed; stop.' >&2; exit 1; }
docker network ls -q --filter "label=com.docker.compose.project=$POPI_PROJECT" || { echo 'Docker network preflight failed; stop.' >&2; exit 1; }
printf 'Using isolated Compose project: %s\n' "$POPI_PROJECT"
```

Proceed only if all three resource checks returned no rows. In the same terminal, define the cleanup function and start the project:

```sh
# Record the configured file identity and contents so cleanup can leave it alone if it changes.
POPI_ENV_ID_LINE=$(ls -di .env) || { echo 'Cannot identify .env; stop before starting the project.' >&2; exit 1; }
POPI_ENV_ID=${POPI_ENV_ID_LINE%% *}
case "$POPI_ENV_ID" in ''|*[!0-9]*) echo 'Cannot validate the .env file identity.' >&2; exit 1 ;; esac
POPI_HASH_OUTPUT=$(shasum -a 256 .env) || { echo 'Cannot hash .env; stop before starting the project.' >&2; exit 1; }
POPI_ENV_SHA=${POPI_HASH_OUTPUT%% *}
case "$POPI_ENV_SHA" in ''|*[!0123456789abcdefABCDEF]*) echo 'Cannot validate the .env checksum.' >&2; exit 1 ;; esac
[ "${#POPI_ENV_SHA}" -eq 64 ] || { echo 'Cannot validate the full .env checksum.' >&2; exit 1; }
export POPI_ENV_ID POPI_ENV_SHA

cleanup_popify_review() {
  docker compose -p "$POPI_PROJECT" down --volumes --remove-orphans || return 1
  REVIEW_CONTAINERS=$(docker ps -aq --filter "label=com.docker.compose.project=$POPI_PROJECT") || return 1
  REVIEW_VOLUMES=$(docker volume ls -q --filter "label=com.docker.compose.project=$POPI_PROJECT") || return 1
  REVIEW_NETWORKS=$(docker network ls -q --filter "label=com.docker.compose.project=$POPI_PROJECT") || return 1
  if [ -n "$REVIEW_CONTAINERS" ] || [ -n "$REVIEW_VOLUMES" ] || [ -n "$REVIEW_NETWORKS" ]; then
    echo 'Cleanup incomplete; inspect only resources with the printed project label.' >&2
    return 1
  fi
  if [ -f .env ]; then
    POPI_CURRENT_ID_LINE=$(ls -di .env) || POPI_CURRENT_ID_LINE=
    POPI_CURRENT_ENV_ID=${POPI_CURRENT_ID_LINE%% *}
    POPI_CURRENT_HASH_OUTPUT=$(shasum -a 256 .env) || POPI_CURRENT_HASH_OUTPUT=
    POPI_CURRENT_ENV_SHA=${POPI_CURRENT_HASH_OUTPUT%% *}
    case "$POPI_CURRENT_ENV_SHA" in *[!0123456789abcdefABCDEF]*|'') POPI_CURRENT_ENV_SHA= ;; esac
    [ "${#POPI_CURRENT_ENV_SHA}" -eq 64 ] || POPI_CURRENT_ENV_SHA=
    if [ -n "$POPI_CURRENT_ENV_ID" ] && [ "$POPI_CURRENT_ENV_ID" = "$POPI_ENV_ID" ] && [ -n "$POPI_CURRENT_ENV_SHA" ] && [ "$POPI_CURRENT_ENV_SHA" = "$POPI_ENV_SHA" ]; then
      rm .env || return 1
      printf 'Removed isolated project %s and its unchanged generated .env.\n' "$POPI_PROJECT"
    else
      echo '.env changed, was replaced, or could not be verified; left it untouched.'
    fi
  else
    echo '.env is missing; nothing to remove.'
  fi
}

docker compose -p "$POPI_PROJECT" up --build -d
docker compose -p "$POPI_PROJECT" ps
```

Only continue when `db`, `api`, and `n8n` report healthy. Follow the [local Docker walkthrough](docs/REVIEWER_WALKTHROUGH.md) in the Browser, or run the documented synthetic workflow check:

```sh
docker compose -p "$POPI_PROJECT" exec -T api python -m scripts.demo
```

After a successful or failed review, call the cleanup function in the **same terminal**:

```sh
cleanup_popify_review || { echo 'Cleanup did not complete; keep the project name and inspect only its labeled resources.' >&2; exit 1; }
unset -f cleanup_popify_review
unset POPI_PROJECT
```

If a preflight stops, no project resources were started. Remove `.env` only if it is still the file this procedure created and configured. Keep the same `-p "$POPI_PROJECT"` on every Compose command. The named volumes are disposable only because this procedure creates them under a unique project name; do not use `down --volumes` with the default project or to troubleshoot a stack containing data you want to keep. This local review uses synthetic data and mock actions; it does not call Shopify, OpenAI, or an external action provider.
