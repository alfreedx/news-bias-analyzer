# Cómo contribuir

¡Gracias! Hay dos tipos de contribución: **datos** (medios, fuentes, propietarios) y **código**.

## Añadir o corregir un medio

Edita `data/media.json`. Reglas:

1. **Toda afirmación necesita una fuente pública enlazable.** Fuentes aceptadas hoy:
   `mbfc` (Media Bias/Fact Check), `wikipedia` (es) y `wikidata`.
2. **Copia las etiquetas tal cual las publica la fuente** (por ejemplo `"Right-Center"` o
   `"Conservadurismo"`). Nunca escribas una posición numérica: se calcula con la tabla de `app/scoring.py`.
3. Si usas una frase de un artículo y no un campo estructurado, añádela literal en `quote`.
4. Si no hay fuentes, deja `"evidence": []`. Es preferible «sin evaluar» a una suposición.
5. Si la etiqueta es nueva y no está en `IDEOLOGY_MAP` o `TERRITORIAL_LABELS`, añádela allí
   en el mismo PR y explica el criterio. Los tests fallan si falta.
6. Para los feeds, usa solo el **RSS oficial** del medio. Si el medio bloquea lectores automáticos, no lo esquives.

Después ejecuta:

```bash
python -m pytest
python scripts/verify_sources.py   # comprueba enlaces y calificaciones de MBFC
```

## Cómo proponer palabras equivalentes

La búsqueda amplía las palabras con `data/synonyms.json` (cada grupo es una lista de términos que los medios usan indistintamente). Para proponer un grupo, abre un PR que lo añada o lo modifique, **un grupo por PR**.

- Escribe los términos sin tildes y en minúsculas; las frases van tal cual («partido popular»). No hace falta añadir el plural.
- Incluye en el PR la justificación con **titulares reales** de al menos dos medios distintos que usen cada término para lo mismo (enlaces o fragmentos).
- Solo equivalentes claros del lenguaje periodístico. No se aceptan términos con carga ideológica o que encuadren la noticia (p. ej. «okupa» e «inquilino», «ilegal» e «irregular», «golpe» y «proceso», «terrorista» y «militante»): `tests/test_search_expansion.py` tiene una lista de pares prohibidos; añade ahí los que detectes.
- Evita palabras muy genéricas o que coincidan con nombres propios («pau», «luz»).
- Añade, si ayuda, un campo `note` al grupo (`{"words": [...], "note": "..."}`) y actualiza `updated`.
- Comprueba el efecto con `python scripts/eval_search.py` y añade, si puedes, un caso a `tests/search_cases.json`.

## Código

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # en Windows: .venv\Scripts\pip
.venv/bin/ruff check app tests scripts
.venv/bin/python -m pytest
```

Principios del proyecto:

- **Privacidad primero**: nada de cookies, analítica ni recursos de terceros en el navegador.
- **Sin estilos ni scripts en línea** (lo impide la CSP). Usa clases CSS.
- **La página funciona sin JavaScript**; el JS solo añade comodidad.
- **Transparencia**: si algo es heurístico, la interfaz lo dice.

Al enviar un PR aceptas que tu código se publique bajo licencia MIT y tus datos bajo CC BY-SA 4.0.
