#!/usr/bin/env python3
"""
WISE camt.053 v10 -> camt.053 v8 (default) or v4 transformer, with bexio-oriented fixes.

Adds/ensures for bexio:
- <Ntry><ValDt><Dt>YYYY-MM-DD</Dt></ValDt> (derived from BookgDt)
- <BkTxCd><Domn><Cd>...<Fmly><Cd>...<SubFmlyCd>...</...> (keeps any existing Prtry)
- XSD element order for <Ntry>, <TxDtls>, and <AmtDtls> (camt XSD is order-sensitive)

Conversion-specific normalization:
- Preserves an existing <XchgRate> from the input (never recalculates it)
- Uses the NET source amount after a separately booked fee for FX amount details
- For source-account entries (e.g. EUR debit of a EUR->CHF conversion):
  keeps <TxAmt> as target currency amount and writes <CntrValAmt> in account currency
  based on the net converted amount.
- For target-account entries (e.g. CHF credit of a EUR->CHF conversion):
  writes the net converted source amount into <InstdAmt> and keeps <TxAmt>
  as the credited amount in the target currency.
- If the fee is not inferable, existing XML amounts are used as fallback.

Usage:
  python wise_camt053_transform.py input.xml --target 8 --copy-prtry-to-addtlinf
  python wise_camt053_transform.py "folder/*.xml" --target 8 --outdir out/ --copy-prtry-to-addtlinf
  python wise_camt053_transform.py input.xml --target 8 --iban CH1234

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
Decimal = decimal.Decimal

_DT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?)?((?:Z)|(?:[+-]\d{2}:\d{2}))?$"
)
_CONVERSION_RE = re.compile(
    r"Converted\s+([0-9][0-9.,']*)\s+([A-Z]{3})\s+to\s+([0-9][0-9.,']*)\s+([A-Z]{3})"
    r"(?:\s+\(fee:\s*([0-9][0-9.,']*)\s+([A-Z]{3})\))?",
    re.IGNORECASE,
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
        el.text = str(abs(Decimal(txt)))
        return True
    except Exception:
        return False


NTRY_ORDER = [
    "NtryRef", "Amt", "CdtDbtInd", "RvslInd", "Sts", "BookgDt", "ValDt",
    "AcctSvcrRef", "Avlbty", "BkTxCd",
    "ComssnWvrInd", "AddtlInfInd", "AmtDtls",
    "Chrgs", "Intrst", "Card", "NtryDtls", "AddtlNtryInf"
]
TXDTLS_ORDER = [
    "Refs", "Amt", "CdtDbtInd", "AmtDtls", "Avlbty", "BkTxCd", "Chrgs", "Intrst",
    "RltdPties", "RltdAgts", "LclInstrm", "Purp", "RltdDts", "RltdPric", "RltdQties",
    "FinInstrmId", "Tax", "RtrInf", "CorpActn", "SfkpgAcct", "CshDpst",
    "CardTx", "AddtlTxInf"
]
AMTDTLS_ORDER = ["InstdAmt", "TxAmt", "CntrValAmt", "AnncdPstngAmt", "PrtryAmt"]


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

def _find_direct(parent: ET.Element | None, ns: str, local_name: str) -> ET.Element | None:
    if parent is None:
        return None
    return parent.find(f"{{{ns}}}{local_name}")


def _ensure_direct(parent: ET.Element, ns: str, local_name: str) -> tuple[ET.Element, bool]:
    el = parent.find(f"{{{ns}}}{local_name}")
    if el is not None:
        return el, False
    el = ET.Element(f"{{{ns}}}{local_name}")
    parent.append(el)
    return el, True


def _fmt_decimal(value: Decimal) -> str:
    q = value.quantize(Decimal("0.01"))
    return format(q, "f")


def _set_amount(details_parent: ET.Element, ns: str, amount_local: str, ccy: str, value: Decimal) -> bool:
    changed = False
    amt_wrap, created = _ensure_direct(details_parent, ns, amount_local)
    changed |= created
    amt_el, created_amt = _ensure_direct(amt_wrap, ns, "Amt")
    changed |= created_amt
    txt = _fmt_decimal(value)
    if amt_el.text != txt:
        amt_el.text = txt
        changed = True
    if amt_el.get("Ccy") != ccy:
        amt_el.set("Ccy", ccy)
        changed = True
    return changed


def _amount_value(wrapper: ET.Element | None, ns: str) -> tuple[Decimal | None, str | None]:
    if wrapper is None:
        return None, None
    amt = wrapper.find(f"{{{ns}}}Amt")
    if amt is None or not amt.text or not amt.text.strip():
        return None, None
    try:
        return Decimal(amt.text.strip()), amt.get("Ccy")
    except Exception:
        return None, amt.get("Ccy")


def _parse_number(num: str) -> Decimal:
    s = num.strip().replace("'", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    return Decimal(s)


def _parse_conversion_text(text: str | None):
    if not text:
        return None
    m = _CONVERSION_RE.search(text)
    if not m:
        return None
    gross_src_amt = _parse_number(m.group(1))
    src_ccy = m.group(2).upper()
    trgt_amt = _parse_number(m.group(3))
    trgt_ccy = m.group(4).upper()
    fee_amt = _parse_number(m.group(5)) if m.group(5) else None
    fee_ccy = m.group(6).upper() if m.group(6) else None
    net_src_amt = gross_src_amt
    if fee_amt is not None and fee_ccy == src_ccy:
        net_src_amt = gross_src_amt - fee_amt
    return {
        "gross_src_amt": gross_src_amt,
        "net_src_amt": net_src_amt,
        "src_ccy": src_ccy,
        "trgt_amt": trgt_amt,
        "trgt_ccy": trgt_ccy,
        "fee_amt": fee_amt,
        "fee_ccy": fee_ccy,
    }


# Wise has used different proprietary BkTxCd/Prtry codes for FX conversions.
# Older exports commonly use CONVERSION_ORDER-* while newer exports may use
# BALANCE-* for the same logical conversion. Treat both as conversion entries.
_CONVERSION_PRTRY_PREFIXES = ("CONVERSION_ORDER-", "BALANCE-")
_FEE_CONVERSION_PRTRY_PREFIXES = tuple(f"FEE-{p}" for p in _CONVERSION_PRTRY_PREFIXES)


def _normalized_conversion_ref_from_prtry(prtry_cd: str | None) -> str | None:
    """Return the Wise conversion reference without a leading FEE-, if applicable.

    Examples:
      CONVERSION_ORDER-4408792     -> CONVERSION_ORDER-4408792
      FEE-CONVERSION_ORDER-4408792 -> CONVERSION_ORDER-4408792
      BALANCE-5160975431          -> BALANCE-5160975431
      FEE-BALANCE-5160975431      -> BALANCE-5160975431
    """
    if not prtry_cd:
        return None
    pr = prtry_cd.strip().upper()
    if pr.startswith("FEE-"):
        pr = pr[len("FEE-"):]
    if pr.startswith(_CONVERSION_PRTRY_PREFIXES):
        return pr
    return None


def _is_conversion_entry(ntry: ET.Element, ns: str) -> bool:
    pr = (_get_prtry_cd(ntry, ns) or "").strip().upper()
    # Fee entries are separate booking lines and must not be normalized as the
    # conversion itself. They are only used by _build_conversion_fee_map().
    return (
        pr.startswith(_CONVERSION_PRTRY_PREFIXES)
        and not pr.startswith("FEE-")
    )


def _conversion_order_ref_from_prtry(prtry_cd: str | None) -> str | None:
    # Backwards-compatible wrapper: older code/comments refer to
    # "conversion order", but the function now also supports BALANCE-* refs.
    return _normalized_conversion_ref_from_prtry(prtry_cd)


def _build_conversion_fee_map(root: ET.Element, ns: str) -> dict[tuple[str, str], Decimal]:
    fees: dict[tuple[str, str], Decimal] = {}
    for ntry in root.findall(f".//{{{ns}}}Stmt/{{{ns}}}Ntry"):
        pr = (_get_prtry_cd(ntry, ns) or "").strip().upper()
        if not pr.startswith(_FEE_CONVERSION_PRTRY_PREFIXES):
            continue
        order_ref = _normalized_conversion_ref_from_prtry(pr)
        amt_el = _find_direct(ntry, ns, "Amt")
        if order_ref is None or amt_el is None or not amt_el.text:
            continue
        ccy = (amt_el.get("Ccy") or "").upper()
        if not ccy:
            continue
        try:
            fee_amt = Decimal(amt_el.text.strip())
        except Exception:
            continue
        key = (order_ref, ccy)
        fees[key] = fees.get(key, Decimal("0")) + fee_amt
    return fees



def _copy_or_update_ccyxchg(dst_parent: ET.Element, src_ccyxchg: ET.Element, ns: str) -> bool:
    changed = False
    dst = dst_parent.find(f"{{{ns}}}CcyXchg")
    if dst is None:
        dst = ET.SubElement(dst_parent, f"{{{ns}}}CcyXchg")
        changed = True

    for child_name in ("SrcCcy", "TrgtCcy", "UnitCcy", "XchgRate", "CtrctId", "QtnDt"):
        src_child = src_ccyxchg.find(f"{{{ns}}}{child_name}")
        if src_child is None:
            continue
        dst_child = dst.find(f"{{{ns}}}{child_name}")
        if dst_child is None:
            dst_child = ET.SubElement(dst, f"{{{ns}}}{child_name}")
            changed = True
        src_text = (src_child.text or "").strip()
        if (dst_child.text or "").strip() != src_text:
            dst_child.text = src_text
            changed = True
    return changed


def _remove_direct(parent: ET.Element | None, ns: str, local_name: str) -> bool:
    if parent is None:
        return False
    child = parent.find(f"{{{ns}}}{local_name}")
    if child is None:
        return False
    parent.remove(child)
    return True


def _normalize_conversion_amtdtls(ntry: ET.Element, ns: str, fee_map: dict[tuple[str, str], Decimal] | None = None) -> bool:
    if not _is_conversion_entry(ntry, ns):
        return False

    amt_el = _find_direct(ntry, ns, "Amt")
    cdi_el = _find_direct(ntry, ns, "CdtDbtInd")
    amtdtls = _find_direct(ntry, ns, "AmtDtls")
    if amt_el is None or cdi_el is None or amtdtls is None or not amt_el.text:
        return False

    try:
        ntry_amt = Decimal(amt_el.text.strip())
    except Exception:
        return False
    ntry_ccy = (amt_el.get("Ccy") or "").upper()
    cdi = (cdi_el.text or "").strip().upper()

    txamt = _find_direct(amtdtls, ns, "TxAmt")
    instdamt = _find_direct(amtdtls, ns, "InstdAmt")
    cntrval = _find_direct(amtdtls, ns, "CntrValAmt")

    existing_ccyxchg = None
    for wrapper in (txamt, instdamt, cntrval):
        if wrapper is None:
            continue
        ccyxchg = _find_direct(wrapper, ns, "CcyXchg")
        if ccyxchg is not None:
            existing_ccyxchg = ccyxchg
            break
    if existing_ccyxchg is None:
        return False

    src_el = _find_direct(existing_ccyxchg, ns, "SrcCcy")
    trgt_el = _find_direct(existing_ccyxchg, ns, "TrgtCcy")
    unit_el = _find_direct(existing_ccyxchg, ns, "UnitCcy")
    rate_el = _find_direct(existing_ccyxchg, ns, "XchgRate")
    src_ccy = ((src_el.text if src_el is not None else "") or "").strip().upper()
    trgt_ccy = ((trgt_el.text if trgt_el is not None else "") or "").strip().upper()
    unit_ccy = ((unit_el.text if unit_el is not None else "") or "").strip().upper()
    try:
        xchg_rate = Decimal((rate_el.text or "").strip()) if rate_el is not None and (rate_el.text or "").strip() else None
    except Exception:
        xchg_rate = None
    if not src_ccy or not trgt_ccy:
        return False

    addtl = _find_direct(ntry, ns, "AddtlNtryInf")
    parsed = _parse_conversion_text(addtl.text if addtl is not None else None)
    prtry_cd = _get_prtry_cd(ntry, ns)
    order_ref = _conversion_order_ref_from_prtry(prtry_cd)

    changed = False

    def net_src_amount() -> Decimal | None:
        if parsed and parsed["src_ccy"] == src_ccy:
            net = parsed["net_src_amt"]
            # Wise often emits the fee as a separate FEE-<conversion-ref> entry
            # instead of embedding it in the conversion text. Only subtract it here
            # if the text itself did not already contain a same-currency fee.
            parsed_fee_same_ccy = bool(parsed.get("fee_amt") is not None and parsed.get("fee_ccy") == src_ccy)
            if not parsed_fee_same_ccy and fee_map and order_ref:
                fee_key = (order_ref, src_ccy)
                if fee_key in fee_map:
                    net -= fee_map[fee_key]
            # If no explicit fee info is available, derive the actually converted
            # net source amount from the booked target amount and the quoted rate.
            elif not parsed_fee_same_ccy and xchg_rate and xchg_rate != 0 and parsed.get("trgt_ccy") == trgt_ccy:
                try:
                    if unit_ccy == src_ccy:
                        inferred = parsed["trgt_amt"] / xchg_rate
                    elif unit_ccy == trgt_ccy:
                        inferred = parsed["trgt_amt"] * xchg_rate
                    else:
                        inferred = None
                    if inferred is not None:
                        net = inferred.quantize(Decimal("0.01"))
                except Exception:
                    pass
            return net
        if ntry_ccy == src_ccy and cdi == "DBIT":
            return ntry_amt.quantize(Decimal("0.01"))
        if ntry_ccy == trgt_ccy and cdi == "CRDT" and xchg_rate and xchg_rate != 0:
            try:
                if unit_ccy == src_ccy:
                    return (ntry_amt / xchg_rate).quantize(Decimal("0.01"))
                if unit_ccy == trgt_ccy:
                    return (ntry_amt * xchg_rate).quantize(Decimal("0.01"))
            except Exception:
                pass
        if txamt is not None:
            v, c = _amount_value(txamt, ns)
            if v is not None and (c or "").upper() == src_ccy:
                return v
        if instdamt is not None:
            v, c = _amount_value(instdamt, ns)
            if v is not None and (c or "").upper() == src_ccy:
                return v
        if cntrval is not None:
            v, c = _amount_value(cntrval, ns)
            if v is not None and (c or "").upper() == src_ccy:
                return v
        return None

    def target_amount() -> Decimal | None:
        if parsed and parsed["trgt_ccy"] == trgt_ccy:
            return parsed["trgt_amt"]
        if txamt is not None:
            v, c = _amount_value(txamt, ns)
            if v is not None and (c or "").upper() == trgt_ccy:
                return v
        if instdamt is not None:
            v, c = _amount_value(instdamt, ns)
            if v is not None and (c or "").upper() == trgt_ccy:
                return v
        return None

    source_net_amt = net_src_amount()
    trgt_amt = target_amount()

    # Source-account side: e.g. EUR statement, debit entry for EUR->CHF conversion.
    if ntry_ccy == src_ccy and cdi == "DBIT":
        if trgt_amt is not None:
            txamt, created = _ensure_direct(amtdtls, ns, "TxAmt")
            changed |= created
            changed |= _set_amount(amtdtls, ns, "TxAmt", trgt_ccy, trgt_amt)
            changed |= _copy_or_update_ccyxchg(txamt, existing_ccyxchg, ns)

        if source_net_amt is not None:
            cntrval, created = _ensure_direct(amtdtls, ns, "CntrValAmt")
            changed |= created
            changed |= _set_amount(amtdtls, ns, "CntrValAmt", src_ccy, source_net_amt)
            changed |= _remove_direct(cntrval, ns, "CcyXchg")
        else:
            changed |= _remove_direct(amtdtls, ns, "CntrValAmt")

        if instdamt is not None:
            amtdtls.remove(instdamt)
            changed = True

    # Target-account side: e.g. CHF statement, credit entry for EUR->CHF conversion.
    elif ntry_ccy == trgt_ccy and cdi == "CRDT":
        instdamt, created = _ensure_direct(amtdtls, ns, "InstdAmt")
        changed |= created
        if source_net_amt is not None:
            changed |= _set_amount(amtdtls, ns, "InstdAmt", src_ccy, source_net_amt)
            changed |= _copy_or_update_ccyxchg(instdamt, existing_ccyxchg, ns)

        txamt, created = _ensure_direct(amtdtls, ns, "TxAmt")
        changed |= created
        changed |= _set_amount(amtdtls, ns, "TxAmt", trgt_ccy, ntry_amt)
        changed |= _remove_direct(txamt, ns, "CcyXchg")

        if cntrval is not None:
            amtdtls.remove(cntrval)
            changed = True

    if _reorder_children(amtdtls, AMTDTLS_ORDER):
        changed = True
    return changed


def _set_stmt_account_iban(root: ET.Element, ns: str, iban: str) -> int:
    changed = 0
    for stmt in root.findall(f".//{{{ns}}}Stmt"):
        acct = stmt.find(f"{{{ns}}}Acct")
        if acct is None:
            continue
        acct_id = acct.find(f"{{{ns}}}Id")
        if acct_id is None:
            acct_id = ET.Element(f"{{{ns}}}Id")
            acct.insert(0, acct_id)
        closing_ws = acct_id.tail if acct_id.tail and not acct_id.tail.strip() else "\n"
        child_ws = closing_ws + "    "
        iban_el = acct_id.find(f"{{{ns}}}IBAN")
        if (
            iban_el is not None
            and len(list(acct_id)) == 1
            and (iban_el.text or "").strip() == iban
            and acct_id.text == child_ws
            and iban_el.tail == closing_ws
        ):
            continue
        acct_id[:] = []
        acct_id.text = child_ws
        iban_el = ET.SubElement(acct_id, f"{{{ns}}}IBAN")
        iban_el.text = iban
        iban_el.tail = closing_ws
        changed += 1
    return changed

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


def transform_tree(tree: ET.ElementTree, target_version: int, *, copy_prtry_to_addtlinf: bool, append_if_present: bool, iban: str | None = None) -> dict:
    if target_version not in (4, 8):
        raise ValueError("target_version must be 4 or 8")
    if iban is not None:
        iban = iban.strip().upper()
        if iban == "":
            iban = None
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
    stmt_iban_set = _set_stmt_account_iban(root, new_ns, iban) if iban else 0

    valdt_added = 0
    bktxcd_fixed = 0
    tx_bktxcd_fixed = 0
    reordered_ntry = 0
    reordered_txdtls = 0
    reordered_amtdtls = 0
    addtl_changed = 0
    conversion_fixed = 0
    fee_map = _build_conversion_fee_map(root, new_ns)

    for ntry in root.findall(f".//{{{new_ns}}}Stmt/{{{new_ns}}}Ntry"):
        cdi_el = ntry.find(f"{{{new_ns}}}CdtDbtInd")
        cdi = cdi_el.text.strip() if cdi_el is not None and cdi_el.text else None

        if _ensure_valdt_on_entry(ntry, new_ns):
            valdt_added += 1
        if _ensure_bktxcd_structure(ntry, new_ns, cdi):
            bktxcd_fixed += 1

        if _normalize_conversion_amtdtls(ntry, new_ns, fee_map=fee_map):
            conversion_fixed += 1

        if copy_prtry_to_addtlinf:
            if _maybe_copy_prtry_to_addtlinf(ntry, new_ns, append_if_present=append_if_present):
                addtl_changed += 1

        amt_parent = ntry.find(f"{{{new_ns}}}AmtDtls")
        if amt_parent is not None and _reorder_children(amt_parent, AMTDTLS_ORDER):
            reordered_amtdtls += 1

        if _reorder_children(ntry, NTRY_ORDER):
            reordered_ntry += 1

        for txdtls in ntry.findall(f".//{{{new_ns}}}TxDtls"):
            if _ensure_bktxcd_structure(txdtls, new_ns, cdi):
                tx_bktxcd_fixed += 1
            amt_parent = txdtls.find(f"{{{new_ns}}}AmtDtls")
            if amt_parent is not None and _reorder_children(amt_parent, AMTDTLS_ORDER):
                reordered_amtdtls += 1
            if _reorder_children(txdtls, TXDTLS_ORDER):
                reordered_txdtls += 1

    return {
        "timestamps_normalized": dt_changed,
        "AdrTp_removed": adr_tp_removed,
        "debit_sum_fixed": debit_sum_fixed,
        "stmt_account_iban_set": stmt_iban_set,
        "valdt_added_on_entries": valdt_added,
        "bktxcd_fixed_on_entries": bktxcd_fixed,
        "bktxcd_fixed_on_txdtls": tx_bktxcd_fixed,
        "conversion_amtdtls_fixed": conversion_fixed,
        "reordered_ntry": reordered_ntry,
        "reordered_txdtls": reordered_txdtls,
        "reordered_amtdtls": reordered_amtdtls,
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
    ap.add_argument("--iban", type=str, default=None)
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
                iban=args.iban,
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
                f"ConvFix={report['conversion_amtdtls_fixed']}, "
                f"StmtIBAN={report['stmt_account_iban_set']}, "
                f"AddtlNtryInf*={report['addtl_ntryinf_changed']}, "
                f"ValDt+={report['valdt_added_on_entries']}"
            )
            ok_count += 1
        except Exception as e:
            print(f"[FAIL] {infile}: {e}", file=sys.stderr)

    return 0 if ok_count == len(input_files) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
