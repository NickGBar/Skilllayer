"""Release build controls for the public SkillLayer distribution.

The source checkout intentionally retains research and validation helpers.  The
wheel and sdist are a separate public product boundary and therefore copy only
the reviewed runtime-module allowlist below.
"""

from __future__ import annotations

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist


# Required to import the public CLI/MCP runtime. These are not a promise that
# every workflow is safe for every repository; runtime gates and metadata make
# that distinction at execution time.
REQUIRED_RUNTIME_MODULES = frozenset({
    "__init__", "__main__", "claude_code_pricing", "cli", "cost_tracking",
    "demand_tracking", "mcp_config", "mcp_server", "security", "session_usage",
    "telemetry", "version", "diagnostics", "sanitization", "operations", "update_check", "policy",
})

# Reachable only through documented maintainer/report commands. They are public
# product tooling, not private fixtures; excluding one requires removing or
# explicitly gating its corresponding CLI command first.
PUBLIC_MAINTAINER_MODULES = frozenset({
    "ab_benchmark", "analytics", "benchmark", "benchmark_automation",
    "benchmark_environment", "bundle_review", "claude_code_prep_check",
    "cost_optimization", "cursor_mcp_validation", "failure_analysis",
    "feedback_registry", "generalization", "live_telemetry_validation",
    "open_source_audit", "packaging_audit", "pricing", "public_readiness_audit",
    "release_validation", "run", "run_tests_real_repo_smoke", "security_check",
    "telemetry_export", "tester_check", "token_savings_benchmark", "workflow_repair",
})

PUBLIC_RUNTIME_MODULES = REQUIRED_RUNTIME_MODULES | PUBLIC_MAINTAINER_MODULES
PUBLIC_PACKAGE_PREFIXES = ("src/skilllayer/config/", "src/skilllayer/llm/", "src/skilllayer/macros/", "src/skilllayer/memory/", "src/skilllayer/router/", "src/skilllayer/runner/", "src/skilllayer/tools/", "src/skilllayer/verifier/", "src/skilllayer/workflows/")


def is_public_release_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized in {"LICENSE", "README.md", "pyproject.toml", "setup.py", "MANIFEST.in"}:
        return True
    if normalized.startswith(PUBLIC_PACKAGE_PREFIXES):
        return True
    if normalized.startswith("src/skilllayer/") and normalized.count("/") == 2 and normalized.endswith(".py"):
        return Path(normalized).stem in PUBLIC_RUNTIME_MODULES
    return False


class PublicBuildPy(_build_py):
    def find_package_modules(self, package: str, package_dir: str):
        modules = super().find_package_modules(package, package_dir)
        if package != "skilllayer":
            return modules
        return [module for module in modules if Path(module[1]).stem in PUBLIC_RUNTIME_MODULES]


class PublicSDist(_sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, [file for file in files if is_public_release_file(file)])


if __name__ == "__main__":
    setup(cmdclass={"build_py": PublicBuildPy, "sdist": PublicSDist})
