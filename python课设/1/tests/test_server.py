from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from music_analyzer.audio import generate_demo_wav
from music_analyzer.database import MusicDatabase
from music_analyzer.server import AppPaths, build_handler


class ServerSmokeTests(unittest.TestCase):
    def test_health_demo_history_and_export_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "static").mkdir()
            paths = AppPaths.from_root(root)
            database = MusicDatabase(paths.data / "test.db")
            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(database, paths))
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(f"{base}/api/health", timeout=10) as response:
                    health = json.loads(response.read().decode("utf-8"))
                self.assertTrue(health["ok"])

                request = Request(f"{base}/api/demo", data=b"", method="POST")
                with urlopen(request, timeout=30) as response:
                    demo = json.loads(response.read().decode("utf-8"))
                self.assertTrue(demo["ok"])
                record_id = demo["record"]["id"]

                upload_source = generate_demo_wav(root / "same_audio.wav")
                upload_request = Request(
                    f"{base}/api/analyze?filename=same_audio.wav",
                    data=upload_source.read_bytes(),
                    method="POST",
                )
                with urlopen(upload_request, timeout=30) as response:
                    duplicate_upload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(duplicate_upload["duplicate"])
                self.assertEqual(duplicate_upload["record"]["id"], record_id)

                with urlopen(f"{base}/api/analyses/{record_id}", timeout=10) as response:
                    detail = json.loads(response.read().decode("utf-8"))
                self.assertEqual(detail["record"]["id"], record_id)
                self.assertTrue(detail["record"]["feature_data"]["waveform"])

                audio_request = Request(f"{base}/api/audio/{record_id}", headers={"Range": "bytes=0-99"})
                with urlopen(audio_request, timeout=10) as response:
                    audio_prefix = response.read()
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                self.assertEqual(len(audio_prefix), 100)
                self.assertEqual(audio_prefix[:4], b"RIFF")

                malicious_request = Request(
                    f"{base}/api/demo",
                    data=b"",
                    method="POST",
                    headers={"Origin": "https://example.invalid"},
                )
                try:
                    urlopen(malicious_request, timeout=10)
                    self.fail("跨站请求应被拒绝")
                except HTTPError as forbidden:
                    self.assertEqual(forbidden.code, 403)
                    forbidden.close()

                import_body = json.dumps(
                    {"schema_version": 1, "records": [{"file_name": "=danger.wav", "classification": "测试"}]}
                ).encode("utf-8")
                import_request = Request(
                    f"{base}/api/import?filename=records.json",
                    data=import_body,
                    method="POST",
                )
                with urlopen(import_request, timeout=10) as response:
                    imported = json.loads(response.read().decode("utf-8"))
                self.assertEqual(imported["result"]["success"], 1)

                with urlopen(f"{base}/api/export?format=csv", timeout=10) as response:
                    csv_text = response.read().decode("utf-8-sig")
                self.assertIn("'=danger.wav", csv_text)

                with urlopen(f"{base}/api/analyses?search=%3Ddanger", timeout=10) as response:
                    imported_list = json.loads(response.read().decode("utf-8"))
                imported_id = imported_list["records"][0]["id"]
                delete_request = Request(f"{base}/api/analyses/{imported_id}", method="DELETE")
                with urlopen(delete_request, timeout=10) as response:
                    self.assertEqual(response.status, 200)

                with urlopen(f"{base}/api/export?format=json", timeout=10) as response:
                    exported = json.loads(response.read().decode("utf-8"))
                self.assertEqual(exported["record_count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
