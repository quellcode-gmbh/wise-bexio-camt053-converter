# WISE CAMT.053 Transformer (v10 → v8/v4) for bexio

This repository contains a small CLI tool (`wise_camt053_transform.py`) that converts **WISE** bank statement XML files from **camt.053.001.10** to **camt.053.001.08** (default) or **camt.053.001.04**, while applying a few **bexio-oriented fixes** that help the import succeed and improve what you see in the bexio UI.

## Why this exists

- WISE exports camt.053 in **v10**, while many accounting tools (including bexio) expect **v8** (or sometimes **v4**).
- camt XML schemas are **order-sensitive**: even if the content is correct, the wrong element order can fail validation/import.
- Some WISE-proprietary transaction codes are present in the XML but not shown in bexio unless moved into a visible field.

## What the tool does

### Core transformation
- Retags the document namespace from **camt.053.001.10** to your chosen target (**v8** or **v4**).
- Normalizes timestamp strings to a consistent ISO format (fractional seconds truncated to 6 digits where present).

### bexio-oriented fixes
- Ensures each `<Ntry>` has a `<ValDt><Dt>YYYY-MM-DD</Dt></ValDt>` derived from `<BookgDt>`.
- Ensures a structured booking transaction code exists:
  - `<BkTxCd><Domn><Cd>…</Cd><Fmly><Cd>…</Cd><SubFmlyCd>…</SubFmlyCd></Fmly></Domn></BkTxCd>`
  - Keeps any existing `<BkTxCd><Prtry>…</Prtry>` block.
- Reorders elements in `<Ntry>` and `<TxDtls>` to match schema expectations (subset ordering used by the script).

### Optional “UI improvement” for bexio
- If enabled, copies WISE’s proprietary code:
  - `<BkTxCd><Prtry><Cd>CONVERSION_ORDER-…</Cd></Prtry>`
  into `<AddtlNtryInf>` when `<AddtlNtryInf>` is missing/empty or equals `"No information"`.
  This often makes the code visible in bexio’s interface.

### Cleanup / robustness
- Removes elements named `AdrTp` (address type) wherever they occur (some imports reject it).
- Fixes a known edge case where `TxsSummry/TtlDbtNtries/Sum` is negative (turns it into an absolute value).

## Requirements

- Python **3.10+**
- No third-party dependencies for basic conversion
- Optional: `lxml` only if you want XSD validation (`--xsd`)

Install `lxml` (optional):
```bash
pip install lxml
```

## Usage

### Convert a single file (default target: v8)
```bash
python wise_camt053_transform.py input.xml
```

### Convert a single file and improve bexio display (copy WISE proprietary code into AddtlNtryInf)
```bash
python wise_camt053_transform.py input.xml --target 8 --copy-prtry-to-addtlinf
```

### Convert multiple files via glob pattern into an output directory
```bash
python wise_camt053_transform.py "folder/*.xml" --target 8 --outdir out/ --copy-prtry-to-addtlinf
```

> Note: Quoting the glob (`"folder/*.xml"`) lets the script expand it itself.  
> If you don’t quote it, your shell may expand it before Python runs — both ways work.

### Append the proprietary code instead of replacing existing AddtlNtryInf
If `<AddtlNtryInf>` already contains something useful and you still want the WISE code included:
```bash
python wise_camt053_transform.py input.xml --copy-prtry-to-addtlinf --append-prtry
```

### Validate the output with an XSD (optional)
```bash
python wise_camt053_transform.py input.xml --target 8 --xsd camt.053.001.08.xsd
```

## CLI Options

- `--target {4,8}`  
  Target camt.053 version (default: `8`)
- `--out FILE`  
  Output file path (only valid when converting **exactly one** input file)
- `--outdir DIR`  
  Directory where output files are written (created if missing)
- `--xsd FILE`  
  Validate the generated XML against the given XSD (requires `lxml`)
- `--copy-prtry-to-addtlinf`  
  Copy `<BkTxCd><Prtry><Cd>…</Cd></Prtry>` into `<AddtlNtryInf>` when needed
- `--append-prtry`  
  When `--copy-prtry-to-addtlinf` is active: append the code instead of overwriting existing AddtlNtryInf

## Output naming

If you don’t specify `--out`, the tool writes to a file next to the input:
- `input.xml` → `input_camt053_v08_bexio.xml` (for `--target 8`)
- `input.xml` → `input_camt053_v04_bexio.xml` (for `--target 4`)

With `--outdir`, the file name stays the same but is written into that directory.

## Exit codes

- `0` – all files processed successfully
- `1` – at least one file failed
- `2` – usage error (e.g., no inputs found, `--out` used with multiple inputs, missing XSD)

## Example console output

Successful run example:
```
[OK] statement.xml -> statement_camt053_v08_bexio.xml | AddtlNtryInf*=12, ValDt+=42
```

## Notes / limitations

- The script expects a **camt.053** input (it checks the root namespace for `camt.053.001.*`).
- Ordering rules applied are a **subset** focused on the elements the script touches; they’re chosen to avoid common schema/import failures.
- The `AdrTp` removal is broad (by local element name). If you rely on `AdrTp` somewhere else, review that behavior before using in production.

## License

Choose a license that fits your project (e.g., MIT). If you want, I can generate a ready-to-use `LICENSE` file and add a short “Contributing” section.
