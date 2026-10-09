"""Phase 6: protected facts and the deterministic preservation comparators.

The core is a table of rewrites. Allowed rewrites (better wording, same facts) must raise no issue;
disallowed ones (changed value, weakened obligation, swapped role...) must raise the expected check.
Real-sample tests then seed faults into the 3 SOPs and the GWP and require every one to be caught.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from app.schemas.v2 import (
    Claim,
    FactKind,
    Gate,
    Modality,
    SectionDraft,
    SlotDraft,
    SourceDocument,
    SourceSection,
    SourceUnit,
    TableCell,
    TableRef,
    UnitType,
)
from app.services.migration_v2.quality.facts import (
    extract_facts,
    extract_terms,
    extract_values,
    find_roles,
    sentence_modalities,
)
from app.services.migration_v2.quality.normalize import PreservationConfig, number_value, words_to_number
from app.services.migration_v2.quality.preservation import check_preservation

UNIT = "SRC-6-U001"


# ── Fixtures ──────────────────────────────────────────────────────────


def _doc(*texts: str) -> SourceDocument:
    """A small SOP: definitions with QA, a roles table, and the units under test in PROCESS."""
    defs = SourceUnit(
        unit_id="SRC-3-U001", content_hash="d1", section_id="SRC-3", seq=0, unit_type=UnitType.PARAGRAPH,
        text="Quality Assurance (QA) is the independent quality function.",
    )
    role_row = SourceUnit(
        unit_id="SRC-5-U001", content_hash="r1", section_id="SRC-5", seq=1, unit_type=UnitType.TABLE_ROW,
        text="Process Owner | Owns the process",
        table_ref=TableRef(table_id="SRC-5-T1", row_index=1, header_cells=["Role", "Responsibility"], cells=[
            TableCell(col=0, text="Process Owner"), TableCell(col=1, text="Owns the process"),
        ]),
    )
    units = [
        SourceUnit(unit_id=f"SRC-6-U{i:03d}", content_hash=f"p{i}", section_id="SRC-6", seq=10 + i,
                   unit_type=UnitType.PARAGRAPH, text=t)
        for i, t in enumerate(texts, start=1)
    ]
    return SourceDocument(document_id="SOP", source_file="sop.docx", file_type="docx", sections=[
        SourceSection(section_id="SRC-3", number="3", heading="DEFINITIONS", level=1, section_order=0, units=[defs]),
        SourceSection(section_id="SRC-5", number="5", heading="ROLES & RESPONSIBILITIES", level=1, section_order=1, units=[role_row]),
        SourceSection(section_id="SRC-6", number="6", heading="PROCESS", level=1, section_order=2, units=units),
    ])


def _draft(claims: list[tuple[str, list[str]]]) -> SectionDraft:
    return SectionDraft(target_section_id="TGT-3", version=1, slots=[SlotDraft(slot_id="TGT-3-STEPS", claims=[
        Claim(claim_id=f"C-{i:03d}", text=text, source_unit_ids=units) for i, (text, units) in enumerate(claims, start=1)
    ])])


def _check(source: str, *claims: str, config: PreservationConfig | None = None):
    doc = _doc(source)
    facts = extract_facts(doc, "MIG-T", config)
    return check_preservation(facts, doc, [_draft([(c, [UNIT]) for c in claims])], config)


def _rules(issues) -> set[str]:
    return {re.match(r"\[(PRES-\d+)\]", i.message).group(1) for i in issues}


# ── The rewrite table ─────────────────────────────────────────────────

ALLOWED = [
    # migration_plan.md section 2.1
    ("QA shall approve the deviation before closure.",
     ["Quality Assurance (QA) must approve the deviation before closure."]),
    # the worked example: the actor moves to the responsibility slot
    ("The Process Owner submits the deviation within five business days.",
     ["Submit the deviation within 5 business days.", "Process Owner: submits the deviation."]),
    ("Submit the deviation within 5 working days.", ["Submit the deviation within five business days."]),
    ("The deviation must be approved by QA.", ["QA must approve the deviation."]),
    ("QA must approve the change. QA must sign the form.", ["QA must approve the change and sign the form."]),
    ("Review the record annually.", ["Review the record once a year."]),
    ("Review the record every 12 months.", ["Review the record annually."]),
    ("Retain records for 10 years.", ["Retain the records for ten years."]),
    ("The temperature must be between 2 °C and 8 °C.", ["Keep the temperature at 2-8 °C."]),
    ("Deviation must be less than 10 %.", ["The deviation must be under ten percent."]),
    ("The form must be signed by two individuals.", ["Two individuals must sign the form."]),
    ("Close the CAPA by 17 Sep 2025.", ["Close the CAPA by 17 September 2025."]),
    ("Details can be found in BI-VQD-10095-S.", ["Details are in BI-VQD-10095-S."]),
    ("The process is described in chapter 6.3.", ["The process is described in {{ref:SRC-6.3}}."]),
    ("QA must not close the deviation.", ["QA must never close the deviation."]),
    ("You must not reuse labels.", ["Do not reuse labels."]),
    ("The goods can only be shipped under quarantine status.", ["The goods must only be shipped under quarantine status."]),
    ("The Process Owner may extend the deadline if required.", ["The Process Owner may extend the deadline when required."]),
    ("The system will notify QA.", ["The system notifies QA."]),
    ("Send the report to CTSU_Complaints.bib@boehringer-ingelheim.com.",
     ["Send the report to ctsu_complaints.bib@boehringer-ingelheim.com."]),
    ("1. Create the deviation record.", ["Create the deviation record."]),  # list numbering is not a value
]

DISALLOWED = [
    # (source, claims, rules that must fire, gate that must be set)
    ("QA shall approve the deviation before closure.", ["Quality Assurance should review the deviation before closure."],
     {"PRES-004"}, Gate.HIGH_RISK_UNRESOLVED),
    ("Submit the deviation within five business days.", ["Submit the deviation within 10 business days."],
     {"PRES-002"}, Gate.NUMERICAL_CHANGE),
    ("Submit the deviation within 5 business days.", ["Submit the deviation within 5 calendar days."],
     {"PRES-002"}, Gate.NUMERICAL_CHANGE),
    ("Submit the deviation within 5 business days.", ["Submit the deviation after 5 business days."],
     {"PRES-002"}, Gate.NUMERICAL_CHANGE),
    ("Store at 2-8 °C for 30 days.", ["Store at 2-8 °C."], {"PRES-002"}, Gate.NUMERICAL_CHANGE),
    ("The yield must be at least 95%.", ["The yield must be 95%."], {"PRES-001"}, Gate.NUMERICAL_CHANGE),
    ("The yield must be at least 95%.", ["The yield must be at least 90%."], {"PRES-001"}, Gate.NUMERICAL_CHANGE),
    ("Review the record annually.", ["Review the record monthly."], {"PRES-002"}, Gate.NUMERICAL_CHANGE),
    ("Close the CAPA by 17 Sep 2025.", ["Close the CAPA by 17 October 2025."], {"PRES-002"}, Gate.NUMERICAL_CHANGE),
    ("Approve the deviation.", ["Approve the deviation within 3 days."], {"PRES-002"}, Gate.NUMERICAL_CHANGE),
    ("The form must be signed by two individuals.", ["The form must be signed by one individual."],
     {"PRES-001"}, Gate.NUMERICAL_CHANGE),
    ("QA must not close the deviation.", ["QA should not close the deviation."], {"PRES-004"}, Gate.HIGH_RISK_UNRESOLVED),
    ("QA must not close the deviation.", ["QA closes the deviation."], {"PRES-004"}, Gate.HIGH_RISK_UNRESOLVED),
    ("The Process Owner may extend the deadline.", ["The Process Owner must extend the deadline."],
     {"PRES-004"}, Gate.HIGH_RISK_UNRESOLVED),
    ("QA should verify the record.", ["QA must verify the record."], {"PRES-004"}, Gate.HIGH_RISK_UNRESOLVED),
    ("The Process Owner submits the record.", ["The Process Owner can submit the record."],
     {"PRES-004"}, Gate.HIGH_RISK_UNRESOLVED),
    ("QA must approve and sign the form.", ["QA must approve the form.", "QA should sign the form."],
     {"PRES-004"}, Gate.HIGH_RISK_UNRESOLVED),
    ("Follow BI-VQD-10095-S.", ["Follow BI-VQD-10095."], {"PRES-006"}, Gate.HIGH_RISK_UNRESOLVED),
    ("Follow the procedure.", ["Follow BI-VQD-10095-S."], {"PRES-006"}, Gate.UNSUPPORTED_CLAIM),
    ("The Process Owner must close the deviation.", ["The QA Manager must close the deviation."],
     {"PRES-003"}, Gate.UNSUPPORTED_CLAIM),
    ("The Process Owner must close the deviation.", ["The deviation must be closed."],
     {"PRES-003"}, Gate.HIGH_RISK_UNRESOLVED),
]


@pytest.mark.parametrize("source, claims", ALLOWED)
def test_allowed_rewrites_raise_nothing(source, claims):
    issues = [i for i in _check(source, *claims) if "makes it mandatory" not in i.message]
    assert issues == [], [i.message for i in issues]


def test_a_plain_statement_made_mandatory_is_listed_for_review():
    issues = _check("The Process Owner submits the deviation within five business days.",
                    "The Process Owner must submit the deviation within five business days.")
    assert [(i.severity.value, i.gate) for i in issues] == [("medium", None)]
    assert "makes it mandatory" in issues[0].message
    assert _check("QA must not close the deviation.", "QA must never close the deviation.") == []  # stated, not added


def test_a_quoted_name_must_survive():
    source = "The RPAS run on the platform. Refer to “UiPath Development Guideline”."
    lost = _check(source, "The RPAS run on the platform.")
    assert any("quoted name 'UiPath Development Guideline'" in i.message for i in lost)
    assert _check(source, "The RPAS run on the platform. See the “UiPath Development Guideline”.") == []
    garbled = "Global RPAS�s start with �BI�. Refer to �UiPath Development Guideline�."
    assert [i.message for i in _check(garbled, "Global RPAS start with BI.")] == [
        "[PRES-006] quoted name 'UiPath Development Guideline' from SRC-6-U001 is missing from the claims that cite it."]


@pytest.mark.parametrize("source, claims, rules, gate", DISALLOWED)
def test_disallowed_rewrites_are_caught(source, claims, rules, gate):
    issues = _check(source, *claims)
    assert rules <= _rules(issues), [i.message for i in issues]
    assert gate in {i.gate for i in issues}
    assert all(i.unit_ids for i in issues) and all(i.claim_ids for i in issues)


def test_a_changed_value_is_one_issue_with_both_values():
    issues = _check("Submit the deviation within five business days.", "Submit the deviation within 10 business days.")
    assert len(issues) == 1
    issue = issues[0]
    assert "within five business days" in issue.message and "within 10 business days" in issue.message
    assert (issue.severity.value, issue.target_section_id, issue.slot_id) == ("critical", "TGT-3", "TGT-3-STEPS")
    assert issue.issue_id == _check("Submit the deviation within five business days.",
                                    "Submit the deviation within 10 business days.")[0].issue_id  # stable IDs


def test_weakened_mandatory_is_critical_strengthened_is_high():
    weakened = _check("QA must approve the form.", "QA should approve the form.")
    strengthened = _check("QA should approve the form.", "QA must approve the form.")
    assert [i.severity.value for i in weakened] == ["critical"]
    assert [i.severity.value for i in strengthened] == ["high"]


def test_unit_equivalences_are_configurable():
    strict = PreservationConfig(unit_aliases={})
    issues = _check("Submit within 5 working days.", "Submit within 5 business days.", config=strict)
    assert _rules(issues) == {"PRES-002"}


def test_claims_spread_over_slots_and_uncited_units():
    doc = _doc("QA must approve the deviation within 5 days.", "Archive the record for 10 years.")
    facts = extract_facts(doc, "MIG-T")
    draft = SectionDraft(target_section_id="TGT-3", version=1, slots=[
        SlotDraft(slot_id="TGT-3-STEPS", claims=[Claim(claim_id="C-1", text="Approve the deviation.", source_unit_ids=[UNIT])]),
        SlotDraft(slot_id="TGT-3-TIMING", claims=[Claim(claim_id="C-2", text="QA: within 5 days.", source_unit_ids=[UNIT])]),
        SlotDraft(slot_id="TGT-3-GAP", claims=[Claim(claim_id="C-3", text="[No source content]", is_gap_marker=True)]),
    ])
    # SRC-6-U002 is cited by no claim: that is the coverage gate's job (Phase 10), not a preservation issue.
    assert check_preservation(facts, doc, [draft]) == []


# ── Extractors ────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    ("within five business days", {("deadline", "<=5 business_day")}),
    ("within 5 working days", {("deadline", "<=5 business_day")}),
    ("for 30 calendar days", {("duration", "30 calendar_day")}),
    ("within a week", {("deadline", "<=1 week")}),
    ("annually", {("frequency", "1/1 year")}),
    ("twice a year", {("frequency", "2/1 year")}),
    ("every 3 months", {("frequency", "1/1 quarter")}),
    ("every 6 months", {("frequency", "1/6 month")}),
    ("at least 95 %", {("percentage", ">=95%")}),
    ("less than ten percent", {("percentage", "<10%")}),
    ("between 2 and 8 °C", {("number", "2 °c"), ("number", "8 °c")}),
    ("2 – 8 degrees C", {("number", "2 °c"), ("number", "8 °c")}),
    ("1,000 mg", {("number", "1000 mg")}),
    ("Version 3.0", {("number", "3")}),
    ("17 Sep 2025", {("date", "2025-09-17")}),
    ("17.09.2025", {("date", "2025-09-17")}),
    ("September 17, 2025", {("date", "2025-09-17")}),
    ("BI-VQD-10095-S and 028-BIS-00493", {("reference", "BI-VQD-10095-S"), ("reference", "028-BIS-00493")}),
    ("21 CFR Part 11", {("reference", "21 CFR PART 11")}),
    ("see chapter 6.3 and Figure 2", set()),
    ("one of the reviewers; no one else", set()),
    ("one copy", {("number", "1")}),
    ("exactly as designed by the Template (1to1)", set()),
    ("a second reviewer", set()),
    # strings from the reviewed golden expectations
    ("a process landscape with up to 5 levels", {("number", "<=5")}),
    ("signed by two individuals", {("number", "2")}),
    ("organized into 11 steps", {("number", "11")}),
])
def test_extract_values(text, expected):
    assert {(v.kind.value, v.normalized) for v in extract_values(text)} == expected


@pytest.mark.parametrize("sentence, expected", [
    ("QA shall approve the deviation.", {Modality.MANDATORY}),
    ("This SOP must be followed at the date of effectiveness.", {Modality.MANDATORY}),
    ("Separate shipping orders in the EVA system need to be created.", {Modality.MANDATORY}),
    ("It is not permitted to use RPAS with a System Administrator account.", {Modality.PROHIBITION}),
    ("Processes that are completely non GxP can omit the RA document.", {Modality.PERMITTED}),
    ("Goods can only be shipped under quarantine status.", {Modality.MANDATORY}),
    ("QA should not close it.", {Modality.RECOMMENDED}),
    ("Approve the deviation.", {Modality.MANDATORY}),
    ("Review of the record is done by QA.", set()),
    ("Details can be found in the manual.", set()),
    ("Escalate if required.", {Modality.MANDATORY}),
    ("The record is archived as required.", set()),
    ("Effective since May 2025.", set()),
])
def test_sentence_modalities(sentence, expected):
    assert sentence_modalities(sentence) == expected


def test_terms_and_roles():
    terms = extract_terms(["Quality Assurance (QA) and the Lean Validation Approach (LeVA); see BI-VQD-1 (2025)."])
    assert terms == {"Quality Assurance": "QA", "Lean Validation Approach": "LeVA"}
    roles = find_roles("The QA Manager informs the Head of Quality Aseptic Manufacturing and the Process Owner.")
    assert roles == ["QA Manager", "Head of Quality Aseptic Manufacturing", "Process Owner"]
    assert find_roles("Write the User Requirements Specification.") == []
    assert find_roles("QA approves; the QA Manager signs.", ["QA"]) == ["QA", "QA Manager"]


def test_number_words():
    assert [words_to_number(w) for w in ("five", "twenty-four", "one hundred", "a hundred", "ninety nine")] == [5, 24, 100, 100, 99]
    assert [number_value(v) for v in ("1,000", "2.50", "3.0", "twice", "an")] == ["1000", "2.5", "3", "2", "1"]


def test_registry_reads_role_tables_and_skips_boilerplate():
    doc = _doc("QA must approve within 5 days.")
    doc.sections[2].units.append(SourceUnit(
        unit_id="SRC-6-U099", content_hash="b", section_id="SRC-6", seq=99, unit_type=UnitType.PARAGRAPH,
        text="Approved on 17 Sep 2025 by J. Doe", is_boilerplate=True,
    ))
    facts = extract_facts(doc, "MIG-T")
    assert "Process Owner" in facts.roles and "QA" in facts.roles
    assert facts.approved_terms == {"Quality Assurance": "QA"}
    assert {(v.unit_id, v.normalized) for v in facts.values} == {(UNIT, "<=5 day")}
    assert [(o.unit_id, o.modality, o.actor) for o in facts.obligations] == [(UNIT, Modality.MANDATORY, "QA")]


# ── Real samples: allowed rewrites raise nothing, seeded faults are all caught ──

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = sorted((ROOT / "documents" / "SOPs").glob("*.docx")) + sorted((ROOT / "documents" / "GWP").glob("*.pdf"))


@pytest.fixture(scope="module")
def sample_docs():
    if not SAMPLES:
        pytest.skip("sample SOPs not available")
    from app.config.settings import Settings
    from app.services.migration_v2.gwp.ingest import parse_guide

    root = Path(tempfile.mkdtemp())
    settings = Settings(project_root=root)
    settings.resolve_paths(root)
    settings.ensure_directories()
    return [parse_guide(path, settings, f"S{i}") for i, path in enumerate(SAMPLES)]


def _verbatim(doc: SourceDocument, rewrite=lambda t: t) -> SectionDraft:
    units = [u for u in doc.iter_units() if not u.is_boilerplate and u.text.strip()]
    return _draft([(rewrite(u.text), [u.unit_id]) for u in units])


def _allowed_rewrite(text: str) -> str:
    text = re.sub(r"\bshall\b", "must", text)
    text = re.sub(r"\bworking days\b", "business days", text)
    return re.sub(r"\bcan be found\b", "is found", text)


def test_samples_allowed_rewrites_raise_nothing(sample_docs):
    for doc in sample_docs:
        facts = extract_facts(doc, "MIG-S")
        assert facts.values and facts.obligations and facts.roles
        for draft in (_verbatim(doc), _verbatim(doc, _allowed_rewrite)):
            issues = check_preservation(facts, doc, [draft])
            assert issues == [], f"{doc.source_file}: {[i.message for i in issues][:5]}"


def _seed(doc: SourceDocument, unit_id: str, new_text: str) -> SectionDraft:
    units = [u for u in doc.iter_units() if not u.is_boilerplate and u.text.strip()]
    return _draft([(new_text if u.unit_id == unit_id else u.text, [u.unit_id]) for u in units])


def test_samples_seeded_faults_are_all_caught(sample_docs):
    seeded = caught = 0
    for doc in sample_docs:
        facts = extract_facts(doc, "MIG-S")
        units = {u.unit_id: u for u in doc.iter_units()}
        faults: list[tuple[str, str, str]] = []  # (unit_id, mutated text, expected rule)

        # A changed number: bump the first digit run inside a protected value.
        done: set[str] = set()
        for v in facts.values:
            if v.kind == FactKind.REFERENCE or v.unit_id in done or not re.search(r"\d", v.raw):
                continue
            text = units[v.unit_id].text
            hit = re.search(rf"(?<![\w.-]){re.escape(v.raw)}(?![\w-])", text)
            m = re.search(r"\d+", v.raw)
            if not hit or not m:
                continue
            at = hit.start()
            start = at + m.start()
            mutated = text[:start] + str(int(m.group(0)) + 1) + text[at + m.end():]
            faults.append((v.unit_id, mutated, "PRES-001|PRES-002"))
            done.add(v.unit_id)

        # A weakened obligation: the first "must"/"shall" becomes "should".
        for unit_id, unit in units.items():
            if unit.is_boilerplate or re.search(r"\bshould\b|\brecommend", unit.text, re.I):
                continue
            if re.search(r"\b(?:must|shall)\b(?!\s+not)", unit.text):
                faults.append((unit_id, re.sub(r"\b(?:must|shall)\b(?!\s+not)", "should", unit.text, count=1), "PRES-004"))

        # An edited document reference: change its last character.
        done = set()
        for v in facts.values:
            if v.kind == FactKind.REFERENCE and v.unit_id not in done and re.search(r"\d$", v.raw):
                text = units[v.unit_id].text
                swapped = v.raw[:-1] + ("0" if v.raw[-1] != "0" else "1")
                faults.append((v.unit_id, text.replace(v.raw, swapped), "PRES-006"))
                done.add(v.unit_id)

        for unit_id, mutated, rule in faults:
            seeded += 1
            issues = check_preservation(facts, doc, [_seed(doc, unit_id, mutated)])
            fired = {i.message[1:9] for i in issues if unit_id in i.unit_ids}
            if fired & set(rule.split("|")):
                caught += 1
            else:
                pytest.fail(f"{doc.source_file} {unit_id}: {rule} not caught for {mutated[:120]!r}; got {fired}")
    assert seeded > 100 and caught == seeded


def test_curated_registry_decides_what_must_be_kept():
    """A reviewer may delete a false fact: it is then neither required nor treated as invented."""
    doc = _doc("Use form 4 within 5 days.")
    facts = extract_facts(doc, "MIG-T")
    assert {v.normalized for v in facts.values} == {"4", "<=5 day"}
    curated = facts.model_copy(update={"values": [v for v in facts.values if v.normalized != "4"]})
    dropped = _draft([("Use the form within 5 days.", [UNIT])])
    kept = _draft([("Use form 4 within 5 days.", [UNIT])])
    assert check_preservation(curated, doc, [dropped]) == []
    assert check_preservation(curated, doc, [kept]) == []
    assert _rules(check_preservation(facts, doc, [dropped])) == {"PRES-001"}
