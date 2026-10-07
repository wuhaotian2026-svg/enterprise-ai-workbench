from __future__ import annotations

import ast
from pathlib import Path


APPROVALS_SOURCE = (
    Path(__file__).resolve().parents[3] / "src" / "policy_api" / "approvals"
)


def approval_source_paths() -> list[Path]:
    return [
        path
        for path in sorted(APPROVALS_SOURCE.rglob("*.py"))
        if not any(part.startswith("backup_") for part in path.parts)
    ]


def test_approvals_package_does_not_import_hr_or_procurement_domains() -> None:
    forbidden_imports = {"policy_api.hr", "policy_api.procurement"}
    violations: list[str] = []

    for source_path in approval_source_paths():
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        for forbidden_import in forbidden_imports:
            if forbidden_import in source:
                violations.append(f"{source_path.name}:text:{forbidden_import}")
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for module_name in imported:
                if any(
                    module_name == forbidden or module_name.startswith(f"{forbidden}.")
                    for forbidden in forbidden_imports
                ):
                    violations.append(f"{source_path.name}:{node.lineno}:{module_name}")

    assert violations == []


def test_approvals_package_does_not_name_domain_models() -> None:
    forbidden_names = {"LeaveRequest", "ProcurementRequest"}
    violations: list[str] = []

    for source_path in approval_source_paths():
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        for forbidden_name in forbidden_names:
            if forbidden_name in source:
                violations.append(f"{source_path.name}:text:{forbidden_name}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{source_path.name}:{node.lineno}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                violations.append(f"{source_path.name}:{node.lineno}:{node.attr}")

    assert violations == []
