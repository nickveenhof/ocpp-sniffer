"""Tests for log message prefixes. Every log line must have exactly one [LOG] or [...] prefix."""
import re


def test_no_double_prefixes():
    """No log message should have [LOG] [LOG] or [LOG] [...] or [...] [LOG]."""
    with open("src/ocpp_proxy/main.py") as f:
        content = f.read()

    # Find all format strings in _LOGGER calls
    lines = content.split("\n")
    issues = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        # Check for double prefix patterns in the format string
        if "_LOGGER." in s and '"' in s:
            # Extract the string part after the opening quote
            match = re.search(r'"(.+?)"', s)
            if match:
                fmt = match.group(1)
                # Check for double brackets
                if "][" in fmt and fmt.count("[") > 1:
                    # Allow %s followed by bracket (runtime prefix)
                    if not fmt.startswith("%s"):
                        issues.append(f"line {i}: {fmt[:60]}")

    assert issues == [], f"Double prefixes found:\n" + "\n".join(issues)


def test_every_logger_call_has_prefix():
    """Every _LOGGER.info/error/warning/exception call must have [LOG] or [...] or %s prefix."""
    with open("src/ocpp_proxy/main.py") as f:
        lines = f.readlines()

    issues = []
    for i, line in enumerate(lines, 1):
        s = line.strip()

        # Single-line logger calls with inline string
        for method in ['_LOGGER.info("', '_LOGGER.error("', '_LOGGER.warning("', '_LOGGER.exception("']:
            if method in s:
                after = s.split(method)[1]
                # Must start with [LOG], [...], or %s (runtime prefix)
                if not (after.startswith("[LOG]") or after.startswith("[...]") or after.startswith("%s")):
                    issues.append(f"line {i}: {s[:80]}")

        # Multi-line: string on next line after _LOGGER.xxx(
        if i > 1:
            prev = lines[i - 2].strip() if i >= 2 else ""
            if s.startswith('"') and any(m in prev for m in ['_LOGGER.info(', '_LOGGER.error(', '_LOGGER.warning(', '_LOGGER.exception(']):
                if '"' not in prev.split('_LOGGER')[1].split('(')[1] if '_LOGGER' in prev else True:
                    if not (s.startswith('"[LOG]') or s.startswith('"[...]') or s.startswith('"%s')):
                        issues.append(f"line {i}: {s[:80]}")

    assert issues == [], f"Missing prefixes:\n" + "\n".join(issues)


def test_no_non_standard_brackets():
    """Only [LOG] and [...] brackets allowed. No [ECO], [ACTION], [SESSION], etc."""
    with open("src/ocpp_proxy/main.py") as f:
        content = f.read()

    # Find all bracket patterns in _LOGGER lines
    issues = []
    for i, line in enumerate(content.split("\n"), 1):
        if "_LOGGER." in line and "[" in line and '"' in line:
            brackets = re.findall(r'\[([^\]]+)\]', line)
            for b in brackets:
                if b not in ("LOG", "...", "") and not b.startswith("LOG") and not b.startswith("..."):
                    # Skip non-string brackets (like array indexing)
                    if f'[{b}]' in line.split('"')[1] if '"' in line else False:
                        issues.append(f"line {i}: [{b}] in {line.strip()[:80]}")

    assert issues == [], f"Non-standard brackets:\n" + "\n".join(issues)
