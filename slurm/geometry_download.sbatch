#!/usr/bin/env bash
#SBATCH --job-name=tcia-geom-download
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=24:00:00
#SBATCH --output=logs/download-%A_%a.out
#SBATCH --error=logs/download-%A_%a.err

set -euo pipefail

: "${TCIA_GEOMETRY_ROOT:?Set TCIA_GEOMETRY_ROOT to the batch workspace}"
: "${TCIA_GEOMETRY_CODE:?Set TCIA_GEOMETRY_CODE to the tcia-query-skill checkout}"

mkdir -p "${TCIA_GEOMETRY_ROOT}/logs" "${TCIA_GEOMETRY_ROOT}/data"

"${TCIA_GEOMETRY_PYTHON:-python3}" \
  "${TCIA_GEOMETRY_CODE}/scripts/tcia_geometry_batch.py" download \
  --plan "${TCIA_GEOMETRY_ROOT}/plan/jobs.private.jsonl" \
  --index "${SLURM_ARRAY_TASK_ID}" \
  --data-root "${TCIA_GEOMETRY_ROOT}/data"
