#!/usr/bin/env bash
# Lanza el Panel Sebastián (dashboard de ventas) y lo abre en el navegador.
# Pensado para ejecutarse desde el icono de escritorio; también puedes
# correrlo a mano: ./iniciar_dashboard.sh

set -e
cd "$(dirname "$0")"

PUERTO=8501

# Si ya hay un panel corriendo (p.ej. otro doble clic al icono sin haber
# cerrado el anterior), no lanzar un segundo servidor -- dos procesos
# escribiendo a la vez en data/asisa.db provocan errores de "database is
# locked". Nos limitamos a abrir el navegador sobre el que ya está vivo.
if curl -s -o /dev/null -m 1 "http://localhost:${PUERTO}"; then
  echo "El Panel Sebastián ya está corriendo — abriendo el navegador."
  xdg-open "http://localhost:${PUERTO}" >/dev/null 2>&1
  exit 0
fi

echo "Arrancando el Panel Sebastián..."
echo "(esta ventana debe quedar abierta mientras uses el panel; ciérrala o pulsa Ctrl+C para apagarlo)"
echo

# Abre el navegador un par de segundos después de lanzar el servidor,
# en vez de depender de que Streamlit lo haga solo (con --server.headless
# no lo hace, así que lo forzamos aquí para tener un único punto de control).
(
  sleep 3
  xdg-open "http://localhost:${PUERTO}" >/dev/null 2>&1
) &

uv run streamlit run src/dashboard/app.py --server.headless true --server.port "${PUERTO}"
