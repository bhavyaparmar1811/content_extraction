"""Inspect what the finished migration stages produce for one SOP or a folder of SOPs.

Writes a self-contained HTML report per SOP (plus the JSON artifacts) and an
index page. Everything stays on this machine. The LLM check is off unless
--llm is given (it sends a compact outline and a few passages to the
configured Azure OpenAI deployment).

Examples (from content_extraction/):
    venv/Scripts/python scripts/inspect_sop.py --sop documents/SOPs
    venv/Scripts/python scripts/inspect_sop.py --sop "documents/SOPs/BI-VQD-10505-S.docx" --llm
    venv/Scripts/python scripts/inspect_sop.py --sop new_sops/ --template "documents/Templates/Other.docx" \
        --template-config documents/template_config/Other.json --gwp-rules documents/GWP/BI-VQD-24416-G_rules.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from app.services.migration_v2.inspection.checks import overall  # noqa: E402
from app.services.migration_v2.inspection.report import render, render_index  # noqa: E402
from app.services.migration_v2.inspection.runner import inspect_sop, prepare_template  # noqa: E402

SUPPORTED = {".docx", ".pdf"}


def _default_template() -> Path:
    found = sorted((ROOT / "documents" / "Templates").glob("*.docx"))
    if len(found) != 1:
        raise SystemExit(f"Give --template: found {len(found)} templates in documents/Templates")
    return found[0]


def _default_config(template: Path) -> Path | None:
    candidates = [ROOT / "documents" / "template_config" / f"{template.stem}.json",
                  ROOT / "documents" / "template_config" / f"{template.stem.replace(' ', '_')}.json"]
    return next((c for c in candidates if c.exists()), None)


def _sops(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.suffix.lower() in SUPPORTED and not p.name.startswith("~$"))
    if path.suffix.lower() not in SUPPORTED:
        raise SystemExit(f"Unsupported file type: {path.suffix} (use .docx or .pdf)")
    return [path]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sop", required=True, type=Path, help="An SOP (.docx/.pdf) or a folder of them")
    parser.add_argument("--template", type=Path, help="Template .docx (default: the only one in documents/Templates)")
    parser.add_argument("--template-config", type=Path, help="Template config JSON (default: documents/template_config/<stem>.json)")
    parser.add_argument("--gwp-rules", type=Path, help="Approved GWP rules JSON (optional)")
    parser.add_argument("--golden-dir", type=Path, default=ROOT / "documents" / "golden")
    parser.add_argument("--llm", action="store_true", help="Run the section planner's LLM confirm call (Azure)")
    parser.add_argument("--out", type=Path, default=ROOT / "documents" / "reports")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    template_path = args.template or _default_template()
    config = args.template_config or _default_config(template_path)
    sops = _sops(args.sop)
    if not sops:
        raise SystemExit(f"No .docx or .pdf files in {args.sop}")
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Template: {template_path.name} (config: {config.name if config else 'none'})")
    template = prepare_template(template_path, config, args.out / "_work" / "_template")
    print(f"  readiness: {template.readiness.status.value}")

    rows, errors = [], []
    for sop in sops:
        print(f"Inspecting {sop.name} ...", flush=True)
        try:
            insp = await inspect_sop(sop, template, args.out, args.gwp_rules, args.golden_dir, llm=args.llm)
        except Exception as exc:  # one bad file must not stop the batch
            log = args.out / "_work" / "errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"\n=== {sop.name}\n{traceback.format_exc()}")
            errors.append((sop.name, f"{type(exc).__name__}: {exc}"))
            print(f"  ERROR: {type(exc).__name__}: {exc} (traceback in {log})")
            continue
        report = args.out / insp.doc_id / "report.html"
        report.write_text(render(insp), encoding="utf-8")
        rows.append((sop.name, f"{insp.doc_id}/report.html", insp.checks))
        print(f"  {overall(insp.checks).upper():5} " + "  ".join(f"{c.name}: {c.status}" for c in insp.checks))

    index = args.out / "index.html"
    index.write_text(render_index(rows, errors), encoding="utf-8")
    print(f"\nReports: {index}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
