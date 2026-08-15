#!/usr/bin/env python3
"""Paired benchmark: delegated implementation vs inline, measured per token.

Two arms run the same task with the same tools, effort and turn cap. The only
difference is whether implementation is delegated to a subagent. Success is a
passing test suite, so a cheap failure cannot be mistaken for a saving.

Cost is computed from usage on every response and gated hard: a run aborts at
PER_RUN_CAP, the whole benchmark aborts at GLOBAL_CAP.
"""

import json
import os
import shutil
import subprocess
import sys
import time

import anthropic

ORCH_MODEL = "claude-fable-5"
IMPL_MODEL = "claude-opus-5"
EFFORT = "medium"
MAX_TOKENS = 8000
MAX_TURNS = 14
SUB_MAX_TURNS = 12
PER_RUN_CAP = 3.50
GLOBAL_CAP = 10.00

# USD per million tokens: (input, output)
PRICE = {"claude-fable-5": (10.0, 50.0), "claude-opus-5": (5.0, 25.0)}

WORK = os.path.expanduser("~/.bench-work")
client = anthropic.Anthropic()


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self):
        self.total = 0.0
        self.by_model = {}

    def charge(self, model, usage):
        pin, pout = PRICE[model]
        fresh = usage.input_tokens or 0
        write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        out = usage.output_tokens or 0
        cost = (fresh * pin + write * pin * 1.25 + read * pin * 0.10
                + out * pout) / 1e6
        self.total += cost
        m = self.by_model.setdefault(model, {"cost": 0.0, "in": 0, "write": 0,
                                             "read": 0, "out": 0, "calls": 0})
        m["cost"] += cost
        m["in"] += fresh
        m["write"] += write
        m["read"] += read
        m["out"] += out
        m["calls"] += 1
        if self.total > GLOBAL_CAP:
            raise BudgetExceeded("global cap $%.2f exceeded ($%.4f)"
                                 % (GLOBAL_CAP, self.total))
        return cost


BUDGET = Budget()

TOOLS_READ = [
    {"name": "bash", "description":
     "Run a shell command in the repository root. Returns combined stdout and "
     "stderr, truncated. Use it to run tests, list files and inspect state.",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string", "description": "The shell command."}},
         "required": ["command"]}},
    {"name": "read", "description":
     "Read a text file. Path is relative to the repository root.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
]

TOOLS_WRITE = [
    {"name": "write", "description":
     "Create or overwrite a file with the given content. Path is relative to "
     "the repository root.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}},
    {"name": "edit", "description":
     "Replace the first exact occurrence of old_str with new_str in a file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old_str": {"type": "string"},
         "new_str": {"type": "string"}},
         "required": ["path", "old_str", "new_str"]}},
]

TOOL_DELEGATE = {
    "name": "delegate_implementation", "description":
    "Hand the implementation to a separate implementer agent that works in its "
    "own fresh context with write access to the repository. Give it a complete, "
    "self-contained brief: what to build, which files, which tests must pass, "
    "and what not to touch. It cannot see this conversation. It returns a short "
    "structured report. You keep verification for yourself.",
    "input_schema": {"type": "object", "properties": {
        "brief": {"type": "string", "description":
                  "The complete, self-contained implementation brief."}},
        "required": ["brief"]}}


def repo_path(root, rel):
    full = os.path.realpath(os.path.join(root, rel))
    if not (full == root or full.startswith(root + os.sep)):
        raise ValueError("path escapes the repository root: %s" % rel)
    return full


def run_tool(root, name, args):
    try:
        if name == "bash":
            r = subprocess.run(args["command"], shell=True, cwd=root,
                               capture_output=True, text=True, timeout=120)
            out = (r.stdout + r.stderr).strip() or "(no output)"
            return out[:4000], False
        if name == "read":
            with open(repo_path(root, args["path"])) as f:
                return f.read()[:6000], False
        if name == "write":
            p = repo_path(root, args["path"])
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(args["content"])
            return "wrote %d bytes to %s" % (len(args["content"]), args["path"]), False
        if name == "edit":
            p = repo_path(root, args["path"])
            with open(p) as f:
                text = f.read()
            if args["old_str"] not in text:
                return "old_str not found in %s" % args["path"], True
            with open(p, "w") as f:
                f.write(text.replace(args["old_str"], args["new_str"], 1))
            return "edited %s" % args["path"], False
        return "unknown tool: %s" % name, True
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e), True


def call(model, system, messages, tools, label, stats):
    body = {"output_config": {"effort": EFFORT}}
    sys_blocks = [{"type": "text", "text": system,
                   "cache_control": {"type": "ephemeral"}}]
    r = client.messages.create(model=model, max_tokens=MAX_TOKENS,
                               system=sys_blocks, tools=tools,
                               messages=messages, extra_body=body)
    cost = BUDGET.charge(model, r.usage)
    ctx = ((r.usage.input_tokens or 0)
           + (getattr(r.usage, "cache_creation_input_tokens", 0) or 0)
           + (getattr(r.usage, "cache_read_input_tokens", 0) or 0))
    stats["calls"] += 1
    stats["cost"] += cost
    stats["out_tokens"] += r.usage.output_tokens or 0
    stats["ctx_peak"] = max(stats["ctx_peak"], ctx)
    stats["ctx_cumulative"] += ctx
    if stats["cost"] > PER_RUN_CAP:
        raise BudgetExceeded("per-run cap $%.2f exceeded in %s ($%.4f)"
                             % (PER_RUN_CAP, label, stats["cost"]))
    print("    [%s] %-14s ctx=%-7d out=%-5d $%.4f  (run $%.4f | total $%.4f)"
          % (label, model.replace("claude-", ""), ctx, r.usage.output_tokens,
             cost, stats["cost"], BUDGET.total), flush=True)
    return r


def new_stats():
    return {"calls": 0, "cost": 0.0, "out_tokens": 0, "ctx_peak": 0,
            "ctx_cumulative": 0}


def agent_loop(model, system, first_user, tools, root, label, stats, max_turns):
    messages = [{"role": "user", "content": first_user}]
    for _ in range(max_turns):
        r = call(model, system, messages, tools, label, stats)
        messages.append({"role": "assistant", "content": r.content})
        uses = [b for b in r.content if b.type == "tool_use"]
        if not uses:
            texts = [b.text for b in r.content if b.type == "text"]
            return "\n".join(texts)
        results = []
        for b in uses:
            if b.name == "delegate_implementation":
                report = agent_loop(
                    IMPL_MODEL, IMPL_SYSTEM, b.input["brief"],
                    TOOLS_READ + TOOLS_WRITE, root, label + ":impl",
                    stats.setdefault("sub", new_stats()), SUB_MAX_TURNS)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": report or "(no report)"})
                continue
            out, is_err = run_tool(root, b.name, b.input)
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": out, "is_error": is_err})
        messages.append({"role": "user", "content": results})
    return "(turn cap reached)"


IMPL_SYSTEM = (
    "You are an implementer agent. You write production code in the repository "
    "you have been given. Work only within the brief you receive. Do not change "
    "test files. When you are done, reply with a short structured report: files "
    "changed, what you implemented, anything you are unsure about."
)

INLINE_SYSTEM = (
    "You are a senior engineer working in a repository. Implement what is asked, "
    "then verify it by running the test suite yourself. Do not change test files. "
    "When the tests pass, reply with a short summary of what you changed."
)

ORCH_SYSTEM = (
    "You are an orchestrator. You do NOT write production code yourself: you "
    "have no write or edit tools. You inspect the repository, then hand the "
    "implementation to the implementer agent via delegate_implementation with a "
    "complete self-contained brief. Afterwards you verify the result yourself by "
    "running the test suite, and if it fails you send a precise defect brief "
    "back to the implementer. Do not change test files. When the tests pass, "
    "reply with a short summary."
)

TASKS = {
    "duration": {
        "prompt": (
            "The test file tests/test_duration.py exists and fails because the "
            "module it imports does not exist yet. Implement app/duration.py so "
            "that the whole suite passes. Run the tests with: python3 -m pytest -q"
        ),
        "files": {
            "app/__init__.py": "",
            "tests/test_duration.py": '''import pytest

from app.duration import parse_duration


def test_seconds():
    assert parse_duration("45s") == 45


def test_minutes_and_seconds():
    assert parse_duration("1m30s") == 90


def test_hours():
    assert parse_duration("2h") == 7200


def test_combined():
    assert parse_duration("1h2m3s") == 3723


def test_whitespace_is_tolerated():
    assert parse_duration("  1h 30m ") == 5400


def test_zero():
    assert parse_duration("0s") == 0


def test_bare_number_is_rejected():
    with pytest.raises(ValueError):
        parse_duration("90")


def test_empty_is_rejected():
    with pytest.raises(ValueError):
        parse_duration("")


def test_unknown_unit_is_rejected():
    with pytest.raises(ValueError):
        parse_duration("5x")


def test_repeated_unit_is_rejected():
    with pytest.raises(ValueError):
        parse_duration("1h1h")


def test_out_of_order_units_are_rejected():
    with pytest.raises(ValueError):
        parse_duration("30s1h")
''',
        },
    },
    "ledger": {
        "prompt": (
            "The test suite fails: app/ledger.py has bugs. Fix the production "
            "code in app/ledger.py so the whole suite passes. Do not change the "
            "tests. Run the tests with: python3 -m pytest -q"
        ),
        "files": {
            "app/__init__.py": "",
            "app/ledger.py": '''"""A tiny append-only ledger with running balances."""


class Ledger:
    def __init__(self, opening=0):
        self.opening = opening
        self.entries = []

    def add(self, description, amount):
        """Append an entry. Amount may be negative. Returns the new balance."""
        self.entries.append((description, amount))
        return self.balance()

    def balance(self):
        return sum(a for _, a in self.entries)

    def history(self):
        """Return [(description, amount, running_balance), ...]."""
        out = []
        running = 0
        for desc, amount in self.entries:
            running += amount
            out.append((desc, amount, running))
        return out

    def total_debits(self):
        return sum(a for _, a in self.entries if a < 0)
''',
            "tests/test_ledger.py": '''import pytest

from app.ledger import Ledger


def test_opening_balance_counts():
    assert Ledger(opening=100).balance() == 100


def test_add_returns_new_balance():
    led = Ledger(opening=10)
    assert led.add("fee", -4) == 6


def test_running_balance_starts_from_opening():
    led = Ledger(opening=50)
    led.add("in", 25)
    led.add("out", -10)
    assert led.history() == [("in", 25, 75), ("out", -10, 65)]


def test_total_debits_is_positive_magnitude():
    led = Ledger()
    led.add("a", -5)
    led.add("b", 20)
    led.add("c", -15)
    assert led.total_debits() == 20


def test_amount_must_be_numeric():
    led = Ledger()
    with pytest.raises(TypeError):
        led.add("bad", "10")


def test_entries_are_not_externally_mutable():
    led = Ledger()
    led.add("a", 5)
    led.entries.clear()
    assert led.balance() == 5
''',
        },
    },
}


def build_pipeline_task():
    """A repo big enough that locating the bug costs real reading."""
    files = {"pipeline/__init__.py": "", "tests/__init__.py": ""}

    files["pipeline/aggregate.py"] = '''"""Aggregation primitives."""


def mean(values):
    if not values:
        raise ValueError("mean of empty sequence")
    return sum(values) / len(values)


def weighted_mean(values, weights):
    """Weighted arithmetic mean of values."""
    if len(values) != len(weights):
        raise ValueError("values and weights must have equal length")
    if not values:
        raise ValueError("weighted_mean of empty sequence")
    numerator = sum(v * w for v, w in zip(values, weights))
    return numerator / len(values)


def median(values):
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def spread(values):
    if not values:
        raise ValueError("spread of empty sequence")
    return max(values) - min(values)
'''

    files["pipeline/summarize.py"] = '''"""Turn validated records into a summary block."""

from pipeline.aggregate import median, spread, weighted_mean


def summarize(records):
    """records: list of {"score": float, "weight": float, "label": str}."""
    if not records:
        return {"score": 0.0, "median": 0.0, "spread": 0.0, "count": 0}
    scores = [r["score"] for r in records]
    weights = [r["weight"] for r in records]
    return {
        "score": weighted_mean(scores, weights),
        "median": median(scores),
        "spread": spread(scores),
        "count": len(records),
    }
'''

    files["pipeline/report.py"] = '''"""Public entry point: build a report from raw rows."""

from pipeline.normalize import normalize_rows
from pipeline.summarize import summarize
from pipeline.validate import validate_rows


def build_report(rows):
    validate_rows(rows)
    records = normalize_rows(rows)
    summary = summarize(records)
    return {
        "score": round(summary["score"], 4),
        "median": round(summary["median"], 4),
        "spread": round(summary["spread"], 4),
        "count": summary["count"],
    }
'''

    files["pipeline/validate.py"] = '''"""Input validation for raw rows."""

REQUIRED = ("score", "weight")


def validate_rows(rows):
    if not isinstance(rows, list):
        raise TypeError("rows must be a list")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TypeError("row %d is not a mapping" % i)
        for key in REQUIRED:
            if key not in row:
                raise ValueError("row %d is missing %r" % (i, key))
        if row["weight"] < 0:
            raise ValueError("row %d has a negative weight" % i)
    return True
'''

    files["pipeline/normalize.py"] = '''"""Normalize raw rows into records."""


def normalize_rows(rows):
    out = []
    for row in rows:
        out.append({
            "score": float(row["score"]),
            "weight": float(row["weight"]),
            "label": str(row.get("label", "")).strip().lower(),
        })
    return out
'''

    decoys = {
        "scale": ("rescale", "return [v * factor for v in values]", "factor"),
        "ratio": ("ratio", "return numerator / denominator if denominator else 0.0",
                  "denominator"),
        "window": ("moving_average",
                   "return [sum(values[i:i + size]) / size\n"
                   "            for i in range(len(values) - size + 1)]", "size"),
        "transform": ("clamp", "return [min(max(v, low), high) for v in values]",
                      "low, high"),
        "filterset": ("drop_outliers",
                      "return [v for v in values if abs(v - pivot) <= limit]",
                      "pivot, limit"),
        "formatting": ("as_percent",
                       'return "%.1f%%" % (value * 100.0)', ""),
        "weights": ("renormalize",
                    "total = sum(values)\n"
                    "    return [v / total for v in values] if total else list(values)",
                    ""),
        "bucket": ("bucketize",
                   "return {b: [v for v in values if v // width == b]\n"
                   "            for b in {int(v // width) for v in values}}", "width"),
    }
    for mod, (fn, body, extra) in decoys.items():
        sig = "values, %s" % extra if extra else "values"
        files["pipeline/%s.py" % mod] = (
            '"""Helper: %s."""\n\n\ndef %s(%s):\n    """%s over the given values."""\n    %s\n'
            % (mod, fn, sig, fn.replace("_", " ").capitalize(), body))

    files["tests/test_report.py"] = '''import pytest

from pipeline.report import build_report

ROWS = [
    {"score": 10.0, "weight": 1.0, "label": "A"},
    {"score": 20.0, "weight": 3.0, "label": "B"},
]


def test_weighted_score_uses_weights_not_count():
    # (10*1 + 20*3) / (1+3) = 17.5
    assert build_report(ROWS)["score"] == 17.5


def test_single_row_is_its_own_score():
    assert build_report([{"score": 7.0, "weight": 5.0}])["score"] == 7.0


def test_zero_weights_do_not_crash_and_yield_zero():
    out = build_report([{"score": 4.0, "weight": 0.0},
                        {"score": 8.0, "weight": 0.0}])
    assert out["score"] == 0.0


def test_median_and_spread_are_unweighted():
    out = build_report(ROWS)
    assert out["median"] == 15.0
    assert out["spread"] == 10.0


def test_count_is_row_count():
    assert build_report(ROWS)["count"] == 2


def test_empty_report():
    assert build_report([]) == {"score": 0.0, "median": 0.0,
                               "spread": 0.0, "count": 0}


def test_negative_weight_is_rejected():
    with pytest.raises(ValueError):
        build_report([{"score": 1.0, "weight": -1.0}])


def test_missing_key_is_rejected():
    with pytest.raises(ValueError):
        build_report([{"score": 1.0}])
'''
    return files


TASKS["pipeline"] = {
    "prompt": (
        "The test suite fails. Find the bug in the pipeline package and fix the "
        "production code so the whole suite passes. Do not change the tests. "
        "Run the tests with: python3 -m pytest -q"
    ),
    "files": build_pipeline_task(),
}


TASKS["feature"] = {
    "prompt": (
        "The test suite tests/test_records.py exists and fails: the records "
        "package it imports does not exist yet. Implement the whole package so "
        "that every test passes. Run the tests with: python3 -m pytest -q"
    ),
    "files": {
        "tests/__init__.py": "",
        "tests/test_records.py": '''import pytest

from records.coerce import coerce
from records.parse import parse_line
from records.table import Table


# --- parse_line -------------------------------------------------------------

def test_plain_fields():
    assert parse_line("a,b,c") == ["a", "b", "c"]


def test_surrounding_whitespace_is_stripped():
    assert parse_line(" a , b ") == ["a", "b"]


def test_quoted_field_keeps_inner_whitespace():
    assert parse_line('"  a  ",b') == ["  a  ", "b"]


def test_quoted_field_may_contain_comma():
    assert parse_line('"a,b",c') == ["a,b", "c"]


def test_doubled_quote_is_a_literal_quote():
    assert parse_line('"a""b"') == ['a"b']


def test_empty_fields_are_preserved():
    assert parse_line("a,,b") == ["a", "", "b"]


def test_trailing_separator_yields_trailing_empty():
    assert parse_line("a,") == ["a", ""]


def test_unterminated_quote_is_rejected():
    with pytest.raises(ValueError):
        parse_line('"a,b')


def test_empty_line_is_one_empty_field():
    assert parse_line("") == [""]


# --- coerce -----------------------------------------------------------------

def test_integer():
    assert coerce("42") == 42 and isinstance(coerce("42"), int)


def test_negative_integer():
    assert coerce("-7") == -7


def test_float():
    assert coerce("3.5") == 3.5


def test_booleans_are_case_insensitive():
    assert coerce("true") is True and coerce("FALSE") is False


def test_empty_becomes_none():
    assert coerce("") is None


def test_plain_string_passes_through():
    assert coerce("hello") == "hello"


def test_numeric_looking_string_with_spaces_is_coerced():
    assert coerce("  8  ") == 8


def test_bool_wins_over_string():
    assert coerce("True") is True


# --- Table ------------------------------------------------------------------

LINES = [
    "name,dept,salary",
    "ann,eng,100",
    "bob,eng,200",
    "cyd,ops,50",
]


def test_from_lines_reads_header_and_coerces():
    t = Table.from_lines(LINES)
    assert t.columns == ["name", "dept", "salary"]
    assert t.rows[0] == {"name": "ann", "dept": "eng", "salary": 100}


def test_len_is_row_count():
    assert len(Table.from_lines(LINES)) == 3


def test_column_returns_values_in_order():
    assert Table.from_lines(LINES).column("salary") == [100, 200, 50]


def test_unknown_column_is_rejected():
    with pytest.raises(KeyError):
        Table.from_lines(LINES).column("nope")


def test_row_with_wrong_field_count_is_rejected():
    with pytest.raises(ValueError):
        Table.from_lines(["a,b", "1,2,3"])


def test_group_by_preserves_first_seen_order():
    groups = Table.from_lines(LINES).group_by("dept")
    assert list(groups) == ["eng", "ops"]
    assert [r["name"] for r in groups["eng"]] == ["ann", "bob"]


def test_aggregate_sum_per_group():
    assert Table.from_lines(LINES).aggregate("dept", "salary", sum) == {
        "eng": 300, "ops": 50}


def test_aggregate_with_max():
    assert Table.from_lines(LINES).aggregate("dept", "salary", max) == {
        "eng": 200, "ops": 50}


def test_aggregate_skips_none_values():
    lines = ["k,v", "a,1", "a,", "b,2"]
    assert Table.from_lines(lines).aggregate("k", "v", sum) == {"a": 1, "b": 2}


def test_empty_table_has_no_rows_but_keeps_columns():
    t = Table.from_lines(["x,y"])
    assert t.columns == ["x", "y"] and len(t) == 0
''',
    },
}


def make_repo(task_name, arm):
    root = os.path.join(WORK, "%s-%s" % (task_name, arm))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    for rel, content in TASKS[task_name]["files"].items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return os.path.realpath(root)


def tests_pass(root):
    r = subprocess.run(["python3", "-m", "pytest", "-q"], cwd=root,
                       capture_output=True, text=True, timeout=180)
    return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1:]


def run_arm(task_name, arm):
    root = make_repo(task_name, arm)
    stats = new_stats()
    started = time.time()
    aborted = None
    print("  --- %s / %s ---" % (task_name, arm), flush=True)
    try:
        if arm == "inline":
            agent_loop(ORCH_MODEL, INLINE_SYSTEM, TASKS[task_name]["prompt"],
                       TOOLS_READ + TOOLS_WRITE, root, arm, stats, MAX_TURNS)
        else:
            agent_loop(ORCH_MODEL, ORCH_SYSTEM, TASKS[task_name]["prompt"],
                       TOOLS_READ + [TOOL_DELEGATE], root, arm, stats, MAX_TURNS)
    except BudgetExceeded as e:
        aborted = str(e)
        print("    ABORT: %s" % e, flush=True)
    ok, tail = tests_pass(root)
    sub = stats.get("sub", new_stats())
    return {
        "task": task_name, "arm": arm, "success": ok, "pytest_tail": tail,
        "seconds": round(time.time() - started, 1), "aborted": aborted,
        "orchestrator": {k: v for k, v in stats.items() if k != "sub"},
        "implementer": sub,
        "total_cost": round(stats["cost"] + sub["cost"], 4),
    }


def main():
    which = sys.argv[1:] or ["duration"]
    os.makedirs(WORK, exist_ok=True)
    results = []
    for task in which:
        for arm in ("delegated", "inline"):
            try:
                results.append(run_arm(task, arm))
            except BudgetExceeded as e:
                print("STOP: %s" % e, flush=True)
                break
        else:
            continue
        break
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "bench_results.json")
    prior = []
    if os.path.exists(out):
        with open(out) as f:
            prior = json.load(f).get("runs", [])
    with open(out, "w") as f:
        json.dump({"runs": prior + results,
                   "budget": {"total": round(BUDGET.total, 4),
                              "by_model": BUDGET.by_model},
                   "config": {"orchestrator": ORCH_MODEL,
                              "implementer": IMPL_MODEL, "effort": EFFORT,
                              "max_turns": MAX_TURNS}}, f, indent=2)
    print("\n=== summary ===")
    for r in results:
        o, i = r["orchestrator"], r["implementer"]
        print("%-9s %-10s success=%-5s orch_ctx_peak=%-7d orch_cost=$%-7.4f "
              "impl_cost=$%-7.4f total=$%.4f %s"
              % (r["task"], r["arm"], r["success"], o["ctx_peak"], o["cost"],
                 i["cost"], r["total_cost"], r["aborted"] or ""))
    print("spent this invocation: $%.4f" % BUDGET.total)
    print("results: %s" % out)


if __name__ == "__main__":
    main()
