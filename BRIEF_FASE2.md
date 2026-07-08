# Fase 2 — Dashboard de Ventas (encargo para Claude Code)

Este documento es el encargo de trabajo completo. Léelo entero antes de tocar
código. El proyecto ya tiene una Fase 1 funcional (ingesta, motor de
comisiones/rappel, dashboard Streamlit) construida y validada contra datos
reales del agente (Sebastián, agente exclusivo ASISA). Corre `uv run pytest`
primero para confirmar que partes de una base verde (15 tests deben pasar).

## Contexto de negocio (no re-derivar, ya está validado con datos reales)

- Comisión Vida: prima del recibo × % (Anexo I ASISA VIDA), cada recibo
  cobrado. Sin rappel. Confianza alta.
- Comisión Salud prepago anual (`forma_pago='A'` en Pólizas): comisión
  íntegra = prima anual × % primer año, en el mes de la fecha de EFECTO
  (¡ojo!, la columna `FECHA ALTA` del CSV de Pólizas es en realidad la fecha
  de EFECTO, no la de emisión — la emisión real está en `FECHA GRAB`).
  Confianza alta.
- Comisión Salud mensual (`forma_pago='M'`): ASISA anticipa la comisión
  anualizada completa en el primer recibo cobrado; el mecanismo exacto de
  regularización de los 11 meses siguientes NO está cerrado al 100%
  (ver `src/engine/comisiones.py`, docstring). Confianza media siempre.
- Rappel inicial: fórmula en `src/engine/rappel.py`, calibrada con certeza
  en los extremos (300€/1.200€) pero no en tramos intermedios.
- Retención IRPF 15% sobre (comisiones + rappel) juntos.
- El fichero `config/contrato.yaml` es la ÚNICA fuente de verdad de
  porcentajes — nunca hardcodear un % en el código.

## Bug ya identificado y corregido (verificar que sigue aplicado)

`src/db/schema.py`, función `conectar()`: debe usar
`sqlite3.connect(db_path, check_same_thread=False)` — Streamlit ejecuta
callbacks en hilos distintos al que crea la conexión cacheada.

---

## TAREA 1 (prioridad alta): cerrar el TODO de "año 2 en adelante"

En `src/engine/comisiones.py`, la función `estimar_comision_poliza` tiene
`es_primer_anio = True` hardcodeado con un comentario TODO. Esto es
importante porque afecta directamente a "cuánto voy a cobrar": una póliza
de salud que ya lleva más de 12 meses activa cobra el % de "mantenimiento"
del segundo año en adelante, no el % de producción del primer año — si no
se corrige esto, el motor SOBREESTIMA la comisión de pólizas antiguas.

Implementar:
- Comparar `fila_poliza["fecha_efecto"]` contra la fecha de referencia
  (parámetro nuevo `fecha_referencia: date`, con default `date.today()`).
- Si han pasado 12 meses o más desde la fecha de efecto → usar
  `segundo_anio_en_adelante` (Salud) en vez de `primer_anio`.
- Para Vida, revisar si `comisiones_vida` en el YAML tiene distinción de
  año (algunos productos sí, como AV Accidentes Compromiso 10 con
  primer/segundo/tercer año — ver `config/contrato.yaml`); si el producto
  no tiene distinción, usar el único valor disponible.
- Actualizar/añadir tests en `tests/test_comisiones.py` cubriendo: póliza
  con 6 meses de antigüedad (primer año) y póliza con 14 meses (segundo
  año en adelante), para Salud y para el producto Vida con escalado.

## TAREA 2: proyección de cierre de mes ("ritmo de venta")

Nueva función en `src/engine/rappel.py` o módulo nuevo
`src/engine/proyeccion.py`:

```
proyectar_cierre_mes(produccion_acumulada_hasta_hoy, dia_actual_del_mes,
                      dias_totales_del_mes) -> proyeccion_fin_de_mes
```

Regla simple y explicable (nada de ML): regla de tres sobre el ritmo diario
medio de producción del mes en curso, proyectado a los días que quedan.
Devolver también el nivel de rappel que tocaría si se mantiene ese ritmo
(reutilizar `calcular_rappel_inicial`). Mostrar esto en el dashboard, en la
pestaña Rappel, cuando el periodo seleccionado sea el mes en curso (no
tiene sentido para meses ya cerrados). Redactar el mensaje con tono
motivador pero honesto: "a este ritmo, cerrarías el mes con ~X€ de
producción, lo que te daría un rappel estimado de Y€" — nunca como certeza.

Tests: casos con ritmo bajo, medio, y por encima del objetivo.

## TAREA 3: nueva pestaña "📈 Insights"

Añadir una quinta pestaña al dashboard (`src/dashboard/app.py`) con
histórico y tendencias, calculado siempre sobre TODO lo que haya en la
base de datos (ignora el selector de periodo, como la pestaña Alertas):

1. **Evolución mensual de producción** (gráfico de líneas): nº de pólizas
   nuevas y prima anualizada total, mes a mes, separando Salud/Vida.
2. **Ranking de productos**: qué producto (`razon_social`) genera más
   pólizas y más prima, histórico completo.
3. **Ranking de provincias**: igual que arriba pero por `provincia_tomador`.
4. **Alertas de cambio de tarifa**: pólizas de salud mensual que están a
   menos de 60 días de cumplir su primer año (es decir, van a pasar de %
   producción a % mantenimiento) — esto es información accionable real
   (saber que la comisión de una póliza va a bajar pronto).
5. **Mes a mes, variación %**: producción de este mes vs. el mes anterior
   (↑/↓ y porcentaje), para ver si la tendencia es de crecimiento.

Todo con gráficos `plotly`, reutilizando el azul corporativo `AZUL_ASISA`
ya definido en `app.py`. Si hay menos de 2 meses de datos, mostrar un aviso
de "necesitas más histórico para ver tendencias" en vez de un gráfico vacío
o engañoso con un solo punto.

## TAREA 4: tabla de snapshots mensuales (para que las tendencias mejoren con el tiempo)

Nueva tabla SQLite `resumen_mensual` (añadir a `src/db/schema.py`) que
guarde, cada vez que se recalculan los datos, un snapshot por
periodo: producción total, nº pólizas nuevas, rappel real (si existe),
comisión neta real (si existe). Esto evita recalcular todo el histórico
desde cero cada vez y es la base de datos sobre la que crecen los
insights de la TAREA 3 a medida que Sebastián suba más meses.

## Criterios de aceptación

- `uv run pytest` sigue en verde, con las nuevas tests añadidas (no bajar
  cobertura de las mecánicas ya validadas).
- `uv run streamlit run src/dashboard/app.py` arranca sin errores, con y
  sin datos cargados.
- Ningún porcentaje ni tramo de rappel hardcodeado fuera de
  `config/contrato.yaml`.
- Todo el texto de la UI en español, mismo tono que el resto del proyecto:
  claro sobre qué es una estimación (confianza media) y qué es un hecho
  confirmado (confianza alta / dato real de Liquidación).
- Commits pequeños y descriptivos por tarea (no un único commit gigante).

## Fuera de alcance en esta fase

- Integración con Salesforce (fase 3, sin definir todavía).
- Modelos de machine learning / predicción "inteligente" — de momento el
  proyecto usa proyecciones estadísticas simples y explicables a propósito.
