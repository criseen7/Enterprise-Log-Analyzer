## Descripción

Plataforma de observabilidad y análisis de logs orientada a entornos empresariales. El sistema permite cargar y procesar logs automáticamente, identificar errores críticos mediante Regex, detectar patrones recurrentes, generar estadísticas operacionales y visualizar métricas en dashboards interactivos. Incluye exportación de reportes y despliegue mediante Docker.

## Estructura

Enterprise-Log-Analyzer/
│
├── app/
│   ├── parser/
│   ├── database/
│   ├── dashboard/
│   ├── alerts/
│   └── reports/
│
├── logs/
├── data/
├── exports/
├── tests/
│
├── app.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md

## Arquitectura

logs/
   app.log
   server.log
   weblogic.log

        ↓

[ Parser Engine ]
 Regex + Pandas

        ↓

[ SQLite Database ]

        ↓

[ Analysis Engine ]
 - Error detection
 - Pattern detection
 - Metrics
 - Alerts

        ↓

[ Streamlit Dashboard ]

## Funcionalidades
1. Parser automatico inteligente:
    Detectar automáticamente:
        timestamp
        nivel
        mensaje
        servicio

2. Detección de errores críticos:
    timeout
    connection refused
    null pointer
    out of memory
    failed login

3. Dashboard real
    Con:
        errores por hora
        top errores
        frecuencia
        timeline
        heatmap

4. Alertas automaticas: 
    mostrar alerta visual
    guardar incidentes
    exportar CSV/PDF

5. Docker
    Archivos:
        Dockerfile
        docker-compose.yml

## Stack
Backend
    Python
    Pandas
    Regex
    SQLite
Frontend
    Streamlit
    Plotly
DevOps
    Docker
Extras
    watchdog → detectar nuevos logs automáticamente
    schedule → tareas automáticas
    matplotlib/plotly → gráficos

## Features
    Correlación de eventos
    Detección de anomalías
        desviación estándar
        picos de errores
    Soporte multi-formato
        Apache logs
        IIS logs
        WebLogic logs
        JSON logs
    Búsqueda estilo Splunk
    Sistema de severidad