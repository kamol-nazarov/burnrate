from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_burnrate_navbar_structure_and_data_wiring() -> None:
    html = (ROOT / "spend_web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "spend_web" / "spend.js").read_text(encoding="utf-8")
    header = html.split('<header class="burnrate-nav"', 1)[1].split("</header>", 1)[0]
    assert 'class="burnrate-mark"' in header
    assert 'src="/favicon.svg?v=1"' in header
    assert 'class="burnrate-lockup"' in header
    assert 'class="burnrate-tagline">AI cost intelligence<' in header
    assert '<span>BURN</span><strong>RATE</strong>' in header
    assert "<svg" not in header
    assert "<title>BURNRATE · AI Cost Intelligence</title>" in html
    assert 'rel="icon" type="image/svg+xml"' in html
    assert 'id="nav-run-rate"' in header
    assert 'id="nav-today"' in header
    assert 'aria-live="polite"' in header
    assert "function renderNavbar" in script
    assert "navigation.burnRatePerDay" in script
    assert "navigation.todayUsd" in script
    assert "renderNavbar(data.nav)" not in script
    assert "/api/spend/nav" not in script
    assert "/api/spend/limits" not in script


def test_burnrate_brand_mark_is_static_crisp_and_productized() -> None:
    css = (ROOT / "spend_web" / "spend.css").read_text(encoding="utf-8")
    for token in (
        "@keyframes ping",
        ".burnrate-mark{",
        "flex:0 0 42px;width:42px;height:42px",
        ".burnrate-wordmark strong{color:var(--accent)",
        ".burnrate-tagline{",
        "AI cost intelligence",
    ):
        assert token in css or token in (ROOT / "spend_web" / "index.html").read_text(encoding="utf-8")
    favicon = (ROOT / "spend_web" / "favicon.svg").read_text(encoding="utf-8")
    assert 'viewBox="0 0 64 64"' in favicon
    assert "#D9A441" in favicon and "#8AB7FF" in favicon
    assert "animation-fill-mode:both" not in css
    assert "animation-fill-mode: both" not in css
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert "burnrate-meter__bar" not in css


def test_burnrate_navbar_is_scoped_sticky_and_responsive() -> None:
    css = (ROOT / "spend_web" / "spend.css").read_text(encoding="utf-8")
    assert ".burnrate-nav{" in css
    assert "position:sticky;top:0;z-index:30;" in css
    assert "background:rgba(10,12,15,.94)" in css
    assert "backdrop-filter:blur(14px)" in css
    assert "width:min(1620px,calc(100% - 48px));min-height:78px" in css
    assert "@media(max-width:1439px)" in css
    assert "@media(max-width:1199px)" in css
    assert "@media(max-width:1023px)" in css
    assert "@media(max-width:767px)" in css
    assert "@media(max-width:390px)" in css
    assert "min-height:44px" in css
