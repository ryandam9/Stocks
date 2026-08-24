#!/usr/bin/env bash
# build_image.sh
# Usage: build_image.sh [--push] [--allow-dirty] [--allow-unpushed]
#
# Builds the container image and stamps it with the commit it was built from.
#
# The stamp is the only durable record of what ran: the image excludes .git, so
# src/runmeta.py cannot ask git and reads STOCKS_CODE_REVISION instead. Passing
# that by hand (docker build --build-arg GIT_REVISION=$(git rev-parse ...)) will
# happily label an image with a commit whose code it does not contain, because
# the build copies the *working tree*, not the commit. Nothing downstream can
# detect the discrepancy afterwards -- run_metadata.code_revision simply lies.
#
# So the checks below refuse to produce a mislabelled image rather than trusting
# the operator to remember. Each has an escape hatch for genuine local work, and
# each escape hatch changes the tags so a suspect image cannot reach production.
#
#   --push            Push to ECR after building. Refused for a dirty tree.
#   --allow-dirty     Build with uncommitted changes. Stamps <sha>-dirty and
#                     tags only stocks:dev -- never :latest, never pushed.
#   --allow-unpushed  Build a commit that is not on origin yet. It is stamped
#                     honestly, but nobody else could reproduce the image.

set -euo pipefail

usage() {
    echo "Usage: $(basename "$0") [--push] [--allow-dirty] [--allow-unpushed]" >&2
    exit 1
}

PUSH=false
ALLOW_DIRTY=false
ALLOW_UNPUSHED=false

for arg in "$@"; do
    case "$arg" in
        --push) PUSH=true ;;
        --allow-dirty) ALLOW_DIRTY=true ;;
        --allow-unpushed) ALLOW_UNPUSHED=true ;;
        -h|--help) usage ;;
        *) echo "Error: unknown option '$arg'" >&2; usage ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
fail() { echo "Error: $*" >&2; exit 1; }

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    fail "not a git work tree; the image could not be stamped with a revision"
fi

REV="$(git rev-parse --short HEAD)"
DIRTY=false
[[ -n "$(git status --porcelain)" ]] && DIRTY=true

# ---- check 1: the working tree matches the commit being claimed -------------
if [[ "$DIRTY" == true ]]; then
    if [[ "$ALLOW_DIRTY" != true ]]; then
        echo "Error: working tree has uncommitted changes." >&2
        echo "  The build copies the working tree, so the image would contain" >&2
        echo "  code that is not in $REV while claiming to be $REV." >&2
        echo "  Commit them, or pass --allow-dirty to build stocks:dev locally." >&2
        git status --short >&2
        exit 1
    fi
    [[ "$PUSH" == true ]] && fail "--push and --allow-dirty are mutually exclusive; a dirty image must not reach ECR"
    REV="${REV}-dirty"
fi

# ---- check 2: the commit exists somewhere other than this laptop ------------
# A tag naming a commit only this machine has is not reproducible: nobody can
# check out git-<sha> to see what shipped.
if [[ "$DIRTY" == false ]]; then
    if ! git merge-base --is-ancestor HEAD "@{upstream}" 2>/dev/null; then
        if [[ "$ALLOW_UNPUSHED" != true ]]; then
            fail "HEAD ($REV) is not on the upstream branch; push it first, or pass --allow-unpushed"
        fi
        log "WARNING: $REV is not on origin; this image is not reproducible from the remote"
    fi
fi

# ---- build -----------------------------------------------------------------
if [[ "$DIRTY" == true ]]; then
    TAGS=(-t "stocks:dev")
    PRIMARY="stocks:dev"
else
    REPO="$(terraform -chdir=infra output -raw ecr_repository_url 2>/dev/null || true)"
    [[ -z "$REPO" ]] && fail "could not read ecr_repository_url from terraform; run terraform init in infra/"
    TAGS=(-t "${REPO}:latest" -t "${REPO}:git-${REV}")
    PRIMARY="${REPO}:git-${REV}"
fi

log "==> Building $PRIMARY (revision $REV)"
# linux/amd64 explicitly: the Fargate task definitions are x86_64, and an image
# built on an ARM host would start and then die on exec format error.
docker build --platform linux/amd64 --build-arg GIT_REVISION="$REV" "${TAGS[@]}" .

# ---- check 3: the stamp survived the build ---------------------------------
# Catches a Dockerfile that stops honouring the build arg, and a cached layer
# carrying an older ENV.
BAKED="$(docker run --rm --entrypoint printenv "$PRIMARY" STOCKS_CODE_REVISION)"
[[ "$BAKED" == "$REV" ]] || fail "image is stamped '$BAKED' but was built from '$REV'"
log "==> Verified STOCKS_CODE_REVISION=$BAKED"

if [[ "$PUSH" == true ]]; then
    log "==> Pushing to ECR"
    REGION="$(terraform -chdir=infra output -raw aws_region 2>/dev/null || echo ap-southeast-2)"
    aws ecr get-login-password --region "$REGION" \
        | docker login --username AWS --password-stdin "${REPO%%/*}"
    docker push "${REPO}:git-${REV}"
    docker push "${REPO}:latest"
    log "==> Pushed ${REPO}:latest and :git-${REV}"
else
    log "==> Built but not pushed (pass --push to publish)"
fi
