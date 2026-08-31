"""MusicScope 的 SQLite 持久化与 CSV/JSON 往返数据模型。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .audio import AnalysisResult


SCHEMA_VERSION = 1
ANALYZER_VERSION = "1.0.0"
CLASSIFIER_VERSION = "rules-1.0"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON 不允许非有限数值 {value}")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL,
    stored_name TEXT,
    file_hash TEXT,
    file_format TEXT NOT NULL DEFAULT 'WAV',
    file_size INTEGER NOT NULL DEFAULT 0,
    duration REAL NOT NULL DEFAULT 0,
    sample_rate INTEGER NOT NULL DEFAULT 0,
    channels INTEGER NOT NULL DEFAULT 0,
    sample_width INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'upload',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_hash ON tracks(file_hash);
CREATE INDEX IF NOT EXISTS idx_tracks_name ON tracks(file_name);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL,
    analyzer_version TEXT NOT NULL,
    analyzed_at TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    analyzed_seconds REAL NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS features (
    analysis_id INTEGER PRIMARY KEY,
    tempo REAL NOT NULL DEFAULT 0,
    tempo_confidence REAL NOT NULL DEFAULT 0,
    key_name TEXT NOT NULL DEFAULT '未知',
    musical_mode TEXT NOT NULL DEFAULT '未知',
    key_confidence REAL NOT NULL DEFAULT 0,
    rms_db REAL NOT NULL DEFAULT -200,
    peak_db REAL NOT NULL DEFAULT -200,
    dynamic_range_db REAL NOT NULL DEFAULT 0,
    avg_pitch_hz REAL NOT NULL DEFAULT 0,
    pitch_note TEXT NOT NULL DEFAULT '未知',
    spectral_centroid REAL NOT NULL DEFAULT 0,
    spectral_rolloff REAL NOT NULL DEFAULT 0,
    zero_crossing_rate REAL NOT NULL DEFAULT 0,
    feature_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(analysis_id) REFERENCES analyses(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS classifications (
    analysis_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    match_score REAL NOT NULL DEFAULT 0,
    explanation TEXT NOT NULL DEFAULT '',
    classifier_version TEXT NOT NULL,
    FOREIGN KEY(analysis_id) REFERENCES analyses(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS data_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_type TEXT NOT NULL,
    file_name TEXT,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


SELECT_RECORD = """
SELECT
    analyses.id AS id,
    tracks.id AS track_id,
    tracks.file_name,
    tracks.stored_name,
    tracks.file_hash,
    tracks.file_format,
    tracks.file_size,
    tracks.duration,
    tracks.sample_rate,
    tracks.channels,
    tracks.sample_width,
    tracks.source,
    tracks.created_at,
    analyses.analyzed_at,
    analyses.analyzer_version,
    analyses.elapsed_ms,
    analyses.analyzed_seconds,
    analyses.truncated,
    features.tempo,
    features.tempo_confidence,
    features.key_name,
    features.musical_mode,
    features.key_confidence,
    features.rms_db,
    features.peak_db,
    features.dynamic_range_db,
    features.avg_pitch_hz,
    features.pitch_note,
    features.spectral_centroid,
    features.spectral_rolloff,
    features.zero_crossing_rate,
    features.feature_json,
    classifications.label AS classification,
    classifications.match_score AS classification_confidence,
    classifications.explanation,
    classifications.classifier_version
FROM analyses
JOIN tracks ON tracks.id = analyses.track_id
JOIN features ON features.analysis_id = analyses.id
JOIN classifications ON classifications.analysis_id = analyses.id
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MusicDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            metadata_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
            ).fetchone()
            if metadata_exists:
                current = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                if current and int(current["value"]) > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"数据库版本 {current['value']} 高于程序支持版本 {SCHEMA_VERSION}，请升级 MusicScope。"
                    )
            connection.executescript(SCHEMA)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()

    def insert_analysis(
        self,
        *,
        file_name: str,
        stored_name: str | None,
        file_hash: str | None,
        file_size: int,
        source: str,
        result: AnalysisResult,
        elapsed_ms: int,
    ) -> int:
        timestamp = now_iso()
        reasons = result.feature_data.get("classification_reasons", [])
        explanation = "；".join(str(reason) for reason in reasons)
        with self.connect() as connection:
            try:
                connection.execute("BEGIN")
                track_cursor = connection.execute(
                    """
                    INSERT INTO tracks(
                        file_name, stored_name, file_hash, file_format, file_size,
                        duration, sample_rate, channels, sample_width, source, created_at
                    ) VALUES (?, ?, ?, 'WAV', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_name,
                        stored_name,
                        file_hash,
                        file_size,
                        result.duration,
                        result.sample_rate,
                        result.channels,
                        result.sample_width,
                        source,
                        timestamp,
                    ),
                )
                track_id = int(track_cursor.lastrowid)
                analysis_cursor = connection.execute(
                    """
                    INSERT INTO analyses(
                        track_id, analyzer_version, analyzed_at, elapsed_ms,
                        analyzed_seconds, truncated
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        track_id,
                        ANALYZER_VERSION,
                        timestamp,
                        elapsed_ms,
                        result.analyzed_seconds,
                        int(result.truncated),
                    ),
                )
                analysis_id = int(analysis_cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO features(
                        analysis_id, tempo, tempo_confidence, key_name, musical_mode,
                        key_confidence, rms_db, peak_db, dynamic_range_db, avg_pitch_hz,
                        pitch_note, spectral_centroid, spectral_rolloff,
                        zero_crossing_rate, feature_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        analysis_id,
                        result.tempo,
                        result.tempo_confidence,
                        result.key_name,
                        result.musical_mode,
                        result.key_confidence,
                        result.rms_db,
                        result.peak_db,
                        result.dynamic_range_db,
                        result.avg_pitch_hz,
                        result.pitch_note,
                        result.spectral_centroid,
                        result.spectral_rolloff,
                        result.zero_crossing_rate,
                        json.dumps(result.feature_data, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO classifications(
                        analysis_id, label, match_score, explanation, classifier_version
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        analysis_id,
                        result.classification,
                        result.classification_confidence,
                        explanation,
                        CLASSIFIER_VERSION,
                    ),
                )
                connection.commit()
                return analysis_id
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        raw_feature = record.pop("feature_json", "{}")
        try:
            record["feature_data"] = json.loads(raw_feature or "{}", parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            record["feature_data"] = {}
        record["truncated"] = bool(record.get("truncated"))
        return record

    def list_analyses(self, limit: int = 200, search: str = "", classification: str = "") -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if search:
            clauses.append("tracks.file_name LIKE ?")
            parameters.append(f"%{search}%")
        if classification:
            clauses.append("classifications.label = ?")
            parameters.append(classification)
        query = SELECT_RECORD
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY analyses.id DESC LIMIT ?"
        parameters.append(max(1, min(1_000, int(limit))))
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        records = [self._row_to_dict(row) for row in rows]
        # 列表接口不传大体积曲线；详情接口再读取。
        for record in records:
            record.pop("feature_data", None)
        return records

    def get_analysis(self, analysis_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(SELECT_RECORD + " WHERE analyses.id = ?", (analysis_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def find_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                SELECT_RECORD + " WHERE tracks.file_hash = ? ORDER BY analyses.id DESC LIMIT 1",
                (file_hash,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def delete_analysis(self, analysis_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT tracks.stored_name FROM analyses
                JOIN tracks ON tracks.id = analyses.track_id
                WHERE analyses.id = ?
                """,
                (analysis_id,),
            ).fetchone()
            if not row:
                return None
            stored_name = row["stored_name"]
            connection.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
            connection.execute(
                "DELETE FROM tracks WHERE id NOT IN (SELECT DISTINCT track_id FROM analyses)"
            )
            connection.commit()
            return stored_name

    def statistics(self) -> dict[str, Any]:
        with self.connect() as connection:
            summary = connection.execute(
                """
                SELECT COUNT(*) AS analysis_count,
                       COALESCE(SUM(tracks.duration), 0) AS total_duration,
                       COALESCE(AVG(NULLIF(features.tempo, 0)), 0) AS average_tempo,
                       COALESCE(AVG(features.rms_db), 0) AS average_rms
                FROM analyses
                JOIN tracks ON tracks.id = analyses.track_id
                JOIN features ON features.analysis_id = analyses.id
                """
            ).fetchone()
            class_rows = connection.execute(
                """
                SELECT label, COUNT(*) AS count
                FROM classifications GROUP BY label ORDER BY count DESC, label
                """
            ).fetchall()
            recent = connection.execute(
                "SELECT analyzed_at FROM analyses ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "analysis_count": int(summary["analysis_count"]),
            "total_duration": round(float(summary["total_duration"]), 2),
            "average_tempo": round(float(summary["average_tempo"]), 1),
            "average_rms": round(float(summary["average_rms"]), 1),
            "classifications": [{"label": row["label"], "count": int(row["count"])} for row in class_rows],
            "last_analyzed_at": recent["analyzed_at"] if recent else None,
        }

    def all_records(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(SELECT_RECORD + " ORDER BY analyses.id").fetchall()
        return [self._row_to_dict(row) for row in rows]

    def log_operation(
        self,
        operation_type: str,
        file_name: str,
        success_count: int,
        failed_count: int,
        message: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO data_operations(
                    operation_type, file_name, success_count, failed_count, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (operation_type, file_name, success_count, failed_count, message, now_iso()),
            )
            connection.commit()

    @staticmethod
    def _float(record: Mapping[str, Any], key: str, default: float = 0.0) -> float:
        value = record.get(key, default)
        if value in (None, ""):
            return default
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return default
        return number

    @staticmethod
    def _int(record: Mapping[str, Any], key: str, default: int = 0) -> int:
        value = record.get(key, default)
        return default if value in (None, "") else int(float(value))

    def import_records(self, records: Sequence[Mapping[str, Any]], source_file: str) -> dict[str, Any]:
        """导入本项目 JSON/CSV 的扁平记录；单条失败不影响其他有效记录。"""
        success = 0
        skipped = 0
        errors: list[str] = []
        with self.connect() as connection:
            for index, raw_record in enumerate(records, start=1):
                try:
                    record = dict(raw_record)
                    file_name = str(record.get("file_name", "")).strip()
                    if not file_name:
                        raise ValueError("缺少 file_name")
                    file_hash = str(record.get("file_hash", "")).strip() or None
                    analyzed_at = str(record.get("analyzed_at", "")).strip() or now_iso()

                    # 同一哈希 + 同一分析时间视为同一条导出记录，避免反复导入膨胀。
                    if file_hash:
                        duplicate = connection.execute(
                            """
                            SELECT 1 FROM analyses JOIN tracks ON tracks.id = analyses.track_id
                            WHERE tracks.file_hash = ? AND analyses.analyzed_at = ? LIMIT 1
                            """,
                            (file_hash, analyzed_at),
                        ).fetchone()
                        if duplicate:
                            skipped += 1
                            continue

                    feature_data = record.get("feature_data", {})
                    if isinstance(feature_data, str):
                        feature_data = json.loads(feature_data, parse_constant=_reject_json_constant) if feature_data.strip() else {}
                    if not isinstance(feature_data, dict):
                        feature_data = {}
                    serialized_feature = json.dumps(
                        feature_data,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )

                    connection.execute("SAVEPOINT import_record")
                    track_cursor = connection.execute(
                        """
                        INSERT INTO tracks(
                            file_name, stored_name, file_hash, file_format, file_size,
                            duration, sample_rate, channels, sample_width, source, created_at
                        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 'import', ?)
                        """,
                        (
                            file_name,
                            file_hash,
                            str(record.get("file_format", "WAV"))[:12],
                            self._int(record, "file_size"),
                            self._float(record, "duration"),
                            self._int(record, "sample_rate"),
                            self._int(record, "channels"),
                            self._int(record, "sample_width"),
                            str(record.get("created_at", analyzed_at)),
                        ),
                    )
                    analysis_cursor = connection.execute(
                        """
                        INSERT INTO analyses(
                            track_id, analyzer_version, analyzed_at, elapsed_ms,
                            analyzed_seconds, truncated
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(track_cursor.lastrowid),
                            str(record.get("analyzer_version", ANALYZER_VERSION)),
                            analyzed_at,
                            self._int(record, "elapsed_ms"),
                            self._float(record, "analyzed_seconds"),
                            1 if str(record.get("truncated", "0")).lower() in ("1", "true", "yes") else 0,
                        ),
                    )
                    analysis_id = int(analysis_cursor.lastrowid)
                    connection.execute(
                        """
                        INSERT INTO features VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            analysis_id,
                            self._float(record, "tempo"),
                            self._float(record, "tempo_confidence"),
                            str(record.get("key_name", "未知")),
                            str(record.get("musical_mode", "未知")),
                            self._float(record, "key_confidence"),
                            self._float(record, "rms_db", -200.0),
                            self._float(record, "peak_db", -200.0),
                            self._float(record, "dynamic_range_db"),
                            self._float(record, "avg_pitch_hz"),
                            str(record.get("pitch_note", "未知")),
                            self._float(record, "spectral_centroid"),
                            self._float(record, "spectral_rolloff"),
                            self._float(record, "zero_crossing_rate"),
                            serialized_feature,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO classifications VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            analysis_id,
                            str(record.get("classification", "未分类")),
                            self._float(record, "classification_confidence"),
                            str(record.get("explanation", "从文件导入")),
                            str(record.get("classifier_version", CLASSIFIER_VERSION)),
                        ),
                    )
                    connection.execute("RELEASE SAVEPOINT import_record")
                    success += 1
                except Exception as exc:
                    try:
                        connection.execute("ROLLBACK TO SAVEPOINT import_record")
                        connection.execute("RELEASE SAVEPOINT import_record")
                    except sqlite3.Error:
                        pass
                    errors.append(f"第 {index} 条：{exc}")
            connection.commit()

        failed = len(errors)
        message = f"成功 {success} 条，跳过重复 {skipped} 条，失败 {failed} 条"
        try:
            self.log_operation("import", source_file, success, failed, message)
        except sqlite3.Error as exc:
            # 审计日志失败不应把已成功提交的主导入误报为失败。
            errors.append(f"操作日志写入失败：{exc}")
        return {"success": success, "skipped": skipped, "failed": failed, "errors": errors[:10], "message": message}

    def backup_to(self, destination: str | Path) -> Path:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3.Connection 的 with 只管理事务，不会关闭连接；closing 可确保
        # Windows 下载/测试后立即释放 .db 文件句柄。
        with self.connect() as source, closing(sqlite3.connect(destination_path)) as target:
            source.backup(target)
        return destination_path
