"""基于 Python 标准库的本地 Web 服务与 REST API。"""

from __future__ import annotations

import argparse
import csv
import io
import json
import mimetypes
import re
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .audio import AudioAnalysisError, analyze_wav, generate_demo_wav, supported_wav_description
from .database import MusicDatabase, SCHEMA_VERSION, file_sha256, now_iso


MAX_AUDIO_BYTES = 80 * 1024 * 1024
MAX_IMPORT_BYTES = 25 * 1024 * 1024
CSV_FIELDS = (
    "file_name",
    "file_hash",
    "file_format",
    "file_size",
    "duration",
    "sample_rate",
    "channels",
    "sample_width",
    "source",
    "created_at",
    "analyzed_at",
    "analyzer_version",
    "elapsed_ms",
    "analyzed_seconds",
    "truncated",
    "tempo",
    "tempo_confidence",
    "key_name",
    "musical_mode",
    "key_confidence",
    "rms_db",
    "peak_db",
    "dynamic_range_db",
    "avg_pitch_hz",
    "pitch_note",
    "spectral_centroid",
    "spectral_rolloff",
    "zero_crossing_rate",
    "classification",
    "classification_confidence",
    "explanation",
    "classifier_version",
)
CSV_TEXT_FIELDS = {
    "file_name",
    "file_hash",
    "file_format",
    "source",
    "created_at",
    "analyzed_at",
    "analyzer_version",
    "key_name",
    "musical_mode",
    "pitch_note",
    "classification",
    "explanation",
    "classifier_version",
}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON 不允许非有限数值 {value}")


def _csv_safe_text(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _restore_csv_text(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("'") and value[1:].lstrip().startswith(("=", "+", "-", "@")):
        return value[1:]
    return value


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    static: Path
    data: Path
    uploads: Path
    exports: Path

    @classmethod
    def from_root(cls, root: Path) -> "AppPaths":
        data = root / "data"
        paths = cls(root=root, static=root / "static", data=data, uploads=data / "uploads", exports=data / "exports")
        paths.uploads.mkdir(parents=True, exist_ok=True)
        paths.exports.mkdir(parents=True, exist_ok=True)
        return paths


def _safe_filename(raw_name: str, fallback: str = "audio.wav") -> str:
    name = Path(unquote(raw_name)).name.strip().replace("\x00", "")
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name)
    return name[:180] or fallback


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _decorate_record(record: dict[str, Any] | None, paths: AppPaths) -> dict[str, Any] | None:
    if record is None:
        return None
    decorated = dict(record)
    stored_name = decorated.get("stored_name")
    decorated["audio_available"] = bool(stored_name and (paths.uploads / str(stored_name)).is_file())
    return decorated


def build_handler(database: MusicDatabase, paths: AppPaths):
    analysis_lock = threading.Lock()

    class MusicScopeHandler(BaseHTTPRequestHandler):
        server_version = "MusicScope/1.0"

        def log_message(self, format_string: str, *args: Any) -> None:
            print(f"[{self.log_date_time_string()}] {self.address_string()} - {format_string % args}")

        def _send_headers(
            self,
            status: int,
            content_type: str,
            content_length: int,
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()

        def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            body = _json_bytes(payload)
            self._send_headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _send_error_json(self, message: str, status: int = HTTPStatus.BAD_REQUEST, detail: str | None = None) -> None:
            payload: dict[str, Any] = {"ok": False, "error": message}
            if detail:
                payload["detail"] = detail
            self._send_json(payload, status)

        def _safe_log(self, operation_type: str, file_name: str, success: int, failed: int, message: str) -> None:
            try:
                database.log_operation(operation_type, file_name, success, failed, message)
            except Exception as exc:
                print(f"数据操作日志写入失败：{exc}")

        def _content_length(self, maximum: int) -> int:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("请求长度无效。") from exc
            if length <= 0:
                raise ValueError("没有收到文件内容。")
            if length > maximum:
                raise ValueError(f"文件过大，最大允许 {maximum // (1024 * 1024)} MB。")
            return length

        def _read_body(self, maximum: int) -> bytes:
            length = self._content_length(maximum)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("文件上传不完整，请重试。")
            return body

        def _receive_to_file(self, destination: Path, maximum: int) -> int:
            """把请求体分块写入临时文件，避免 80 MB 上传同时驻留内存。"""
            length = self._content_length(maximum)
            remaining = length
            with destination.open("wb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("文件上传不完整，请重试。")
                    handle.write(chunk)
                    remaining -= len(chunk)
            return length

        def _allow_state_change(self) -> bool:
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                self._send_error_json("已拒绝跨站操作。", HTTPStatus.FORBIDDEN)
                return False
            origin = self.headers.get("Origin")
            host_header = self.headers.get("Host", "").lower()
            if origin and urlparse(origin).netloc.lower() != host_header:
                self._send_error_json("请求来源与本地服务不一致。", HTTPStatus.FORBIDDEN)
                return False
            return True

        def _live_duplicate(self, digest: str) -> dict[str, Any] | None:
            duplicate = database.find_by_hash(digest)
            if not duplicate or not duplicate.get("stored_name"):
                return None
            audio_path = paths.uploads / Path(str(duplicate["stored_name"])).name
            return duplicate if audio_path.is_file() else None

        def _serve_file(self, file_path: Path, content_type: str | None = None) -> None:
            if not file_path.is_file():
                self._send_error_json("资源不存在。", HTTPStatus.NOT_FOUND)
                return
            body = file_path.read_bytes()
            mime = content_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            if mime.startswith("text/") or mime in ("application/javascript", "image/svg+xml"):
                mime += "; charset=utf-8"
            self._send_headers(HTTPStatus.OK, mime, len(body))
            self.wfile.write(body)

        def _serve_audio(self, file_path: Path) -> None:
            """支持 Range 的 WAV 流，允许浏览器播放并拖动进度。"""
            if not file_path.is_file():
                self._send_error_json("音频文件不存在。", HTTPStatus.NOT_FOUND)
                return
            file_size = file_path.stat().st_size
            start = 0
            end = max(0, file_size - 1)
            status = HTTPStatus.OK
            range_header = self.headers.get("Range", "").strip()
            if range_header:
                try:
                    if not range_header.startswith("bytes=") or "," in range_header:
                        raise ValueError
                    start_text, end_text = range_header[6:].split("-", 1)
                    if start_text:
                        start = int(start_text)
                        end = int(end_text) if end_text else file_size - 1
                    else:
                        suffix_length = int(end_text)
                        if suffix_length <= 0:
                            raise ValueError
                        start = max(0, file_size - suffix_length)
                        end = file_size - 1
                    end = min(end, file_size - 1)
                    if start < 0 or start >= file_size or end < start:
                        raise ValueError
                    status = HTTPStatus.PARTIAL_CONTENT
                except (ValueError, OverflowError):
                    self._send_headers(
                        HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                        "audio/wav",
                        0,
                        {"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
                    )
                    return
            length = max(0, end - start + 1)
            extra = {"Accept-Ranges": "bytes"}
            if status == HTTPStatus.PARTIAL_CONTENT:
                extra["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            self._send_headers(status, "audio/wav", length, extra)
            with file_path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _send_download_file(self, file_path: Path, content_type: str, download_name: str) -> None:
            size = file_path.stat().st_size
            encoded = quote(download_name)
            self._send_headers(
                HTTPStatus.OK,
                content_type,
                size,
                {"Content-Disposition": f"attachment; filename=musicscope_backup.db; filename*=UTF-8''{encoded}"},
            )
            with file_path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    self.wfile.write(chunk)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            route = parsed.path
            query = parse_qs(parsed.query)
            try:
                if route == "/api/health":
                    self._send_json(
                        {
                            "ok": True,
                            "service": "MusicScope",
                            "version": "1.0.0",
                            "schema_version": SCHEMA_VERSION,
                            "supported_audio": supported_wav_description(),
                            "time": now_iso(),
                        }
                    )
                    return
                if route == "/api/stats":
                    self._send_json({"ok": True, "stats": database.statistics()})
                    return
                if route == "/api/analyses":
                    limit = int(query.get("limit", ["200"])[0])
                    search = query.get("search", [""])[0].strip()
                    category = query.get("classification", [""])[0].strip()
                    records = database.list_analyses(limit=limit, search=search, classification=category)
                    self._send_json({"ok": True, "records": records})
                    return
                if route.startswith("/api/analyses/"):
                    analysis_id = int(route.rsplit("/", 1)[-1])
                    record = _decorate_record(database.get_analysis(analysis_id), paths)
                    if not record:
                        self._send_error_json("找不到这条分析记录。", HTTPStatus.NOT_FOUND)
                    else:
                        self._send_json({"ok": True, "record": record})
                    return
                if route.startswith("/api/audio/"):
                    analysis_id = int(route.rsplit("/", 1)[-1])
                    record = database.get_analysis(analysis_id)
                    stored_name = record.get("stored_name") if record else None
                    if not stored_name:
                        self._send_error_json("导入记录不包含原始音频。", HTTPStatus.NOT_FOUND)
                        return
                    audio_path = paths.uploads / Path(str(stored_name)).name
                    self._serve_audio(audio_path)
                    return
                if route == "/api/export":
                    export_format = query.get("format", ["json"])[0].lower()
                    self._handle_export(export_format)
                    return
                if route == "/api/database-backup":
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    file_name = f"MusicScope_数据库备份_{stamp}.db"
                    backup_path = database.backup_to(paths.exports / file_name)
                    backups = sorted(
                        paths.exports.glob("MusicScope_数据库备份_*.db"),
                        key=lambda item: item.stat().st_mtime,
                        reverse=True,
                    )
                    for stale_backup in backups[10:]:
                        stale_backup.unlink(missing_ok=True)
                    self._send_download_file(backup_path, "application/vnd.sqlite3", file_name)
                    self._safe_log("backup", file_name, 1, 0, "SQLite 数据库备份成功")
                    return
                if route == "/" or route == "/index.html":
                    self._serve_file(paths.static / "index.html")
                    return
                if route == "/favicon.svg":
                    self._serve_file(paths.static / "favicon.svg")
                    return
                if route.startswith("/static/"):
                    relative = Path(route[len("/static/") :])
                    target = (paths.static / relative).resolve()
                    if paths.static.resolve() not in target.parents:
                        self._send_error_json("非法资源路径。", HTTPStatus.FORBIDDEN)
                        return
                    self._serve_file(target)
                    return
                self._send_error_json("页面不存在。", HTTPStatus.NOT_FOUND)
            except (ValueError, TypeError) as exc:
                self._send_error_json("请求参数无效。", detail=str(exc))
            except BrokenPipeError:
                return
            except Exception as exc:  # 防止服务线程因单个请求退出
                self._send_error_json("服务器处理请求时出现异常。", HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def _handle_export(self, export_format: str) -> None:
            records = database.all_records()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if export_format == "json":
                payload = {
                    "application": "MusicScope",
                    "schema_version": SCHEMA_VERSION,
                    "exported_at": now_iso(),
                    "record_count": len(records),
                    "records": records,
                }
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
                file_name = f"MusicScope_完整数据_{stamp}.json"
                content_type = "application/json; charset=utf-8"
            elif export_format == "csv":
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
                writer.writeheader()
                for record in records:
                    writer.writerow(
                        {
                            field: _csv_safe_text(record.get(field)) if field in CSV_TEXT_FIELDS else record.get(field)
                            for field in CSV_FIELDS
                        }
                    )
                body = ("\ufeff" + stream.getvalue()).encode("utf-8")
                file_name = f"MusicScope_数据摘要_{stamp}.csv"
                content_type = "text/csv; charset=utf-8"
            else:
                self._send_error_json("仅支持 JSON 或 CSV 导出。")
                return
            encoded = quote(file_name)
            self._send_headers(
                HTTPStatus.OK,
                content_type,
                len(body),
                {"Content-Disposition": f"attachment; filename=musicscope_export.{export_format}; filename*=UTF-8''{encoded}"},
            )
            self.wfile.write(body)
            self._safe_log("export", file_name, len(records), 0, f"导出 {len(records)} 条记录")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            query = parse_qs(parsed.query)
            if not self._allow_state_change():
                return
            try:
                if route == "/api/analyze":
                    file_name = _safe_filename(query.get("filename", ["audio.wav"])[0])
                    self._handle_analyze(file_name)
                    return
                if route == "/api/demo":
                    self._handle_demo()
                    return
                if route == "/api/import":
                    file_name = _safe_filename(query.get("filename", ["import.json"])[0], "import.json")
                    self._handle_import(file_name)
                    return
                self._send_error_json("接口不存在。", HTTPStatus.NOT_FOUND)
            except AudioAnalysisError as exc:
                self._send_error_json(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY)
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError, csv.Error) as exc:
                self._send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
            except BrokenPipeError:
                return
            except Exception as exc:
                self._send_error_json("操作失败。", HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def _handle_analyze(self, file_name: str) -> None:
            if Path(file_name).suffix.lower() != ".wav":
                raise AudioAnalysisError("当前离线版仅支持 PCM WAV；请先把 MP3/M4A/FLAC 转换为 WAV。")
            stored_name = f"{uuid.uuid4().hex}.wav"
            upload_path = paths.uploads / stored_name
            partial_path = paths.uploads / f".{stored_name}.part"
            analysis_id: int | None = None
            try:
                file_size = self._receive_to_file(partial_path, MAX_AUDIO_BYTES)
                partial_path.replace(upload_path)
                digest = file_sha256(upload_path)
                with analysis_lock:
                    duplicate = self._live_duplicate(digest)
                if duplicate:
                    upload_path.unlink(missing_ok=True)
                    duplicate = _decorate_record(duplicate, paths)
                    self._send_json(
                        {
                            "ok": True,
                            "duplicate": True,
                            "message": "该音频已分析过，已为你打开原记录。",
                            "record": duplicate,
                        }
                    )
                    return
                started = time.perf_counter()
                result = analyze_wav(upload_path)
                elapsed_ms = round((time.perf_counter() - started) * 1_000)
                with analysis_lock:
                    # 两个标签页可能同时上传同一文件；分析后再次原子检查。
                    duplicate = self._live_duplicate(digest)
                    if duplicate is None:
                        analysis_id = database.insert_analysis(
                            file_name=file_name,
                            stored_name=stored_name,
                            file_hash=digest,
                            file_size=file_size,
                            source="upload",
                            result=result,
                            elapsed_ms=elapsed_ms,
                        )
                if duplicate is not None:
                    upload_path.unlink(missing_ok=True)
                    self._send_json(
                        {
                            "ok": True,
                            "duplicate": True,
                            "message": "该音频已由另一个任务完成分析，已打开原记录。",
                            "record": _decorate_record(duplicate, paths),
                        }
                    )
                    return
                record = _decorate_record(database.get_analysis(analysis_id), paths)
                self._send_json({"ok": True, "duplicate": False, "record": record}, HTTPStatus.CREATED)
            except Exception:
                partial_path.unlink(missing_ok=True)
                # 入库完成后即使客户端断开，也保留音频与数据库的一致关系。
                if analysis_id is None:
                    upload_path.unlink(missing_ok=True)
                raise

        def _handle_demo(self) -> None:
            display_name = f"演示音频_C大调_120BPM_{datetime.now().strftime('%H%M%S')}.wav"
            stored_name = f"demo_{uuid.uuid4().hex}.wav"
            audio_path = paths.uploads / stored_name
            analysis_id: int | None = None
            try:
                generate_demo_wav(audio_path)
                started = time.perf_counter()
                result = analyze_wav(audio_path)
                elapsed_ms = round((time.perf_counter() - started) * 1_000)
                analysis_id = database.insert_analysis(
                    file_name=display_name,
                    stored_name=stored_name,
                    file_hash=file_sha256(audio_path),
                    file_size=audio_path.stat().st_size,
                    source="demo",
                    result=result,
                    elapsed_ms=elapsed_ms,
                )
                record = _decorate_record(database.get_analysis(analysis_id), paths)
                self._send_json({"ok": True, "record": record}, HTTPStatus.CREATED)
            except Exception:
                if analysis_id is None:
                    audio_path.unlink(missing_ok=True)
                raise

        def _handle_import(self, file_name: str) -> None:
            body = self._read_body(MAX_IMPORT_BYTES)
            suffix = Path(file_name).suffix.lower()
            if suffix == ".json":
                payload = json.loads(body.decode("utf-8-sig"), parse_constant=_reject_json_constant)
                if isinstance(payload, dict):
                    version = int(payload.get("schema_version", SCHEMA_VERSION))
                    if version > SCHEMA_VERSION:
                        raise ValueError(f"文件版本 {version} 高于本程序支持的版本 {SCHEMA_VERSION}。")
                    records = payload.get("records")
                else:
                    records = payload
                if not isinstance(records, list):
                    raise ValueError("JSON 中缺少 records 数组。")
            elif suffix == ".csv":
                text = body.decode("utf-8-sig")
                records = list(csv.DictReader(io.StringIO(text)))
                for record in records:
                    for field in CSV_TEXT_FIELDS:
                        if field in record:
                            record[field] = _restore_csv_text(record[field])
            else:
                raise ValueError("导入文件必须是本系统导出的 .json 或 .csv。")
            if len(records) > 10_000:
                raise ValueError("单次最多导入 10000 条记录。")
            result = database.import_records(records, file_name)
            self._send_json({"ok": True, "result": result})

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._allow_state_change():
                return
            route = urlparse(self.path).path
            if not route.startswith("/api/analyses/"):
                self._send_error_json("接口不存在。", HTTPStatus.NOT_FOUND)
                return
            try:
                analysis_id = int(route.rsplit("/", 1)[-1])
                record = database.get_analysis(analysis_id)
                if record is None:
                    self._send_error_json("记录不存在。", HTTPStatus.NOT_FOUND)
                    return
                stored_name = record.get("stored_name")
                audio_path = paths.uploads / Path(str(stored_name)).name if stored_name else None
                quarantine_path: Path | None = None
                if audio_path and audio_path.exists():
                    quarantine_path = paths.uploads / f".delete-{uuid.uuid4().hex}.tmp"
                    try:
                        audio_path.replace(quarantine_path)
                    except PermissionError:
                        self._send_error_json("音频仍在播放，请暂停播放器后再删除。", HTTPStatus.CONFLICT)
                        return
                try:
                    database.delete_analysis(analysis_id)
                except Exception:
                    if quarantine_path and quarantine_path.exists() and audio_path:
                        quarantine_path.replace(audio_path)
                    raise
                if quarantine_path:
                    try:
                        quarantine_path.unlink(missing_ok=True)
                    except OSError as exc:
                        print(f"待清理音频 {quarantine_path.name}：{exc}")
                self._send_json({"ok": True, "message": "记录及其本地音频已删除。"})
            except ValueError:
                self._send_error_json("记录编号无效。")
            except Exception as exc:
                self._send_error_json("删除失败。", HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    return MusicScopeHandler


def run_server(root: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    if host.lower() not in {"127.0.0.1", "localhost"}:
        raise ValueError("为保护本地音频与数据库，MusicScope 仅允许绑定 127.0.0.1 或 localhost。")
    paths = AppPaths.from_root(root)
    database = MusicDatabase(paths.data / "musicscope.db")
    try:
        server = ThreadingHTTPServer((host, port), build_handler(database, paths))
    except OSError as exc:
        if port == 0:
            raise
        print(f"端口 {port} 不可用（{exc}），正在自动选择空闲端口……")
        server = ThreadingHTTPServer((host, 0), build_handler(database, paths))
    server.daemon_threads = True
    actual_host, actual_port = server.server_address[:2]
    browser_host = "127.0.0.1" if actual_host in ("0.0.0.0", "::") else actual_host
    url = f"http://{browser_host}:{actual_port}"
    print("=" * 64)
    print("  MusicScope 音乐特征分析器 1.0")
    print(f"  访问地址：{url}")
    print("  按 Ctrl+C 可停止服务")
    print("=" * 64)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在安全关闭 MusicScope……")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MusicScope 音乐特征分析器")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"), help="监听地址（仅允许本机）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口；设为 0 可自动分配")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    run_server(root, arguments.host, arguments.port, not arguments.no_browser)
