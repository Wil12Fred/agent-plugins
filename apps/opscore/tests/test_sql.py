"""The read-only guard: every way a write hides inside something that reads.

This is the security-relevant half of `opscore`, it is used by four packages,
and it had **no tests at all** until this file — which is the shape of debt this
toolkit's own skills exist to name: a guard nobody has watched fail is not a
guard, it is a hope.

It is a regex over text and therefore weaker than a read-only connection. Where
the backend can be told to refuse writes, tell it. These tests pin what this
layer does catch.
"""

from __future__ import annotations

import pytest

from opscore.errors import GuardError
from opscore.sql import assert_read_only, strip_sql_comments


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select id from users where id = 1",
        "SHOW TABLES",
        "EXPLAIN SELECT 1",
        "DESCRIBE users",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ],
)
def test_a_read_is_allowed(sql: str) -> None:
    """The control. Without it, 'refuses everything' would pass every test below."""
    assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO users VALUES (1)",
        "UPDATE users SET name = 'x'",
        "DELETE FROM users",
        "DROP TABLE users",
        "TRUNCATE users",
        "ALTER TABLE users ADD COLUMN x INT",
        "CREATE TABLE t (id INT)",
        "REPLACE INTO users VALUES (1)",
        "GRANT ALL ON *.* TO 'x'",
    ],
)
def test_a_write_is_refused(sql: str) -> None:
    with pytest.raises(GuardError):
        assert_read_only(sql)


def test_a_write_hidden_in_a_cte_is_refused() -> None:
    """Rule: `WITH` starts a read, so the first keyword proves nothing.

    A statement can open with the one prefix on the allowlist and still write.
    """
    with pytest.raises(GuardError):
        assert_read_only("WITH x AS (DELETE FROM users RETURNING id) SELECT * FROM x")


def test_a_select_that_writes_a_file_is_refused() -> None:
    """Rule: `SELECT … INTO OUTFILE` reads rows and writes to the database host.

    It passes every check that looks at the first keyword, which is exactly why
    it is checked separately.
    """
    with pytest.raises(GuardError):
        assert_read_only("SELECT * FROM users INTO OUTFILE '/tmp/x'")


def test_a_write_hidden_behind_a_comment_is_refused() -> None:
    """Rule: comments are stripped before the statement is classified.

    Leading `/* ... */` moves the real first keyword, so a classifier reading
    the raw text sees a comment and shrugs.
    """
    with pytest.raises(GuardError):
        assert_read_only("/* harmless */ DELETE FROM users")

    with pytest.raises(GuardError):
        assert_read_only("-- select\nDROP TABLE users")


def test_comments_are_stripped_without_eating_the_statement() -> None:
    stripped = strip_sql_comments("SELECT 1 -- a comment\n")
    assert "SELECT 1" in stripped
    assert "comment" not in stripped


def test_a_hash_comment_counts_too() -> None:
    """MySQL accepts `#` as a line comment, and a guard that only knows `--`
    is a guard with a documented bypass."""
    with pytest.raises(GuardError):
        assert_read_only("# select\nDELETE FROM users")


def test_case_and_whitespace_do_not_smuggle_anything_through() -> None:
    with pytest.raises(GuardError):
        assert_read_only("\n\n   dElEtE   FROM users")
