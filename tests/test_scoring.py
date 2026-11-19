from app.scoring import assess, bucket


def ev(source, *labels, kind="ideology"):
    return {"source": source, "kind": kind, "labels": list(labels), "url": "https://example.org"}


def test_bucket_edges():
    assert bucket(-2) == -2
    assert bucket(-1.5) == -2
    assert bucket(-0.5) == -1
    assert bucket(0) == 0
    assert bucket(0.49) == 0
    assert bucket(0.5) == 1
    assert bucket(1.5) == 2


def test_sources_are_averaged_and_agreement_reported():
    a = assess([ev("mbfc", "Right-Center"), ev("wikipedia", "Centroderecha")])
    assert a.score == 1.0
    assert a.label == "Centroderecha"
    assert a.agreement == "Fuentes coinciden"


def test_multiple_labels_in_one_source_are_averaged():
    a = assess([ev("wikipedia", "Liberalismo", "Conservadurismo social", "Anticomunismo", "Derecha")])
    assert a.score == 1.25


def test_territorial_labels_do_not_move_left_right_position():
    a = assess([ev("wikipedia", "Independentismo catalán", "Centrismo")])
    assert a.score == 0
    assert a.territorial == ["Independentismo catalán"]


def test_only_territorial_labels_leaves_outlet_unrated():
    a = assess([ev("wikipedia", "Nacionalismo vasco")])
    assert not a.rated
    assert a.territorial == ["Nacionalismo vasco"]


def test_no_evidence_means_unrated_not_centre():
    a = assess([])
    assert a.position is None
    assert a.label == "Sin etiqueta publicada"
    assert a.factual_label == "Sin evaluación externa"


def test_factual_and_ifcn():
    a = assess([ev("mbfc", "Mixed", kind="factual")])
    assert a.factual_level == 1
    b = assess([ev("wikipedia", "Signatario IFCN", kind="ifcn")])
    assert b.ifcn and b.factual_label.startswith("Verificador")


def test_disagreement_is_flagged():
    a = assess([ev("mbfc", "Left-Center"), ev("wikidata", "Derecha")])
    assert a.agreement == "Fuentes discrepan"
