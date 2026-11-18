# Changelog

Todos los cambios relevantes se documentan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el proyecto usa
[versionado semántico](https://semver.org/lang/es/).

## [1.0.0] - 2026-11-18

Reescritura completa del prototipo.

### Añadido
- Índice propio de noticias (SQLite FTS5) alimentado por los RSS oficiales de más de 40 medios y verificadores.
- Catálogo versionado de 58 medios y 18 grupos con evidencias enlazadas (Media Bias/Fact Check, Wikipedia, Wikidata).
- Cálculo transparente de orientación ideológica y fiabilidad con tablas públicas (`app/scoring.py`).
- Agrupación de titulares sobre el mismo hecho, radiografía de cobertura, avisos de punto ciego y de concentración.
- Verificaciones relacionadas (Maldita.es, Newtral, EFE Verifica) y señales heurísticas del titular.
- Páginas de medios, grupos, metodología, privacidad y acerca de; API JSON.
- Seguridad: CSP estricta, `defusedxml`, consultas parametrizadas, limitador de peticiones sin guardar IPs.
- Docker, CI (lint, tests, `pip-audit`, construcción y prueba de la imagen), Dependabot y verificación semanal de fuentes.

### Eliminado
- Interfaz Streamlit y dependencia de Supabase.
