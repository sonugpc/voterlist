#!/usr/bin/env python3
"""
Voter List Extractor - Masalpur
Extracts voter data from 11 Hindi PDFs into a JSON database.
Text is garbled due to custom font encoding — searchable as-is, optimize later.

Exact per-voter sequence in PDF text:
  नरम:            ← label (skip)
  ललग:            ← label (skip)
  मकरन सपखजर:    ← label (skip)
  नपतर/पनत कर नरम: ← label (sets relation type)
  आजच:  {N}      ← age value
  {gender}        ← सल (F) / पचरष (M)
  {house_no}      ← digits
  {name}          ← actual name
  {relation_name} ← father/husband name
  {voter_id}      ← WJS... / RJ/... / KFX...
   {serial}       ← padded number
"""

import pymupdf
import json
import re
import os
from pathlib import Path

# ── Known label strings (garbled from custom font encoding) ───────────────────
LABEL_NAM   = "नरम:"           # Name:
LABEL_LING  = "ललग:"           # Gender:
LABEL_MAKAN = "मकरन सपखजर:"   # House No:
LABEL_PITA  = "नपतर कर नरम:"  # Father's name:
LABEL_PATI  = "पनत कर नरम:"   # Husband's name:
LABEL_AAYU  = "आजच:"           # Age:

ALL_LABELS = (LABEL_NAM, LABEL_LING, LABEL_MAKAN, LABEL_PITA, LABEL_PATI, LABEL_AAYU)

# Gender garbled values
GENDER_MAP = {"सल": "F", "पचरष": "M", "अनज": "O"}

# Voter ID pattern
VOTER_ID_RE = re.compile(r'^(WJS\d+|KFX\d+|RJ/\d+/\d+/\d+)$')

# Serial number line (leading/trailing spaces around digits)
SERIAL_RE = re.compile(r'^\s+(\d+)\s*$')

# Age line: "आजच:  23"
AGE_RE = re.compile(r'आजच:\s*(\d+)')

# Lines to always skip
SKIP_PREFIXES = (
    "नजलरपररषद", "पपचरजत सनमनत", "गरमपपचरजत", "रररर कमरपक",
    "नरधरनसभर ककत", "पतष सपखजर", "मतदरन बबस", "मतदरतरओ",
    "आरनमभक", "अपनतम", "ननरररचन पपजल", "रररर कर नकशर",
    "रररर कर पररसल", "मकरन स.", "ररजककय", "ररजककज",
    "नज॰", "पप॰", "ररजज ननरररचन", " पपचरजत चचनरर",
    "आजच 1 जनररल",
)

SKIP_EXACT = {
    "ररजज ननरररचन आजयग, ररजससरन",
    "ननरररचन पपजलकरण अनधकररल कक हसतरकर",
}


def skip(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s in SKIP_EXACT:
        return True
    for p in SKIP_PREFIXES:
        if s.startswith(p):
            return True
    # Rows of 5+ numbers (summary counts)
    if re.match(r'^\d+(\s+\d+){4,}$', s):
        return True
    return False


def ward_from_filename(name: str) -> int:
    m = re.search(r'Ward No-0*(\d+)', name)
    return int(m.group(1)) if m else 0


def is_mohalla(line: str) -> bool:
    """Locality/mohalla header: Devanagari phrase that's not a known label."""
    s = line.strip()
    if not s or skip(s):
        return False
    if any(s.startswith(lbl) for lbl in ALL_LABELS):
        return False
    if VOTER_ID_RE.match(s) or SERIAL_RE.match(line) or AGE_RE.match(s):
        return False
    if s in GENDER_MAP:
        return False
    if re.match(r'^\d+[A-Za-z]?$', s):
        return False
    # Must have meaningful Devanagari content
    dev = sum(1 for c in s if '\u0900' <= c <= '\u097F')
    return dev >= 3


# ── Per-page parser ───────────────────────────────────────────────────────────

def parse_page(page_text: str, ward: int, pdf_name: str,
               page_num: int, mohalla: str) -> tuple[list[dict], str]:
    """
    State machine with explicit steps matching the exact voter card format.
    Steps (in order after नरम: label):
      0: waiting for age (आजच:)
      1: waiting for gender
      2: waiting for house_no
      3: waiting for name
      4: waiting for relation_name
      5: waiting for voter_id
      (serial resets to idle)
    """
    voters = []
    lines = page_text.split('\n')

    step = -1          # -1 = idle (between cards)
    relation_type = "father"
    cur: dict = {}

    def flush():
        if cur.get("voter_id") and cur.get("name"):
            dev = sum(1 for c in cur["name"] if '\u0900' <= c <= '\u097F')
            if dev >= 2:
                voters.append({
                    "voter_id":      cur["voter_id"],
                    "serial":        cur.get("serial", 0),
                    "name":          cur["name"].strip(),
                    "relation_type": cur.get("relation_type", "father"),
                    "relation_name": cur.get("relation_name", "").strip(),
                    "house_no":      cur.get("house_no", ""),
                    "age":           cur.get("age", 0),
                    "gender":        cur.get("gender", ""),
                    "ward":          ward,
                    "mohalla":       cur.get("mohalla", mohalla),
                    "pdf":           pdf_name,
                    "page":          page_num,
                })

    for line in lines:
        s = line.strip()

        if skip(s):
            continue

        # Mohalla/locality header
        if step == -1 and is_mohalla(line):
            mohalla = s
            continue

        # ── नरम: label → start a new card ─────────────────────────────────
        if s.startswith(LABEL_NAM):
            flush()
            cur = {"mohalla": mohalla}
            relation_type = "father"
            step = 0
            continue

        # ── Other label lines ──────────────────────────────────────────────
        if s.startswith(LABEL_PITA):
            relation_type = "father"
            continue
        if s.startswith(LABEL_PATI):
            relation_type = "husband"
            continue
        if s.startswith(LABEL_LING) or s.startswith(LABEL_MAKAN):
            continue

        # ── Serial number → end of card ────────────────────────────────────
        m = SERIAL_RE.match(line)
        if m:
            cur["serial"] = int(m.group(1))
            step = -1   # idle until next नरम:
            # check for mohalla header in next lines is handled above
            continue

        # ── Only process data lines while inside a card ────────────────────
        if step < 0:
            # Still might be a mohalla header
            if is_mohalla(line):
                mohalla = s
            continue

        # Step 0: age
        if step == 0:
            age_m = AGE_RE.search(s)
            if age_m:
                cur["age"] = int(age_m.group(1))
                step = 1
            continue

        # Step 1: gender
        if step == 1:
            if s in GENDER_MAP:
                cur["gender"] = GENDER_MAP[s]
                step = 2
            elif re.match(r'^\d+[A-Za-z]?$', s):
                # Some cards skip gender line — treat as house_no
                cur["house_no"] = s
                step = 3
            continue

        # Step 2: house number
        if step == 2:
            if re.match(r'^\d+[A-Za-z]?$', s):
                cur["house_no"] = s
                step = 3
            continue

        # Step 3: name
        if step == 3:
            if VOTER_ID_RE.match(s):
                # Voter ID appeared before name — skip this card
                step = -1
                cur = {}
                continue
            cur["name"] = s
            cur["relation_type"] = relation_type
            step = 4
            continue

        # Step 4: relation name
        if step == 4:
            if VOTER_ID_RE.match(s):
                cur["voter_id"] = s
                step = -1
                continue
            cur["relation_name"] = s
            step = 5
            continue

        # Step 5: voter ID
        if step == 5:
            if VOTER_ID_RE.match(s):
                cur["voter_id"] = s
                step = -1
            continue

    flush()
    return voters, mohalla


# ── Main extraction ───────────────────────────────────────────────────────────

def transliterate_roman(name: str) -> str:
    try:
        from indic_transliteration import sanscript
        from indic_transliteration.sanscript import transliterate
        return transliterate(name, sanscript.DEVANAGARI, sanscript.ITRANS).lower()
    except Exception:
        return name.lower()


def extract_all(pdf_dir: str) -> list[dict]:
    pdf_dir = Path(pdf_dir)
    all_voters = []
    pdfs = sorted(pdf_dir.glob("WithPhoto_MASALPUR-Ward No-*.pdf"))
    print(f"Found {len(pdfs)} PDF files\n")

    for pdf_path in pdfs:
        ward = ward_from_filename(pdf_path.name)
        print(f"Ward {ward:2d}: {pdf_path.name}")
        doc = pymupdf.open(str(pdf_path))
        ward_voters = []
        mohalla = ""

        for pn in range(2, len(doc)):
            text = doc[pn].get_text()
            pv, mohalla = parse_page(text, ward, pdf_path.name, pn + 1, mohalla)
            ward_voters.extend(pv)

        doc.close()
        print(f"       → {len(ward_voters)} records")
        all_voters.extend(ward_voters)

    return all_voters


def main():
    here = os.path.dirname(os.path.abspath(__file__))

    print("=" * 60)
    print("  Masalpur Voter List Extractor")
    print("=" * 60 + "\n")

    voters = extract_all(here)
    print(f"\n✅ Total: {len(voters)} voter records\n")

    print("Adding Hinglish (Roman) search index...")
    for v in voters:
        v["name_roman"]          = transliterate_roman(v["name"])
        v["relation_name_roman"] = transliterate_roman(v["relation_name"])

    output = {
        "meta": {
            "total":    len(voters),
            "wards":    11,
            "location": "Masalpur, Karauli, Rajasthan",
            "year":     2026,
            "note":     "Text garbled (custom font encoding). Searchable as-is. Optimization pending."
        },
        "voters": voters
    }

    out = os.path.join(here, "voters.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    kb = os.path.getsize(out) / 1024
    print(f"✅ Saved → {out}  ({kb:.1f} KB)\n")

    # Quality check
    complete = [v for v in voters if v["relation_name"]]
    print(f"Records with relation name filled: {len(complete)}/{len(voters)}")

    print("\n--- Sample Records ---")
    for v in voters[:5]:
        print(f"  [{v['ward']}-{v['serial']}] {v['name']:18s} | {v['relation_type']}: {v['relation_name']:18s} | "
              f"Age:{v['age']:3d} | {v['gender']} | ID:{v['voter_id']}")


if __name__ == "__main__":
    main()
