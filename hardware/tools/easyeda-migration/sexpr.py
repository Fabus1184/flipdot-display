"""Minimal KiCad S-expression reader/writer.

Lists are Python lists, symbols are `Sym`, strings are `str`, numbers stay
as their original text in `Num` so nothing is reformatted by accident.
"""

import re


class Sym(str):
    pass


class Num(str):
    pass


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def loads(text):
    stack = [[]]
    pos = 0
    while True:
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        op, cl, s, atom = m.groups()
        if op:
            stack.append([])
        elif cl:
            done = stack.pop()
            stack[-1].append(done)
        elif s is not None:
            stack[-1].append(s.replace('\\"', '"').replace("\\\\", "\\"))
        else:
            if re.fullmatch(r"-?\d+(\.\d+)?(e-?\d+)?", atom):
                stack[-1].append(Num(atom))
            else:
                stack[-1].append(Sym(atom))
    assert len(stack) == 1, "unbalanced"
    return stack[0][0]


def _atom(a):
    if isinstance(a, (Sym, Num)):
        return str(a)
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, (int, float)):
        return fmt(a)
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def fmt(v):
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def dumps(node, indent=0):
    if not isinstance(node, list):
        return _atom(node)
    simple = all(not isinstance(c, list) for c in node)
    if simple:
        return "(" + " ".join(_atom(c) for c in node) + ")"
    out = "(" + " ".join(_atom(c) for c in node if not isinstance(c, list))
    for c in node:
        if isinstance(c, list):
            out += "\n" + "\t" * (indent + 1) + dumps(c, indent + 1)
    return out + "\n" + "\t" * indent + ")"


def head(node):
    return node[0] if isinstance(node, list) and node else None


def find(node, name):
    for c in node:
        if head(c) == name:
            return c
    return None


def find_all(node, name):
    return [c for c in node if head(c) == name]


def prop(node, name):
    for c in node:
        if head(c) == "property" and c[1] == name:
            return c
    return None


def walk(node, fn, skip=()):
    """Calls fn(child, parent) for every list in the tree, depth first."""
    for c in list(node):
        if isinstance(c, list):
            if head(c) in skip:
                continue
            fn(c, node)
            walk(c, fn, skip)
