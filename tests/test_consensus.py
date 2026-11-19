from app.analysis import make_article
from app.catalog import get_catalog
from app.consensus import (
    agency_of,
    build_voices,
    consensus,
    extract_dates,
    extract_names,
    extract_numbers,
    extract_pieces,
    extract_quotes,
    extract_terms,
    norm_number,
    representative_headline,
    term_stem,
)

cat = get_catalog()
LEFT = ["eldiario-es", "publico", "infolibre"]
CENTER = ["efe", "ara", "cronica-global"]
RIGHT = ["abc", "el-confidencial", "okdiario", "el-mundo"]
UNRATED = ["cope", "20minutos"]


def art(media_id, title, summary="", n=[0]):
    n[0] += 1
    return make_article(cat, title=title, url=f"https://x.test/{media_id}/{n[0]}", summary=summary,
                        published=1000, media_id=media_id)


def keys(fn, text):
    return {k for k, _ in fn(text)}


# --- extractors -----------------------------------------------------------------------
def test_numbers_with_units_decimals_and_percent():
    got = keys(extract_numbers, "Sube 20 %, 20% y 20 por ciento; 83 dólares; 1,5 billones de euros; 3 muertos")
    assert {"20|%", "83|usd", "1.5|billone", "3|muerto"} <= got


def test_number_decimal_and_thousands_normalisation():
    assert norm_number("1,5") == "1.5" == norm_number("1.5")
    assert norm_number("2.500") == "2500"
    assert norm_number("83,00") == "83"
    assert keys(extract_numbers, "2.500 millones") == keys(extract_numbers, "2500 millones")
    assert keys(extract_numbers, "1,5 millones") == keys(extract_numbers, "1.5 millones")


def test_numbers_symbols_and_unitless_ignored():
    assert "5|eur" in keys(extract_numbers, "cuesta 5€")
    assert "83|usd" in keys(extract_numbers, "$83")
    assert extract_numbers("en 2025 hubo cambios") == []


def test_numbers_accent_variants_match():
    assert keys(extract_numbers, "83 dólares") == keys(extract_numbers, "83 DOLARES")


def test_dates_long_form_weekday_and_tag():
    assert ("29 noviembre", "29 de noviembre") in extract_dates("El lunes 29 de Noviembre de 2026 habrá huelga")
    assert "29-n" in keys(extract_dates, "tras el 29-N")
    assert extract_dates("hay 29 casos") == []


def test_quotes_word_limits_and_styles():
    assert keys(extract_quotes, "dijo «No vamos a ceder nunca»") == {"no vamos a ceder nunca"}
    assert keys(extract_quotes, "dijo “No vamos a ceder nunca”") == {"no vamos a ceder nunca"}
    assert keys(extract_quotes, 'dijo "No vamos a ceder nunca"') == {"no vamos a ceder nunca"}
    assert extract_quotes("«Sí señor»") == []  # fewer than 3 words
    assert extract_quotes("«" + " ".join(["palabra"] * 16) + "»") == []  # more than 15


def test_names_connectors_accents_and_sentence_start():
    got = dict(extract_names("Dimite el gobernador del Banco de España por el caso de Isak Andic"))
    assert "banco de espana" in got and got["banco de espana"] == "Banco de España"
    assert "isak andic" in got
    # a lone capitalised word that opens the sentence is not a name
    assert "dimite" not in got
    assert "sanchez" in dict(extract_names("Anuncia hoy Sánchez un plan"))
    assert "sanchez" not in dict(extract_names("Sánchez anuncia un plan"))
    assert "pedro sanchez" in dict(extract_names("Pedro Sánchez anuncia un plan"))


def test_names_strip_leading_article_and_generic_words():
    assert extract_names("El Gobierno aprueba la ley") == []
    assert extract_names("Hoy el Gobierno de España aprueba") == []
    assert "mundo" not in dict(extract_names("según El Mundo, algo"))
    assert "el mundo" in dict(extract_names("según El Mundo, algo"))


def test_names_conjunction_yields_parts_and_all_caps_headline_is_skipped():
    got = dict(extract_names("Pacto entre Illa y Puigdemont"))
    assert {"illa", "puigdemont"} <= set(got)
    assert extract_names("EL GOBIERNO APRUEBA LA LEY DE VIVIENDA EN ESPAÑA") == []


def test_extract_pieces_kinds():
    kinds = {k for k, *_ in extract_pieces("Pedro Sánchez dijo «no habrá más recortes» el 29 de noviembre: 3 muertos")}
    assert kinds == {"nombre", "cita", "fecha", "cifra"}


# --- voices ---------------------------------------------------------------------------
def test_syndicated_copies_collapse_into_one_voice():
    arts = [art("abc", "Mismo titular"), art("el-correo", "mismo  TITULAR"), art("el-pais", "Otro titular")]
    assert len(build_voices(arts)) == 2


def test_same_group_is_one_voice():
    vocento = ["abc", "el-correo", "diario-vasco"]
    voices = build_voices([art(m, f"Titular distinto {i}") for i, m in enumerate(vocento)])
    assert len(voices) == 1 and len(voices[0].outlets) == 3


def test_agency_detection_and_collapse():
    assert agency_of(art("abc", "Algo (EFE)")) == "EFE"
    assert agency_of(art("abc", "x", "Madrid. Europa Press")) == "Europa Press"
    assert agency_of(art("abc", "x", "según agencias")) == "Agencias"
    assert agency_of(art("abc", "Sin marca")) is None
    arts = [art("abc", "Cifra uno (EFE)"), art("publico", "Cifra dos (EFE)"), art("el-pais", "Cifra tres (EFE)"),
            art("okdiario", "Cifra cuatro (EP)")]
    voices = build_voices(arts)
    assert len(voices) == 2
    agency = next(v for v in voices if v.label == "Agencia EFE")
    assert agency.agency and agency.side is None and len(agency.outlets) == 3


def test_voice_side_is_most_frequent_labelled_side():
    voices = build_voices([art("el-pais", "a b c uno"), art("huffpost", "a b c dos")])
    assert voices[0].side == "left"  # both Prisa, both left
    assert build_voices([art("cope", "x y z")])[0].side is None


# --- consensus ------------------------------------------------------------------------
def spread(summary_for=lambda m: "El paro baja un 12,5 % según los datos.", title=None):
    media = LEFT[:2] + CENTER[:1] + RIGHT[:2]
    return [art(m, title or f"Titular del {m}", summary_for(m)) for m in media]


def test_not_enough_voices_gives_note_state():
    c = consensus([art("eldiario-es", "A b c"), art("abc", "A b d")])
    assert not c.enough and c.pieces == [] and c.voices == 2


def test_consensus_requires_voices_groups_and_sides():
    c = consensus(spread())
    texts = {p.text for p in c.pieces}
    assert "12,5 %" in texts
    piece = next(p for p in c.pieces if p.text == "12,5 %")
    assert piece.kind == "cifra" and piece.voices == 5
    assert piece.sides == ["left", "center", "right"] and "elDiario.es" in piece.outlets


def test_consensus_needs_two_sides():
    arts = [art(m, f"T {m}", "El paro baja un 12,5 % hoy.") for m in RIGHT[:3]]
    c = consensus(arts)
    assert c.enough and c.pieces == []


def test_consensus_threshold_of_three_voices():
    arts = [art(m, f"T {m}", "El paro baja un 12,5 % hoy.") for m in ("eldiario-es", "abc")]
    arts.append(art("publico", "T otro", "Nada que ver"))
    assert consensus(arts).pieces == []  # only two voices share it


def test_unlabelled_voices_count_as_voices_not_sides():
    arts = [art(m, f"T {m}", "El paro baja un 12,5 % hoy.") for m in ("cope", "20minutos", "eldiario-es")]
    c = consensus(arts)
    assert c.enough and c.pieces == []  # 3 voices but a single labelled side


def test_agency_copies_count_once():
    arts = [art(m, f"Sube el paro un 7 % (EFE) {i}") for i, m in enumerate(["eldiario-es", "abc", "publico", "okdiario"])]
    c = consensus(arts)
    assert c.voices == 1 and not c.enough
    assert c.agencies == [("EFE", 4)]


def test_one_sided_pieces():
    arts = [art("eldiario-es", "Uno", "Cita: «recortes de la derecha en sanidad» ayer."),
            art("publico", "Dos", "Cita: «recortes de la derecha en sanidad» ayer."),
            art("abc", "Tres", "Otro asunto."), art("okdiario", "Cuatro", "Otro asunto más.")]
    c = consensus(arts)
    one = [o for o in c.one_sided if o.piece.kind == "cita"]
    assert one and one[0].side == "left" and one[0].side_label == "izquierda" and one[0].piece.voices == 2


def test_one_sided_needs_opposite_side_to_cover_story():
    arts = [art("eldiario-es", "Uno", "Cifra: 40 muertos."), art("publico", "Dos", "Cifra: 40 muertos."),
            art("infolibre", "Tres", "Nada.")]
    assert consensus(arts).one_sided == []


def test_representative_headline_has_highest_average_overlap():
    arts = [art("eldiario-es", "Gobierno aprueba ley vivienda alquiler"),
            art("abc", "Gobierno aprueba ley vivienda"),
            art("okdiario", "Sánchez celebra cumbre europea Bruselas")]
    assert representative_headline(build_voices(arts)).title in (
        "Gobierno aprueba ley vivienda alquiler", "Gobierno aprueba ley vivienda")
    assert consensus(arts).headline.title != "Sánchez celebra cumbre europea Bruselas"


def test_max_pieces_limit():
    summary = " ".join(f"{n} muertos," for n in range(1, 15))
    c = consensus(spread(lambda m: summary))
    assert len(c.pieces) == 8


# --- words and expressions -------------------------------------------------------------
def term_keys(text):
    return {k for k, _ in extract_terms(text)}


def test_terms_phrases_allow_inner_connectors_not_edges():
    got = term_keys("Planeó apartar a su hijo del negocio")
    assert "apartar a su hijo" in got and "hijo del negocio" in got
    assert "apartar a" not in got and "a su hijo" not in got and "su hijo" not in got
    assert "ano sabatico" in term_keys("Se tomó un año sabático") and "ano" not in term_keys("Se tomó un año")


def test_terms_break_at_punctuation_stopwords_and_digits():
    got = term_keys("Sube el paro, baja el empleo: 25 casos nuevos que preocupan")
    assert "paro baja" not in got and "paro" in got  # the comma splits phrases
    assert "empleo" in got
    assert not any("25" in k for k in got)
    assert "empleo 25" not in got and "caso nuevo" not in got  # digits and stopwords split phrases
    assert not any(len(k.split()) > 5 for k in got)


def test_terms_plural_folding_keeps_verbs_apart():
    assert term_stem("terapeutas") == term_stem("terapeuta") == "terapeuta"
    assert term_stem("papeles") == "papel"
    assert term_keys("la terapeuta") & term_keys("las terapeutas") == {"terapeuta"}
    assert "planeo" in term_keys("Planeó apartar") and "planea" in term_keys("Planea apartar")
    assert "planeo" not in term_keys("Planea apartar")


def test_terms_display_in_most_frequent_spelling():
    media = LEFT[:2] + CENTER[:1] + RIGHT[:2]
    texts = ["Dice. Terapeutas. Más", "Dice. Las terapeutas. Más"] + ["Dice. Una terapeuta. Más"] * 3
    arts = [art(m, f"Ya hoy {chr(97 + i) * 2}", t) for i, (m, t) in enumerate(zip(media, texts, strict=True))]
    c = consensus(arts)
    assert next(p for p in c.terms if p.key == "terapeuta").text == "terapeuta"


def story(texts):
    """One article per outlet: texts maps media id -> summary."""
    return [art(m, f"Ya hoy {chr(97 + i) * 2}", t) for i, (m, t) in enumerate(texts.items())]


def test_shared_terms_thresholds_and_sides():
    base = {m: "La terapeuta lo cuenta." for m in ("eldiario-es", "publico", "abc", "okdiario")}
    assert "terapeuta" in [p.key for p in consensus(story(base)).terms]
    two = dict(list(base.items())[:2] + [("abc", "Otra cosa distinta."), ("okdiario", "Algo diferente.")])
    assert consensus(story(two)).terms == []  # only 2 voices
    same_side = {m: "La terapeuta lo cuenta." for m in ("abc", "okdiario", "el-mundo")}
    assert consensus(story(same_side)).terms == []  # only one side
    piece = consensus(story(base)).terms[0]
    assert piece.voices == 4 and piece.sides == ["left", "right"] and "ABC" in piece.outlets


def test_shared_terms_exclude_names_outlets_generic_and_numbers():
    text = "Isak Andic habló. La terapeuta. Gobierno y El Mundo. Sobre 2025. La semana"
    c = consensus(story({m: text for m in ("eldiario-es", "publico", "abc", "okdiario")}))
    keys_ = {p.key for p in c.terms}
    assert "terapeuta" in keys_
    assert not keys_ & {"isak", "andic", "gobierno", "mundo", "2025", "semana"}
    assert not any("isak" in k or "andic" in k or "el mundo" in k for k in keys_)
    assert all(len(p.key) >= 4 for p in c.terms if " " not in p.key)


def test_shared_terms_capitalised_midsentence_words_are_names_not_terms():
    c = consensus(story({m: "Habló la terapeuta con Perico" for m in ("eldiario-es", "publico", "abc", "okdiario")}))
    assert not any("perico" in p.key for p in c.terms)


def test_shared_terms_contained_in_longer_phrase_are_dropped():
    same = {m: "Quiso. Apartar a su hijo. Negocio familiar." for m in ("eldiario-es", "publico", "abc", "okdiario")}
    keys_ = {p.key for p in consensus(story(same)).terms}
    assert "apartar a su hijo" in keys_
    assert "apartar" not in keys_ and "hijo" not in keys_ and "negocio" not in keys_
    # a single word with clearly more voices than the phrase survives
    mixed = story({**same, "infolibre": "Habló del hijo.", "el-mundo": "Un hijo ausente."})
    keys_ = {p.key for p in consensus(mixed).terms}
    assert "hijo" in keys_ and "apartar a su hijo" in keys_


def test_one_sided_terms_need_opposite_coverage_and_no_use_by_other_side():
    arts = story({"eldiario-es": "Un recorte brutal en sanidad.", "publico": "Un recorte brutal hoy.",
                  "abc": "Ajuste necesario en sanidad.", "okdiario": "Ajuste necesario ahora."})
    c = consensus(arts)
    left = {o.piece.key for o in c.one_sided_terms_of("left")}
    right = {o.piece.key for o in c.one_sided_terms_of("right")}
    assert "recorte brutal" in left and "ajuste necesario" in right
    assert "sanidad" not in left | right  # both sides use it
    assert next(o for o in c.one_sided_terms if o.piece.key == "recorte brutal").piece.voices == 2


def test_one_sided_terms_without_two_opposite_voices_are_dropped():
    arts = story({"eldiario-es": "Un recorte brutal.", "publico": "Un recorte brutal.", "infolibre": "Un recorte brutal.",
                  "abc": "Ajuste necesario."})
    assert consensus(arts).one_sided_terms == []
    # center voices count as neither side but a term they use is not exclusive to one side
    arts = story({"eldiario-es": "Un recorte brutal.", "publico": "Un recorte brutal.", "efe": "Un recorte brutal.",
                  "abc": "Ajuste.", "okdiario": "Ajuste."})
    assert not [o for o in consensus(arts).one_sided_terms if o.piece.key == "recorte brutal"]


def test_one_sided_terms_limits_per_side():
    words = "alfa beta gamma delta epsilon zeta theta iota kappa lambda".split()
    text = " ".join(f"termino{w}" for w in words)
    arts = story({"eldiario-es": text, "publico": text, "abc": "Otro asunto.", "okdiario": "Otro asunto."})
    assert len(consensus(arts).one_sided_terms_of("left")) <= 6
