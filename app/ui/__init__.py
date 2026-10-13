"""Dashboard Streamlit.

Módulos y separación de responsabilidades:

    app.py            entrada (`streamlit run app/ui/app.py`): navegación, filtros, auto-seed
    theme.py          paleta por severidad, CSS y formato de números
    data.py           consultas con filtros — **sin** Streamlit, testeables a mano
    cache.py          caché de Streamlit + conexiones de corta vida
    components/       filtros, KPIs, gráficos Plotly y tablas
    pages/            Overview · Events · Incidents · Settings

La regla que hace testeable el paquete: `data.py` no importa Streamlit. Todo lo
que decide *qué SQL corre* vive ahí y se prueba con `unittest`; lo que decide
*cómo se ve* vive en `components/` y `pages/`.
"""
