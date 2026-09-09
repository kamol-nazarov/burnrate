"""Pure committed-asset checks: no application, database, browser or provider."""
import gzip
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "spend_web"


def test_packaged_asset_inventory_matches_generated_files():
    cli = ast.parse((ROOT / "spend_app" / "cli.py").read_text(encoding="utf-8"))
    assets = next(
        ast.literal_eval(node.value)
        for node in cli.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_WEB_ASSETS" for target in node.targets)
    )
    manifest = json.loads((WEB / "assets.json").read_text(encoding="utf-8"))
    assert set(assets) == set(manifest["outputs"]) | {"favicon.svg"}
    assert all((WEB / name).is_file() for name in assets)


def test_sources_and_shipped_outputs_match_recorded_generation():
    manifest = json.loads((WEB / "assets.json").read_text(encoding="utf-8"))
    for directory, entries in ((ROOT / "frontend_src", manifest["sources"]), (WEB, manifest["outputs"])):
        for name, expected in entries.items():
            content = (directory / name).read_text(encoding="utf-8").replace("\r\n", "\n").encode()
            assert hashlib.sha256(content).hexdigest() == expected, name


def test_shipped_files_keep_existing_asset_budgets():
    core = (WEB / "spend.js").read_bytes()
    helper = (WEB / "request-state.js").read_bytes()
    css = (WEB / "spend.css").read_bytes()
    assert len(core) < 104_000
    assert len(css) < 45_000
    assert len(helper) < 10_000
    assert len(gzip.compress(core, 6)) + len(gzip.compress(helper, 6)) < 30_000
    assert len(gzip.compress((WEB / "product.js").read_bytes(), 6)) < 6_000
    assert sum(len(gzip.compress(file.read_bytes(), 6)) for file in WEB.glob("*.js")) < 36_000
    assert len(gzip.compress(css, 6)) < 12_000


def test_aliases_do_not_replace_dynamic_state_classes_or_public_ids():
    manifest = json.loads((WEB / "assets.json").read_text(encoding="utf-8"))
    aliases = manifest["aliases"]
    assert not {"active", "loading", "good", "warn", "burnrate-skeleton", "day-column", "activity-row", "opportunity-panel"} & aliases.keys()
    import re

    source_ids = re.findall(r'\bid="([^"]+)"', (ROOT / "frontend_src" / "index.html").read_text(encoding="utf-8"))
    shipped_ids = re.findall(r'\bid="([^"]+)"', (WEB / "index.html").read_text(encoding="utf-8"))
    assert source_ids == shipped_ids
    source_classes = re.findall(r'class="([\w -]+)"', (ROOT / "frontend_src" / "index.html").read_text(encoding="utf-8"))
    shipped_classes = re.findall(r'class="([\w -]+)"', (WEB / "index.html").read_text(encoding="utf-8"))
    assert len(source_classes) == len(shipped_classes)
    for original, shipped in zip(source_classes, shipped_classes):
        assert set(original.split()) <= set(shipped.split())
        for name in original.split():
            if name in aliases:
                assert aliases[name] in shipped.split()
