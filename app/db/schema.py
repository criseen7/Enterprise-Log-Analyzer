"""Rutas al esquema y número de versión.

`SCHEMA_VERSION` es el contrato entre el esquema en disco y el código: si cambia
una columna, debe subir aquí y existir un script de migración que lo transforme.
"""

from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1

__all__ = ["SCHEMA_PATH", "SCHEMA_VERSION"]