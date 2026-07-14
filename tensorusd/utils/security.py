"""
ltensorusd/utils/security.py --> Agent file security validation.

Validates that a submitted file is:
  1. A valid UTF-8 text file (not a binary / compiled blob).
  2. Parseable as Python source (AST parse must succeed).
  3. Free of dangerous constructs detected via AST walk (cannot be evaded
     by string encoding, unicode lookalikes, or dynamic attribute tricks).
  4. Free of suspiciously large encoded string literals.
  5. Structurally valid: must define an AgentMiner class with an infer()
     method, a main() function, and main() must call agent.infer()
"""

from __future__ import annotations

import ast
import io
import re
import tokenize

from tensorusd.utils.logging import get_logger

log = get_logger(__name__)


# Tuneable limits

MAX_FILE_SIZE: int = 5 * 1024 * 1024  # 5 MB max limit for agent file
MAX_STRING_LITERAL_LEN: int = 50_000  # characters; encoded payloads are often huge

# Forbidden imports
_FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {
        # Process execution
        "subprocess",
        "multiprocessing",
        "concurrent.futures",  # ProcessPoolExecutor can spawn subprocesses
        # Low-level OS / FFI
        "ctypes",
        "cffi",
        # Networking primitives
        "socket",
        "ssl",
        "asyncio",  # can open raw network connections
        "aiohttp",
        "httpx",
        # Direct OpenAI SDK access is not allowed; miners must use our wrapper.
        "openai",
        "paramiko",  # SSH
        "fabric",
        "scapy",  # packet crafting
        # Arbitrary-code-execution on load
        "pickle",
        "shelve",
        "marshal",
        "dill",
        "cloudpickle",
        # Pseudo-terminal / tty
        "pty",
        "tty",
        "termios",
        # Dynamic code loading
        "importlib.util",  # importlib itself is ok; .util lets you load arbitrary paths
        "zipimport",
        "pkgutil",
    }
)

# Forbidden call patterns

_FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {
        # Built-in code execution
        "eval",
        "exec",
        "compile",
        "__import__",
        # OS-level execution
        "os.system",
        "os.popen",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execlpe",
        "os.fork",
        "os.forkpty",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
        "os.startfile",
        # subprocess module
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        # ctypes
        "ctypes.CDLL",
        "ctypes.cdll",
        "ctypes.WinDLL",
        "ctypes.windll",
        "ctypes.OleDLL",
        "ctypes.oledll",
        # socket
        "socket.socket",
        "socket.create_connection",
        "socket.create_server",
    }
)

# Attribute names that are dangerous when reached via getattr() dynamically.
_FORBIDDEN_GETATTR_ATTRS: frozenset[str] = frozenset(
    {
        "exec",
        "eval",
        "system",
        "__import__",
        "popen",
        "Popen",
        "fork",
        "spawn",
    }
)

# Suspicious string pattern — long base64-like blobs suggest an encoded payload
_BASE64_BLOB_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


# Public API
def validate_agent_file(agent_bytes: bytes) -> str | None:
    """
    Full security validation of a submitted agent file.
    """

    # Size guard
    if len(agent_bytes) > MAX_FILE_SIZE:
        return (
            f"File too large: {len(agent_bytes):,} bytes (max {MAX_FILE_SIZE // (1024 * 1024)} MB)"
        )

    # Must be valid UTF-8 text
    try:
        source = agent_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return (
            "File is not valid UTF-8 text. "
            "Only pure Python source files (.py) are accepted — "
            "compiled, binary, or non-UTF-8 files are rejected."
        )

    # Null byte check
    if "\x00" in source:
        return "File contains null bytes — not a valid Python source file."

    # Python magic / shebang that reveals non-Python content
    if agent_bytes[:4] in _KNOWN_BINARY_MAGIC:
        return "File begins with a known binary magic number — not a Python source file."

    # Must parse as valid Python
    try:
        tree = ast.parse(source, filename="agent.py")
    except SyntaxError as exc:
        return f"File is not valid Python syntax: {exc}"
    except ValueError as exc:
        return f"File could not be parsed: {exc}"

    # AST security scan
    ast_reason = _ast_security_scan(tree)
    if ast_reason:
        return ast_reason

    # Token-level scan for encoded / obfuscated payloads
    token_reason = _token_scan(source)
    if token_reason:
        return token_reason

    log.debug("Agent file passed all security checks (%d bytes).", len(agent_bytes))
    return None


def validate_agent_format(agent_bytes: bytes) -> str | None:
    """
    Structural / contract validation of a submitted agent file.
    """
    try:
        source = agent_bytes.decode("utf-8")
        tree = ast.parse(source, filename="agent.py")
    except Exception as exc:
        # Should not happen if validate_agent_file passed, but be defensive.
        return f"Format check: could not parse file: {exc}"

    reason = _format_check(tree)
    if reason:
        log.warning("Agent format check failed: %s", reason)
    else:
        log.debug("Agent file passed format check.")
    return reason



# Common binary file magic numbers (first 4 bytes).
_KNOWN_BINARY_MAGIC: frozenset[bytes] = frozenset(
    {
        b"\xc3\r\r\n",
        b"\x55\xaa\x00\x00",
        b"\x7fELF",
        b"MZ\x90\x00",
        b"PK\x03\x04",
        b"\x1f\x8b\x08",
        b"BZh",
        b"\xfd7zXZ",
        b"\x89PNG",
        b"\xff\xd8\xff",
        b"%PDF",
    }
)


def _ast_security_scan(tree: ast.AST) -> str | None:
    """
    Walk every node in the AST and look for forbidden patterns.
    """
    for node in ast.walk(tree):
        # ── import foo / import foo.bar ───────────────────────────────────
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                full = alias.name
                if root in _FORBIDDEN_IMPORTS or full in _FORBIDDEN_IMPORTS:
                    return f"Forbidden import: '{alias.name}'"

        # ── from foo import bar ───────────────────────────────────────────
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            full = node.module
            if root in _FORBIDDEN_IMPORTS or full in _FORBIDDEN_IMPORTS:
                return f"Forbidden import from: '{node.module}'"
            # Special case: "from importlib import util" is forbidden
            # even though plain "import importlib" is allowed.
            if node.module == "importlib":
                for alias in node.names:
                    if alias.name == "util":
                        return "Forbidden import: 'importlib.util'"

        # function calls
        if isinstance(node, ast.Call):
            call_name = _dotted_call_name(node.func)

            # Direct forbidden call (eval, exec, os.system, …)
            if call_name in _FORBIDDEN_CALLS:
                return f"Forbidden function call: '{call_name}'"

            # getattr(obj, "exec") — dynamic dispatch to dangerous attr
            if call_name == "getattr" and len(node.args) >= 2:
                attr_arg = node.args[1]
                if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):  # noqa: SIM102
                    if attr_arg.value in _FORBIDDEN_GETATTR_ATTRS:
                        return f"Forbidden dynamic attribute access via getattr: '{attr_arg.value}'"

            # __builtins__['exec'] / __builtins__.__dict__['exec']
            if isinstance(node.func, ast.Subscript):  # noqa: SIM102
                if isinstance(node.func.slice, ast.Constant):  # noqa: SIM102
                    if node.func.slice.value in _FORBIDDEN_CALLS:
                        return (
                            f"Forbidden subscript access to dangerous builtin: "
                            f"'{node.func.slice.value}'"
                        )

        # Catches: __builtins__.exec, __builtins__.__dict__
        if isinstance(node, ast.Attribute) and (
            isinstance(node.value, ast.Name)
            and node.value.id == "__builtins__"
            and node.attr in _FORBIDDEN_GETATTR_ATTRS
        ):
            return f"Forbidden access to __builtins__.{node.attr}"

    return None


def _dotted_call_name(func_node: ast.expr) -> str:
    """
    Extract a dotted name string from a function-call's func node.
    """
    parts: list[str] = []
    node: ast.expr = func_node
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _token_scan(source: str) -> str | None:
    """
    Tokenize the source and inspect string literals for suspicious content.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError as exc:
        # Malformed source that somehow passed ast.parse — be conservative.
        return f"File failed tokenization: {exc}"

    for tok_type, tok_val, tok_start, _, _ in tokens:
        if tok_type != tokenize.STRING:
            continue

        # Strip quotes to get the raw string content for length check
        try:
            # ast.literal_eval safely evaluates the string token
            raw_value = ast.literal_eval(tok_val)
        except Exception:
            raw_value = tok_val  # fall back to token text

        content = raw_value if isinstance(raw_value, str) else tok_val

        if len(content) > MAX_STRING_LITERAL_LEN:
            return (
                f"Suspiciously large string literal at line {tok_start[0]} "
                f"({len(content):,} chars). "
                f"Possible encoded payload. Max allowed: {MAX_STRING_LITERAL_LEN:,} chars."
            )

        if _BASE64_BLOB_RE.search(content):
            log.warning(
                "String literal at line %d contains a long base64-like blob — "
                "flagging as suspicious.",
                tok_start[0],
            )
            return (
                f"String literal at line {tok_start[0]} contains a long base64-like blob "
                f"(≥200 consecutive base64 characters). Possible encoded payload."
            )

    return None


# Internal helpers — format / contract check


def _format_check(tree: ast.Module) -> str | None:
    """
    Verify the structural contract required of every miner agent.

    The check verifies that the required class 'AgentMiner' is defined with
    an 'infer' method (no setup required), and that a top-level 'main'
    function exists that instantiates AgentMiner and calls .infer().
    """

    # AgentMiner class with infer() method
    agent_class: ast.ClassDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AgentMiner":
            agent_class = node
            break

    if agent_class is None:
        return (
            "Missing required class 'AgentMiner'. "
            "Your agent.py must define a class named AgentMiner."
        )

    class_method_names: set[str] = {
        n.name
        for n in ast.walk(agent_class)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    if "infer" not in class_method_names:
        return (
            "AgentMiner is missing required method 'infer'. "
            "Define 'def infer(self, ...): ...' inside AgentMiner."
        )

    # top-level main() function
    main_func: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
            main_func = node
            break

    if main_func is None:
        return (
            "Missing required top-level function 'main'. "
            "Your agent.py must define a 'def main() -> None:' function."
        )

    # main() must instantiate AgentMiner and call .infer()
    #
    # Strategy: collect all names assigned via  `<name> = AgentMiner(...)`
    # inside main(), then verify that `<name>.infer()` appears.

    agent_var_names: set[str] = set()

    for node in ast.walk(main_func):
        if not isinstance(node, ast.Assign):
            continue
        # RHS must be a call to AgentMiner (possibly with args/kwargs)
        if not (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "AgentMiner"
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                agent_var_names.add(target.id)

    if not agent_var_names:
        return (
            "main() does not instantiate AgentMiner. Add 'agent = AgentMiner()' inside main()."
        )

    # Collect all method calls of the form  <var>.<method>()  inside main()
    calls_found: set[str] = set()  # elements like "agent.infer"
    for node in ast.walk(main_func):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in agent_var_names
        ):
            continue
        calls_found.add(f"{func.value.id}.{func.attr}")

    for var in agent_var_names:
        # Only require .infer() — no .setup()
        dotted = f"{var}.infer"
        if dotted not in calls_found:
            return (
                f"main() never calls '{dotted}()'. "
                f"Ensure main() calls {var}.infer() "
                f"in the appropriate --infer branch."
            )

    return None
