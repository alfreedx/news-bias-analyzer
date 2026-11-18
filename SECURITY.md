# Política de seguridad

## Cómo informar de una vulnerabilidad

**No abras una incidencia pública.** Usa el aviso privado de GitHub:
pestaña *Security* → *Report a vulnerability* (o `/security/advisories/new`).

Incluye, si puedes, los pasos para reproducirla y su impacto. Intentaremos responder en
7 días. Agradeceremos públicamente tu ayuda si así lo deseas.

## Versiones con soporte

Solo la rama `main`.

## Medidas implantadas

| Riesgo | Medida |
|---|---|
| XSS | Plantillas Jinja2 con autoescape; los textos de los feeds se convierten a texto plano; CSP estricta sin `unsafe-inline` (ni scripts ni estilos en línea). |
| Clickjacking | `frame-ancestors 'none'` y `X-Frame-Options: DENY`. |
| Inyección en la búsqueda | Cada palabra se entrecomilla antes de llegar a FTS5; todas las consultas SQL usan parámetros. |
| XML malicioso (XXE, «billion laughs») | Los feeds se analizan con `defusedxml`. |
| SSRF | Solo se descargan URLs fijas del catálogo versionado; nunca URLs aportadas por el usuario. |
| Feeds enormes o lentos | Límite de 5 MB por respuesta, tiempo máximo de 15 s y como mucho 5 redirecciones. |
| Enlaces peligrosos | Solo se aceptan enlaces `http`/`https`; `javascript:` y similares se descartan. |
| Abuso / denegación de servicio | Límite de búsquedas por minuto y cliente, consultas recortadas a 120 caracteres y caché de resultados. |
| Host header | `TrustedHostMiddleware` con lista de dominios permitidos. |
| Fugas de privacidad | Sin cookies, sin recursos de terceros, `Referrer-Policy: no-referrer`, IPs solo como hash con sal diaria en memoria, búsquedas sin registrar. |
| Dependencias | Versiones fijadas, `pip-audit` en CI y Dependabot semanal. |
| Contenedor | Imagen *slim* y usuario sin privilegios; admite `--read-only` (solo escribe en `/data`). |

## Secretos

La aplicación no necesita ningún secreto. `.env` está en `.gitignore`; no lo subas nunca.
