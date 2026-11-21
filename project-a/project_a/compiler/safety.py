"""AST allow-list validator for LLM-compiled tax code.

Accepts exactly one function `def compute(inp):` built from: assignments, if/else, for loops over a
name, a list/tuple literal, a subscript or range(...), return, arithmetic (+ - * / // %), comparisons,
boolean operators, conditional expressions, dict/list/tuple literals, subscripts and slices, and calls
to min, max, round, abs, sum, len, range, int, float (positional arguments only).

Everything else is rejected with a reason naming the line. Passing this check means the code cannot
import, touch attributes, reach builtins beyond that list, or define more code; it does NOT mean the
code is correct, and it does not bound running time or memory (the sandbox does that).
"""
import ast
import builtins

ALLOWED_CALLS = ("min", "max", "round", "abs", "sum", "len", "range", "int", "float")
MAX_SOURCE_BYTES = 100_000
FUNCTION_NAME = "compute"

_STATEMENTS = (ast.Assign, ast.AugAssign, ast.If, ast.For, ast.Return, ast.Pass, ast.Break, ast.Continue)
_EXPRESSIONS = (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Name, ast.Constant,
                ast.Dict, ast.List, ast.Tuple, ast.Subscript, ast.Slice)
_HELPERS = (ast.Load, ast.Store, ast.arguments, ast.arg,
            ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
            ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
            ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)
_WHY = {
    ast.Import: "import is not allowed", ast.ImportFrom: "import is not allowed",
    ast.Attribute: "attribute access is not allowed (use inp['key'], and x = x + [item] instead of append)",
    ast.Lambda: "lambda is not allowed", ast.While: "while loops are not allowed (use for over range)",
    ast.Global: "global is not allowed", ast.Nonlocal: "nonlocal is not allowed",
    ast.Try: "try/except is not allowed (errors must surface)", ast.ClassDef: "class definitions are not allowed",
    ast.With: "with is not allowed", ast.Raise: "raise is not allowed", ast.Assert: "assert is not allowed",
    ast.Delete: "del is not allowed", ast.Yield: "yield is not allowed", ast.YieldFrom: "yield is not allowed",
    ast.Await: "await is not allowed", ast.AsyncFunctionDef: "async functions are not allowed",
    ast.ListComp: "comprehensions are not allowed (use a for loop)", ast.SetComp: "comprehensions are not allowed",
    ast.DictComp: "comprehensions are not allowed", ast.GeneratorExp: "generator expressions are not allowed",
    ast.JoinedStr: "f-strings are not allowed", ast.Starred: "starred expressions are not allowed",
    ast.NamedExpr: "the walrus operator is not allowed", ast.Set: "set literals are not allowed",
    ast.Pow: "the power operator is not allowed (it can build huge numbers)",
    ast.FunctionDef: "only one function, compute(inp), may be defined",
    ast.Expr: "bare expressions are not allowed (only a docstring)",
    ast.AnnAssign: "annotated assignments are not allowed",
}
_BUILTIN_NAMES = set(dir(builtins)) - set(ALLOWED_CALLS) - {"True", "False", "None"}


def validate(source, allow_helpers=False):
    """Returns a list of violations, each 'line N: reason'. An empty list means the code passed.

    allow_helpers=True is a DIAGNOSTIC relaxation only: it also permits extra function definitions
    (top-level or nested, under the same restrictions) and calls to them. The engine never uses it
    by default; it exists to measure the logic of compiles that broke the one-function rule.
    """
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return ["line 1: source is larger than %d bytes" % MAX_SOURCE_BYTES]
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ["line %s: syntax error: %s" % (e.lineno, e.msg)]
    problems = []

    def bad(node, reason, line=None):
        problems.append("line %d: %s" % (getattr(node, "lineno", None) or line or 1, reason))

    body = [n for n in tree.body if not _is_docstring(n)]
    funcs = [n for n in body if isinstance(n, ast.FunctionDef)]
    main = next((f for f in funcs if f.name == FUNCTION_NAME), None)
    helpers = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)} - {FUNCTION_NAME}
    if main is None:
        bad(funcs[0] if funcs else tree, "the function must be named %s" % FUNCTION_NAME)
    for n in body:
        if not isinstance(n, ast.FunctionDef):
            bad(n, _WHY.get(type(n), "only the function definition may appear at the top level"))
        elif n is not main and not allow_helpers:
            bad(n, "only one function, compute(inp), may be defined (found %s)" % n.name)
    for f in funcs:
        a = f.args
        if f.decorator_list:
            bad(f, "decorators are not allowed")
        if f.returns is not None:
            bad(f, "return annotations are not allowed")
        if f is main and (len(a.args) != 1 or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg
                          or a.defaults or a.kw_defaults or a.args[0].annotation is not None):
            bad(f, "the signature must be exactly compute(inp)")
        if f is not main and (a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg or a.defaults or a.kw_defaults):
            bad(f, "helper functions take plain positional arguments only")

    def check(node, line):
        """Recursive walk; does not descend into a node it has rejected (one reason per construct)."""
        line = getattr(node, "lineno", None) or line
        if _is_docstring(node):
            return
        if isinstance(node, ast.FunctionDef) and allow_helpers:
            if node.decorator_list or node.args.vararg or node.args.kwarg or node.args.defaults or node.args.kwonlyargs:
                bad(node, "helper functions take plain positional arguments only", line)
            for child in node.args.args + node.body:
                check(child, line)
            return
        if type(node) in _WHY:
            bad(node, _WHY[type(node)], line)
            return
        if not isinstance(node, _STATEMENTS + _EXPRESSIONS + _HELPERS):
            bad(node, "%s is not allowed" % type(node).__name__, line)
            return
        _check_node(node, line)
        for child in ast.iter_child_nodes(node):
            check(child, line)

    def _check_node(node, line):
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                bad(node, "names starting with an underscore are not allowed: %s" % node.id)
            elif node.id in _BUILTIN_NAMES:
                bad(node, "builtin %r is not allowed" % node.id)
            elif node.id in ALLOWED_CALLS and isinstance(node.ctx, ast.Store):
                bad(node, "cannot assign to the builtin %r" % node.id)
        elif isinstance(node, ast.arg) and node.arg.startswith("_"):
            bad(node, "names starting with an underscore are not allowed: %s" % node.arg)
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if not (name in ALLOWED_CALLS or (allow_helpers and name in helpers)):
                bad(node, "only calls to %s are allowed%s" % (", ".join(ALLOWED_CALLS),
                                                               " (called %s)" % name if name else ""))
            if node.keywords:
                bad(node, "keyword arguments are not allowed")
        elif isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str, bool, type(None))):
            bad(node, "constant of type %s is not allowed" % type(node.value).__name__)
        elif isinstance(node, ast.For):
            it = node.iter
            ok_iter = (isinstance(it, (ast.Name, ast.List, ast.Tuple, ast.Subscript))
                       or (isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range"))
            if not ok_iter:
                bad(node, "for loops may only run over a list, a name, a subscript or range(...)")
            if not isinstance(node.target, (ast.Name, ast.Tuple)):
                bad(node, "for-loop target must be a name or a tuple of names")

    for f in funcs:
        if f is main or allow_helpers:
            for child in f.args.args + f.body:
                check(child, f.lineno)
    return sorted(set(problems), key=lambda p: int(p.split(":")[0].split()[1]))


def _is_docstring(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
