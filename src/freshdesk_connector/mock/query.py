"""A small parser/evaluator for Freshdesk's ticket-filter mini-language.

Supports the subset Freshdesk documents:

    "priority:4 OR priority:3"
    "(status:2 OR status:3) AND created_at:>'2026-09-01'"
    "agent_id:null AND tag:'autopay'"
    "cf_autopay_status:'failed'"

This exists so the mock server rejects bad filters the way the real API does -
with an ``invalid_field`` validation error - which is what makes the connector's
"validate before you spend a rate-limit unit" behaviour testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<paren>[()])
      | (?P<op>:>=|:<=|:>|:<|:)
      | (?P<quoted>'(?:[^']*)'|"(?:[^"]*)")
      | (?P<word>[^\s():<>]+)
    )
    """,
    re.VERBOSE,
)

#: Fields the account exposes for filtering, per Freshdesk's ticket-fields model.
STANDARD_FILTER_FIELDS = {
    "status",
    "priority",
    "source",
    "type",
    "agent_id",
    "responder_id",
    "group_id",
    "requester_id",
    "company_id",
    "product_id",
    "tag",
    "created_at",
    "updated_at",
    "due_by",
    "fr_due_by",
    "closed_at",
    "resolved_at",
    "is_escalated",
    "fr_escalated",
    "spam",
    "deleted",
    "email_config_id",
}

#: Contact records expose a different filter vocabulary from tickets.
CONTACT_FILTER_FIELDS = {
    "email",
    "mobile",
    "phone",
    "name",
    "job_title",
    "company_id",
    "unique_external_id",
    "tag",
    "created_at",
    "updated_at",
}

DATE_FIELDS = {"created_at", "updated_at", "due_by", "fr_due_by", "closed_at", "resolved_at"}


class QueryError(ValueError):
    """Raised for malformed queries or unknown fields."""

    def __init__(self, message: str, *, field: str | None = None, code: str = "invalid_field") -> None:
        super().__init__(message)
        self.field = field
        self.code = code


@dataclass(frozen=True)
class Cond:
    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class Bool:
    op: str
    left: Any
    right: Any


def tokenize(query: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(query):
        match = TOKEN_RE.match(query, pos)
        if not match or match.end() == pos:
            raise QueryError(f"Could not parse filter near {query[pos:pos + 20]!r}", code="invalid_query")
        pos = match.end()
        kind = match.lastgroup
        text = match.group(kind)
        if kind == "word" and text.upper() in {"AND", "OR"}:
            tokens.append(("kw", text.upper()))
        elif kind == "word":
            tokens.append(("word", text))
        elif kind == "quoted":
            tokens.append(("value", text[1:-1]))
        elif kind == "op":
            tokens.append(("op", text))
        else:
            tokens.append((kind or "word", text))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], query: str) -> None:
        self.tokens = tokens
        self.pos = 0
        self.query = query

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise QueryError("Unexpected end of filter expression", code="invalid_query")
        self.pos += 1
        return token

    def parse(self) -> Any:
        node = self.parse_or()
        if self.peek() is not None:
            raise QueryError(f"Unexpected trailing tokens in filter: {self.query!r}", code="invalid_query")
        return node

    def parse_or(self) -> Any:
        node = self.parse_and()
        while True:
            token = self.peek()
            if token and token[0] == "kw" and token[1] == "OR":
                self.next()
                node = Bool("or", node, self.parse_and())
            else:
                return node

    def parse_and(self) -> Any:
        node = self.parse_factor()
        while True:
            token = self.peek()
            if token and token[0] == "kw" and token[1] == "AND":
                self.next()
                node = Bool("and", node, self.parse_factor())
            else:
                return node

    def parse_factor(self) -> Any:
        token = self.peek()
        if token and token[0] == "paren" and token[1] == "(":
            self.next()
            node = self.parse_or()
            closing = self.peek()
            if not closing or closing[1] != ")":
                raise QueryError("Unbalanced parentheses in filter", code="invalid_query")
            self.next()
            return node
        return self.parse_condition()

    def parse_condition(self) -> Cond:
        kind, field = self.next()
        if kind not in {"word", "value"}:
            raise QueryError(f"Expected a field name, got {field!r}", code="invalid_query")
        op_token = self.next()
        if op_token[0] != "op":
            raise QueryError(f"Expected an operator after {field!r}, got {op_token[1]!r}", code="invalid_query")
        operator = op_token[1]
        if operator.startswith(":"):
            operator = operator[1:] or "="

        value_tokens: list[str] = []
        while True:
            token = self.peek()
            if token is None or token[0] in {"kw", "paren"}:
                break
            if token[0] == "op":
                break
            value_tokens.append(token[1])
            self.next()
        if not value_tokens:
            raise QueryError(f"Missing value for field {field!r}", code="invalid_query")
        raw = " ".join(value_tokens)
        return Cond(field=field, op=operator, value=_coerce(raw))


def _coerce(raw: str) -> Any:
    low = raw.strip().lower()
    if low in {"true", "false"}:
        return low == "true"
    if low == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse(
    query: str,
    *,
    allowed_custom_fields: set[str] | None = None,
    allowed_fields: set[str] | None = None,
) -> Any:
    if not query or not query.strip():
        raise QueryError("Filter query must not be empty", code="missing_query")
    node = _Parser(tokenize(query.strip()), query).parse()
    _validate(node, allowed_custom_fields or set(), set(STANDARD_FILTER_FIELDS) | set(allowed_fields or set()))
    return node


def _validate(node: Any, allowed_custom: set[str], allowed: set[str]) -> None:
    if isinstance(node, Bool):
        _validate(node.left, allowed_custom, allowed)
        _validate(node.right, allowed_custom, allowed)
        return
    field = node.field
    if field in allowed:
        return
    if field.startswith("cf_") and (not allowed_custom or field in allowed_custom):
        return
    raise QueryError(f"Unexpected/invalid field in request: {field}", field=field)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #

_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.replace("Z", "+0000") if fmt.endswith("%z") else value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def field_value(ticket: Mapping[str, Any], field: str) -> Any:
    """Resolve a filter field against a ticket object.

    Note the deliberate quirk carried over from the real API: tickets *filter* on
    ``agent_id`` but the object *returns* ``responder_id``. A connector that
    assumes one name works for both gets confusing empty result sets.
    """
    if field == "agent_id":
        return ticket.get("responder_id")
    if field == "tag":
        return ticket.get("tags") or []
    if field in {"closed_at", "resolved_at"}:
        stats = ticket.get("stats") or {}
        return stats.get(field)
    if field.startswith("cf_"):
        if field in ticket:
            return ticket[field]
        return (ticket.get("custom_fields") or {}).get(field[3:])
    return ticket.get(field)


def matches(node: Any, ticket: Mapping[str, Any]) -> bool:
    if isinstance(node, Bool):
        if node.op == "and":
            return matches(node.left, ticket) and matches(node.right, ticket)
        return matches(node.left, ticket) or matches(node.right, ticket)

    actual = field_value(ticket, node.field)
    expected = node.value
    op = node.op

    if expected is None:
        return actual in (None, "", [], {})
    if op == "=":
        if isinstance(actual, list):
            return any(_equal(item, expected) for item in actual)
        return _equal(actual, expected)
    if op in {">", "<", ">=", "<="}:
        return _compare(actual, expected, op)
    return False


def _equal(actual: Any, expected: Any) -> bool:
    if actual is None:
        return False
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.strip().lower() == expected.strip().lower()
    if isinstance(actual, bool) or isinstance(expected, bool):
        return bool(actual) == bool(expected)
    try:
        return float(actual) == float(expected)
    except (TypeError, ValueError):
        return str(actual).strip().lower() == str(expected).strip().lower()


def _compare(actual: Any, expected: Any, op: str) -> bool:
    left = _parse_dt(actual) if isinstance(actual, str) and "-" in actual else actual
    right = _parse_dt(expected) if isinstance(expected, str) and "-" in str(expected) else expected
    if isinstance(left, datetime) and isinstance(right, datetime):
        if left.tzinfo is None:
            left = left.replace(tzinfo=right.tzinfo)
        if right.tzinfo is None:
            right = right.replace(tzinfo=left.tzinfo)
    else:
        try:
            left, right = float(left), float(right)
        except (TypeError, ValueError):
            # Comparisons on non-numeric, non-date fields are unsupported, so the
            # item simply does not match (Freshdesk returns 400 here; we prefer to
            # keep the mock permissive and let the connector's own validation catch it).
            return False
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == ">=":
        return left >= right
    return left <= right
