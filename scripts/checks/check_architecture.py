"""Static import boundary check for application packages (not a Python sandbox)."""

import argparse
import ast
import sys
from pathlib import Path

PACKAGES = (
    ("backend/src", "enterprise_ai_platform"),
    ("reference_service/src", "reimbursement_service"),
)
ROOT_FILES = {"main", "worker", "composition", "database_check", "__main__"}
PUBLIC = {"service", "ports"}
UNSAFE_NAMES = {"__import__", "__builtins__", "eval", "exec"}
DATABASE_STDLIB = {"sqlite3", "_sqlite3", "dbm", "shelve"}


def imports(node: ast.AST, module: tuple[str, ...], is_package: bool):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            package = module if is_package else module[:-1]
            if node.level > len(package):
                return ["<relative-outside-package>"]
            prefix = package[:len(package) - node.level + 1]
            base = (*prefix, *((node.module or "").split(".") if node.module else ()))
        else:
            base = tuple((node.module or "").split("."))
        return [".".join((*base, alias.name)) for alias in node.names]
    return []


def violation(source: tuple[str, ...], target: str, package: str) -> str | None:
    parts = target.split(".")
    head = parts[0]
    if head in {"builtins", "importlib"}:
        return "dynamic import machinery is not allowed in application source"
    if "*" in parts or head == "<relative-outside-package>":
        return "explicit imports inside the package boundary are required"
    if head in {name for _, name in PACKAGES} and head != package:
        return "application packages communicate through API contracts, not source imports"
    layer = source[1] if len(source) > 1 else "__init__"
    if head in DATABASE_STDLIB and layer in {"api", "modules", "config"}:
        return "database drivers belong in adapters, including standard-library drivers"
    if len(source) == 2 and layer == "config":
        return None if head in sys.stdlib_module_names or head in {"pydantic", "pydantic_settings"} else "config must remain independent of application wiring and adapters"
    if len(source) == 2 and layer in ROOT_FILES:
        return None
    if len(source) == 1:
        return None if head in sys.stdlib_module_names else "package initializer must remain independent"
    if layer == "modules":
        if head != package:
            return None if head in sys.stdlib_module_names else "application/domain modules use stdlib and ports only"
        if len(parts) < 4 or parts[1] != "modules":
            return "application modules cannot import transport, config, root wiring or adapters"
        own = len(source) >= 3 and parts[2] == source[2]
        source_kind = source[3] if len(source) >= 4 else "__init__"
        target_kind = parts[3]
        if source_kind == "domain":
            allowed = own and target_kind == "domain"
        elif source_kind == "ports":
            allowed = own and target_kind in {"domain", "ports"}
        else:
            allowed = (own and target_kind in {"domain", "ports", "service"}) or (
                not own and target_kind in PUBLIC
            )
        return None if allowed else "use the module's public service/port, not private internals"
    if layer == "api":
        if head != package:
            return None if head in sys.stdlib_module_names or head in {"fastapi", "starlette", "pydantic"} else "transport cannot import provider/DB SDKs"
        allowed = len(parts) >= 2 and (
            parts[1] == "api" or (
                len(parts) >= 4 and parts[1] == "modules" and parts[3] in PUBLIC
            )
        )
        return None if allowed else "transport depends on services/ports, not composition or adapters"
    if layer in {"infrastructure", "integrations"}:
        if head != package:
            return None
        allowed = len(parts) >= 2 and (
            parts[1] in {"infrastructure", "integrations"} or (
                len(parts) >= 4 and parts[1] == "modules" and parts[3] in {"domain", "ports"}
            )
        )
        return None if allowed else "adapters cannot depend on transport/root wiring/application services"
    return "unclassified source layer; define its dependency rule before using it"


def check(root: Path) -> list[str]:
    errors = []
    count = 0
    for location, package in PACKAGES:
        base = root / location / package
        for file in sorted(base.rglob("*.py")):
            count += 1
            relative = file.relative_to(root)
            path = file.relative_to(base).with_suffix("").parts
            is_package = path[-1] == "__init__"
            module = (package, *(path[:-1] if is_package else path))
            try:
                tree = ast.parse(file.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeError):
                errors.append(f"{relative}: invalid Python source")
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Name) and node.id in UNSAFE_NAMES) or (
                    isinstance(node, ast.Attribute) and node.attr in {"__import__", "import_module"}
                ):
                    errors.append(f"{relative}:{node.lineno}: dynamic execution/import is forbidden")
                for target in imports(node, module, is_package):
                    reason = violation(module, target, package)
                    if reason:
                        errors.append(f"{relative}:{node.lineno}: {target}: {reason}")
    if count == 0:
        errors.append("No Python application sources found.")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    errors = check(args.root.resolve())
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print("Architecture checks passed.")


if __name__ == "__main__":
    main()
