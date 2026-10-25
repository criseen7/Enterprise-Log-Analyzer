"""Rutas al esquema y número de versión.

`SCHEMA_VERSION` es el contrato entre el esquema en disco y el código: si cambia
una columna, debe subir aquí y existir un script de migración que lo transforme.

El número es el de la última migración, no cuántas hay: una base creada desde
cero registra toda la cobertura y queda en `SCHEMA_VERSION` igual que una
antigua que sí recorrió los deltas. Añadir una migración sin subir esto produce
bases que se quedan en un esquema viejo sin avisar.
"""

from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 7

__all__ = ["SCHEMA_PATH", "SCHEMA_VERSION"]
