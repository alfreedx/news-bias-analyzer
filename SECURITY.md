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

La aplicación funciona sin ningún secreto. La única excepción opcional es `SNAPSHOT_KEY`, la clave Fernet de la instantánea de datos (ver abajo). `.env` está en `.gitignore`; no lo subas nunca.

## Copia de seguridad cifrada (instantánea de datos)

Para no perder el historial cuando Render borra su disco, un workflow de GitHub Actions publica el índice como
asset de la release `data-snapshot` (repositorio público). Por eso:

- **Va cifrada** (`gzip` + Fernet, AES-128-CBC con HMAC-SHA256, paquete `cryptography` fijado y auditado): los
  titulares y resúmenes son de terceros y no deben redistribuirse en bloque. Sin la clave el archivo es ilegible.
- **Sin firmas**: antes de exportar se pone `author = NULL` en la copia y se vuelve a compactar (`VACUUM INTO`)
  para que los nombres no queden en páginas libres. No contiene IPs, búsquedas ni otros datos personales.
- **Respeta `RETENTION_DAYS`**: cada ejecución purga lo antiguo igual que la aplicación.
- **La clave** vive solo en el secreto `SNAPSHOT_KEY` de GitHub Actions y en una variable de entorno privada de
  Render (`sync: false`). Nunca se escribe en el repositorio, en el registro ni en mensajes de error. Para
  rotarla, genera otra, actualiza ambos sitios y lanza el workflow; la instantánea anterior dejará de abrirse y
  el workflow se detendrá con error en lugar de sobrescribirla: borra el asset para empezar de cero.
- **Restauración defensiva**: descarga con límite de 80 MB y 60 s, redirecciones solo a `github.com`,
  `objects.githubusercontent.com` y `release-assets.githubusercontent.com`, descompresión limitada a 300 MB,
  `PRAGMA integrity_check` y comprobación de tablas antes de sustituir la base de forma atómica. Un fallo deja
  la base existente intacta.
