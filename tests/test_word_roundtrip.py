"""A normalized template must survive being opened and saved by Microsoft Word (Phase 3).

People edit templates in Word, so the content controls the normalizer writes
must come back unchanged. Word rewrites some structures on save; for example
it splits a content control spanning several table rows into a separate table.

Opt-in: runs only with ``SOP_RUN_WORD_TESTS=1`` on Windows with Word installed.
Uses ``scripts/word_resave.ps1`` (hidden, read-only, no repair).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.schemas.v2 import ReadinessStatus
from app.services.migration_v2.template.overrides import TemplateConfig, load_config
from app.services.migration_v2.template.service import build_template_model, normalize_and_save

from test_v2_template_slots import _decide_all, _template

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "word_resave.ps1"
SAMPLE = ROOT / "documents" / "Templates" / "Template Main GP Docs.docx"
SAMPLE_CONFIG = ROOT / "documents" / "template_config" / "Template_Main_GP_Docs.json"

pytestmark = [
    pytest.mark.word,
    pytest.mark.skipif(os.environ.get("SOP_RUN_WORD_TESTS") != "1", reason="set SOP_RUN_WORD_TESTS=1 to run"),
    pytest.mark.skipif(sys.platform != "win32" or shutil.which("powershell") is None, reason="needs Windows"),
]


def _word_resave(source: Path, target: Path) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-Source", str(source), "-Target", str(target)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0 and target.exists(), result.stdout + result.stderr
    return result.stdout.strip()


def _signature(model):
    slots = [(s.slot_id, s.anchor.kind, s.anchor.ref, s.anchor.table_index, s.anchor.row_index, s.instruction)
             for s in model.iter_slots()]
    regions = [(r.region_id, r.anchor.ref) for sec in model.sections for r in sec.conditional_regions]
    return slots, regions


def _check_round_trip(source: Path, config: TemplateConfig, out: Path) -> None:
    normalized = normalize_and_save(source, "tpl", 1, out, config)
    assert normalized.build.report.status == ReadinessStatus.READY
    resaved = out / "word_resaved.docx"
    stats = _word_resave(normalized.normalized_path, resaved)
    after = build_template_model(resaved, "tpl", 1, config)
    assert after.report.status == ReadinessStatus.READY, (stats, after.report.blocking)
    assert _signature(after.model) == _signature(normalized.build.model)


def test_synthetic_template_survives_word(tmp_path):
    source = _template(tmp_path / "template.docx", tmp_path)
    config = _decide_all(build_template_model(source, "tpl").detection,
                         {"inline_instruction": "conditional", "instruction_table": "conditional"})
    _check_round_trip(source, config, tmp_path / "out")


@pytest.mark.skipif(not (SAMPLE.exists() and SAMPLE_CONFIG.exists()), reason="sample template not available")
def test_sample_template_survives_word(tmp_path):
    _check_round_trip(SAMPLE, load_config(SAMPLE_CONFIG), tmp_path / "out")
