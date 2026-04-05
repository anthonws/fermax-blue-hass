#!/bin/bash
# Pre-push hook: replicates CI pipeline locally using pre-built dev image.
# Install: cp scripts/pre-push.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
# Or run: make pre-push

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

DEV_IMG="fermax-blue-dev"
PROJECT_DIR="$(git rev-parse --show-toplevel)"
RUN="docker run --rm -v $PROJECT_DIR:/app -w /app"

step() { echo -e "\n${YELLOW}▶ $1${NC}"; }
pass() { echo -e "${GREEN}✓ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

echo -e "${YELLOW}══════════════════════════════════════════════${NC}"
echo -e "${YELLOW}  Pre-push CI check                           ${NC}"
echo -e "${YELLOW}══════════════════════════════════════════════${NC}"

# Build dev image if needed (cached, instant if no changes)
step "Ensuring dev image"
docker build -q -t "$DEV_IMG" -f "$PROJECT_DIR/Dockerfile.dev" "$PROJECT_DIR" > /dev/null \
    && pass "Dev image ready" || fail "Dev image build failed"

# All checks in one container (fast: no pip install, no image pull)
step "Lint + Format + Typecheck + Tests (Python 3.12)"
$RUN "$DEV_IMG" sh -c "\
    echo '  Lint...' && ruff check custom_components/ tests/ scripts/ && \
    echo '  Format...' && ruff format --check custom_components/ tests/ scripts/ && \
    echo '  Typecheck...' && mypy custom_components/fermax_blue/ --ignore-missing-imports && \
    echo '  Tests...' && pytest tests/ -q --tb=short" \
    && pass "All checks (3.12)" || fail "Checks failed on Python 3.12"

echo -e "\n${GREEN}══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  All checks passed — safe to push             ${NC}"
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
