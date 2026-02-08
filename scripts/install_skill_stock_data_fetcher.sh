#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_SKILL_DIR="${REPO_ROOT}/skills/stock-data-fetcher"
DEST_SKILL_DIR="${HOME}/.codex/skills/stock-data-fetcher"

if [[ ! -d "${SRC_SKILL_DIR}" ]]; then
  echo "Source skill directory not found: ${SRC_SKILL_DIR}" >&2
  exit 1
fi

if [[ ! -f "${SRC_SKILL_DIR}/SKILL.md" ]]; then
  echo "Invalid source skill: missing SKILL.md in ${SRC_SKILL_DIR}" >&2
  exit 1
fi

mkdir -p "${HOME}/.codex/skills"

# Preserve destination directory permissions when it already exists.
if [[ -d "${DEST_SKILL_DIR}" ]]; then
  find "${DEST_SKILL_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
else
  mkdir -p "${DEST_SKILL_DIR}"
fi

cp -a "${SRC_SKILL_DIR}/." "${DEST_SKILL_DIR}/"

if [[ ! -f "${DEST_SKILL_DIR}/SKILL.md" ]]; then
  echo "Install failed: SKILL.md not found at destination ${DEST_SKILL_DIR}" >&2
  exit 1
fi

echo "Installed skill to: ${DEST_SKILL_DIR}"
