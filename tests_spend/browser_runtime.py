"""One browser-discovery contract for optional local and required release tests."""
import os
from pathlib import Path

CHROME = Path(os.environ.get("BURNRATE_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
