"""Rerun extraction on every reviewed eval case and report accuracy: make eval.

Includes evals/private/ (git-ignored real cases) when it exists. The report is printed and
saved to evals/reports/ (git-ignored, because it can name private cases).
"""

import sys
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.db import get_engine
from app.services.evals import ROOT, render_report, run_evals


def main() -> int:
    """Run the evals and write the report."""
    settings = get_settings()
    report = run_evals(get_engine(), settings)
    text = render_report(report)
    mode = "mock" if settings.claude_mock else "real Claude"
    text = text.replace("# Eval report", f"# Eval report ({mode})", 1)
    folder = ROOT / "reports"
    folder.mkdir(exist_ok=True)
    path = folder / f"eval-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"Saved: {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
