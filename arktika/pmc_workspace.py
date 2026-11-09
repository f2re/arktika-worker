"""Local, persistent operator workspaces; saving is not a scientific rerun.

Only a bounded, declarative JSON state is stored. Raw fields remain in the
verified field cache; annotations never change the detector's candidate status.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import uuid

from .products import PRESETS

SCHEMA_VERSION = "arktika.pmc_workspace.v1"
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_BOOKMARKS = 200
MAX_ANNOTATIONS = 250
_ID = re.compile(r"[0-9a-f]{24}")
_SECRET_KEY = re.compile(r"(^|_)(token|password|secret|authorization|cookie|credentials?|api_key|signed_url)(_|$)", re.I)


def _text(value, name, maximum, *, empty=True):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{name}: нужна строка длиной не более {maximum} символов.")
    if "\x00" in value:
        raise ValueError(f"{name}: недопустимый символ.")
    return value


def _number(value, name, low=-1e12, high=1e12):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}: недопустимое числовое значение.")
    return value


def _numbers(value, count, name):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"{name}: ожидаются {count} числа.")
    return [_number(v, name) for v in value]


def _keys(value, allowed, name, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ValueError(f"{name}: неверный состав полей.")


def _identity(value, *, optional=False):
    if optional and value in (None, ""):
        return
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Неверный идентификатор сохранённого объекта.")


def _utc(value, name, *, optional=False):
    if optional and value in (None, ""):
        return
    _text(value, name, 40, empty=False)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name}: нужен срок ISO 8601 в UTC.") from None
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name}: укажите явный часовой пояс UTC.")


def _declarative(value, depth=0):
    """Reject secret-bearing or excessively nested objects without logging them."""
    if depth > 24:
        raise ValueError("Слишком сложная структура закладки.")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key in ("__proto__", "constructor", "prototype") or _SECRET_KEY.search(key):
                raise ValueError("Закладка содержит недопустимые служебные поля или реквизиты доступа.")
            _declarative(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _declarative(child, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Закладка содержит неопределённые числовые значения.")
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError("Закладка должна содержать только данные JSON.")


def validate_workspace(state):
    """Validate persisted display state independently from provider availability."""
    _keys(state, {"schema_version", "map", "product_id", "field_stack", "raster_visible", "night_visible",
                  "result", "run", "archive", "annotations"}, "Состояние анализа",
          {"schema_version", "map", "annotations"})
    if state["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Неподдерживаемая версия закладки ПМЦ.")
    _declarative(state)
    try:
        serialized = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Неверный JSON закладки.") from None
    if len(serialized.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("Закладка превышает 2 МиБ. Уменьшите число объектов или размер отчёта.")

    g = state["map"]
    _keys(g, {"preset", "width", "height", "crs", "bounds", "view"}, "Карта",
          {"preset", "width", "height", "crs", "bounds", "view"})
    if not isinstance(g["preset"], str) or g["preset"] not in PRESETS:
        raise ValueError("Неизвестная область карты в закладке.")
    for key in ("width", "height"):
        if isinstance(g[key], bool) or not isinstance(g[key], int) or not 128 <= g[key] <= 8192:
            raise ValueError("Неверный размер сохранённой карты.")
    _text(g["crs"], "CRS", 512, empty=False)
    left, bottom, right, top = _numbers(g["bounds"], 4, "Границы карты")
    if left >= right or bottom >= top:
        raise ValueError("Неверный порядок границ карты.")
    _, _, width, height = _numbers(g["view"], 4, "Масштаб карты")
    if width <= 0 or height <= 0:
        raise ValueError("Размер видимой области карты должен быть положительным.")
    # CRS is data, not executable text; reject malformed projection definitions.
    from pyproj import CRS
    try:
        CRS.from_user_input(g["crs"])
    except Exception:
        raise ValueError("Неизвестная система координат в закладке.") from None
    _identity(state.get("product_id"), optional=True)
    for key in ("raster_visible", "night_visible"):
        if key in state and not isinstance(state[key], bool):
            raise ValueError("Видимость слоя должна быть логическим значением.")

    stack = state.get("field_stack", [])
    if not isinstance(stack, list) or len(stack) > 8:
        raise ValueError("В закладке допускается не более восьми полей.")
    seen = set()
    for layer in stack:
        _keys(layer, {"id", "style", "opacity", "visible"}, "Слой", {"id", "style", "opacity", "visible"})
        _identity(layer["id"])
        if layer["id"] in seen:
            raise ValueError("В закладке повторяется одно поле.")
        seen.add(layer["id"])
        _number(layer["opacity"], "Прозрачность", 0, 1)
        if layer["style"] not in ("fill", "contours", "fill_contours", "arrows", "fill_arrows") or not isinstance(layer["visible"], bool):
            raise ValueError("Неверный способ отображения поля.")

    run = state.get("run")
    if run is not None:
        _keys(run, {"field_id", "preset", "width", "observation_id", "thresholds"}, "Параметры расчёта")
        _identity(run.get("field_id"), optional=True)
        _identity(run.get("observation_id"), optional=True)
        if "preset" in run and (not isinstance(run["preset"], str) or run["preset"] not in PRESETS):
            raise ValueError("Неизвестная область расчёта.")
        if "width" in run:
            _number(run["width"], "Ширина расчётной сетки", 128, 2048)
            if not isinstance(run["width"], int):
                raise ValueError("Ширина расчётной сетки должна быть целым числом.")
        if run.get("thresholds") is not None:
            from .pmc_detection import validate_candidate_thresholds
            validate_candidate_thresholds(run["thresholds"])

    archive = state.get("archive")
    if archive is not None:
        _keys(archive, {"sources", "start", "end", "area", "include_context"}, "Параметры архива",
              {"sources", "start", "end", "area"})
        sources = archive["sources"]
        if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or s not in ("era5", "carra2", "merra2") for s in sources) or len(set(sources)) != len(sources):
            raise ValueError("Неверный список источников архива.")
        _utc(archive["start"], "Начало")
        _utc(archive["end"], "Конец")
        if datetime.fromisoformat(archive["end"].replace("Z", "+00:00")) < datetime.fromisoformat(archive["start"].replace("Z", "+00:00")):
            raise ValueError("Конец интервала раньше начала.")
        from .reanalysis.catalog import validate_area
        validate_area(archive["area"])
        if "include_context" in archive and not isinstance(archive["include_context"], bool):
            raise ValueError("Неверный признак контекстных полей.")

    result = state.get("result")
    if result is not None:
        if not isinstance(result, dict) or not isinstance(result.get("candidates"), list) or len(result["candidates"]) > 10_000:
            raise ValueError("В закладке должен быть снимок результата с массивом кандидатов.")
        count = result.get("candidate_count")
        if isinstance(count, bool) or not isinstance(count, int) or count != len(result["candidates"]):
            raise ValueError("Число кандидатов не совпадает с сохранённым списком.")
        _utc(result.get("valid_time"), "Срок результата")
        _identity(result.get("field_id"), optional=True)
        for key in ("field_passport", "parameters"):
            if result.get(key) is not None and not isinstance(result[key], dict):
                raise ValueError("Паспорт и параметры результата должны быть объектами JSON.")
        passport = result.get("field_passport") or {}
        _identity(passport.get("source_field_id"), optional=True)
        digest = passport.get("claimed_source_file_sha256")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Неверная контрольная сумма исходного поля в паспорте.")
        if result.get("maturity") != "experimental":
            raise ValueError("Снимок результата ПМЦ должен сохранять экспериментальный статус.")
        for candidate in result["candidates"]:
            if (not isinstance(candidate, dict) or candidate.get("confirmation_status") != "not_confirmed"
                    or candidate.get("classification") != "candidate"):
                raise ValueError("Операторская оценка хранится отдельно от статуса модельного кандидата.")

    annotations = state["annotations"]
    if not isinstance(annotations, list) or len(annotations) > MAX_ANNOTATIONS:
        raise ValueError("Допускается не более 250 пометок на один анализ.")
    seen = set()
    from pyproj import Transformer
    to_geographic = Transformer.from_crs(g["crs"], "EPSG:4326", always_xy=True)
    for mark in annotations:
        _keys(mark, {"id", "x", "y", "lon", "lat", "label", "note", "status", "candidate_id", "valid_time", "source"},
              "Пометка", {"id", "x", "y", "lon", "lat", "label", "note", "status"})
        identity = _text(mark["id"], "Идентификатор пометки", 100, empty=False)
        if identity in seen:
            raise ValueError("Идентификаторы пометок должны различаться.")
        seen.add(identity)
        _number(mark["x"], "X пометки", 0, g["width"])
        _number(mark["y"], "Y пометки", 0, g["height"])
        _number(mark["lon"], "Долгота", -180, 180)
        _number(mark["lat"], "Широта", -90, 90)
        x = left + mark["x"] * (right - left) / g["width"]
        y = top - mark["y"] * (top - bottom) / g["height"]
        lon, lat = to_geographic.transform(x, y)
        lon_error = abs((lon - mark["lon"] + 180) % 360 - 180)
        if not math.isfinite(lon + lat) or lon_error > 1e-4 or abs(lat - mark["lat"]) > 1e-4:
            raise ValueError("Координаты пометки не совпадают с её положением на сохранённой карте.")
        _text(mark["label"], "Название пометки", 120, empty=False)
        _text(mark["note"], "Основание пометки", 2000)
        if mark["status"] not in ("manual_candidate", "expert_supported", "rejected"):
            raise ValueError("Неизвестный статус пометки.")
        if mark["status"] in ("expert_supported", "rejected") and not mark["note"].strip():
            raise ValueError("Для экспертной оценки укажите наблюдаемые признаки и источник.")
        _utc(mark.get("valid_time"), "Срок пометки", optional=True)
        for key in ("candidate_id", "source"):
            if mark.get(key) is not None:
                _text(mark[key], key, 200)
    return serialized


class PmcWorkspaceMixin:
    def _pmc_bookmarks_init(self):
        with self.store.lock, self.store.conn:
            self.store.conn.execute("""CREATE TABLE IF NOT EXISTS pmc_bookmarks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                state TEXT NOT NULL, state_sha256 TEXT NOT NULL)""")

    @staticmethod
    def _pmc_bookmark_row(row, *, include_state=False):
        result = {key: row[key] for key in ("id", "title", "notes", "created_at", "updated_at", "state_sha256")}
        if include_state:
            if hashlib.sha256(row["state"].encode("utf-8")).hexdigest() != row["state_sha256"]:
                raise ValueError("Закладка повреждена: контрольная сумма не совпадает.")
            result["state"] = json.loads(row["state"])
            result["status"] = "saved_analysis_not_recomputed"
            result["snapshot_attestation"] = "client_supplied_not_recomputed; sha256 verifies stored JSON only"
        return result

    def pmc_bookmarks(self):
        self._pmc_bookmarks_init()
        with self.store.lock:
            rows = self.store.conn.execute("SELECT id,title,notes,created_at,updated_at,state_sha256 FROM pmc_bookmarks ORDER BY updated_at DESC,id DESC").fetchall()
        return {"bookmarks": [self._pmc_bookmark_row(row) for row in rows], "limit": MAX_BOOKMARKS}

    def pmc_bookmark(self, identity):
        _identity(identity)
        self._pmc_bookmarks_init()
        with self.store.lock:
            row = self.store.conn.execute("SELECT * FROM pmc_bookmarks WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Закладка не найдена.")
        bookmark = self._pmc_bookmark_row(row, include_state=True)
        state = bookmark["state"]
        validate_workspace(state)
        field_ids = {item["id"] for item in state.get("field_stack", [])}
        run_id = (state.get("run") or {}).get("field_id")
        result = state.get("result") or {}
        result_id = result.get("field_id") or (result.get("field_passport") or {}).get("source_field_id")
        field_ids.update(identity for identity in (run_id, result_id) if identity)
        expected_digest = (result.get("field_passport") or {}).get("claimed_source_file_sha256")
        missing = []
        changed = []
        for field_id in sorted(field_ids):
            try:
                meta = self.field(field_id)
                if field_id == result_id and expected_digest and meta.get("sha256") != expected_digest:
                    changed.append(field_id)
                    missing.append(field_id)
            except (ValueError, OSError, KeyError, TypeError):
                missing.append(field_id)
        product_available = None
        if state.get("product_id"):
            try:
                self.product(state["product_id"])
                product_available = True
            except (ValueError, OSError, KeyError):
                product_available = False
        warnings = ["Сохранённый анализ не пересчитан. Пометки отражают оценку оператора, а не автоматическое подтверждение ПМЦ."]
        if missing:
            warnings.append("Часть исходных полей отсутствует или не прошла проверку целостности. Для пересчёта получите эти поля повторно.")
        if changed:
            warnings.append("Текущее поле отличается от исходного поля сохранённого расчёта. Оно не будет подставлено в сохранённый анализ.")
        if run_id and result_id and run_id != result_id:
            warnings.append("В форме выбран другой исходный файл. Сохранённый результат относится к файлу из своего паспорта.")
        configured = (state.get("run") or {}).get("thresholds")
        applied = result.get("parameters") or {}
        if configured and any(configured.get(key) != applied.get(key) for key in configured):
            warnings.append("Пороги в форме отличаются от порогов сохранённого расчёта. Их применение требует отдельного запуска.")
        if product_available is False:
            warnings.append("Спутниковый продукт отсутствует или повреждён. Сохранённые пометки и результат доступны отдельно.")
        bookmark["availability"] = {"missing_fields": missing, "changed_fields": changed,
                                    "product_available": product_available, "warnings": warnings}
        return {"bookmark": bookmark}

    def pmc_bookmark_save(self, data):
        _keys(data, {"id", "title", "notes", "state"}, "Закладка", {"title", "state"})
        title = _text(data["title"], "Название", 160, empty=False).strip()
        notes = _text(data.get("notes", ""), "Заметки", 10_000)
        serialized = validate_workspace(data["state"])
        identity = data.get("id")
        _identity(identity, optional=True)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._pmc_bookmarks_init()
        with self.store.lock, self.store.conn:
            if identity:
                existing = self.store.conn.execute("SELECT id FROM pmc_bookmarks WHERE id=?", (identity,)).fetchone()
                if existing is None:
                    raise ValueError("Обновляемая закладка не найдена.")
                self.store.conn.execute("UPDATE pmc_bookmarks SET title=?,notes=?,updated_at=?,state=?,state_sha256=? WHERE id=?",
                                        (title, notes, stamp, serialized, digest, identity))
            else:
                if self.store.conn.execute("SELECT COUNT(*) FROM pmc_bookmarks").fetchone()[0] >= MAX_BOOKMARKS:
                    raise ValueError("Достигнут предел 200 закладок. Удалите ненужные сохранённые анализы.")
                identity = uuid.uuid4().hex[:24]
                self.store.conn.execute("INSERT INTO pmc_bookmarks VALUES(?,?,?,?,?,?,?)",
                                        (identity, title, notes, stamp, stamp, serialized, digest))
            row = self.store.conn.execute("SELECT * FROM pmc_bookmarks WHERE id=?", (identity,)).fetchone()
        return {"bookmark": self._pmc_bookmark_row(row, include_state=True)}

    def pmc_bookmark_delete(self, data):
        _keys(data, {"id"}, "Удаление закладки", {"id"})
        _identity(data["id"])
        self._pmc_bookmarks_init()
        with self.store.lock, self.store.conn:
            cursor = self.store.conn.execute("DELETE FROM pmc_bookmarks WHERE id=?", (data["id"],))
        return {"deleted": cursor.rowcount == 1, "id": data["id"]}
