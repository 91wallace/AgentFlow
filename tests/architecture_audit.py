"""Static architecture inventory. Never imports application subpackages.

Direct imports, resolvable reexports and literal dynamic imports are recorded.
TYPE_CHECKING is a separate graph; function-local imports remain runtime edges.
This is not a proof of Python's complete dynamic import/initialization behavior.
"""

import ast
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path


NATIVE = {f"agentflow.agents.{name}" for name in
          ("codex", "antigravity", "opencode", "claude_code", "hermes", "factory")}
ADVANCED = {"approval", "security", "teg"}
PURE = {f"agentflow.{name}.contracts" for name in
        ("apl", "approval", "agents", "git", "state", "validation", "teg")}
PURE.add("agentflow.planning.models")
PURE.update({"agentflow.approval.protocol", "agentflow.agents.selection"})


@dataclass(frozen=True)
class Import:
    source: str
    target: str
    symbol: str
    kind: str
    deferred: bool
    line: int
    exported_as: str = ""


def component(module):
    return module.split(".")[1] if module.startswith("agentflow.") else module


class Scanner(ast.NodeVisitor):
    def __init__(self, module, package, known):
        self.module, self.package, self.known = module, package, known
        self.kind, self.deferred, self.module_scope = "runtime", False, True
        self.imports, self.unresolved = [], []
        self.typing_aliases, self.type_flags, self.importlib_aliases, self.loaders = set(), set(), set(), set()

    def condition(self, node):
        if isinstance(node, ast.Name) and node.id in self.type_flags:
            return False
        if (isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
                and isinstance(node.value, ast.Name) and node.value.id in self.typing_aliases):
            return False
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = self.condition(node.operand)
            return None if value is None else not value
        if isinstance(node, ast.BoolOp):
            values = [self.condition(value) for value in node.values]
            if isinstance(node.op, ast.And):
                return False if False in values else True if all(value is True for value in values) else None
            return True if True in values else False if all(value is False for value in values) else None
        return None

    def visit_If(self, node):
        self.visit(node.test)
        original = self.kind
        condition = self.condition(node.test)
        for body, excluded in ((node.body, condition is False), (node.orelse, condition is True)):
            self.kind = "type-only" if excluded or original == "type-only" else "runtime"
            for child in body:
                self.visit(child)
        self.kind = original

    def visit_FunctionDef(self, node):
        previous = self.deferred, self.module_scope
        self.deferred, self.module_scope = True, False
        self.generic_visit(node)
        self.deferred, self.module_scope = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous = self.module_scope
        self.module_scope = False
        self.generic_visit(node)
        self.module_scope = previous

    def add(self, target, symbol, line, exported_as=""):
        self.imports.append(Import(self.module, target, symbol, self.kind, self.deferred, line,
                                   exported_as if self.module_scope and self.kind == "runtime" else ""))

    def visit_Import(self, node):
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            if alias.name == "typing":
                self.typing_aliases.add(bound)
            if alias.name == "importlib":
                self.importlib_aliases.add(bound)
            self.add(alias.name, "", node.lineno, bound)

    def visit_ImportFrom(self, node):
        target = resolve_name("." * node.level + (node.module or ""), self.package) if node.level else node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if target == "typing" and alias.name == "TYPE_CHECKING":
                self.type_flags.add(bound)
            if target == "importlib" and alias.name == "import_module":
                self.loaders.add(bound)
            child = target + "." + alias.name
            self.add(child if child in self.known else target,
                     "" if child in self.known else alias.name, node.lineno, bound)

    def visit_Call(self, node):
        loader = (isinstance(node.func, ast.Name) and node.func.id in self.loaders | {"__import__"}
                  or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
                  and isinstance(node.func.value, ast.Name) and node.func.value.id in self.importlib_aliases)
        if loader:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                target = node.args[0].value
                if target.startswith("."):
                    # Resolve against the syntactic package only when explicitly requested.
                    package = node.args[1] if len(node.args) > 1 else next(
                        (item.value for item in node.keywords if item.arg == "package"), None)
                    if isinstance(package, ast.Constant) and isinstance(package.value, str):
                        target = resolve_name(target, package.value)
                    elif isinstance(package, ast.Name) and package.id == "__package__":
                        target = resolve_name(target, self.package)
                    else:
                        self.unresolved.append(f"{self.module}: {ast.unparse(node)}")
                        self.generic_visit(node)
                        return
                self.add(target, "", node.lineno)
            else:
                self.unresolved.append(f"{self.module}: {ast.unparse(node)}")
        self.generic_visit(node)


def scan_package(root):
    root = Path(root)
    sources, packages = {}, set()
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            module = ".".join(("agentflow", *parts[:-1]))
            packages.add(module)
        else:
            module = ".".join(("agentflow", *parts))
        sources[module] = path.read_text(encoding="utf-8-sig")
    imports, unresolved = [], []
    for module, source in sorted(sources.items()):
        package = module if module in packages else module.rpartition(".")[0]
        scanner = Scanner(module, package, sources)
        scanner.visit(ast.parse(source, filename=module))
        imports.extend(scanner.imports)
        unresolved.extend(scanner.unresolved)
    return imports, set(unresolved), set(sources)


def resolve_reexport(edge, imports, known):
    exports = {(item.source, item.exported_as): item for item in imports if item.exported_as}
    target, symbol, visited = edge.target, edge.symbol, set()
    while symbol and (target, symbol) not in visited:
        visited.add((target, symbol))
        binding = exports.get((target, symbol))
        if binding is None:
            break
        target, symbol = binding.target, binding.symbol
    return target


def cyclic_components(edges):
    graph = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())
    # Reachability is sufficient for this small component graph and deterministic.
    def reachable(source):
        visited, pending = set(), [source]
        while pending:
            current = pending.pop()
            if current not in visited:
                visited.add(current)
                pending.extend(graph.get(current, ()))
        return visited
    closure = {node: reachable(node) for node in graph}
    groups = {tuple(sorted(other for other in graph if other in closure[node] and node in closure[other]))
              for node in graph}
    return sorted(group for group in groups if len(group) > 1)


def audit(root):
    imports, unresolved, known = scan_package(root)
    runtime = [edge for edge in imports if edge.kind == "runtime"]
    pairs = {(component(edge.source), component(edge.target)) for edge in runtime
             if edge.target.startswith("agentflow.") and component(edge.source) != component(edge.target)}
    cycles = cyclic_components(pairs)
    cyclic_pairs = {(a, b) for group in cycles for a in group for b in group if a != b}
    violations = set()
    for edge in runtime:
        source, target = component(edge.source), component(edge.target)
        internal = edge.target.startswith("agentflow.")
        resolved = resolve_reexport(edge, imports, known)
        if internal and (source, target) in cyclic_pairs:
            violations.add(f"package-cycle|{edge.source}|{edge.target}")
        if source not in ADVANCED and target in ADVANCED:
            violations.add(f"local-to-advanced|{edge.source}|{edge.target}")
        if source not in ADVANCED and edge.target.split(".")[0] == "cryptography":
            violations.add(f"local-crypto|{edge.source}|{edge.target}")
        if source in {"core", "apl"} and resolved in NATIVE:
            violations.add(f"concrete-agent|{edge.source}|{resolved}")
        if source == "core" and component(resolved) == "state" and resolved not in PURE:
            violations.add(f"concrete-state|{edge.source}|{resolved}")
        if source in {"core", "apl"} and edge.target == "agentflow.agents" and not edge.symbol:
            violations.add(f"agent-facade-import|{edge.source}|{edge.target}")
        if (source in {"core", "apl"} or edge.source in PURE) and edge.symbol == "*":
            violations.add(f"wildcard-import|{edge.source}|{edge.target}")
        if edge.source in PURE and internal and resolved not in PURE:
            violations.add(f"contract-to-implementation|{edge.source}|{resolved}")
        if edge.source in PURE and edge.target.split(".")[0] in {"sqlite3", "subprocess", "cryptography"}:
            violations.add(f"contract-io|{edge.source}|{edge.target}")
    return {"violations": sorted(violations), "unresolved_dynamic_imports": sorted(unresolved),
            "cycles": [list(group) for group in cycles], "edges": [list(pair) for pair in sorted(pairs)],
            "runtime_imports": len(runtime), "type_imports": len(imports) - len(runtime),
            "deferred_runtime_imports": sum(edge.deferred for edge in runtime), "modules": len(known)}


def baseline_difference(report, baseline):
    messages = []
    for key in ("violations", "unresolved_dynamic_imports"):
        current, allowed = set(report[key]), set(baseline[key])
        if current - allowed:
            messages.append(f"New {key}: " + ", ".join(sorted(current - allowed)))
        if allowed - current:
            messages.append(f"Remove stale baseline {key}: " + ", ".join(sorted(allowed - current)))
    return messages
