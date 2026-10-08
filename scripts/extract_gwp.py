"""Register a GWP guide in the app and extract its rules with the LLM, as the /api/v1/gwp endpoints do.

Steps: store the file as the next guide version (``data/gwp/uploads``), parse it into
source units, run ``GwpRuleExtractor`` on the configured planner model, and save the
candidate rules and the extraction report (``data/gwp/{guide_id}_v{n}_*.json``).
The rules come out as ``candidate``; approve them with ``--approve`` or later through
``POST /api/v1/gwp/{guide}/approve`` before a migration can name the guide.

The guide text is sent to the configured LLM deployment (Azure OpenAI).

Examples (from content_extraction/):
    venv/Scripts/python scripts/extract_gwp.py --file documents/GWP/BI-VQD-24416-G.pdf
    venv/Scripts/python scripts/extract_gwp.py --file documents/GWP/BI-VQD-24416-G.pdf --copy-to documents/GWP
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.services.llm.chain_factory import ChainFactory  # noqa: E402
from app.services.extraction.metadata_extractor import SOPMetadataExtractor  # noqa: E402
from app.services.migration_v2.gwp import service  # noqa: E402
from app.services.migration_v2.gwp.extractor import GwpRuleExtractor  # noqa: E402
from app.stores.gwp_store import GwpStore  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, type=Path, help="GWP guide (.docx or .pdf)")
    parser.add_argument("--name", help="Guide name (default: the file name)")
    parser.add_argument("--approve", action="store_true", help="Approve every extracted candidate rule")
    parser.add_argument("--copy-to", type=Path, help="Also copy the rules and report JSON to this folder")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO")
    settings = get_settings()
    path = args.file
    if path.suffix.lower() not in settings.gwp_allowed_extensions:
        raise SystemExit(f"Unsupported GWP file type: {path.suffix}")

    store = GwpStore(settings.project_root / "data" / "gwp_guides.db", settings)  # the store the API uses
    guide_id = SOPMetadataExtractor.sanitize_string(path.stem) or "GWP"
    version = store.get_next_version(guide_id)
    upload = settings.gwp_upload_dir / f"{guide_id}_v{version}{path.suffix.lower()}"
    upload.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, upload)
    record = store.create_record(
        guide_id=guide_id, guide_name=args.name or path.stem, file_type=path.suffix.lstrip(".").lower(),
        file_size_bytes=upload.stat().st_size, source_filename=path.name, upload_path=str(upload),
    )
    record = service.ingest(store, settings, record)
    print(f"{guide_id} v{version}: parsed, {record['total_units']} rule-input units")

    extractor = GwpRuleExtractor.from_factory(ChainFactory(settings))
    record = await service.extract(store, settings, record, extractor)
    rules = service.load_rule_set(Path(record["rules_path"]))
    report = service.load_report(Path(record["report_path"]))
    if args.approve:
        approved, _ = service.approve_rules(rules)
        record = service.save_reviewed(store, settings, record, approved)
        rules = approved

    by_category = Counter(r.category.value for r in rules.rules)
    print(f"status {record['status']}: {len(rules.rules)} rules ({', '.join(f'{k} {n}' for k, n in sorted(by_category.items()))}), "
          f"{record['candidate_rules']} candidate")
    print(f"coverage: {len(report.input_unit_ids) - len(report.uncovered_unit_ids)}/{len(report.input_unit_ids)} units cited or skipped; "
          f"{len(report.dropped)} dropped, {len(report.downgraded)} downgraded")
    print(f"rules:  {record['rules_path']}\nreport: {record['report_path']}")
    if args.copy_to:
        args.copy_to.mkdir(parents=True, exist_ok=True)
        for key in ("rules_path", "report_path"):
            dest = args.copy_to / Path(record[key]).name
            shutil.copyfile(record[key], dest)
            print(f"copied: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
