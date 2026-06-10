# IBM Aspera Faspex Packages

Some TCIA non-DICOM data are distributed through IBM Aspera Faspex package links from WordPress dataset pages or download metadata. Use this page when a TCIA download record points to a Faspex package URL, especially for large NIfTI, annotation, pathology, or supporting-data packages.

## Core Rules

- Do not reconstruct package URLs. Use the exact Faspex link exposed by the TCIA dataset page or WordPress download metadata.
- Do not directly download controlled-access packages. Use this guidance only after WordPress license/access metadata confirms the relevant download is open/non-controlled.
- Browse before transferring large packages. A package inventory is often enough to answer file-count, filename, and folder-layout questions.
- Preserve browse output as a local CSV when doing metadata work. Future refreshes can reuse successful listings until the WordPress download ID, URL, date, or size changes.
- Treat Aspera package listings as file inventory, not rich metadata. Look for companion spreadsheets/TSVs/XLSX files and root `.sums` files before recursively scanning a huge package.
- For non-DICOM pathology that is also represented in PathDB, treat the Aspera package as the original submitter-provided data. PathDB files may be converted or reformatted for TCIA's browser-based pathology viewer, so recommend Aspera when the user needs the exact submitted files for analysis.

## Tooling

The command-line route uses IBM's open-source `aspera-cli` Ruby gem, which provides the `ascli` command. Ruby 3.1 or newer is recommended.

Install pattern:

```bash
gem install aspera-cli
ascli conf ascp install
ascli --version
```

If SDK installation fails, the TCIA notebook describes downloading the transfer SDK and installing it locally through `ascli config ascp install --sdk-url=file:///path/to/sdk.zip`.

Some local environments print warnings such as `Operation not permitted ... persist_store`. These warnings are not necessarily fatal; judge by the command exit code and whether CSV/data rows were returned.

## Browse Package

List the root of a package:

```bash
ascli --format=csv faspex5 packages browse \
  --url="<TCIA_FASPEX_PACKAGE_URL>" > root.csv
```

Recursively list package contents as CSV:

```bash
ascli --format=csv faspex5 packages browse \
  --query=@json:'{"recursive":true,"per_page":500}' \
  --url="<TCIA_FASPEX_PACKAGE_URL>" > package.csv
```

Recursive browse works for many packages, but large packages can time out or fail. If recursive browse is slow, first look for a package-level `.sums` file, then use staged browsing of known folders.

## Root `.sums` Shortcut

Many TCIA packages include a summary file in the package root, commonly named like `<package>.sums`. These files can be a much cheaper file inventory than walking the full package tree.

Suggested flow:

1. Browse the package root.
2. If the root exposes one or more directories, browse one level into the root folder.
3. Look for `.sums` files before trying a full recursive browse.
4. Use `.sums` rows as package inventory when they contain one row per file path.

Example:

```bash
ascli --format=csv faspex5 packages browse \
  --url="<TCIA_FASPEX_PACKAGE_URL>" > root.csv

ascli --format=csv faspex5 packages browse \
  --url="<TCIA_FASPEX_PACKAGE_URL>" \
  /PackageRoot > package-root.csv
```

If a `.sums` file exists, download just that file rather than the whole package:

```bash
ascli faspex5 packages receive \
  --url="<TCIA_FASPEX_PACKAGE_URL>" \
  /PackageRoot.sums
```

## Staged Folder Browsing

When the package root is very large or recursive browse fails, browse in stages. This mirrors the interactive approach in `TCIA_Aspera_CLI_Downloads.ipynb`: start at the root, identify package folders, then browse specific subfolders.

For example, if the root exposes `/UCSF-PDGM-v5` and subject folders are known to look like `UCSF-PDGM-0004_nifti`, browse the subject folders directly:

```bash
ascli --format=csv faspex5 packages browse \
  --url="<TCIA_FASPEX_PACKAGE_URL>" \
  UCSF-PDGM-v5/UCSF-PDGM-0004_nifti > UCSF-PDGM-0004.csv
```

When seeding folder paths from metadata spreadsheets, remember that subject IDs may not enumerate every study folder. UCSF-PDGM had 495 subjects but 501 studies; six follow-up study folders used names such as `UCSF-PDGM-0391_FU016d_nifti`, so a subject-only seed list missed them. If WordPress package stats include both subject and study counts, compare the browsed folder count with the study count.

## Offset Paging And Leading Slashes

Faspex folder browse supports two paging modes. The default iteration-token paging usually works, but for some flat or large folders an explicit offset/limit query is more reliable:

```bash
ascli --format=csv faspex5 packages browse \
  --url="<TCIA_FASPEX_PACKAGE_URL>" \
  --query=@json:'{"paging":false,"limit":1000}' \
  /FolderName > folder.csv
```

Use a leading slash when a known root folder fails without one. In one TCIA package, browsing `Vestibular-Schwannoma-MC-RC2_Oct2025` returned a Faspex/HSTS 500 error, while `/Vestibular-Schwannoma-MC-RC2_Oct2025` returned the expected flat inventory.

`ascli` may emit a leading status line such as `Items: 1691/1691` in offset-paged CSV output. Treat that as metadata, not a file row, when parsing.

## Download Package

Download an entire package only after confirming size and access constraints:

```bash
ascli faspex5 packages receive \
  --url="<TCIA_FASPEX_PACKAGE_URL>"
```

For very large packages, recommend browsing first and downloading a small subset before transferring everything.

## Download Part Of A Package

Append one or more package paths after the URL:

```bash
ascli faspex5 packages receive \
  --url="<TCIA_FASPEX_PACKAGE_URL>" \
  path/in/package

ascli faspex5 packages receive \
  --url="<TCIA_FASPEX_PACKAGE_URL>" \
  path/one path/two
```

If a path fails and the root browse shows it with a leading slash, retry the receive command with the leading slash.

## NIfTI Metadata Harvest Lessons

The TCIA NIfTI metadata harvest used these practical rules:

- Prefer companion spreadsheets for series/acquisition/patient metadata when available. Aspera listings usually provide only filenames, paths, type, size, and modification time.
- Use root `.sums` files as inventory when they exist. They can avoid expensive recursive browsing.
- Use staged browsing for packages without `.sums` files.
- For packages organized by subject or study folders, generate seed paths from companion metadata, but verify the root folder count to catch follow-up or repeat-study folders.
- For flat folders with around thousands of files, use offset paging and a leading slash if needed.
- Exclude sidecar files such as `.bvec`, `.bval`, or `.eddy_rotated_bvecs` from NIfTI-only tables, but preserve them in raw package inventory when the package stats include them.
- Store failed browse attempts and stderr where possible. Faspex errors can be path-shape specific; a failed no-slash path may succeed with a slash.

Concrete examples from the harvest:

| Dataset | Lesson |
| --- | --- |
| `UCSF-PDGM` | Root folder was `UCSF-PDGM-v5`. Subject folders were browseable directly, but six follow-up study folders were only found after a non-paged root browse showed 501 study folders. |
| `Vestibular-Schwannoma-MC-RC2` | Flat root folder `Vestibular-Schwannoma-MC-RC2_Oct2025` required a leading slash plus offset paging to return all 1,691 files. |
| `Yale-Brain-Mets-Longitudinal` | Companion spreadsheet had one row per NIfTI file, but one listed file was manually confirmed absent from the package; preserve source quality flags separately from file inventory. |
| `UPENN-GBM` and `RSNA-ASNR-MICCAI-BraTS-2021` | Root `.sums` files were sufficient for large inventory coverage without recursive browsing. |
