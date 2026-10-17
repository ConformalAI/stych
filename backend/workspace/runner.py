from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


# Imports that grant filesystem / process / network escape are blocked outright.
# Everything else — including any third-party data-science library (scipy, sklearn,
# plotly, statsmodels, …) — is allowed. File I/O within scripts goes through the
# workspace-confined ``open`` injected below.
FORBIDDEN_IMPORTS = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "shutil",
    "pathlib",
    "requests",
    "httpx",
    "urllib",
    "ctypes",
    "multiprocessing",
}

FORBIDDEN_CALLS = {"eval", "exec", "compile", "input"}

DEFAULT_ERROR_PREVIEW_HEAD = 200
DEFAULT_ERROR_PREVIEW_TAIL = 200


def head_tail_preview(
    text: str,
    head: int = DEFAULT_ERROR_PREVIEW_HEAD,
    tail: int = DEFAULT_ERROR_PREVIEW_TAIL,
) -> str:
    """Keep the start and end of a sandboxed error, not just the start.

    A Python traceback's last line carries the actual exception type/message —
    a head-only truncation discards exactly the part needed to diagnose the
    failure, keeping only outer stack frames. Callers that need a different
    budget (e.g. a larger allowance for ``inspect_data``-style ad-hoc output)
    can pass their own ``head``/``tail``.
    """
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n… [{omitted:,} chars omitted] …\n{text[-tail:]}"


FORBIDDEN_ATTR_PREFIXES = {
    "os.",
    "sys.",
    "subprocess.",
    "socket.",
    "shutil.",
    "pathlib.",
    "requests.",
    "httpx.",
    "urllib.",
}


def _scrub_surrogates(text: str) -> str:
    """Normalise surrogate code points so text is UTF-8 / compile safe.

    Surrogates enter from two independent sources: LLM transport decoding (the
    authored source) and data files read back with ``surrogateescape``. CPython
    must encode source to UTF-8 to compile it, so a lone surrogate makes
    ``exec``/``compile`` raise ``UnicodeEncodeError: surrogates not allowed``
    before a single line runs. First recombine adjacent surrogate *pairs* into
    their astral character (recovering e.g. an emoji that arrived as two separate
    code points), then replace any remaining *lone* surrogate. Applied
    symmetrically: to code on the way in and to captured text on the way out.
    """
    text = text.encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")
    return text.encode("utf-8", "replace").decode("utf-8")


# Sandbox resource limits (overridable via config workspace.sandbox).
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_CPU_SECONDS = 30
DEFAULT_FSIZE_MB = 50
DEFAULT_AS_GB = 3


class WorkspaceSandboxRunner:
    """Run arbitrary Python code inside a workspace with restricted IO.

    The subprocess keeps the full builtin set (so ``import`` works for any library)
    except a small dangerous blocklist, runs under ``python -I`` with rlimits and
    network denial, and confines ``open()`` to the workspace root.
    """

    def __init__(
        self,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        *,
        cpu_seconds: int = DEFAULT_CPU_SECONDS,
        fsize_mb: int = DEFAULT_FSIZE_MB,
        address_space_gb: int = DEFAULT_AS_GB,
    ):
        self.timeout_seconds = timeout_seconds
        self.cpu_seconds = cpu_seconds
        self.fsize_mb = fsize_mb
        self.address_space_gb = address_space_gb

    def _validate_code(self, code: str) -> None:
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(f"Generated code is not valid Python: {exc}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in FORBIDDEN_IMPORTS:
                        raise ValueError(f"Import '{alias.name}' is not allowed in sandbox.")
            if isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".")[0]
                if module and module in FORBIDDEN_IMPORTS:
                    raise ValueError(f"Import from '{node.module}' is not allowed in sandbox.")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_CALLS:
                    raise ValueError(f"Call '{node.func.id}(...)' is not allowed in sandbox.")
            if isinstance(node, ast.Attribute):
                attr_chain = self._attribute_to_str(node)
                if any(attr_chain.startswith(prefix) for prefix in FORBIDDEN_ATTR_PREFIXES):
                    raise ValueError(f"Attribute access '{attr_chain}' is not allowed in sandbox.")

    def _attribute_to_str(self, node: ast.Attribute) -> str:
        parts = []
        current: Any = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))

    def _build_wrapper_script(self, user_code: str, workspace: Path) -> str:
        # ensure_ascii=False so astral chars (e.g. emoji) are written as real UTF-8
        # in the wrapper source. With the default escaping, json emits a "\ud83d
        # \udcca" surrogate-pair escape that Python's source tokenizer compiles to
        # two *separate* surrogate code points (unlike json.loads, it does not
        # recombine them), so exec(user_code) would then fail. The run()-level
        # scrub guarantees no lone surrogate remains to break this UTF-8 file write.
        user_code_literal = json.dumps(user_code, ensure_ascii=False)
        workspace_literal = json.dumps(str(workspace))
        cpu = self.cpu_seconds
        fsize = self.fsize_mb * 1024 * 1024
        addr = self.address_space_gb * 1024 * 1024 * 1024
        return f"""import io
import json
import traceback
import contextlib
import builtins
import socket
from pathlib import Path

try:
    import resource
except Exception:
    resource = None

if resource:
    try:
        resource.setrlimit(resource.RLIMIT_CPU, ({cpu}, {cpu}))
        resource.setrlimit(resource.RLIMIT_FSIZE, ({fsize}, {fsize}))
        resource.setrlimit(resource.RLIMIT_AS, ({addr}, {addr}))
    except Exception:
        pass

class _DeniedSocket:
    # A class, not a function: stdlib modules subclass socket.socket at
    # import time (ssl.py: "class SSLSocket(socket): ..."), and a function
    # is not a valid base class. Swapping in a plain function here breaks
    # ssl's own import with a cryptic TypeError, which then breaks anything
    # sandboxed code imports that transitively needs ssl (e.g. pyarrow's
    # filesystem module, even for purely local parquet I/O). Blocking still
    # happens — just at construction time instead of at "is this callable".
    def __init__(self, *args, **kwargs):
        raise RuntimeError("Network access is disabled in sandbox execution")

def _deny_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled in sandbox execution")

socket.socket = _DeniedSocket
socket.create_connection = _deny_network

workspace_root = Path({workspace_literal}).resolve()
user_code = {user_code_literal}

def _safe_open(path, *args, **kwargs):
    p = Path(path).resolve()
    if not str(p).startswith(str(workspace_root)):
        raise RuntimeError("File access outside workspace is not allowed")
    return builtins.open(p, *args, **kwargs)

_BLOCKED_BUILTINS = {{
    "eval", "exec", "compile", "input", "breakpoint",
    "__loader__", "__spec__", "__package__",
}}
safe_builtins = {{k: v for k, v in vars(builtins).items() if k not in _BLOCKED_BUILTINS}}
safe_builtins["open"] = _safe_open

result_payload = {{
    "success": False,
    "stdout": "",
    "error": None,
}}

def _surrogate_safe(text):
    # Same policy as the parent-side scrub: recombine surrogate pairs (so a split
    # emoji survives), then replace any lone surrogate that would break the UTF-8
    # JSON artifact round-trip.
    text = text.encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")
    return text.encode("utf-8", "replace").decode("utf-8")

try:
    stdout_capture = io.StringIO()
    # Single namespace = true module-level semantics. Top-level defs, vars and
    # dicts must be visible to functions, lambdas and comprehensions defined in
    # the same script. Passing separate globals/locals dicts makes exec run with
    # class-body scoping, so any name referenced inside df.apply(fn) or a
    # comprehension raises NameError on names defined at the top of the script.
    module_ns = {{"__builtins__": safe_builtins, "__name__": "__main__"}}
    with contextlib.redirect_stdout(stdout_capture):
        exec(user_code, module_ns)
    result_payload["success"] = True
    result_payload["stdout"] = stdout_capture.getvalue()
except Exception:
    result_payload["error"] = traceback.format_exc()

result_payload["stdout"] = _surrogate_safe(result_payload["stdout"])
if result_payload["error"]:
    result_payload["error"] = _surrogate_safe(result_payload["error"])

artifact_path = workspace_root / "run_artifact.json"
with builtins.open(artifact_path, "w", encoding="utf-8") as f:
    json.dump(result_payload, f)
"""

    def run(self, workspace: Path, code: str) -> Dict[str, Any]:
        # Single chokepoint: normalise surrogates before anything compiles the
        # source. Both _validate_code (ast.parse) and _build_wrapper_script (exec)
        # encode the text to UTF-8 and would otherwise raise on lone surrogates.
        code = _scrub_surrogates(code)
        self._validate_code(code)
        workspace.mkdir(parents=True, exist_ok=True)
        wrapper_path = workspace / "sandbox_exec.py"
        wrapper_path.write_text(self._build_wrapper_script(code, workspace), encoding="utf-8")

        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(wrapper_path)],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                cwd=str(workspace),
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "success": False,
                "error": f"Sandbox execution timed out after {self.timeout_seconds}s.",
                "stderr": str(exc),
            }

        artifact_path = workspace / "run_artifact.json"
        if not artifact_path.exists():
            return {
                "success": False,
                "error": "Sandbox did not produce an artifact output.",
                "stderr": completed.stderr,
            }

        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "Sandbox artifact could not be parsed as JSON.",
                "stderr": completed.stderr,
            }

        artifact["stderr"] = completed.stderr
        artifact["files"] = self.list_files(workspace)
        return artifact

    # Internal scratch files the agent should not see in listings.
    _HIDDEN_FILES = {"sandbox_exec.py", "run_artifact.json"}

    @classmethod
    def list_files(cls, root: Path) -> list[str]:
        return sorted(
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file() and p.name not in cls._HIDDEN_FILES
        )

    # Back-compat: previously a private staticmethod.
    _list_files = list_files
