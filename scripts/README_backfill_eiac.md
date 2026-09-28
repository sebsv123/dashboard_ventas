# Backfill quirúrgico de Wanderlust

La utilidad planifica exclusivamente tres campos de EIAC POLI. No reimporta
XML ni modifica la lógica de negocio. El snapshot anterior del 26/09/2026
(SHA BD `ab0ac255...`) y su manifiesto (SHA `ca52e5a0...`) son evidencia
histórica y no sirven para ejecutar el plan actual. El nuevo snapshot es
`2e9a493999e608b525a428c2980ea5a32b4759ceef29de80ba84a6089c97a61f`;
los cortes de conciliación son 28/09/2026 y 31/08/2026.

## Generar un plan de solo lectura

```bash
uv run python scripts/backfill_eiac_wanderlust.py --dry-run
```

El artefacto nuevo se guarda en `data/processed/eiac_wanderlust_backfill_manifest_2026-09-28.json`,
directorio ya ignorado por Git, con permisos 0600. No se sobrescribe un manifiesto
existente. La única escritura del dry-run es este artefacto solicitado; BD y XML
se leen. Las coberturas se representan mediante hash y número de elementos para
no incluir nombres de asegurados. Apply las recupera del XML cuyo hash verifica.

## Apply preparado, pendiente de autorización

`--apply` requiere manifiesto, backup y exportación explícitos. **No ejecutar
durante la fase de protecciones.** Parar antes las escrituras del dashboard.

La aplicación exige el mismo snapshot y los mismos XML/configuración, vuelve a
generar el plan y lo compara íntegramente. Verifica el backup binario, abre
`BEGIN IMMEDIATE`, usa UPDATE por campo con `id_poliza = ? AND campo IS NULL`,
valida 97 filas/195 celdas del nuevo manifiesto, todas las filas/campos protegidos, otras tablas,
integridad SQLite y PAE antes del COMMIT. Cualquier discrepancia previa revierte.
La exportación Excel se prepara en memoria dentro de la transacción, con fecha
28/09/2026 para compararla con la referencia. Después del COMMIT se escribe el
Excel y se devuelve el SHA posterior. Un fallo de exportación posterior al COMMIT
se informa explícitamente: ya no es posible prometer ROLLBACK; queda el backup.

La suite y los controles de integración deben estar verdes antes de aplicar.
Los tests de escritura utilizan exclusivamente SQLite en memoria. El test del
snapshot privado omite la integración si la BD no existe; si existe pero cambió
su SHA, falla antes de consultar sus tablas.
