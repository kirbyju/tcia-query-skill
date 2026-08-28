# Non-IDC geometry batch analysis on SLURM

This workflow downloads the open, non-IDC DICOM and volume-capable assets
represented in the TCIA Metadata V2 public non-DICOM artifact and analyzes
their geometry without reading pixel or voxel arrays into memory.

It deliberately excludes:

- ordinary IDC DICOM, which should use `volume_geometry_index`;
- controlled or restricted assets;
- pathology, still-image, video, clinical, and supporting-file formats;
- files not represented by the selected V2 release.

The default formats are DICOM, NIfTI, MHA, MHD, and NRRD. Add another format
only after adding an explicit header parser; a filename extension alone is not
treated as proof of geometric coherence.

## Storage and network expectations

At the time this workflow was prepared, the local V2 detail artifact resolved
54 open download groups with approximately 870 GiB of cataloged payload. Some
Aspera packages contain additional non-target files, so provision more space
than the catalog total. Use a shared project or scratch filesystem rather than
home-directory storage.

The job plan groups transfers by published download route. It does not submit
one transfer per file. HTTP downloads use resumable `curl`; Aspera downloads
use the exact Faspex URL from the V2 artifact and `ascli`/`ascp`. The downloader
distinguishes TCIA's Faspex 4 public-package links from its Faspex 5 public-link
form and invokes the matching plugin.

## 1. Prepare software

Clone this branch or copy the `scripts/tcia_geometry_batch.py`, `slurm/`, and
this reference to the cluster. On a login node:

```bash
module load python/3.11
python3 -m venv venv
venv/bin/pip install \
  -r slurm/geometry_requirements.txt
```

Install a current IBM Aspera CLI release using the method approved by your
company. The upstream command-line installation pattern is:

```bash
gem install aspera-cli
ascli config transferd install
ascli -v
```

Ruby 3.1 or newer is recommended. If outbound compute-node networking is
blocked, ask the SLURM administrators whether transfers must run on a dedicated
data-transfer node or partition.

## 2. Create the plan locally or install the current detail artifact

The preferred portable workflow is to create the plan on a workstation that
already has the current `research_detail` profile, then securely copy the
entire `plan/` directory to the cluster. The plan is relocatable and is much
smaller than `public_non_dicom_metadata.sqlite`. Keep
`jobs.private.jsonl` protected during transfer because it contains the exact
published package links.

If the cluster will create the plan instead, install the current V2 detail
artifact there first. From a `tcia-query-skill` checkout:

From the `tcia-query-skill` checkout:

```bash
python3 scripts/tcia_freshness.py check
python3 scripts/tcia_v2_bundle.py install --profile research_detail
```

Use the `public_non_dicom_metadata.sqlite` selected by the resulting bundle
install record. Do not substitute an old standalone NIfTI database.

## 3. Create or copy the job plan

```bash
export TCIA_GEOMETRY_CODE=/path/to/tcia-query-skill
export TCIA_GEOMETRY_ROOT=/path/to/project/tcia-geometry/run-001
export TCIA_GEOMETRY_PYTHON=/path/to/project/tcia-geometry/venv/bin/python
mkdir -p "${TCIA_GEOMETRY_ROOT}"/{logs,data,results}

python3 "${TCIA_GEOMETRY_CODE}/scripts/tcia_geometry_batch.py" plan \
  --db /path/to/cache/tcia-metadata-v2-latest/public_non_dicom_metadata.sqlite \
  --out-dir "${TCIA_GEOMETRY_ROOT}/plan"
```

Review `plan/plan_summary.json` and `plan/jobs.csv`. The shareable CSV excludes
download URLs. `plan/jobs.private.jsonl` contains the exact published routes,
is created with mode 0600 where supported, and should not be committed, pasted
into tickets, or included in job logs.

If you created the plan locally, copy `plan/` as one directory and place it at
`${TCIA_GEOMETRY_ROOT}/plan`. Do not copy only `jobs.private.jsonl`; the
relative `job_manifests/` files are required for asset-level result mapping.

To test one or more datasets first, repeat planning with one or more filters:

```bash
python3 "${TCIA_GEOMETRY_CODE}/scripts/tcia_geometry_batch.py" plan \
  --db /path/to/public_non_dicom_metadata.sqlite \
  --out-dir "${TCIA_GEOMETRY_ROOT}/pilot-plan" \
  --dataset Pedi-Cranial-CT-Healthy \
  --dataset RSNA-ASNR-MICCAI-BraTS-2021
```

## 4. Submit downloads

Read `array_max` from `plan/plan_summary.json`. For the validated 54-job plan it
is 53:

```bash
cd "${TCIA_GEOMETRY_ROOT}"
DOWNLOAD_JOB_ID=$(sbatch --parsable \
  --array=0-53%4 \
  "${TCIA_GEOMETRY_CODE}/slurm/geometry_download.sbatch")
echo "${DOWNLOAD_JOB_ID}"
```

The `%4` concurrency cap is intentionally conservative. Confirm an acceptable
transfer concurrency and bandwidth policy with the cluster administrators.
Adjust the partition/account directives and wall time for your site; a large
package or throttled data-transfer node may require more than the template's
24-hour limit.
Completed jobs contain `.download_complete.json`; rerunning the same array is
idempotent and skips those jobs. HTTP transfers resume partial files. Aspera's
transfer engine manages its own resume behavior.

## 5. Submit header analysis

After downloads complete successfully:

```bash
ANALYZE_JOB_ID=$(sbatch --parsable \
  --dependency=afterok:${DOWNLOAD_JOB_ID} \
  --array=0-53%12 \
  "${TCIA_GEOMETRY_CODE}/slurm/geometry_analyze.sbatch")
echo "${ANALYZE_JOB_ID}"
```

The analyzer uses:

- `pydicom.dcmread(..., stop_before_pixels=True)` for DICOM;
- nibabel header and affine access with memory mapping for NIfTI;
- `SimpleITK.ImageFileReader.ReadImageInformation()` for MHA/MHD/NRRD.

DICOM results are one row per Series Instance UID. Single-file results are one
row per discovered volume file. Enhanced/multiframe DICOM is reported as
`unsupported_multiframe`; it is not silently classified as a coherent volume.

## 6. Merge and return the compact results

```bash
python3 "${TCIA_GEOMETRY_CODE}/scripts/tcia_geometry_batch.py" merge \
  --results-dir "${TCIA_GEOMETRY_ROOT}/results" \
  --out "${TCIA_GEOMETRY_ROOT}/geometry_results.sqlite"
```

Return these files for artifact integration:

- `geometry_results.sqlite`
- `geometry_results.summary.json`
- `plan/plan_summary.json`
- `plan/jobs.csv`
- failed SLURM logs, if any

Do not return `jobs.private.jsonl`; it is unnecessary for interpreting the
results and contains opaque published package routes.

## Result meanings

| Status | Meaning |
| --- | --- |
| `checked_regular` | A DICOM series passed all orientation, position, spacing, and dimension checks |
| `checked_not_regular` | DICOM metadata were checked and one or more coherence checks failed |
| `checked_grid_geometry` | A single-file image header described a finite, nonsingular spatial grid |
| `checked_invalid_geometry` | The header was readable but its grid geometry failed a required check |
| `insufficient_slices` | A DICOM series had fewer than three usable slices, so regular spacing could not be established |
| `missing_geometry` | Required DICOM geometry attributes were absent on one or more instances |
| `unsupported_multiframe` | The initial analyzer does not validate Enhanced/multiframe DICOM geometry |
| `not_applicable_localizer_or_mip` | The DICOM series is a localizer/scout or MIP rather than a volume stack |
| `not_applicable_nonvolume_sop_class` | The DICOM SOP Class is outside single-frame CT, MR, and PET volume scope |
| `read_error` | A supported file could not be parsed; inspect the retained error text |

These are metadata/header assessments, not statements about image quality,
clinical usability, or correctness of voxel values.
