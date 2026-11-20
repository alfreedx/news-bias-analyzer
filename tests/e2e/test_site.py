import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

PAGES = [
    "/",
    "/buscar?q=vivienda",
    "/medios",
    "/medios/el-pais",
    "/medios/cope",
    "/buscar?autor=Ana+P%C3%A9rez",
    "/grupos/prisa",
    "/metodologia",
    "/como-leer",
    "/privacidad",
    "/acerca",
]
SEARCH = "/buscar?q=vivienda"


@pytest.fixture(scope="module")
def story_path(base_url):
    import urllib.request

    html = urllib.request.urlopen(base_url + "/").read().decode()
    m = re.search(r'href="(/historia/\d+)"', html)
    assert m, "front page does not link to a persisted story"
    return m.group(1)


def open_filters(page: Page) -> None:
    box = page.locator("details[data-filters]")
    expect(box).to_be_visible()
    if box.get_attribute("open") is None:
        box.locator("summary").click()


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
    expect(page.get_by_role("heading", name="Quién la cuenta")).to_be_visible()
    assert page.locator("[data-item]").count() > 0
    expect(page.locator("[data-filters]")).to_be_visible()


def test_position_pill_filters_items(page: Page, base_url):
    page.goto(base_url + SEARCH)
    open_filters(page)
    total = count(page)
    positions = {p for p in page.locator("[data-item]").evaluate_all("els => els.map(e => e.dataset.pos)")}
    assert len(positions) >= 2, f"need several positions, got {positions}"
    target = sorted(positions)[0]
    page.locator(f".pill[data-pos='{target}']").click()
    assert count(page) < total
    for item in page.locator("[data-item]").all():
        if item.evaluate("e => e.hidden"):  # items inside the closed «Más titulares» fold are not hidden by the filter
            assert item.get_attribute("data-pos") != target
        else:
            assert item.get_attribute("data-pos") == target
    page.locator(".pill[data-pos='all']").click()
    assert count(page) == total


def test_flat_view_and_recent_sort(page: Page, base_url):
    page.goto(base_url + SEARCH)
    open_filters(page)
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


def test_syndicated_copies_collapse_into_one_row(page: Page, base_url):
    page.goto(base_url + SEARCH)
    merged = page.locator(".story__list .item", has_text="ABC").filter(has_text="OKDiario")
    assert merged.count() == 1


@pytest.mark.parametrize("width", [390, 1280])
def test_comparison_block(browser, base_url, width):
    ctx = browser.new_context(java_script_enabled=False, viewport={"width": width, "height": 800})
    page = ctx.new_page()
    page.goto(base_url + SEARCH)
    block = page.locator(".compare").first
    expect(block).to_be_visible()
    cols = block.locator(".compare__col")
    assert cols.count() == 3
    xs = {round(c.bounding_box()["x"]) for c in cols.all()}
    ys = {round(c.bounding_box()["y"]) for c in cols.all()}
    assert (len(xs) == 1) if width == 390 else (len(ys) == 1)
    assert "No aparece en los RSS que leemos de medios de este lado" in block.inner_text()
    sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw
    ctx.close()


def test_coverage_summary_sentence(page: Page, base_url):
    page.goto(base_url + SEARCH)
    expect(page.locator(".panel__summary")).to_contain_text("Poca base para comparar")
    expect(page.locator(".panel__summary")).to_contain_text("medios con etiqueta publicada")


@pytest.mark.parametrize("width", [390, 1280])
def test_outlet_ficha(browser, base_url, width):
    ctx = browser.new_context(java_script_enabled=False, viewport={"width": width, "height": 800})
    page = ctx.new_page()
    page.goto(base_url + "/medios/cope")
    ficha = page.locator("dl.ficha")
    expect(ficha).to_be_visible()
    text = ficha.inner_text().lower()
    for word in ("fundado", "lengua", "ámbito", "institución religiosa", "conferencia episcopal"):
        assert word in text, word
    link = ficha.locator("a[href^='https://www.wikidata.org/wiki/Q']").first
    expect(link).to_be_visible()
    assert "Wikidata" in link.inner_text()
    sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw
    ctx.close()


def test_catalog_filters_by_language_and_ownership(page: Page, base_url):
    page.goto(base_url + "/medios")
    def shown():
        return int(re.search(r"\d+", page.locator("[data-table-count]").inner_text()).group())
    total = shown()
    page.locator("select[data-filter='langs']").select_option("catalán")
    assert 0 < shown() < total
    page.locator("select[data-filter='langs']").select_option("")
    page.locator("select[data-filter='owner']").select_option("publica")
    assert 0 < shown() < total
    assert page.locator("tr[data-row]:visible", has_text="RTVE").count() == 1


def test_filters_hidden_without_javascript_but_table_complete(browser, base_url):
    ctx = browser.new_context(java_script_enabled=False)
    page = ctx.new_page()
    page.goto(base_url + "/medios")
    expect(page.locator("[data-table-filters]")).to_be_hidden()
    assert page.locator("tr[data-row]:visible").count() > 50
    ctx.close()


def test_author_link_leads_to_author_page(page: Page, base_url):
    page.goto(base_url + SEARCH)
    link = page.locator("a.byline__author", has_text="Ana Pérez").first
    expect(link).to_be_visible()
    assert "/buscar?autor=" in link.get_attribute("href")
    link.click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("Ana Pérez")
    assert "El País" in page.locator(".profile__side").inner_text()
    assert page.locator(".profile__main .item").count() == 1


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
def test_story_page_loads_clean_without_overflow(page: Page, base_url, story_path, width, height):
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("requestfailed", lambda r: problems.append(f"failed {r.url}"))
    page.set_viewport_size({"width": width, "height": height})
    resp = page.goto(base_url + story_path, wait_until="networkidle")
    assert resp.status == 200 and not problems, problems
    sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw
    expect(page.get_by_role("heading", name="En lo que coinciden")).to_be_visible()
    expect(page.get_by_text("no prueba que algo sea cierto").first).to_be_visible()
    for name in ("Palabras y expresiones compartidas", "Palabras que solo usa un lado"):
        heading = page.get_by_role("heading", name=name)
        if heading.count():  # present only when the story has enough voices and terms
            expect(heading).to_be_visible()


def test_story_page_details_expand(page: Page, base_url, story_path):
    page.goto(base_url + story_path)
    details = page.locator(".piece__outlets").first
    expect(details).to_be_visible()
    assert details.get_attribute("open") is None
    details.locator("summary").click()
    assert details.get_attribute("open") is not None
    expect(details.locator("p")).to_be_visible()


def test_story_page_works_without_javascript(browser, base_url, story_path):
    ctx = browser.new_context(java_script_enabled=False)
    try:
        page = ctx.new_page()
        page.goto(base_url + story_path)
        expect(page.get_by_role("heading", name="En lo que coinciden")).to_be_visible()
        assert page.locator(".piece").count() >= 1
        page.locator(".piece__outlets summary").first.click()
        assert page.locator(".piece__outlets[open]").count() == 1
    finally:
        ctx.close()


def test_filters_folded_in_details_closed_by_default(page: Page, base_url):
    page.goto(base_url + SEARCH)
    box = page.locator("details[data-filters]")
    expect(box).to_be_visible()
    assert box.get_attribute("open") is None
    expect(page.locator(".pill[data-pos='all']")).to_be_hidden()
    expect(page.locator("input[name='q']").first).to_be_visible()
    expect(page.get_by_role("button", name="30 días")).to_be_visible()
    box.locator("summary").click()
    expect(page.locator(".pill[data-pos='all']")).to_be_visible()


def test_coverage_bar_shows_unlabelled_segment(page: Page, base_url):
    page.goto(base_url + SEARCH)
    # a handful of labelled headlines is too thin a base: no bar, no percentages, an explicit warning
    assert page.locator(".stack__seg--na").count() == 0 and page.locator(".stack").count() == 0
    expect(page.locator(".panel__summary")).to_contain_text("Poca base para comparar")
    assert "%" not in page.locator(".panel__summary").inner_text()
    expect(page.locator(".panel__summary")).to_contain_text("cuánto publica cada medio")
    assert "Sin etiqueta" in page.locator(".legend").inner_text()


def test_como_leer_page(page: Page, base_url):
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.goto(base_url + "/como-leer", wait_until="networkidle")
    assert not problems, problems
    expect(page.get_by_role("heading", level=1)).to_have_text("Cómo leer Contraste")
    for term in ("sin-etiqueta", "fiabilidad", "agrupacion-automatica"):
        expect(page.locator(f"dt#{term}")).to_be_visible()
    page.goto(base_url + "/como-leer#sin-etiqueta")
    expect(page.locator("dt#sin-etiqueta")).to_be_in_viewport()
    page.goto(base_url + "/")
    page.get_by_role("link", name="Cómo leer", exact=True).first.click()
    page.wait_for_url(re.compile(r"/como-leer$"))


def test_story_page_summary_box_visible_first(page: Page, base_url, story_path):
    page.goto(base_url + story_path)
    box = page.locator(".resumen")
    expect(box).to_be_visible()
    text = box.inner_text()
    for label in ("Qué coinciden en contar", "Qué cambia según el medio", "Verificaciones"):
        assert label in text
    expect(page.get_by_text("Coincidir no prueba que algo sea cierto")).to_be_visible()
    expect(page.get_by_role("link", name="¿Mal agrupada? Avísanos")).to_be_visible()
    assert page.get_by_role("heading", name="Cronología").count() == 1
    assert page.get_by_text("Primera publicación en nuestro índice").count() == 1
    y_box = box.bounding_box()["y"]
    y_panel = page.locator(".panel--summary").bounding_box()["y"]
    assert y_box < y_panel
    assert page.locator(".signal").count() == 0


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
def test_sponsored_mark_is_visible_apart_and_without_overflow(page: Page, base_url, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(base_url + SEARCH)
    box = page.locator("details.sponsored-box")
    expect(box).to_have_count(1)
    expect(box.locator("summary")).to_contain_text("1 pieza marcada como patrocinada por su medio")
    assert box.get_attribute("open") is None  # collapsed, but announced: nothing is hidden silently
    box.locator("summary").click()
    badge = box.locator(".mark-sponsored")
    expect(badge).to_be_visible()
    expect(badge).to_have_text("Marcado como patrocinado por el medio")
    assert badge.get_attribute("title") == "Ruta /branded/"
    expect(page.locator("[data-stories] .mark-sponsored")).to_have_count(0)
    sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw, f"scrollWidth {sw} > clientWidth {cw}"


def test_media_filter_by_speciality(page: Page, base_url):
    page.goto(base_url + "/medios")
    page.select_option("select[data-filter=spec]", "economía")
    assert page.locator("tr[data-row]:visible").count() >= 4
    expect(page.locator("tr[data-row][data-spec='economía']").first).to_contain_text("Especializado en economía")


# --- suggestions while typing ----------------------------------------------------------------------------------
def type_slowly(page: Page, selector: str, text: str) -> None:
    page.locator(selector).click()
    page.locator(selector).press_sequentially(text, delay=20)


def test_typing_in_header_search_shows_listbox(page: Page, base_url):
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.goto(base_url + SEARCH, wait_until="networkidle")
    box = page.locator("#q-top")
    type_slowly(page, "#q-top", "ley de vivi")
    listbox = page.get_by_role("listbox")
    expect(listbox).to_be_visible()
    options = page.get_by_role("option")
    assert options.count() >= 1
    expect(box).to_have_attribute("role", "combobox")
    expect(box).to_have_attribute("aria-expanded", "true")
    assert box.get_attribute("aria-controls") == listbox.get_attribute("id")
    assert re.search(r"\d+ medios? · ", options.first.inner_text())
    assert not problems, problems


def test_no_suggestions_below_three_characters(page: Page, base_url):
    page.goto(base_url + SEARCH, wait_until="networkidle")
    type_slowly(page, "#q-top", "vi")
    page.wait_for_timeout(600)
    expect(page.get_by_role("listbox")).to_be_hidden()


def test_keyboard_down_enter_opens_story(page: Page, base_url):
    page.goto(base_url + "/", wait_until="networkidle")
    type_slowly(page, "#q", "vivienda")
    expect(page.get_by_role("listbox")).to_be_visible()
    page.keyboard.press("ArrowDown")
    first = page.get_by_role("option").first
    expect(first).to_have_attribute("aria-selected", "true")
    assert page.locator("#q").get_attribute("aria-activedescendant") == first.get_attribute("id")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/historia/\d+"))


def test_escape_closes_and_enter_without_selection_searches(page: Page, base_url):
    page.goto(base_url + "/", wait_until="networkidle")
    type_slowly(page, "#q", "vivienda")
    expect(page.get_by_role("listbox")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("listbox")).to_be_hidden()
    expect(page.locator("#q")).to_have_attribute("aria-expanded", "false")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/buscar\?q=vivienda"))


def test_click_outside_closes_and_click_option_navigates(page: Page, base_url):
    page.goto(base_url + "/", wait_until="networkidle")
    type_slowly(page, "#q", "vivienda")
    expect(page.get_by_role("listbox")).to_be_visible()
    page.locator(".tagline").click()
    expect(page.get_by_role("listbox")).to_be_hidden()
    type_slowly(page, "#q", " ")  # reopen by typing again
    expect(page.get_by_role("listbox")).to_be_visible()
    page.get_by_role("option").first.click()
    page.wait_for_url(re.compile(r"/historia/\d+"))


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
def test_open_listbox_does_not_overflow_and_taps_are_large(page: Page, base_url, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(base_url + "/", wait_until="networkidle")
    type_slowly(page, "#q", "vivienda")
    expect(page.get_by_role("listbox")).to_be_visible()
    sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw, f"scrollWidth {sw} > clientWidth {cw}"
    box = page.get_by_role("option").first.bounding_box()
    assert box["height"] >= 44 and box["x"] >= 0 and box["x"] + box["width"] <= width


def test_suggestions_work_in_dark_mode(browser, base_url):
    ctx = browser.new_context(color_scheme="dark")
    page = ctx.new_page()
    page.goto(base_url + "/", wait_until="networkidle")
    type_slowly(page, "#q", "vivienda")
    expect(page.get_by_role("listbox")).to_be_visible()
    bg = page.get_by_role("listbox").evaluate("e => getComputedStyle(e).backgroundColor")
    assert bg != "rgb(251, 249, 244)"  # not the light card colour
    ctx.close()


def test_without_javascript_search_still_works(browser, base_url):
    ctx = browser.new_context(java_script_enabled=False)
    page = ctx.new_page()
    page.goto(base_url + "/")
    assert page.locator("#q").get_attribute("role") is None
    assert page.get_by_role("listbox").count() == 0
    page.fill("#q", "vivienda")
    page.press("#q", "Enter")
    page.wait_for_url(re.compile(r"/buscar\?q=vivienda"))
    assert page.locator("[data-item]").count() > 0
    ctx.close()


def test_no_match_message_only_when_filters_hide_everything(page: Page, base_url):
    page.goto(base_url + SEARCH)
    assert "Ningún titular coincide con los filtros" not in page.content()
    open_filters(page)
    page.locator(".pill[data-pos='0']").click()  # no seeded outlet sits in the centre
    expect(page.get_by_text("Ningún titular coincide con los filtros")).to_be_visible()
    page.locator(".pill[data-pos='all']").click()
    assert page.get_by_text("Ningún titular coincide con los filtros").count() == 0


def test_no_match_message_absent_without_javascript(browser, base_url):
    ctx = browser.new_context(java_script_enabled=False)
    page = ctx.new_page()
    page.goto(base_url + SEARCH)
    assert "Ningún titular coincide" not in page.content()
    assert page.locator("[data-item]").count() > 0
    ctx.close()


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
def test_search_folds_minor_headlines_and_shows_brief(page: Page, base_url, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(base_url + SEARCH)
    brief = page.locator("[data-brief]").first
    expect(brief).to_be_visible()
    expect(brief).to_contain_text("En resumen:")
    expect(brief).to_contain_text("Verificaciones:")
    fold = page.locator("details[data-more]")
    expect(fold).to_be_visible()
    expect(fold.locator("summary")).to_contain_text("Más titulares (")
    assert fold.get_attribute("open") is None
    # the incident story and the single headline are inside the fold, the policy story is above it
    assert "Incendio" not in page.locator("[data-stories] > article.story").first.inner_text()
    fold.locator("summary").click()
    expect(fold.get_by_text("La vivienda turística crece")).to_be_visible()
    expect(fold.locator(".story__title").first).to_be_visible()
    sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw


def test_opinion_badge_is_visible_with_evidence(page: Page, base_url):
    page.goto(base_url + SEARCH)
    page.locator("details[data-more] summary").click()
    badge = page.locator(".mark-opinion").first
    expect(badge).to_be_visible()
    assert "Categoría RSS" in badge.get_attribute("title")


def test_home_positioning_sentence_is_visible(page: Page, base_url):
    page.goto(base_url + "/")
    expect(page.locator(".positioning")).to_contain_text("Contraste no decide qué es verdad")


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 800)])
def test_story_page_majority_block_comes_first(page: Page, base_url, story_path, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(base_url + story_path)
    block = page.locator(".majority")
    expect(block).to_be_visible()
    expect(block).to_contain_text("La noticia, según la mayoría")
    expect(block).to_contain_text("Titular más representativo (el que más comparte con los demás)")
    y_major = block.bounding_box()["y"]
    assert y_major < page.locator(".resumen").bounding_box()["y"]
    sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw
