"""Mandatory release browser validation. Missing coverage cannot pass."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests_spend.browser_runtime import CHROME


def main():
    if not CHROME.is_file() or not shutil.which("node"):
        raise SystemExit("Required Chrome/Node runtime missing. Set BURNRATE_CHROME to the Chrome executable.")
    suites = [
        "tests_spend/test_request_races.py",
        "tests_spend/test_frontend_viewports.py",
        "tests_spend/test_performance_phase2.py",
    ]
    suites.extend(str(p.relative_to(ROOT)) for p in sorted((ROOT / "tests_spend").glob("test_product_browser*.py")))
    report = ROOT / "test-results" / "required-browser.xml"
    report.parent.mkdir(exist_ok=True)
    env = dict(os.environ, BURNRATE_CHROME=str(CHROME))
    env.pop("BURNRATE_RACE_BASELINE", None)
    result = subprocess.run([sys.executable, "-m", "pytest", *suites, "-q",
                             "-o", "addopts=", "--junitxml=" + str(report)], cwd=ROOT, env=env)
    if result.returncode:
        return result.returncode
    cases = ET.parse(report).findall(".//testcase")
    races = [c for c in cases if "request_races" in c.get("classname", "")]
    if len(races) < 29 or any(c.find("skipped") is not None for c in cases):
        raise SystemExit("Required browser coverage missing or skipped")
    print(f"Required browser validation: {len(cases)} executed; {len(races)} request races; zero skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
