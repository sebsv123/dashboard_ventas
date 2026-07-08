"""Proyección de cierre de mes ("ritmo de venta").

Regla simple y explicable (nada de ML ni estadística compleja): regla de
tres sobre el ritmo diario medio de producción del mes en curso, proyectado
a los días que quedan. Sirve para dar una idea de "a este ritmo, ¿cómo
cerraría el mes?" — nunca se presenta como un hecho, siempre como una
estimación (ver `mensaje`, que nunca afirma con certeza el resultado final).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from engine.config_contrato import ContratoConfig
from engine.rappel import ResultadoRappelInicial, calcular_rappel_inicial


@dataclass
class ProyeccionCierreMes:
    produccion_proyectada: float
    ritmo_diario: float
    dias_transcurridos: int
    dias_totales: int
    rappel_proyectado: ResultadoRappelInicial
    mensaje: str


def proyectar_cierre_mes(
    produccion_acumulada_hasta_hoy: float,
    dia_actual_del_mes: int,
    dias_totales_del_mes: int,
    contrato: ContratoConfig,
    fecha_referencia: date,
    produccion_mes_vida: float = 0.0,
) -> ProyeccionCierreMes:
    """Proyecta la producción de fin de mes y el rappel que le correspondería.

    `dia_actual_del_mes` y `dias_totales_del_mes` permiten pasar el día real
    del calendario o, en pruebas, cualquier ventana equivalente.
    """
    if dia_actual_del_mes <= 0:
        raise ValueError("dia_actual_del_mes debe ser >= 1")

    ritmo_diario = produccion_acumulada_hasta_hoy / dia_actual_del_mes
    produccion_proyectada = round(ritmo_diario * dias_totales_del_mes, 2)

    rappel_proyectado = calcular_rappel_inicial(
        contrato,
        fecha_referencia=fecha_referencia,
        produccion_mes_salud=produccion_proyectada,
        produccion_mes_vida=produccion_mes_vida,
    )

    objetivo = rappel_proyectado.objetivo_mes
    if not objetivo:
        mensaje = (
            f"A este ritmo, cerrarías el mes con ~{produccion_proyectada:,.0f}€ de "
            f"producción, lo que te daría un rappel estimado de "
            f"~{rappel_proyectado.importe:,.0f}€."
        )
    elif produccion_proyectada >= objetivo:
        mensaje = (
            f"A este ritmo vas a superar el objetivo del tramo: cerrarías el mes "
            f"con ~{produccion_proyectada:,.0f}€ de producción, lo que te daría un "
            f"rappel estimado de ~{rappel_proyectado.importe:,.0f}€. Sigue así."
        )
    else:
        pct = produccion_proyectada / objetivo * 100
        mensaje = (
            f"A este ritmo, cerrarías el mes con ~{produccion_proyectada:,.0f}€ de "
            f"producción ({pct:.0f}% del objetivo del tramo), lo que te daría un "
            f"rappel estimado de ~{rappel_proyectado.importe:,.0f}€. Todavía puedes "
            f"acelerar el ritmo para mejorar esa cifra."
        )

    return ProyeccionCierreMes(
        produccion_proyectada=produccion_proyectada,
        ritmo_diario=round(ritmo_diario, 2),
        dias_transcurridos=dia_actual_del_mes,
        dias_totales=dias_totales_del_mes,
        rappel_proyectado=rappel_proyectado,
        mensaje=mensaje,
    )
