"""Tests del parser: registry, normalización, severidad y golden files.

Estrategia de los golden files: cada línea de `tests/fixtures/` tiene una línea
correspondiente en `tests/expected/<formato>.jsonl` con la salida esperada del
parser. El test compara ambas.

El archivo esperado se regenera a mano y se revisa como diff:

    python -m tests.regenerate_golden

Por eso el ratio se exige explícitamente y no se "acepta" degradado: si un
cambio rompe el parseo de un formato entero, el ratio cae y el test lo dice.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.parser.normalization import (
    ISO8601,
    TimestampError,
    parse_timestamp,
    replace_english_months,
    to_utc_iso,
)
from app.parser.registry import PatternError, Registry, load_patterns
from app.parser.severity import (
    ERROR,
    FATAL,
    INFO,
    UNKNOWN,
    WARN,
    canonical_level,
    severity_name,
    severity_of,
)
from tests.helpers import PATTERNS_DIR

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = Path(__file__).parent / "expected"
MIN_PARSE_RATIO = 0.95


def build_registry() -> Registry:
    return Registry.from_directory(PATTERNS_DIR)


class SeverityTests(unittest.TestCase):
    def test_canonical_names(self):
        for raw, expected in [
            ("INFO", "INFO"),
            ("info", "INFO"),
            ("[WARN]", "WARN"),
            ("WARNING", "WARN"),
            ("ERR", "ERROR"),
            ("SEVERE", "ERROR"),
            ("CRITICAL", "FATAL"),
            ("FINE", "DEBUG"),
            ("CONFIG", "INFO"),
            ("<ERROR>", "ERROR"),
            ("", "UNKNOWN"),
            (None, "UNKNOWN"),
            ("nonsense", "UNKNOWN"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(canonical_level(raw), expected)

    def test_numeric_aliases_are_ignored(self):
        """'7' podría ser un nivel syslog o un código HTTP: se trata como syslog."""
        self.assertEqual(canonical_level("3"), "ERROR")
        self.assertEqual(canonical_level("7"), "DEBUG")

    def test_severity_values(self):
        self.assertEqual(severity_of("ERROR"), ERROR)
        self.assertEqual(severity_of("WARNING"), WARN)
        self.assertEqual(severity_of("nonsense"), UNKNOWN)
        self.assertEqual(severity_name(severity_of("critical")), "FATAL")

    def test_scale_is_ordered_and_leaves_room(self):
        # ERROR(45) < FATAL(50): hay hueco para ALERT/EMERGENCY sin renumerar.
        self.assertLess(INFO, WARN)
        self.assertLess(WARN, ERROR)
        self.assertLess(ERROR, FATAL)
        self.assertLess(FATAL - ERROR, 6)


class TimestampTests(unittest.TestCase):
    def test_offset_is_converted_to_utc(self):
        moment, zone = parse_timestamp("2026-10-05T08:03:22-06:00", (ISO8601,))
        self.assertEqual(to_utc_iso(moment), "2026-10-05T14:03:22.000Z")
        self.assertEqual(zone, "-0600")

    def test_naive_timestamp_uses_default_zone(self):
        moment, zone = parse_timestamp(
            "2026-10-05 08:03:22",
            ("%Y-%m-%d %H:%M:%S",),
            default_timezone="America/Mexico_City",
        )
        self.assertEqual(to_utc_iso(moment), "2026-10-05T14:03:22.000Z")
        # La zona asumida queda registrada: una suposición sin rastro es un bug oculto.
        self.assertEqual(zone, "America/Mexico_City")

    def test_english_months_are_locale_independent(self):
        moment, zone = parse_timestamp(
            "05/Oct/2026:14:03:22 -0600",
            ("%d/%m/%Y:%H:%M:%S %z",),
            english_months=True,
        )
        self.assertEqual(to_utc_iso(moment), "2026-10-05T20:03:22.000Z")
        self.assertEqual(zone, "-0600")

    def test_replace_english_months(self):
        self.assertEqual(replace_english_months("05/Oct/2026"), "05/10/2026")
        self.assertEqual(replace_english_months("01-jan-2026 09:00"), "01-01-2026 09:00")
        # No debe tocar meses dentro de otra palabra.
        self.assertEqual(replace_english_months("octubre 2026"), "octubre 2026")

    def test_iso8601_keeps_milliseconds(self):
        moment, _ = parse_timestamp("2026-10-05T14:03:22.481Z", (ISO8601,))
        self.assertEqual(to_utc_iso(moment), "2026-10-05T14:03:22.481Z")

    def test_naive_datetime_is_treated_as_utc(self):
        """Un naive no debe interpretarse en la zona local del proceso.

        `astimezone()` sobre un datetime sin zona usa la hora local del sistema:
        en un contenedor con otra zona eso produce un desfase silencioso. Se fija
        `TZ` a una zona no-UTC para que el test sea determinista.
        """
        import os
        import time as _time
        from datetime import datetime

        naive = datetime(2026, 10, 5, 14, 3, 22)
        expected = "2026-10-05T14:03:22.000Z"

        if not hasattr(_time, "tzset"):  # pragma: no cover - no aplica en Linux
            self.assertEqual(to_utc_iso(naive), expected)
            return

        saved = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/Mexico_City"
            _time.tzset()
            self.assertEqual(to_utc_iso(naive), expected)
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            _time.tzset()

    def test_unparseable_raises(self):
        with self.assertRaises(TimestampError):
            parse_timestamp("no es una fecha", (ISO8601,))

    def test_invalid_default_zone_raises_on_naive_input(self):
        with self.assertRaises(TimestampError):
            parse_timestamp(
                "2026-10-05 08:03:22", ("%Y-%m-%d %H:%M:%S",), default_timezone="No/Existe"
            )

    def test_zone_is_ignored_when_input_carries_one(self):
        # Un log con offset no necesita la zona por defecto: no debe fallar.
        moment, zone = parse_timestamp(
            "2026-10-05T08:03:22-06:00", (ISO8601,), default_timezone="No/Existe"
        )
        self.assertEqual(to_utc_iso(moment), "2026-10-05T14:03:22.000Z")


class PatternLoadingTests(unittest.TestCase):
    def test_shipped_patterns_load(self):
        specs = load_patterns(PATTERNS_DIR)
        self.assertGreaterEqual(len(specs), 3)
        self.assertEqual({s.name for s in specs}, {"apachev2", "json", "weblogic"})

    def test_missing_required_group_fails_at_load(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            Path(d, "bad.yaml").write_text("name: roto\nregex: '^(?P<msg>.+)$'\n")
            with self.assertRaises(PatternError) as ctx:
                load_patterns(d)
            self.assertIn("ts", str(ctx.exception))

    def test_invalid_regex_fails_at_load(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            Path(d, "bad.yaml").write_text("name: roto\nregex: '(?P<ts>[unclosed'\n")
            with self.assertRaises(PatternError):
                load_patterns(d)

    def test_unknown_flag_fails_at_load(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            Path(d, "bad.yaml").write_text("name: r\nregex: '(?P<ts>x)'\nflags: [nope]\n")
            with self.assertRaises(PatternError):
                load_patterns(d)

    def test_template_referencing_unknown_group_fails(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            Path(d, "bad.yaml").write_text(
                "name: r\nregex: '(?P<ts>x)'\nmessage_template: '{no_existe}'\n"
            )
            with self.assertRaises(PatternError):
                load_patterns(d)

    def test_invalid_timezone_fails_at_load(self):
        """Zona inválida = arranque roto, no una tanda de eventos con parsed_ok=0."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            Path(d, "bad.yaml").write_text(
                "name: r\nregex: '(?P<ts>x)'\ntimestamp:\n"
                "  formats: ['iso8601']\n  default_timezone: No/Existe\n"
            )
            with self.assertRaises(PatternError) as ctx:
                load_patterns(d)
            self.assertIn("default_timezone", str(ctx.exception))

    def test_pattern_inherits_registry_timezone(self):
        """Sin default_timezone propio, el patrón hereda del Registry."""
        specs = {s.name: s for s in load_patterns(PATTERNS_DIR)}
        self.assertIsNone(specs["json"].default_timezone)
        registry = Registry(specs=list(specs.values()), default_timezone="America/Mexico_City")
        result = registry.parse_line(
            '{"timestamp":"2026-10-05 08:03:22","level":"INFO","message":"a"}'
        )
        self.assertEqual(result.timestamp_utc, "2026-10-05T14:03:22.000Z")
        self.assertEqual(result.source_tz, "America/Mexico_City")

    def test_duplicate_names_fail(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            for f in ("a.yaml", "b.yaml"):
                Path(d, f).write_text(f"name: mismo\nregex: '(?P<ts>{f})'\n")
            with self.assertRaises(PatternError):
                load_patterns(d)

    def test_priority_ordering(self):
        registry = build_registry()
        # apachev2 (50) debe probarse antes que json (40).
        self.assertEqual(registry.names[0], "apachev2")


class RegistryBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry()

    def test_apache_maps_status_to_severity(self):
        result = self.registry.parse_line(
            '10.0.0.1 - - [05/Oct/2026:14:03:22 -0600] "GET /x HTTP/1.1" 500 12 "-" "UA"'
        )
        self.assertTrue(result.parsed_ok)
        self.assertEqual(result.pattern, "apachev2")
        self.assertEqual(result.level_norm, "ERROR")
        self.assertEqual(result.severity, ERROR)

    def test_apache_2xx_is_unknown_severity(self):
        # CLF no tiene nivel; un 200 no es un error y no debe inventarse uno.
        result = self.registry.parse_line(
            '10.0.0.1 - - [05/Oct/2026:14:03:22 -0600] "GET /x HTTP/1.1" 200 12 "-" "UA"'
        )
        self.assertEqual(result.level_norm, "UNKNOWN")

    def test_unparseable_line_is_kept_with_reason(self):
        line = "esto no es un log de nada"
        result = self.registry.parse_line(line)
        self.assertFalse(result.parsed_ok)
        self.assertIsNotNone(result.parse_error)
        # El dato no se pierde: la línea cruda sobrevive.
        self.assertEqual(result.raw_line, line)
        self.assertEqual(result.message, line)

    def test_bad_timestamp_keeps_the_rest(self):
        line = '10.0.0.1 - - [NO-ES-FECHA] "GET /x HTTP/1.1" 500 12 "-" "UA"'
        result = self.registry.parse_line(line)
        self.assertFalse(result.parsed_ok)
        self.assertIn("timestamp", result.parse_error)
        self.assertEqual(result.raw_line, line)

    def test_empty_line(self):
        result = self.registry.parse_line("   \n")
        self.assertFalse(result.parsed_ok)

    def test_json_field_resolution_finds_first_present(self):
        result = self.registry.parse_line(
            '{"@timestamp":"2026-10-05T15:00:00Z","@level":"SEVERE","@message":"boom"}'
        )
        self.assertEqual(result.level_norm, "ERROR")
        self.assertEqual(result.message, "boom")

    def test_json_nested_paths(self):
        result = self.registry.parse_line(
            json.dumps(
                {
                    "@timestamp": "2026-10-05T15:00:00Z",
                    "log": {"level": "critical", "message": "pool agotado"},
                    "resource": {"attributes": {"service": {"name": "db-pool"}}},
                }
            )
        )
        self.assertEqual(result.level_norm, "FATAL")
        self.assertEqual(result.service, "db-pool")
        self.assertEqual(result.message, "pool agotado")

    def test_json_invalid_payload_does_not_raise(self):
        result = self.registry.parse_line('{"@timestamp": "2026-10-05T15:00:00Z",}')
        self.assertFalse(result.parsed_ok)
        self.assertIsNotNone(result.parse_error)

    def test_json_without_timestamp(self):
        result = self.registry.parse_line('{"level":"ERROR","message":"sin fecha"}')
        self.assertFalse(result.parsed_ok)


class GoldenFileTests(unittest.TestCase):
    """Cada línea de fixture tiene su línea esperada. El ratio no se degrada."""

    CASES = [
        ("apachev2/access_combined.log", "apachev2/access_combined.jsonl"),
        ("apachev2/access_mixed_timezones.log", "apachev2/access_mixed_timezones.jsonl"),
        ("json/appliance.json", "json/appliance.jsonl"),
        ("json/weblogic_flat.json", "json/weblogic_flat.jsonl"),
    ]

    def test_golden_files_exist(self):
        for fixture, expected in self.CASES:
            self.assertTrue((FIXTURES / fixture).is_file(), f"falta fixture {fixture}")
            self.assertTrue((EXPECTED / expected).is_file(), f"falta golden {expected}")

    def test_parse_ratio_meets_threshold(self):
        registry = build_registry()
        for fixture, _ in self.CASES:
            lines = read_lines(FIXTURES / fixture)
            ok = sum(1 for line in lines if registry.parse_line(line).parsed_ok)
            ratio = ok / len(lines)
            with self.subTest(fixture=fixture):
                self.assertGreaterEqual(
                    ratio, MIN_PARSE_RATIO, f"{fixture}: solo {ok}/{len(lines)} líneas parseadas"
                )

    def test_matches_golden_output(self):
        registry = build_registry()
        for fixture, expected in self.CASES:
            lines = read_lines(FIXTURES / fixture)
            golden = read_golden(EXPECTED / expected)
            self.assertEqual(
                len(lines), len(golden), f"{fixture}:_fixture y golden de distinto tamaño"
            )
            for number, (line, want) in enumerate(zip(lines, golden, strict=True), 1):
                got = as_golden(registry.parse_line(line))
                with self.subTest(fixture=fixture, line=number):
                    for key, value in want.items():
                        self.assertEqual(got.get(key), value, f"{fixture}:{number} campo {key}")

    def test_normalized_timestamps_are_utc(self):
        """D4: todo lo que llega a la base es UTC."""
        registry = build_registry()
        for fixture, _ in self.CASES:
            for number, line in enumerate(read_lines(FIXTURES / fixture), 1):
                result = registry.parse_line(line)
                if result.parsed_ok:
                    with self.subTest(fixture=fixture, line=number):
                        self.assertIsNotNone(result.timestamp_utc)
                        self.assertTrue(
                            result.timestamp_utc.endswith("Z"),
                            f"{fixture}:{number} no está en UTC: {result.timestamp_utc}",
                        )

    def test_source_timezone_is_always_recorded(self):
        """Una zona asumida debe quedar registrada, aunque el log no la traiga."""
        registry = build_registry()
        for fixture, _ in self.CASES:
            for number, line in enumerate(read_lines(FIXTURES / fixture), 1):
                result = registry.parse_line(line)
                if result.parsed_ok:
                    with self.subTest(fixture=fixture, line=number):
                        self.assertIsNotNone(result.source_tz)


class TimezoneEquivalenceTests(unittest.TestCase):
    """El motivo de normalizar: el mismo instante, escrito distinto, es el mismo."""

    def test_same_instant_written_three_ways(self):
        registry = build_registry()
        forms = [
            # Apache CLF con offset -0600
            '10.0.0.1 - - [05/Oct/2026:08:03:22 -0600] "GET /a HTTP/1.1" 200 1 "-" "UA"',
            # JSON ISO con el mismo offset
            '{"@timestamp":"2026-10-05T08:03:22-06:00","level":"INFO","message":"a"}',
            # JSON ISO en UTC
            '{"@timestamp":"2026-10-05T14:03:22Z","level":"INFO","message":"a"}',
        ]
        stamps = {registry.parse_line(f).timestamp_utc for f in forms}
        self.assertEqual(len(stamps), 1, f"el mismo instante produjo {stamps}")
        self.assertEqual(stamps.pop(), "2026-10-05T14:03:22.000Z")

    def test_naive_and_offset_agree_when_zone_is_declared(self):
        registry = Registry.from_directory(PATTERNS_DIR, default_timezone="America/Mexico_City")
        naive = registry.parse_line(
            '{"timestamp":"2026-10-05 08:03:22","level":"INFO","message":"a"}'
        )
        explicit = registry.parse_line(
            '{"timestamp":"2026-10-05T08:03:22-06:00","level":"INFO","message":"a"}'
        )
        self.assertEqual(naive.timestamp_utc, explicit.timestamp_utc)

    def test_assumed_zone_is_visible_in_source_tz(self):
        registry = build_registry()
        naive = registry.parse_line(
            '{"timestamp":"2026-10-05 08:03:22","level":"INFO","message":"a"}'
        )
        self.assertEqual(naive.source_tz, "UTC")  # config.yaml: UTC
        explicit = registry.parse_line(
            '{"timestamp":"2026-10-05T08:03:22-06:00","level":"INFO","message":"a"}'
        )
        self.assertEqual(explicit.source_tz, "-0600")
        self.assertNotEqual(naive.source_tz, explicit.source_tz)


# --- helpers ---

GOLDEN_FIELDS = (
    "pattern",
    "parsed_ok",
    "parse_error",
    "timestamp_utc",
    "timestamp_raw",
    "source_tz",
    "level_raw",
    "level_norm",
    "severity",
    "service",
    "logger",
    "message",
)


def as_golden(result) -> dict:
    return {field: getattr(result, field) for field in GOLDEN_FIELDS}


def read_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_golden(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


if __name__ == "__main__":
    unittest.main()
