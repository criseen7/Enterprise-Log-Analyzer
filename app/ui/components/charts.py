"""Gráficos Plotly del dashboard.

Todos comparten tres cosas, y por eso hay un `_base()`:

* **Altura y márgenes fijos.** Dejarlos en automático produce saltos de layout
  al cambiar de filtro, y el dashboard parece roto.
* **Fondo transparente.** Streamlit elige claro u oscuro según el tema del
  navegador; un `paper_bgcolor` blanco fijo hace ilegible el dashboard en oscuro.
* **`hovermode="x unified"`** en las series temporales: la pregunta es "¿qué pasó
  a esa hora?", no "¿qué punto es este?".

Devuelven la figura y **no llaman a `st.plotly_chart`**: eso deja los gráficos
testeables sin un contexto de Streamlit.
"""

from __future__ import annotations

import plotly.graph_objects as go

from app.parser.severity import severity_name
from app.ui.theme import ACCENT, DAY_NAMES, MUTED, SEVERITY_COLORS, SEVERITY_ORDER


def _base(fig: go.Figure, *, height: int, y_title: str = "") -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=28, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        yaxis_title=y_title,
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.25)", zeroline=False)
    return fig


def _short(bucket: str, *, day: bool = False) -> str:
    """`2026-10-05T14:00` -> `05 14:00` (o `05-10` si es por día)."""
    if len(bucket) >= 13:
        return f"{bucket[8:10]} {bucket[11:13]}:00"
    if day and len(bucket) >= 10:
        return bucket[5:10]
    return bucket


def errors_over_time(counts: dict[str, int], *, height: int = 280) -> go.Figure:
    buckets = list(counts)
    values = [counts[b] for b in buckets]
    fig = go.Figure(
        go.Scatter(
            x=[_short(b) for b in buckets],
            y=values,
            mode="lines+markers",
            line=dict(color=ACCENT, width=2),
            marker=dict(size=6),
            fill="tozeroy",
            fillcolor="rgba(66,165,245,0.18)",
            hovertemplate="%{x}<br>%{y} errores<extra></extra>",
            name="errores",
        )
    )
    return _base(fig, height=height, y_title="errores")


def top_errors_bar(rows: list[dict], *, height: int = 340) -> go.Figure:
    """Barra horizontal: el orden de lectura y el ranking coinciden.

    Se invierte el eje Y (`autorange="reversed"`) porque Plotly dibuja la primera
    categoría abajo: sin invertir, el error más frecuente aparece al pie.
    """
    rows = list(reversed(rows))
    labels = [
        f"{(row.get('category') or 'sin categoría')} · {(row.get('sample_message') or '')[:48]}"
        for row in rows
    ]
    values = [row["count"] for row in rows]
    colors = [SEVERITY_COLORS[severity_name(row.get("severity"))] for row in rows]
    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker=dict(color=colors),
            hovertemplate="%{y}<br>%{x} eventos<extra></extra>",
        )
    )
    fig.update_layout(showlegend=False, margin=dict(l=8, r=8, t=8, b=8))
    fig.update_xaxes(title="eventos", showgrid=True, gridcolor="rgba(128,128,128,0.25)")
    fig.update_yaxes(tickfont=dict(size=11))
    return fig


def severity_area(rows: list[dict], *, height: int = 280) -> go.Figure:
    """Serie apilada por severidad. El apilado va de menos a más grave."""
    buckets: list[str] = []
    for row in rows:
        if row["bucket"] not in buckets:
            buckets.append(row["bucket"])
    by_bucket: dict[str, dict[int, int]] = {b: {} for b in buckets}
    for row in rows:
        by_bucket[row["bucket"]][int(row["severity"])] = int(row["count"])

    fig = go.Figure()
    for severity in SEVERITY_ORDER:
        values = [by_bucket[b].get(severity, 0) for b in buckets]
        if not any(values):
            continue
        name = severity_name(severity)
        fig.add_trace(
            go.Scatter(
                x=[_short(b) for b in buckets],
                y=values,
                mode="lines",
                stackgroup="one",
                name=name,
                line=dict(width=0.5, color=SEVERITY_COLORS[name]),
                hovertemplate=f"{name}: %{{y}}<extra></extra>",
            )
        )
    return _base(fig, height=height, y_title="eventos")


def heatmap_figure(rows: list[dict], *, height: int = 340) -> go.Figure:
    """Mapa día×hora. La escala va de gris a rojo: vacío ≈ sin actividad."""
    grid = [[0] * 24 for _ in range(7)]
    for row in rows:
        grid[int(row["day"])][int(row["hour"])] = int(row["count"])
    fig = go.Figure(
        go.Heatmap(
            z=grid,
            x=[f"{h:02d}" for h in range(24)],
            y=list(DAY_NAMES),
            colorscale=[[0.0, "#1f1f1f"], [0.15, "#3a2b2b"], [0.5, "#ef5350"], [1.0, "#7f0000"]],
            hovertemplate="%{y} %{x}:00<br>%{z} eventos<extra></extra>",
            colorbar=dict(title="eventos", thickness=12),
            showscale=True,
        )
    )
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=8, b=8), paper_bgcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=False, title="hora del día (UTC)")
    fig.update_yaxes(showgrid=False, autorange="reversed")
    return fig


def category_bar(rows: list[dict], *, height: int = 300) -> go.Figure:
    """Errores por categoría. Los `None` se muestran como texto, no como hueco."""
    rows = list(reversed(rows))
    labels = [row.get("category") or "sin categoría" for row in rows]
    fig = go.Figure(
        go.Bar(
            x=[row["count"] for row in rows],
            y=labels,
            orientation="h",
            marker=dict(color=ACCENT),
            hovertemplate="%{y}<br>%{x} errores<extra></extra>",
        )
    )
    fig.update_layout(showlegend=False, margin=dict(l=8, r=8, t=8, b=8))
    fig.update_xaxes(title="errores", showgrid=True, gridcolor="rgba(128,128,128,0.25)")
    return fig


def hourly_with_spikes(series: list[dict], spikes: list[dict], *, height: int = 300) -> go.Figure:
    """Serie horaria rellenada + las horas marcadas como anómalas.

    Se dibuja la serie completa (con huecos) y encima los picos: un marcador
    suelto sin la serie de fondo obliga a mirar los números para saber si el pico
    es grande o si la serie entera estaba alta.
    """
    buckets = [row["bucket"] for row in series]
    values = [row["count"] for row in series]
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=[_short(b) for b in buckets],
            y=values,
            mode="lines",
            line=dict(color=MUTED, width=1.5),
            name="eventos/hora",
            hovertemplate="%{x}<br>%{y} eventos<extra></extra>",
        )
    )
    if spikes:
        spike_buckets = {row["bucket"] for row in spikes}
        fig.add_trace(
            go.Scatter(
                x=[_short(b) for b in buckets if b in spike_buckets],
                y=[v for b, v in zip(buckets, values, strict=True) if b in spike_buckets],
                mode="markers",
                marker=dict(color=SEVERITY_COLORS["FATAL"], size=11, symbol="diamond"),
                name="anomalía",
                hovertemplate="%{x}<br>%{y} eventos (z alto)<extra></extra>",
            )
        )
    return _base(fig, height=height, y_title="eventos")


__all__ = [
    "category_bar",
    "errors_over_time",
    "heatmap_figure",
    "hourly_with_spikes",
    "severity_area",
    "top_errors_bar",
]
