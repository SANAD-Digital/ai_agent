"""Turn a `bench run-tests` log into GitHub annotations and a job summary (both readable without signing in):
the failing tests, each with the last line of its error."""

import re
import sys

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
log = re.sub(r"\x1b\[[0-9;]*m", "", log)

failed = re.findall(r"^\s*✖\s+(\S+)", log, re.M)
# Frappe's report: " ERROR  test_x (module.Class.test_x)" / " FAIL  ...", then the traceback (with locals) up to
# the next "=====" line; the error is the last line that starts with an exception name.
blocks = re.findall(r"^\s*(?:ERROR|FAIL)\s+(\S+) \(([^)]+)\)\n-+\n(.*?)(?=\n={20,}|\n-{20,}\nRan |\Z)", log, re.M | re.S)
lines = []
for test, where, trace in blocks:
	errors = [l.strip() for l in trace.splitlines() if re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b", l)]
	last = errors[-1] if errors else ([l for l in trace.strip().splitlines() if l.strip()] or [""])[-1].strip()
	lines.append(f"{where.rsplit('.', 2)[-2]}.{test}: {last[:300]}")
totals = re.findall(r"^(Ran \d+ tests.*|FAILED \(.*\)|OK.*)$", log, re.M)

summary = ["## Tests", "", *[f"- {t}" for t in totals], "", f"{len(failed)} failed", ""]
summary += [f"- `{l}`" for l in lines] or [f"- `{t}`" for t in failed]
with open(sys.argv[2], "a", encoding="utf-8") as out:
	out.write("\n".join(summary) + "\n")
# GitHub keeps 10 error annotations per step: put several failures in each
chunk = max(1, -(-len(lines) // 10))
for i in range(0, len(lines), chunk):
	text = " | ".join(lines[i : i + chunk]).replace("%", "%25").replace("\n", " ")
	print(f"::error title=Failed tests {i + 1}-{min(i + chunk, len(lines))} of {len(lines)}::{text}")
