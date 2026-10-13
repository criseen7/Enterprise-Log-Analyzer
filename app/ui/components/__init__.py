"""Piezas de UI reutilizables: filtros, KPIs, gráficos y tablas.

Se importan los módulos (`from app.ui.components import charts, kpis`) en vez de
re-exportar cada función: con re-exports, un nombre duplicado entre módulos pasa
desapercibido y el que gana depende del orden de import.
"""

from app.ui.components import charts, filters, kpis, tables

__all__ = ["charts", "filters", "kpis", "tables"]
