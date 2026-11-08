"""Check published documentation against the Git index without network access."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]
REQUIRED = (
    "README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
    "RESOURCE_SAFETY_POLICY.md", "docs/README.md", "docs/GETTING_STARTED.md",
    "docs/portfolio/CASE_STUDY.md", "docs/portfolio/APPLICATION_PACK.md",
    "docs/product-tour/README.md",
)


class HTMLLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"href", "src"} and value:
                self.links.append(value)


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True,
    )
    tracked = set(result.stdout.decode("utf-8").split("\0")) - {""}
    failures: list[str] = []
    for required in REQUIRED:
        if required not in tracked:
            failures.append(f"Required published document missing: {required}")

    for name in sorted(tracked):
        if not name.lower().endswith(".md"):
            continue
        path = ROOT / name
        if not path.is_file():
            failures.append(f"Indexed document missing from checkout: {name}")
            continue
        content = path.read_text(encoding="utf-8-sig")
        # Code examples may contain deliberately non-existent placeholder paths.
        content = re.sub(r"^```[^\n]*\n.*?^```[^\n]*$", "", content, flags=re.M | re.S)
        links = re.findall(r"\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)", content)
        links += re.findall(r"^\s*\[[^]]+\]:\s*(\S+)", content, flags=re.M)
        parser = HTMLLinks()
        parser.feed(content)
        links += parser.links
        for value in links:
            value = value.strip("<>")
            target = urlsplit(value)
            if target.scheme or target.netloc or not target.path:
                continue
            if re.match(r"^[A-Za-z]:", target.path) or target.path.startswith("/"):
                failures.append(f"Machine-specific link in {name}")
                continue
            destination = (path.parent / unquote(target.path)).resolve()
            try:
                relative = destination.relative_to(ROOT).as_posix()
            except ValueError:
                failures.append(f"Link leaves repository in {name}: {value}")
                continue
            prefix = str(PurePosixPath(relative)).rstrip("/") + "/"
            if relative not in tracked and not any(item.startswith(prefix) for item in tracked):
                failures.append(f"Unpublished link target in {name}: {value}")
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("Published documentation links and required entry points are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
