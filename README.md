# WISE CAMT.053 Transformer for bexio  
**Convert WISE camt.053.001.10 XML to camt.053.001.08 (default) or camt.053.001.04, with bexio-oriented fixes and FX/conversion amount normalization.**

This repository contains a small CLI tool, `wise_camt053_transform.py`, for converting **WISE** bank statement XML files from **camt.053.001.10** to **camt.053.001.08** (default) or **camt.053.001.04**, while applying several fixes that improve compatibility with **bexio** and preserve important FX/conversion details.

---

## Why this exists

- WISE exports bank statements as **camt.053.001.10**.
- bexio often expects **camt.053.001.08** and may also accept **camt.053.001.04**.
- CAMT XML is **schema-order-sensitive**: valid-looking XML can still fail import or validation if child elements appear in the wrong order.
- WISE conversion entries can contain multi-currency amount structures that need cleanup so the imported data in bexio remains meaningful.

---

## What the tool does

## 1) Namespace/version conversion
- Detects the input namespace and verifies that it is a **camt.053** document.
- Retags the XML namespace from the input version to either:
  - `urn:iso:std:iso:20022:tech:xsd:camt.053.001.08` (default), or
  - `urn:iso:std:iso:20022:tech:xsd:camt.053.001.04`
- Removes any existing `xsi:schemaLocation` attribute from the root.

---

## 2) Timestamp normalization
Normalizes timestamp-like text values in these elements:
- `CreDtTm`
- `DtTm`
- `FrDtTm`
- `ToDtTm`

Behavior:
- Preserves the original date/time value
- Truncates fractional seconds to at most **6 digits**
- Keeps timezone suffixes such as `Z` or `+02:00`
- Leaves pure date values unchanged

Example:
- `2026-01-01T12:34:56.123456789Z` → `2026-01-01T12:34:56.123456Z`

---

## 3) bexio-oriented structural fixes

### Ensure `<ValDt>` exists on each `<Ntry>`
For each statement entry (`<Stmt>/<Ntry>`), the tool ensures:
```xml
<ValDt><Dt>YYYY-MM-DD</Dt></ValDt>
```
exists.

If missing, it is derived from `<BookgDt>`:
- from `<BookgDt><Dt>`
- or from `<BookgDt><DtTm>` by extracting the date part

This is inserted directly after `<BookgDt>` when possible.

---

### Ensure structured `<BkTxCd>` exists
For each `<Ntry>` and each `<TxDtls>`, the tool ensures a structured booking transaction code exists:

```xml
<BkTxCd>
  <Domn>
    <Cd>PMNT</Cd>
    <Fmly>
      <Cd>RCDT|ICDT</Cd>
      <SubFmlyCd>OTHR</SubFmlyCd>
    </Fmly>
  </Domn>
  ...
</BkTxCd>
```

Default values:
- `Domn/Cd = PMNT`
- `Fmly/Cd = RCDT` for credit entries (`CRDT`)
- `Fmly/Cd = ICDT` for debit entries (`DBIT`)
- `SubFmlyCd = OTHR`

If a proprietary code already exists under:
```xml
<BkTxCd><Prtry>...</Prtry></BkTxCd>
```
it is preserved.

---

## 4) Element reordering for schema compatibility
The script reorders child elements to match schema-sensitive order expectations in these structures:

- `<Ntry>`
- `<TxDtls>`
- `<AmtDtls>`

This is one of the most important compatibility fixes, because CAMT XSD validation is order-sensitive.

The script applies explicit order rules for:
- entry-level elements in `<Ntry>`
- transaction detail elements in `<TxDtls>`
- amount detail wrappers in `<AmtDtls>`

---

## 5) FX / conversion amount normalization
The script contains special handling for WISE conversion entries, especially entries whose proprietary code starts with:

- `CONVERSION_ORDER-...`
- and related fee entries `FEE-CONVERSION_ORDER-...`

This logic is intended to preserve the meaning of conversion amounts when importing into bexio.

### What is normalized
For conversion entries, the script may adjust `<AmtDtls>` so that:
- existing exchange rates are preserved
- source/target amounts are placed into the most meaningful CAMT amount fields
- separately booked conversion fees are reflected in the **net** source amount where possible

### Important behaviors
- **Preserves an existing `<XchgRate>`** from the source XML  
  It never recalculates or overwrites the exchange rate itself.
- Uses the **net source amount** after a separately booked fee, if the fee can be inferred.
- If fee information is not inferable, it falls back to existing XML amounts.
- Uses both:
  - conversion text in `<AddtlNtryInf>` such as  
    `Converted 100.00 EUR to 95.00 CHF (fee: 0.50 EUR)`
  - and separate fee entries like  
    `FEE-CONVERSION_ORDER-...`

### Source-account side
For a source-currency debit entry, for example an **EUR account** entry representing an **EUR → CHF** conversion:

- `<TxAmt>` is set to the **target currency amount**
- `<CntrValAmt>` is written in the **account/source currency**
- that source-side value is the **net converted amount** when fee information is available

`<InstdAmt>` is removed on this side.

### Target-account side
For a target-currency credit entry, for example a **CHF account** entry representing the same **EUR → CHF** conversion:

- `<InstdAmt>` is set to the **net converted source amount**
- `<TxAmt>` remains the credited **target currency amount**

`<CntrValAmt>` is removed on this side.

### Exchange-rate substructure handling
When updating amount wrappers, the script preserves/copies the existing `<CcyXchg>` block where appropriate and removes it where not appropriate.

---

## 6) Optional bexio UI improvement: copy proprietary code into `AddtlNtryInf`
If enabled, the tool copies the proprietary WISE booking code:

```xml
<BkTxCd><Prtry><Cd>...</Cd></Prtry></BkTxCd>
```

into:

```xml
<AddtlNtryInf>...</AddtlNtryInf>
```

This only happens when:
- `AddtlNtryInf` is missing, or
- it is empty, or
- it contains a placeholder such as:
  - `No information`
  - `No info`
  - `n/a`

This can make WISE-specific transaction references more visible in the bexio UI.

If `--append-prtry` is used, the code is appended instead of replacing existing useful content.

---

## 7) Cleanup / robustness fixes

### Remove `AdrTp`
The script removes every element whose local name is `AdrTp` anywhere in the XML.

This is broad by design and intended to avoid import/validation issues.

### Fix negative debit summary totals
If the script finds:

```xml
TxsSummry/TtlDbtNtries/Sum
```

with a negative number, it converts it to its absolute value.

---

## Requirements

- Python **3.10+**
- No third-party dependencies for normal conversion
- Optional: `lxml` for XSD validation

Install optional validation dependency:
```bash
pip install lxml
```

---

## Usage

### Convert one file to camt.053.001.08
```bash
python wise_camt053_transform.py input.xml
```

### Convert one file to camt.053.001.04
```bash
python wise_camt053_transform.py input.xml --target 4
```

### Convert one file and copy WISE proprietary codes into `AddtlNtryInf`
```bash
python wise_camt053_transform.py input.xml --target 8 --copy-prtry-to-addtlinf
```

### Convert multiple files using a glob pattern into an output directory
```bash
python wise_camt053_transform.py "folder/*.xml" --target 8 --outdir out/ --copy-prtry-to-addtlinf
```

### Append the proprietary code instead of replacing useful existing `AddtlNtryInf`
```bash
python wise_camt053_transform.py input.xml --copy-prtry-to-addtlinf --append-prtry
```

### Validate the output against an XSD
```bash
python wise_camt053_transform.py input.xml --target 8 --xsd camt.053.001.08.xsd
```

> Note: Quoting the glob (`"folder/*.xml"`) lets the script expand it internally.  
> If you leave it unquoted, the shell may expand it first. Both can work.

---

## CLI options

- `inputs`  
  One or more input files or glob patterns

- `--target {4,8}`  
  Target CAMT version. Default: `8`

- `--out FILE`  
  Exact output file path. Only valid when converting **one** input file

- `--outdir DIR`  
  Output directory for converted files. Created automatically if missing

- `--xsd FILE`  
  Validate the generated XML against the given XSD file. Requires `lxml`

- `--copy-prtry-to-addtlinf`  
  Copy `<BkTxCd><Prtry><Cd>…</Cd></Prtry>` into `<AddtlNtryInf>` when appropriate

- `--append-prtry`  
  When used together with `--copy-prtry-to-addtlinf`, append the proprietary code instead of replacing existing useful text

---

## Output naming

If you do not specify `--out`, the script writes a file next to the input using this naming pattern:

- `input.xml` → `input_camt053_v08_bexio.xml`
- `input.xml` → `input_camt053_v04_bexio.xml`

If `--outdir` is used, the filename stays the same but is written to that directory.

---

## Exit codes

- `0` – all files processed successfully
- `1` – at least one file failed
- `2` – usage error, such as:
  - no input files found
  - `--out` used with multiple inputs
  - missing XSD file

---

## Example console output

### Successful conversion
```text
[OK] statement.xml -> statement_camt053_v08_bexio.xml | ConvFix=3, AddtlNtryInf*=12, ValDt+=42
```

### Successful conversion with XSD validation
```text
[OK] statement.xml -> statement_camt053_v08_bexio.xml | XSD:OK | ConvFix=3, AddtlNtryInf*=12, ValDt+=42
```

### Failed conversion
```text
[FAIL] statement.xml: <error message>
```

---

## What the script reports internally
For each processed file, the transformation logic tracks counts such as:
- timestamps normalized
- `AdrTp` elements removed
- whether negative debit summary total was fixed
- `ValDt` nodes added
- `BkTxCd` fixed on entries
- `BkTxCd` fixed on transaction details
- conversion amount detail blocks normalized
- reordered `Ntry`
- reordered `TxDtls`
- reordered `AmtDtls`
- changed `AddtlNtryInf`

The console output currently shows only a subset of these counters:
- `ConvFix`
- `AddtlNtryInf*`
- `ValDt+`

---

## Notes / limitations

- The script only accepts input that looks like **camt.053** based on the root namespace.
- It does not attempt to fully semantically reinterpret all possible WISE booking variants; it only applies the specific normalization rules implemented in the code.
- FX/conversion normalization depends on the available information in:
  - existing amount structures
  - existing `CcyXchg`
  - `AddtlNtryInf` conversion text
  - and/or matching `FEE-CONVERSION_ORDER-*` entries
- `AdrTp` removal is intentionally broad and works by local element name.
- XSD validation is optional and only performed if `--xsd` is given.

---

