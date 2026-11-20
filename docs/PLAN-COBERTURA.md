# Plan de cobertura total

Objetivo: que Contraste indexe **todas las noticias que se puedan indexar legalmente** de los medios
españoles, manteniendo las cuatro condiciones del proyecto: **gratuito, seguro, privado y sostenible**.

«Todas» se entiende de forma realista: todos los medios españoles con identidad verificable que
publican sus noticias de forma abierta (RSS/Atom o *sitemaps* de noticias permitidos por su
`robots.txt`). Quedan fuera, por diseño, el texto completo (derechos de autor), el contenido tras
muro de pago y los medios que pidan no ser indexados.

## Punto de partida (noviembre de 2026)

| | Hoy |
|---|---|
| Medios | ~95 + 4 verificadores, alta manual |
| Titulares | ~6.000 vivos, retención 120 días |
| Alojamiento | Render gratuito (512 MB RAM, disco efímero) + instantánea cifrada en GitHub |
| Coste | 0 € |

## Principios que no cambian

1. **Solo metadatos públicos**: titular, entradilla corta del RSS, fecha, firma y enlace. Nunca texto
   completo. Encaja con el límite de «fragmentos muy breves» del derecho de los editores de prensa
   (art. 32.2 LPI / art. 15 Directiva 2019/790).
2. **Puerta de entrada igual para todos**: feed que funcione + identidad o propietario documentado.
   Las etiquetas ideológicas siguen siendo solo citas de fuentes externas.
3. **Rastreo educado**: una petición condicional por feed cada 10–30 min, `User-Agent` identificable con
   URL del proyecto, respeto de `robots.txt`, `Retry-After` y 429/503, y **baja inmediata** de quien la pida.
4. **Privacidad**: ningún tercero en el navegador, sin cookies, sin registro de búsquedas.
5. **Nada de pago**. Si algún día se quiere un dominio propio (~10 €/año), es opcional.

## Fases

### Fase 1 — Descubrimiento automático de medios (objetivo: 300 medios)

Estado: **hecho** (código, pruebas y workflow) / **pendiente** (decisión humana o dato externo).

- [x] **Lista de candidatos** generada cada semana por GitHub Actions (`discover-media.yml`, lunes 05:23 UTC):
  - [x] Wikidata (SPARQL): periódicos, webs de noticias, agencias, emisoras, canales y revistas de actualidad con
    país o lugar de publicación en España, web oficial y sin fecha de disolución (`scripts/discover_media.py`).
  - [x] Autodescubrimiento de feeds en la portada (`<link rel="alternate">`, `/feed`, `/rss`…), con `robots.txt`
    y como máximo 3 peticiones por sitio.
  - [ ] Verificadores firmantes de la IFCN en España (hoy los 4 del catálogo se mantienen a mano; falta una fuente
    abierta y estable de la lista de firmantes).
- [x] **Validación automática** de cada candidato: feed con 5 o más titulares y alguno de los últimos 7 días,
  enlaces en su propio dominio, idioma de España, propietario o identidad en Wikidata/Wikipedia, ni agregador ni
  duplicado. Informe completo con los motivos en `data/candidates.json`.
- [x] **Alta por *pull request***: el workflow abre un PR con los candidatos que pasan (`bot/media-candidates`);
  la revisión humana es obligatoria y el alta se hace con `discover_media.py --apply` (ver `CONTRIBUTING.md`).
- [ ] **Revisión humana del primer lote** y alta de los candidatos elegidos (llevar el catálogo hacia 300 medios).
- [ ] **Feeds de sección** (economía, local, deportes…) de los medios ya incluidos, deduplicando por URL.
- Ajustes técnicos necesarios:
  - [x] Agrupado de historias: candidatos por palabras poco frecuentes (principio del palomar, exacto: mismos
    grupos que antes; 1,5–2 veces más rápido con 12.000–60.000 titulares). MinHash no hace falta todavía.
  - [x] Desactivación automática de feeds muertos: tras 20 fallos seguidos, pausa de 24 h con retroceso
    exponencial (hasta 7 días); visible en `/estado`. [ ] Aviso en un issue (aún no hace falta).
  - [x] Cifrado **por bloques** de 4 MB de la instantánea (ya no se descifra entera en memoria; lee el formato anterior).
  - [x] Ingesta con concurrencia configurable (`INGEST_CONCURRENCY`, 8) y máximo 2 peticiones por servidor.
  - [x] Página pública `/estado`.


Con ~300 medios el índice ronda 300.000 titulares en 120 días (~300 MB). Sigue cabiendo en Render
gratuito y en la instantánea de GitHub (límite de 2 GB por archivo).

### Fase 2 — Servidor gratuito y persistente (objetivo: 1.000+ medios, incluidos locales)

Render gratuito deja de bastar (memoria y disco). Opción elegida: **Oracle Cloud Always Free**
(máquina ARM de hasta 4 núcleos y 24 GB de RAM, 200 GB de disco, sin coste permanente).

- Despliegue con el mismo `Dockerfile` + **Caddy** (HTTPS automático) en `docker compose`.
- Actualizaciones de seguridad automáticas (`unattended-upgrades`), cortafuegos solo 80/443,
  acceso SSH únicamente con clave, sin contraseña.
- Copias de seguridad cifradas: la instantánea de GitHub (como hoy) y, además, Object Storage
  gratuito de Oracle (20 GB).
- Render se mantiene como **réplica de reserva** que arranca desde la instantánea si el servidor cae.
- Riesgo conocido: Oracle puede reclamar instancias gratuitas **inactivas**. Mitigación: convertir la
  cuenta a «Pay As You Go» conservando solo recursos *Always Free* (sigue costando 0 €, pero exige
  tarjeta para verificar) y vigilar el consumo con alertas de presupuesto a 0 €.

### Fase 3 — Cobertura de lo que no tiene RSS

- **Sitemaps de noticias** (`news:news`) de medios sin RSS, solo si su `robots.txt` lo permite.
- **GDELT** (gratuito, abierto) únicamente como **detector de huecos**: qué medios españoles publican
  y no están en el catálogo. No se muestra su contenido; sirve para proponer altas.
- **Retención por niveles**: detalle completo 120 días; después solo el resumen de cada historia
  (medios, lados, fechas), sin entradillas, para mantener el tamaño estable años.

## Sostenibilidad

- **Todo automatizado y visible**: página pública `/estado` (feeds activos, caídos, última lectura,
  tamaño del índice) y issues automáticos cuando algo falla.
- **Mantenimiento mínimo**: Dependabot, verificación semanal de fuentes, tests y despliegue solo si
  todo pasa (ya existe).
- **Comunidad**: plantillas de issue para proponer medios y corregir datos; política de rectificación.
- **Sin dependencia de una persona**: código MIT, datos CC BY-SA, catálogo en el repositorio y
  procedimientos documentados (`SECURITY.md`, este plan).
- **Coste vigilado**: nada de servicios con tarjeta sin límite; alertas de presupuesto a 0 €.

## Riesgos y respuesta

| Riesgo | Respuesta |
|---|---|
| Un medio no quiere aparecer | Baja inmediata y lista de exclusión pública |
| Reclamación de derechos | Solo fragmentos breves + enlace; retirada rápida documentada |
| Bloqueo por exceso de peticiones | Peticiones condicionales, frecuencia adaptativa por feed, respeto de `Retry-After` |
| El agrupado empeora con más volumen | Bloqueo/MinHash + batería de búsquedas de referencia (`tests/search_cases.json`) |
| Pérdida del servidor gratuito | Réplica en Render + instantánea cifrada |

## Orden de trabajo propuesto

1. Fase 1: código hecho (descubrimiento, validación, PR automáticos, agrupado por palabras raras, cifrado por bloques, feeds muertos); falta la revisión humana de los candidatos.
2. Alertas (issues automáticos) cuando un feed lleve días caído; la página `/estado` ya existe.
3. Migración a Oracle Always Free cuando el índice supere ~400 MB o la RAM de Render se quede corta.
4. Fase 3 según los huecos detectados.
