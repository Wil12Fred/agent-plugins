"""Refusing a statement that is not a read.

Extracted from a database layer, because the guard is useful without one: a
Cloudflare Log Explorer query, an Athena query and a MySQL `SELECT` all need the
same refusal, and none of them needs a driver.

It is a **regex over text**, which is a weaker guarantee than a read-only
connection and is not a substitute for one. Where the backend can be told to
refuse writes itself, tell it; use this where it cannot — a hosted query API you
reach over HTTP has no session to set read-only.
"""

from __future__ import annotations

import re

from opscore.errors import GuardError

READ_ONLY_STATEMENTS = frozenset({"select", "show", "explain", "describe", "desc", "with"})

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"(--|#)[^\n]*")
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|replace|alter|create|drop|truncate|rename|grant|revoke|"
    r"lock|call|set\s+global)\b",
    re.IGNORECASE,
)

# A `SELECT` is not automatically a read: `SELECT ... INTO OUTFILE` writes a
# file on the *server*. The statement-type check passes it, and the write-keyword
# scan only runs for `WITH`, so this was the one shape that got through — the
# exact class this guard exists to catch.
_SELECT_THAT_WRITES = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.IGNORECASE)

# A trailing row bound the caller already wrote. Appending a second `LIMIT`
# would be a syntax error, so a statement carrying one is left alone.
_TRAILING_LIMIT = re.compile(r"\blimit\s+\d+(\s*,\s*\d+|\s+offset\s+\d+)?\s*$", re.IGNORECASE)

# Only these accept a trailing `LIMIT`. Verified against the server, because
# guessing got it wrong once: **no** form of `SHOW` takes one — `SHOW TABLES
# LIMIT 3`, `SHOW DATABASES LIMIT 2` and `SHOW COLUMNS FROM t LIMIT 2` all fail
# with 1064. `DESCRIBE t LIMIT 5` is likewise a syntax error, and appending one
# to an `EXPLAIN` would change the statement being explained.
_ACCEPTS_LIMIT = frozenset({"select", "with"})


def strip_sql_comments(sql: str) -> str:
    """Remove block and line comments so keyword checks cannot be fooled."""
    return _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql)).strip()


def assert_read_only(sql: str) -> None:
    """Reject anything that is not a single read statement.

    Raises:
        GuardError: the statement writes, or more than one statement was given.
    """
    cleaned = strip_sql_comments(sql)
    if not cleaned:
        raise GuardError("empty statement")

    body = cleaned.rstrip().rstrip(";")
    if ";" in body:
        raise GuardError(
            "multiple statements are not allowed",
            detail="send one read statement per call",
        )

    first = body.lstrip().split(maxsplit=1)[0].lower()
    if first not in READ_ONLY_STATEMENTS:
        raise GuardError(
            f"statement type {first.upper()!r} is forbidden",
            detail=f"allowed: {', '.join(sorted(s.upper() for s in READ_ONLY_STATEMENTS))}",
        )

    # `WITH` can front a writing CTE on MySQL 8; check the body too.
    if first == "with" and (match := _WRITE_KEYWORDS.search(body)):
        raise GuardError(f"write keyword {match.group(0).upper()!r} found inside the CTE")

    if match := _SELECT_THAT_WRITES.search(body):
        raise GuardError(
            f"{match.group(0).upper()!r} writes a file on the database server",
            detail="a read statement may not have a destination",
        )
