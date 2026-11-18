"""Translation review sheet for a native speaker, and applying their corrections.

  python -m scripts.i18n_review sheet            writes docs/translation-review.md
  python -m scripts.i18n_review apply <file.md>  applies the Fix column of a filled-in copy
  python -m scripts.i18n_review check            placeholders and numbers match in every language

In the Fix column a reviewer writes the corrected text, prefixed with its language, for example
`hi: नया वाक्य` or `mr: नवे वाक्य` (both separated by ` || `). A correction is refused if it
adds or drops a {placeholder} or changes a number, so a fix can never break a page.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "app" / "i18n"
SHEET = ROOT / "docs" / "translation-review.md"
LANGS = ("en", "hi", "mr")

# Screen order for the sheet: (title, key prefixes).
SCREENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Home page", ("app", "home", "contact", "lang", "label")),
    ("Start page", ("start", "stt", "upload", "wait")),
    ("Confirmation cards", ("confirm", "intake", "done", "already", "phone")),
    ("Owner page", ("owner", "publish", "details", "gallery", "photos", "ratelimit")),
    ("Report", ("score", "fix")),
    ("Agent panel", ("agent",)),
    ("Web address", ("domains",)),
    ("Delete my site", ("delete",)),
    ("Privacy page", ("privacy",)),
    ("Business site (customers see this)", ("site", "cta")),
    ("Errors and email", ("error", "mail")),
)

PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def load() -> dict[str, dict[str, str]]:
    """All three language files."""
    return {lang: json.loads((I18N / f"{lang}.json").read_text("utf-8")) for lang in LANGS}


def save(data: dict[str, dict[str, str]]) -> None:
    """Write the language files back in the same format."""
    for lang in LANGS:
        text = json.dumps(data[lang], ensure_ascii=False, indent=2) + "\n"
        (I18N / f"{lang}.json").write_text(text, encoding="utf-8")


def placeholders(text: str) -> list[str]:
    """The {placeholders} in a string, sorted."""
    return sorted(PLACEHOLDER.findall(text))


def numbers(text: str) -> list[str]:
    """The numbers in a string (Devanagari digits read as ASCII), sorted."""
    return sorted(NUMBER.findall(PLACEHOLDER.sub("", text.translate(DEVANAGARI_DIGITS))))


def problem(key: str, english: str, text: str) -> str | None:
    """Why a translation does not fit the English, or None."""
    if placeholders(text) != placeholders(english):
        return f"{key}: placeholders {placeholders(text)} differ from {placeholders(english)}"
    if numbers(text) != numbers(english):
        return f"{key}: numbers {numbers(text)} differ from {numbers(english)}"
    return None


def problems(data: dict[str, dict[str, str]]) -> list[str]:
    """Every translation whose placeholders or numbers differ from the English."""
    found = []
    for lang in ("hi", "mr"):
        for key, english in data["en"].items():
            issue = problem(key, english, data[lang].get(key, ""))
            if issue:
                found.append(f"{lang} {issue}")
    return found


def ordered_keys(keys: list[str]) -> list[tuple[str, list[str]]]:
    """Keys grouped by screen, in file order within each screen."""
    groups: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for title, prefixes in SCREENS:
        chosen = [k for k in keys if k.split(".")[0] in prefixes]
        groups.append((title, chosen))
        seen.update(chosen)
    rest = [k for k in keys if k not in seen]
    if rest:
        groups.append(("Other", rest))
    return groups


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def sheet(data: dict[str, dict[str, str]], human_read: list[str]) -> str:
    """The review sheet as Markdown."""
    lines = [
        "# Translation review",
        "",
        "For a native Hindi and Marathi speaker. Read each row; if a string sounds wrong,",
        "write the corrected text in the Fix column, starting with its language, for example",
        "`hi: ...` or",
        "`mr: ...` (both: `hi: ... || mr: ...`). Keep every `{placeholder}` and number exactly as",
        "it is. Then run `make i18n-apply file=<your copy>`.",
        "",
        "Style: friendly and respectful spoken language (आप), short sentences, everyday words.",
        'The owner is addressed as "you" with no gendered verb form.',
        "",
    ]
    if human_read:
        lines += [
            "## Needs a human read",
            "",
            "These privacy strings were reworded. Their meaning must stay identical (what is",
            "stored, how long, and the right to delete). Please read them first.",
            "",
            *[f"- `{key}`" for key in human_read],
            "",
        ]
    for title, keys in ordered_keys(list(data["en"])):
        if not keys:
            continue
        lines += [
            f"## {title}",
            "",
            "| Key | English | Hindi | Marathi | Fix |",
            "| --- | --- | --- | --- | --- |",
        ]
        for key in keys:
            row = [f"`{key}`", *(_cell(data[lang][key]) for lang in LANGS), ""]
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    return "\n".join(lines)


def corrections(markdown: str) -> list[tuple[str, str, str]]:
    """(key, language, text) from the Fix column of a filled-in sheet."""
    found = []
    for line in markdown.splitlines():
        row = line.strip()
        if not (row.startswith("| `") and row.endswith("|")):
            continue
        # The Fix column is last and may hold "hi: ... || mr: ...", so split at most 4 times.
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", row[1:-1], maxsplit=4)]
        if len(cells) != 5 or not cells[0].startswith("`"):
            continue
        key, fix = cells[0].strip("`"), cells[4].replace("\\|", "|")
        for part in (p.strip() for p in fix.split("||") if p.strip()):
            match = re.match(r"^(hi|mr|en)\s*:\s*(.+)$", part, re.S)
            if match:
                found.append((key, match.group(1), match.group(2).strip()))
    return found


def apply(data: dict[str, dict[str, str]], fixes: list[tuple[str, str, str]]) -> list[str]:
    """Apply fixes in place. Returns refused fixes (unknown key, or placeholders/numbers)."""
    refused = []
    for key, lang, text in fixes:
        if key not in data["en"]:
            refused.append(f"{key}: no such key")
            continue
        issue = problem(key, data["en"][key], text) if lang != "en" else None
        if issue:
            refused.append(issue)
            continue
        data[lang][key] = text
    return refused


HUMAN_READ_FILE = ROOT / "docs" / ".translation-human-read.json"


def main(argv: list[str]) -> int:
    """Command line entry."""
    command = argv[0] if argv else ""
    data = load()
    if command == "check":
        found = problems(data)
        print("\n".join(found) or "All placeholders and numbers match.")
        return 1 if found else 0
    if command == "sheet":
        human = json.loads(HUMAN_READ_FILE.read_text()) if HUMAN_READ_FILE.is_file() else []
        SHEET.write_text(sheet(data, human), encoding="utf-8")
        print(f"Wrote {SHEET.relative_to(ROOT)} ({len(data['en'])} keys)")
        return 0
    if command == "apply" and len(argv) == 2:
        fixes = corrections(Path(argv[1]).read_text(encoding="utf-8"))
        refused = apply(data, fixes)
        save(data)
        print(f"Applied {len(fixes) - len(refused)} corrections.")
        for line in refused:
            print(f"Refused: {line}")
        return 1 if refused else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
