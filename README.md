# Dashboard de Ventas

Panel personal de comisiones y rappel para agente exclusivo de seguros de salud.
Corre 100% en local — tus datos no salen de tu máquina.

## Qué hace

- Ingiere los ficheros CSV que exporta el portal (Facturación, Pólizas,
  Liquidación) y las facturas PDF mensuales.
- Estima comisión y rappel en tiempo casi real a partir de Facturación +
  Pólizas, marcando siempre el **nivel de confianza** de cada cifra
  (alta / media) — nunca presenta una estimación como un hecho confirmado.
- Reconcilia automáticamente lo estimado contra la Liquidación real en
  cuanto la subes, y detecta pólizas activas que deberían haberse cobrado
  y no aparecen en ningún fichero (alertas).
- Desglose de producción por producto y por provincia.

## Requisitos

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) (recomendado) o `pip` normal

## Instalación

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Clona el repo e instala dependencias:

```bash
git clone https://github.com/sebsv123/dashboard_ventas.git
cd dashboard_ventas
uv sync
```

## Uso

```bash
uv run streamlit run src/dashboard/app.py
```

Se abre en `http://localhost:8501`. Desde la barra lateral:

1. Sube **Facturación** y **Pólizas** cuando quieras (idealmente semanal) —
   te da la vista estimada de producción y rappel en curso.
2. Sube **Liquidación** (CSV) y/o **Factura** (PDF) en cuanto ASISA te
   liquide el mes — confirma los números reales y reconcilia automáticamente.

## Configuración del contrato

Todas las tablas de comisión, tramos de rappel y el % de retención de IRPF
viven en [`config/contrato.yaml`](config/contrato.yaml), no en el código.
Si ASISA cambia condiciones (avisan con 2 meses de antelación según
contrato), edita ese fichero y haz commit — así queda trazabilidad de qué
reglas aplicaban a qué periodo.

## Tests

```bash
uv run pytest
```

Los tests usan fixtures **sintéticas** en `tests/fixtures/` (mismos
encabezados que los ficheros reales de ASISA, datos inventados) — nunca
datos reales de clientes.

## ⚠️ Importante: datos personales

Los ficheros que descargas de ASISA contienen datos personales de tus
clientes (nombres, NIF, direcciones). El `.gitignore` ya excluye la carpeta
`data/` completa por este motivo — **no fuerces la subida de esos ficheros
al repositorio** aunque sea privado. Guárdalos únicamente en `data/raw/`
en tu máquina.

## Estado del proyecto

Fase 1 (ingesta + motor de cálculo + dashboard base) completada. Pendiente
de calibrar con más datos:

- Fórmula exacta del rappel inicial en tramos intermedios (ver nota en
  `src/engine/rappel.py`).
- Mecanismo exacto de regularización mensual de comisión anualizada para
  pólizas de salud con pago mensual (ver `src/engine/comisiones.py`).

Salesforce queda como integración de Fase 2 (pendiente de definir alcance).
