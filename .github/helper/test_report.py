"""Turn a `bench run-tests` log into GitHub annotations and a job summary (both readable without signing in):
the failing tests, each with the last line of its error."""

import re
import sys

try:
	log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except OSError:
	log = ""
log = re.sub(r"\x1b\[[0-9;]*m", "", log)

failed = re.findall(r"^\s*✖\s+(\S+)", log, re.M)
# Frappe's report: " ERROR  test_x (module.Class.test_x)" / " FAIL  ...", then the traceback (with locals) up to
# the next "=====" line; the error is the last line that starts with an exception name.
blocks = re.findall(r"^\s*(?:ERROR|FAIL)\s+(\S+) \(([^)]+)\)\n-+\n(.*?)(?=\n={20,}|\n-{20,}\nRan |\Z)", log, re.M | re.S)
lines = []
for test, where, trace in blocks:
	errors = [l.strip() for l in trace.splitlines() if re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b", l)]
	last = errors[-1] if errors else ([l for l in trace.strip().splitlines() if l.strip()] or [""])[-1].strip()
	# where it was raised: the last frame of the traceback ("File …/apps/<app>/…", line N, in fn)
	frames = re.findall(r'File "[^"]*/apps/([^"]+)", line (\d+), in (\S+)', trace)
	at = f" [at {frames[-1][0]}:{frames[-1][1]} {frames[-1][2]}]" if frames else ""
	lines.append(f"{where.rsplit('.', 2)[-2]}.{test}: {last[:300]}{at}")
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
if not lines:
	# no test report (a setup command failed): the end of the log says why
	tail = [l for l in log.strip().splitlines() if l.strip()][-40:]
	for i in range(0, len(tail), 4):
		print("::error title=Log tail " + str(i // 4 + 1) + "::" + " / ".join(tail[i : i + 4]).replace("%", "%25"))

# Errors the code caught and logged (Error Log), which a test only sees as a message: the end of each traceback.
if len(sys.argv) > 3:
	try:
		logged = open(sys.argv[3], encoding="utf-8", errors="replace").read().strip()
	except OSError:
		logged = ""
	for i, entry in enumerate([e for e in logged.split("\n=====\n") if e.strip()][:8], start=1):
		keep = [l.strip() for l in entry.strip().splitlines()
			if l.strip().startswith('File "') or re.match(r"^[A-Za-z_][\w.]*(Error|Exception)\b", l.strip())]
		tail = [entry.strip().splitlines()[0]] + keep[-5:]
		print(f"::error title=Error Log {i}::" + " / ".join(tail).replace("%", "%25"))
