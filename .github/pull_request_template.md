## Qué cambia

<!-- Resumen breve del cambio y su motivo. -->

## Tipo

- [ ] Datos (medios, fuentes, propietarios)
- [ ] Corrección de errores
- [ ] Nueva funcionalidad
- [ ] Documentación / mantenimiento

## Comprobaciones

- [ ] `ruff check app tests scripts` sin errores
- [ ] `python -m pytest` pasa
- [ ] Si cambian datos: cada afirmación enlaza a una fuente y `python scripts/verify_sources.py` pasa
- [ ] Sin estilos ni scripts en línea, sin recursos de terceros en el navegador
