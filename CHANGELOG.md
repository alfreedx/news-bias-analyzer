# Changelog

Todos los cambios relevantes se documentan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el proyecto usa
[versionado semántico](https://semver.org/lang/es/).

## [1.1.0] - 2026-11-19

### Añadido
- 35 medios nuevos (93 cabeceras en total), sobre todo prensa regional, seleccionados con un criterio fijo: RSS operativo y etiqueta citable o propietario documentado. Nuevos grupos: Edigrup Media y Editorial La Capital.
- Etiquetas citadas: cada etiqueta se muestra con la palabra exacta de su fuente, enlace a la revisión consultada y fecha. Filtro por etiqueta y por ámbito en el catálogo.
- Auditoría de equilibrio en la metodología: etiquetas citadas por lado del espectro.
- Detección semanal de cambios en las fichas de Wikipedia respecto a la revisión fijada.
- Política de rectificación (`RECTIFICATION.md`).
- Pruebas de extremo a extremo con Playwright (32 pruebas) y job en CI.
- Tipografía Newsreader servida desde el propio dominio.

### Cambiado
- «Sin evaluar» pasa a «Sin etiqueta publicada», para no sugerir neutralidad.
- Propiedad mostrada como hecho con su fuente.
- Pulido visual: sin antetítulos, avisos con borde completo, selección, cursor y barras de desplazamiento con la paleta propia.

### Corregido
- `Permissions-Policy` ya no declara funciones que Chromium no reconoce.

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
