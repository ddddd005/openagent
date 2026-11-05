import json
import sys


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def loads(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant,
                      parse_float=_finite_float)


def _finite_float(value):
    import math
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def read(path):
    if path == "-":
        return loads(sys.stdin.read())
    with open(path, encoding="utf-8") as stream:
        return loads(stream.read())


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)


def write_new(path, value):
    text = dumps(value) + "\n"
    with open(path, "x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
