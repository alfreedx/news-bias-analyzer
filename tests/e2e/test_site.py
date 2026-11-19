import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

PAGES = [
    "/",
    "/buscar?q=vivienda",
    "/medios",
    "/medios/el-pais",
    "/grupos/prisa",
    "/metodologia",
    "/privacidad",
    "/acerca",
]
SEARCH = "/buscar?q=vivienda"


def count(page: Page) -> int:
    m = re.search(r"\d+", page.locator("[data-count]").inner_text())
    assert m, "counter has no number"
    return int(m.group())


@pytest.mark.parametrize("path", PAGES)
def test_page_loads_without_console_errors_or_csp(page: Page, base_url, path):
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("requestfailed", lambda r: problems.append(f"failed {r.url}"))
    resp = page.goto(base_url + path, wait_until="networkidle")
    assert resp.status == 200
    assert not problems, problems


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
@pytest.mark.parametrize("path", PAGES)
def test_no_horizontal_overflow(page: Page, base_url, path, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(base_url + path, wait_until="networkidle")
    sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw, f"scrollWidth {sw} > clientWidth {cw}"


def test_search_shows_results_and_coverage(page: Page, base_url):
    page.goto(base_url + SEARCH)
    expect(page.get_by_role("heading", name="Radiografía de la cobertura")).to_be_visible()
    assert page.locator("[data-item]").count() > 0
    expect(page.locator("[data-filters]")).to_be_visible()


def test_position_pill_filters_items(page: Page, base_url):
    page.goto(base_url + SEARCH)
    total = count(page)
    positions = {p for p in page.locator("[data-item]").evaluate_all("els => els.map(e => e.dataset.pos)")}
    assert len(positions) >= 2, f"need several positions, got {positions}"
    target = sorted(positions)[0]
    page.locator(f".pill[data-pos='{target}']").click()
    assert count(page) < total
    for item in page.locator("[data-item]").all():
        if item.is_visible():
            assert item.get_attribute("data-pos") == target
        else:
            assert item.get_attribute("data-pos") != target
    page.locator(".pill[data-pos='all']").click()
    assert count(page) == total


def test_flat_view_and_recent_sort(page: Page, base_url):
    page.goto(base_url + SEARCH)
    page.locator("[data-view]").select_option("flat")
    page.locator("[data-sort]").select_option("recent")
    visible = [i for i in page.locator("[data-item]").all() if i.is_visible()]
    assert visible
    times = [int(i.get_attribute("data-time")) for i in visible]
    assert times == sorted(times, reverse=True)
    assert count(page) == len(visible)


def test_works_without_javascript(browser, base_url):
    ctx = browser.new_context(java_script_enabled=False)
    page = ctx.new_page()
    page.goto(base_url + SEARCH)
    assert page.locator("[data-item]:visible").count() > 0
    expect(page.locator("[data-filters]")).to_be_hidden()
    ctx.close()


def test_keyboard_search_submit(page: Page, base_url):
    page.goto(base_url + "/")
    box = page.locator("input[name='q']").first
    box.focus()
    expect(box).to_be_focused()
    box.fill("vivienda")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/buscar\?.*q=vivienda"))


@pytest.mark.parametrize("path", ["/", SEARCH, "/medios"])
def test_basic_a11y(page: Page, base_url, path):
    page.goto(base_url + path)
    assert page.locator("img:not([alt])").count() == 0
    bad = page.evaluate(
        """() => [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea')]
            .filter(el => !(el.labels && el.labels.length) && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby'))
            .map(el => el.outerHTML.slice(0, 120))"""
    )
    assert not bad, bad
