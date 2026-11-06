"""STEP 8: lightweight static checks on the iPhone web shell (no browser)."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def read(name):
    return (WEB / name).read_text()


def test_index_has_four_tabs_in_order():
    html = read("index.html")
    tabs = re.findall(r'<button data-tab="(\w+)"', html)
    assert tabs == ["home", "agents", "activity", "account"]
    for label in ("Home", "Agents", "Activity", "Account"):
        assert f"</svg>{label}</button>" in html
    for sid in ("view-home", "view-agents", "view-activity", "view-account", "screen-coin", "screen-audit"):
        assert f'id="{sid}"' in html
    assert "<canvas id=\"home-chart\"" in html
    assert "viewport-fit=cover" in html


def test_css_palette_and_layout_tokens():
    css = re.sub(r"/\*.*?\*/", "", read("app.css"), flags=re.S).lower()
    for tok in ("#000", "#fff", "rgba(235,235,245,0.6)", "rgba(235,235,245,0.3)", "rgba(84,84,88,0.55)",
                "#1c1c1e", "#2c2c2e", "#00d35a", "#ff453a", "#ff9f0a", "#64d2ff", "#0a84ff", "#ffd60a", "#ff6b9a"):
        assert tok in css, tok
    for tok in ("env(safe-area-inset-top)", "env(safe-area-inset-bottom)", "tabular-nums", "backdrop-filter",
                "aspect-ratio:504/470", "42 / 504", "scroll-snap-type:x mandatory", "prefers-reduced-motion",
                "sf pro display", "sf pro text", "sf mono", "ui-monospace"):
        assert tok in css, tok
    # no neon/glow, no all-caps eyebrows, no gradients on cards
    assert "text-shadow" not in css and "box-shadow" not in css
    assert "text-transform:uppercase" not in css.replace(" ", "")
    assert "gradient" not in css


def test_app_js_ranges_gate_and_polling():
    js = read("app.js")
    assert '["LIVE", "1H", "4H", "1D", "1W", "ALL"]' in js
    assert "chart/${r}.json" in js and 'get("chart.json")' in js
    assert "POLL_MS = 15000" in js
    # soft gate + hash strip + localStorage d/t unchanged
    assert 'SALT = "fomo-desk-gate:"' in js and "sha256(SALT + T)" in js
    assert 'LS_D = "desk.d"' in js and 'LS_T = "desk.t"' in js
    assert "history.replaceState(null, \"\", location.pathname + location.search)" in js
    assert "Desk offline · retrying in" in js and "last update" in js
    # Plan B Pages push cadence: stale banner after 600 s (was 90)
    assert "age > 600" in js and "age > 90" not in js
    # Plan B: no POST, no SSE, no push subscription
    assert "EventSource" not in js and "method: \"POST\"" not in js and "pushManager" not in js
    for copy in ("Nothing yet tonight", "Waiting for the first trade", "Listening for graduations", "No coin taken apart yet",
                 "Link copied", "Telegram only", "navigator.share"):
        assert copy in js, copy


def test_charts_on_canvas_dpr_aware():
    cj = read("chart.js")
    assert "devicePixelRatio" in cj
    for fn in ("function line(", "function candles(", "function ring("):
        assert fn in cj


def test_no_hardcoded_fomo_urls_in_web():
    for p in WEB.rglob("*"):
        if p.is_file() and p.suffix in {".html", ".css", ".js", ".json"}:
            assert "fomo.family" not in p.read_text(), p


def test_sw_never_caches_data_dir():
    sw = read("sw.js")
    assert 'u.pathname.includes("/d/")' in sw
    shell = re.search(r"const SHELL = \[(.*?)\];", sw, re.S).group(1)
    assert "d/" not in shell
    assert "push" not in re.sub(r"/\*.*?\*/", "", sw, flags=re.S).lower()
    assert 'const CACHE = "desk-shell-v3-step13"' in sw
