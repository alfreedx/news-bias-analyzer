# Changelog

Todos los cambios relevantes se documentan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el proyecto usa
[versionado semántico](https://semver.org/lang/es/).

## [Unreleased]

### Añadido
- Página `/como-leer` («Cómo leer Contraste»): qué hace y qué no hace el sitio y glosario con anclas (izquierda … derecha, sin etiqueta, grupo de comunicación, fiabilidad, verificador, fuente independiente, titulares sindicados, noticias de un lado, agrupación automática). Enlazada desde la cabecera, la portada, el pie y los paneles; las etiquetas y la fiabilidad llevan una definición en `title`.
- Página de historia: caja «En resumen» (qué coinciden en contar, qué cambia según el medio, verificaciones) con la advertencia «Coincidir no prueba que algo sea cierto», aviso de agrupación automática con enlace a una incidencia de GitHub prefijada (solo dirección y título) y «Cronología» por hora del RSS.
- Búsqueda: `amplio=1` para incluir coincidencias solo en la entradilla; marca «en catalán/gallego/euskera/asturiano» en la firma de medios que no publican en español; pie de página «Contraste no sustituye leer la noticia completa».

### Cambiado
- Lenguaje llano: «Radiografía de la cobertura» pasa a «Quién la cuenta»; «Puntos ciegos» a «Noticias que cuenta sobre todo un lado»; «Contada desde ambos lados» a «La publican medios de izquierda y de derecha»; fracciones sustituidas por frases.
- La barra de reparto muestra el tramo «sin etiqueta» (rayado) y la frase resumen indica la base («Calculado sobre 107 de 210 titulares…») y avisa si menos de la mitad tienen etiqueta.
- Relevancia: las coincidencias en el titular pesan diez veces más que en la entradilla; con 15 o más titulares coincidentes se ocultan por defecto los que solo coinciden en la entradilla.
- Búsqueda: posición, grupo, vista y orden quedan dentro de «Filtrar y ordenar» (plegado). En listas se muestra como máximo una etiqueta citada y «+N» enlaza a la ficha del medio.
- Las verificaciones de la historia pasan a la caja «En resumen».

### Eliminado
- Las «señales del titular» (cebo, lenguaje cargado, exclamaciones, mayúsculas): eran una opinión algorítmica sin fuente citada. Se elimina `headline_signals` y el campo `signals` de la API.

## [1.2.0] - 2026-11-19

### Añadido
- Historias persistentes: tablas `stories` y `story_articles` (migración idempotente), asignación incremental tras cada ingesta (titulares de las últimas 72 h; misma regla de similitud que el agrupado de búsquedas; título = titular más central; sin historias de un solo titular) y limpieza con la retención. Nueva página `/historia/{id}` con radiografía de cobertura, «Cómo lo cuenta cada lado», titulares y verificaciones; la portada y la búsqueda enlazan a ella cuando la historia está guardada.
- «En lo que coinciden» (`app/consensus.py`): cifras, nombres, citas y fechas que repiten al menos 3 fuentes independientes de 2 o más grupos y 2 o más lados, «Solo lo cuenta un lado» y titular representativo, con reglas transparentes (sin IA ni dependencias nuevas). Copias, grupos y agencias cuentan como una sola voz. Explicado en `/metodologia#coincidencias`.
- «Palabras y expresiones compartidas» y «Palabras que solo usa un lado» en la página de historia: palabras de contenido y expresiones de 2 o 3 palabras (con conectores internos, plural simplificado) que usan al menos 3 voces independientes de 2 o más grupos y lados, y las que solo usa un lado mientras el opuesto cubre la historia. Excluyen nombres coincidentes, medios, palabras genéricas y números. Explicado en `/metodologia#coincidencias`.
- `robots.txt` permite `/historia/`.
- Ficha de cada medio con hechos de Wikidata (CC0), cada uno con enlace y fecha de consulta: fundación, tipo, lengua, sede, propiedad/editor, ámbito y titularidad. Los datos se guardan en `data/media.json` bajo `facts`; solo se escribe lo que Wikidata publica.
- `scripts/enrich_media.py`: obtiene y actualiza esos datos (lotes, pausa de 1 s, reintentos con espera ante 403/429).
- Clasificaciones derivadas y documentadas en `/metodologia`: titularidad pública, sin grupo registrado en el catálogo, propiedad de una institución religiosa y lengua. Nunca se usan como adjetivos editoriales.
- Filtros por lengua y por titularidad en `/medios`, y faceta «Eje territorial» (a partir de etiquetas ya citadas).
- La verificación semanal de fuentes compara también los hechos de Wikidata y avisa si han cambiado.
- Firmas: junto a cada titular se muestra «Por <nombre>» tal como lo publica el RSS del medio (`dc:creator`, `author`, Atom), con enlace a `/buscar?autor=…`, que lista los titulares con esa firma y cuántos hay por medio. Solo nombre y medio: sin perfiles, biografías, fotos ni orientación. Columna `author` con migración automática de bases de datos antiguas; se borra junto con el titular.
- Titulares sindicados (mismo titular en varios medios) colapsados en una sola fila.
- Ranking por diversidad de cobertura (medios, grupos y lados).
- Bloque «Cómo lo cuenta cada lado» con un titular representativo por lado.
- Resumen en lenguaje llano de la cobertura de cada búsqueda.
- «Puntos ciegos» en la portada: historias cubiertas casi solo por medios de un lado.
- Despliegue gratuito en Render: `render.yaml` (Docker, HTTPS, despliegue automático) y botón en el README. El nombre que asigna Render se admite automáticamente en `ALLOWED_HOSTS` y como `SITE_URL`.

### Cambiado
- `/privacidad` explica el tratamiento de las firmas y cómo pedir su retirada.
- Las páginas de firma llevan `noindex` para que Contraste no funcione como buscador de personas.
- Los medios sin grupo aparecen como «(sin grupo)» en lugar de «(independiente)», que sería un juicio.

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
