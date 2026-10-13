"""Páginas del dashboard. Cada una expone `render(filtros)`.

    overview.py    volumen, calidad del dato, top errores, series y heatmap
    events.py      tabla paginada y visor de la línea cruda
    incidents.py   recurrencia en la ventana y horas anómalas
    settings.py    configuración efectiva, auditoría de `ingest_runs` y acciones

Se importan explícitamente desde `app.py` y no se descubren del directorio: el
orden del menú se decide en un solo sitio, no por el orden alfabético de los
archivos.
"""
