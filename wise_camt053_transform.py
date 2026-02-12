#!/usr/bin/env python3
"""
WISE camt.053 v10 -> camt.053 v8 (default) or v4 transformer, with bexio-oriented fixes.

Adds/ensures for bexio:
- <Ntry><ValDt><Dt>YYYY-MM-DD</Dt></ValDt> (derived from BookgDt)
- <BkTxCd><Domn><Cd>...<Fmly><Cd>...<SubFmlyCd>...</...> (keeps any existing Prtry)
- XSD element order for <Ntry> and <TxDtls> (camt XSD is order-sensitive)

Quality-of-life:
- Optional: copy WISE proprietary code (<BkTxCd><Prtry><Cd>...</Cd></Prtry>) into <AddtlNtryInf>
  when AddtlNtryInf is missing/empty/"No information", so bexio shows it in the UI.

Usage:
  python wise_camt053_transform.py input.xml --target 8 --copy-prtry-to-addtlinf
  python wise_camt053_transform.py "folder/*.xml" --target 8 --outdir out/ --copy-prtry-to-addtlinf

Optional XSD validation (requires lxml):
  python wise_camt053_transform.py input.xml --target 8 --xsd camt.053.001.08.xsd
"""

from __future__ import annotations
import argparse
import glob
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
import decimal

XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

_DT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?)?((?:Z)|(?:[+-]\d{2}:\d{2}))?$"
)

def _localname(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag

def _get_default_ns(elem: ET.Element) -> str:
    m = re.match(r"^\{([^}]+)\}", elem.tag)
    return m.group(1) if m else ""

def normalize_datetime(text: str, max_frac: int = 6) -> str:
    if not text:
        return text
    t = text.strip()
    m = _DT_RE.match(t)
    if not m:
        return t
    date, hms, frac, tz = m.group(1), m.group(2), m.group(3), m.group(4) or ""
    if hms is None:
        return date
    if frac:
        frac = frac[:max_frac]
        return f"{date}T{hms}.{frac}{tz}"
    return f"{date}T{hms}{tz}"

def _date_from_dt_or_dttm(text: str) -> str | None:
    if not text:
        return None
    t = text.strip()
    m = _DT_RE.match(t)
    if m:
        return m.group(1)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        return t
    return None

def _retag_namespace(root: ET.Element, old_ns: str, new_ns: str) -> None:
    for el in root.iter():
        if el.tag.startswith("{" + old_ns + "}"):
            el.tag = "{" + new_ns + "}" + _localname(el.tag)

def _remove_elements_by_localname(root: ET.Element, name: str) -> int:
    removed = 0
    for parent in root.iter():
        for child in list(parent):
            if _localname(child.tag) == name:
                parent.remove(child)
                removed += 1
    return removed

def _fix_negative_debit_sum(root: ET.Element, ns: str) -> bool:
    el = root.find(f".//{{{ns}}}TxsSummry/{{{ns}}}TtlDbtNtries/{{{ns}}}Sum")
    if el is None or not (el.text and el.text.strip()):
        return False
    txt = el.text.strip()
    if not txt.startswith("-"):
        return False
    try:
        el.text = str(abs(decimal.Decimal(txt)))
        return True
    except Exception:
        return False

# XSD-order sequences (subset used by our edits)
NTRY_ORDER = [
    "NtryRef", "Amt", "CdtDbtInd", "RvslInd", "Sts", "BookgDt", "ValDt",
    "AcctSvcrRef", "Avlbty", "BkTxCd", "ComssnWvrInd", "Chrgs", "Intrst",
    "Card", "NtryDtls", "AddtlNtryInf"
]
TXDTLS_ORDER = [
    "Refs", "AmtDtls", "Avlbty", "BkTxCd", "Chrgs", "Intrst",
    "RltdPties", "RltdAgts", "RltdDts", "RltdPric", "RltdQties",
    "FinInstrmId", "Tax", "RtrInf", "CorpActn", "SfkpgAcct", "CshDpst",
    "CardTx", "AddtlTxInf"
]

def _reorder_children(parent: ET.Element, order: list[str]) -> bool:
    idx_map = {name: i for i, name in enumerate(order)}
    children = list(parent)
    if not children:
        return False
    keyed = []
    for orig_i, ch in enumerate(children):
        keyed.append((idx_map.get(_localname(ch.tag), 10_000), orig_i, ch))
    keyed_sorted = sorted(keyed, key=lambda t: (t[0], t[1]))
    new_children = [ch for _, _, ch in keyed_sorted]
    if new_children == children:
        return False
    parent[:] = new_children
    return True

def _entry_booking_date(ntry: ET.Element, ns: str) -> str | None:
    bookg = ntry.find(f"{{{ns}}}BookgDt")
    if bookg is None:
        return None
    dt = bookg.find(f"{{{ns}}}Dt")
    if dt is not None and dt.text:
        return _date_from_dt_or_dttm(dt.text)
    dttm = bookg.find(f"{{{ns}}}DtTm")
    if dttm is not None and dttm.text:
        return _date_from_dt_or_dttm(dttm.text)
    return None

def _ensure_valdt_on_entry(ntry: ET.Element, ns: str) -> bool:
    if ntry.find(f"{{{ns}}}ValDt") is not None:
        return False
    date = _entry_booking_date(ntry, ns)
    if not date:
        return False
    valdt = ET.Element(f"{{{ns}}}ValDt")
    dt_el = ET.SubElement(valdt, f"{{{ns}}}Dt")
    dt_el.text = date
    # Insert after BookgDt if possible
    inserted = False
    for i, ch in enumerate(list(ntry)):
        if _localname(ch.tag) == "BookgDt":
            ntry.insert(i + 1, valdt)
            inserted = True
            break
    if not inserted:
        ntry.insert(0, valdt)
    return True

def _default_bk_tx_cd(cdt_dbt_ind: str | None) -> tuple[str, str, str]:
    domn = "PMNT"
    fam = "RCDT" if (cdt_dbt_ind or "").upper() == "CRDT" else "ICDT"
    sub = "OTHR"
    return domn, fam, sub

def _ensure_bktxcd_structure(parent: ET.Element, ns: str, cdt_dbt_ind: str | None) -> bool:
    changed = False
    bktxcd = parent.find(f"{{{ns}}}BkTxCd")
    if bktxcd is None:
        bktxcd = ET.Element(f"{{{ns}}}BkTxCd")
        parent.append(bktxcd)
        changed = True

    domn_el = bktxcd.find(f"{{{ns}}}Domn")
    if domn_el is None:
        domn, fam, sub = _default_bk_tx_cd(cdt_dbt_ind)
        domn_el = ET.Element(f"{{{ns}}}Domn")
        cd = ET.SubElement(domn_el, f"{{{ns}}}Cd"); cd.text = domn
        fmly_el = ET.SubElement(domn_el, f"{{{ns}}}Fmly")
        fam_cd = ET.SubElement(fmly_el, f"{{{ns}}}Cd"); fam_cd.text = fam
        sub_cd = ET.SubElement(fmly_el, f"{{{ns}}}SubFmlyCd"); sub_cd.text = sub

        prtry = bktxcd.find(f"{{{ns}}}Prtry")
        if prtry is not None:
            bktxcd.remove(prtry)
            bktxcd.insert(0, domn_el)
            bktxcd.append(prtry)
        else:
            bktxcd.insert(0, domn_el)
        changed = True
    return changed

def _get_prtry_cd(ntry: ET.Element, ns: str) -> str | None:
    el = ntry.find(f"{{{ns}}}BkTxCd/{{{ns}}}Prtry/{{{ns}}}Cd")
    return el.text.strip() if el is not None and el.text and el.text.strip() else None

def _maybe_copy_prtry_to_addtlinf(ntry: ET.Element, ns: str, *, append_if_present: bool) -> bool:
    pr_cd = _get_prtry_cd(ntry, ns)
    if not pr_cd:
        return False
    add = ntry.find(f"{{{ns}}}AddtlNtryInf")
    if add is None:
        add = ET.Element(f"{{{ns}}}AddtlNtryInf")
        add.text = pr_cd
        ntry.append(add)
        return True

    cur = (add.text or "").strip()
    if cur == "" or cur.lower() in {"no information", "no info", "n/a"}:
        add.text = pr_cd
        return True
    if append_if_present and pr_cd not in cur:
        add.text = f"{cur} | {pr_cd}"
        return True
    return False

def _validate_with_xsd(xml_path: Path, xsd_path: Path) -> tuple[bool, list[str]]:
    try:
        import lxml.etree as LET
    except Exception:
        return False, ["lxml is not installed; cannot validate with XSD. Install lxml or omit --xsd."]
    schema_doc = LET.parse(str(xsd_path))
    schema = LET.XMLSchema(schema_doc)
    doc = LET.parse(str(xml_path))
    ok = schema.validate(doc)
    errs = []
    if not ok:
        for e in schema.error_log[:25]:
            errs.append(f"line {e.line}: {e.message}")
    return ok, errs

def transform_tree(tree: ET.ElementTree, target_version: int, *, copy_prtry_to_addtlinf: bool, append_if_present: bool) -> dict:
    if target_version not in (4, 8):
        raise ValueError("target_version must be 4 or 8")
    root = tree.getroot()
    old_ns = _get_default_ns(root)
    if "camt.053.001." not in old_ns:
        raise ValueError(f"Input does not look like camt.053.* (root namespace: {old_ns})")

    new_ns = f"urn:iso:std:iso:20022:tech:xsd:camt.053.001.{target_version:02d}"
    ET.register_namespace("", new_ns)
    ET.register_namespace("xsi", XSI_NS)

    _retag_namespace(root, old_ns, new_ns)

    schema_loc_attr = "{" + XSI_NS + "}schemaLocation"
    if schema_loc_attr in root.attrib:
        root.attrib.pop(schema_loc_attr, None)

    dt_changed = 0
    for el in root.iter():
        if _localname(el.tag) in {"CreDtTm", "DtTm", "FrDtTm", "ToDtTm"} and el.text:
            new_txt = normalize_datetime(el.text, max_frac=6)
            if new_txt != el.text:
                el.text = new_txt
                dt_changed += 1

    adr_tp_removed = _remove_elements_by_localname(root, "AdrTp")
    debit_sum_fixed = _fix_negative_debit_sum(root, new_ns)

    valdt_added = 0
    bktxcd_fixed = 0
    tx_bktxcd_fixed = 0
    reordered_ntry = 0
    reordered_txdtls = 0
    addtl_changed = 0

    for ntry in root.findall(f".//{{{new_ns}}}Stmt/{{{new_ns}}}Ntry"):
        cdi_el = ntry.find(f"{{{new_ns}}}CdtDbtInd")
        cdi = cdi_el.text.strip() if cdi_el is not None and cdi_el.text else None

        if _ensure_valdt_on_entry(ntry, new_ns):
            valdt_added += 1
        if _ensure_bktxcd_structure(ntry, new_ns, cdi):
            bktxcd_fixed += 1

        if copy_prtry_to_addtlinf:
            if _maybe_copy_prtry_to_addtlinf(ntry, new_ns, append_if_present=append_if_present):
                addtl_changed += 1

        if _reorder_children(ntry, NTRY_ORDER):
            reordered_ntry += 1

        for txdtls in ntry.findall(f".//{{{new_ns}}}TxDtls"):
            if _ensure_bktxcd_structure(txdtls, new_ns, cdi):
                tx_bktxcd_fixed += 1
            if _reorder_children(txdtls, TXDTLS_ORDER):
                reordered_txdtls += 1

    return {
        "timestamps_normalized": dt_changed,
        "AdrTp_removed": adr_tp_removed,
        "debit_sum_fixed": debit_sum_fixed,
        "valdt_added_on_entries": valdt_added,
        "bktxcd_fixed_on_entries": bktxcd_fixed,
        "bktxcd_fixed_on_txdtls": tx_bktxcd_fixed,
        "reordered_ntry": reordered_ntry,
        "reordered_txdtls": reordered_txdtls,
        "addtl_ntryinf_changed": addtl_changed,
        "new_ns": new_ns,
        "old_ns": old_ns,
    }

def _default_outfile(infile: Path, target_version: int) -> Path:
    return infile.with_name(infile.stem + f"_camt053_v{target_version:02d}_bexio.xml")

def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--target", type=int, choices=[4, 8], default=8)
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--outdir", type=str, default=None)
    ap.add_argument("--xsd", type=str, default=None)
    ap.add_argument("--copy-prtry-to-addtlinf", action="store_true")
    ap.add_argument("--append-prtry", action="store_true")
    return ap.parse_args(argv)

def main(argv: list[str]) -> int:
    args = parse_args(argv)
    input_files: list[Path] = []
    for pat in args.inputs:
        matches = glob.glob(pat)
        if matches:
            input_files.extend(Path(m) for m in matches)
        else:
            input_files.append(Path(pat))
    input_files = [p for p in input_files if p.exists() and p.is_file()]
    if not input_files:
        print("No input files found.", file=sys.stderr)
        return 2
    if args.out and len(input_files) != 1:
        print("--out only with one input.", file=sys.stderr)
        return 2
    outdir = Path(args.outdir) if args.outdir else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
    xsd_path = Path(args.xsd) if args.xsd else None
    if xsd_path and not xsd_path.exists():
        print(f"XSD not found: {xsd_path}", file=sys.stderr)
        return 2

    ok_count = 0
    for infile in input_files:
        try:
            tree = ET.parse(infile)
            report = transform_tree(
                tree, args.target,
                copy_prtry_to_addtlinf=args.copy_prtry_to_addtlinf,
                append_if_present=args.append_prtry,
            )
            outfile = Path(args.out) if args.out else _default_outfile(infile, args.target)
            if outdir and not args.out:
                outfile = outdir / outfile.name
            tree.write(outfile, encoding="utf-8", xml_declaration=True)

            valid_txt = ""
            if xsd_path:
                ok, errs = _validate_with_xsd(outfile, xsd_path)
                valid_txt = " | XSD:OK" if ok else (" | XSD:FAIL " + ("; ".join(errs[:3]) if errs else ""))

            print(
                f"[OK] {infile.name} -> {outfile.name}{valid_txt} | "
                f"AddtlNtryInf*={report['addtl_ntryinf_changed']}, "
                f"ValDt+={report['valdt_added_on_entries']}"
            )
            ok_count += 1
        except Exception as e:
            print(f"[FAIL] {infile}: {e}", file=sys.stderr)

    return 0 if ok_count == len(input_files) else 1

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
