"""Minimal SNBT (stringified NBT) parser, tolerant of the FTB Quests dialect.

FTB Quests .snbt files use newline-separated key/value pairs (no commas),
unquoted keys, typed numeric suffixes (1b, 5.0d, 3L, 2s, 1.5f), quoted
strings with escapes, lists and compounds. This parser returns plain
Python dicts/lists/str/int/float/bool.
"""

class SNBTError(ValueError):
    pass


class _Parser:
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)

    def parse(self):
        self._ws()
        val = self._value()
        self._ws()
        if self.i < self.n:
            raise SNBTError(f"trailing data at {self.i}: {self.s[self.i:self.i+40]!r}")
        return val

    def _ws(self):
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t\r\n,":
                self.i += 1
            elif c == "#":  # comment to end of line (rare, but harmless)
                while self.i < self.n and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def _peek(self):
        return self.s[self.i] if self.i < self.n else ""

    def _value(self):
        c = self._peek()
        if c == "{":
            return self._compound()
        if c == "[":
            return self._list()
        if c in "\"'":
            return self._string(c)
        return self._scalar()

    def _compound(self):
        self.i += 1  # {
        out = {}
        while True:
            self._ws()
            if self._peek() == "}":
                self.i += 1
                return out
            if self.i >= self.n:
                raise SNBTError("unterminated compound")
            key = self._key()
            self._ws()
            if self._peek() != ":":
                raise SNBTError(f"expected ':' after key {key!r} at {self.i}")
            self.i += 1
            self._ws()
            out[key] = self._value()

    def _list(self):
        self.i += 1  # [
        # typed arrays: [I; 1, 2], [B; ...], [L; ...]
        save = self.i
        self._ws()
        if self._peek() in "IBL" and self.i + 1 < self.n and self.s[self.i + 1] == ";":
            self.i += 2
        else:
            self.i = save
        out = []
        while True:
            self._ws()
            if self._peek() == "]":
                self.i += 1
                return out
            if self.i >= self.n:
                raise SNBTError("unterminated list")
            out.append(self._value())

    def _key(self):
        c = self._peek()
        if c in "\"'":
            return self._string(c)
        start = self.i
        while self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] in "_-.+"):
            self.i += 1
        if start == self.i:
            raise SNBTError(f"empty key at {self.i}")
        return self.s[start:self.i]

    def _string(self, quote):
        self.i += 1
        buf = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                nxt = self.s[self.i + 1]
                buf.append({"n": "\n", "t": "\t", "\\": "\\", quote: quote}.get(nxt, nxt))
                self.i += 2
            elif c == quote:
                self.i += 1
                return "".join(buf)
            else:
                buf.append(c)
                self.i += 1
        raise SNBTError("unterminated string")

    def _scalar(self):
        start = self.i
        while self.i < self.n and self.s[self.i] not in " \t\r\n,}]:":
            self.i += 1
        tok = self.s[start:self.i]
        if not tok:
            raise SNBTError(f"empty token at {start}")
        if tok == "true":
            return True
        if tok == "false":
            return False
        # typed numerics: 1b, 2s, 3L, 1.5f, 5.0d
        body, suffix = tok, ""
        if tok[-1] in "bBsSlLfFdD" and len(tok) > 1:
            body, suffix = tok[:-1], tok[-1].lower()
        try:
            if suffix in ("f", "d") or "." in body or "e" in body.lower():
                return float(body)
            return int(body)
        except ValueError:
            return tok  # bare string (e.g. unquoted enum)


def loads(text):
    return _Parser(text).parse()


def load(path):
    with open(path, encoding="utf-8") as f:
        return loads(f.read())
