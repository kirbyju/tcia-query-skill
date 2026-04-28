# IBM Aspera Faspex Packages

Some TCIA non-DICOM data are distributed through IBM Aspera Faspex package links from WordPress dataset pages or download metadata.

## When To Use

Use Aspera guidance when WordPress download metadata or a TCIA dataset page points to a Faspex package URL, especially for large non-DICOM packages, annotations, or supporting data.

Do not reconstruct package URLs. Use the exact TCIA-provided Faspex link.

## Tooling

The command-line route uses IBM's open-source `aspera-cli` Ruby gem, which provides the `ascli` command. Ruby 3.1 or newer is required.

Install pattern:

```bash
gem install aspera-cli
ascli conf ascp install
ascli --version
```

If SDK installation fails, the TCIA notebook describes downloading the transfer SDK and installing it locally through `ascli config ascp install --sdk-url=file:///path/to/sdk.zip`.

## Download Package

Download an entire package:

```bash
ascli faspex5 packages receive --url="<TCIA_FASPEX_PACKAGE_URL>"
```

## Browse Package

List the root of a package:

```bash
ascli faspex5 packages browse --url="<TCIA_FASPEX_PACKAGE_URL>"
```

Recursively list package contents as CSV:

```bash
ascli --format=csv faspex5 packages browse \
  --query=@json:'{"recursive":true}' \
  --url="<TCIA_FASPEX_PACKAGE_URL>" > package.csv
```

## Download Part Of A Package

Append one or more package paths after the URL:

```bash
ascli faspex5 packages receive --url="<TCIA_FASPEX_PACKAGE_URL>" path/in/package
ascli faspex5 packages receive --url="<TCIA_FASPEX_PACKAGE_URL>" path/one path/two
```

For very large packages, recommend browsing first and downloading a small subset before transferring everything.
