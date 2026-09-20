#!/usr/bin/env python3
"""Regression checks for job concurrency, real process deadlines, and Claude setup."""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import transcribe
import claude_text


class RuntimeTests(unittest.TestCase):
    def test_silent_process_obeys_deadline(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            transcribe._stream([sys.executable, '-c', 'import time; time.sleep(10)'],
                               None, 0.15, lambda _: None)
        self.assertLess(time.monotonic() - started, 3)

    def test_output_does_not_extend_deadline(self):
        lines = []
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            transcribe._stream([sys.executable, '-u', '-c',
                               'import time\nwhile True:\n print("progress")\n time.sleep(.02)'],
                               None, 0.15, lines.append)
        self.assertTrue(lines)
        self.assertLess(time.monotonic() - started, 3)

    def test_child_holding_stdout_is_killed(self):
        started = time.monotonic()
        script = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(10)"])'
        with self.assertRaises(subprocess.TimeoutExpired):
            transcribe._stream([sys.executable, '-c', script], None, 0.15, lambda _: None)
        self.assertLess(time.monotonic() - started, 3)

    def test_regular_process_deadline(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            transcribe.processes.run([sys.executable, '-c', 'import time; time.sleep(10)'],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=.15)

    def test_shutdown_stops_active_children(self):
        script = ("import processes,sys; "
                  "p=processes.spawn([sys.executable,'-c','import time; time.sleep(10)']); "
                  "processes.stop_all(); assert p.wait(timeout=2) != 0; processes.release(p)")
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).parent,
                                capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_success_keeps_progress_and_exit_code(self):
        lines = []
        code, tail = transcribe._stream([sys.executable, '-c', 'print("done")'], None, 3, lines.append)
        self.assertEqual((code, tail, lines), (0, 'done', ['done']))

    def test_shared_jobs_and_duplicate_requests_over_http(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        active = 0
        peak = 0
        state_lock = threading.Lock()
        @server.serialized
        def fake_summary(vid):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
                calls.append(vid)
            entered.set()
            release.wait(5)
            with state_lock:
                active -= 1
            return 200, {'video_id': vid, 'summary': 'A summary'}

        server.JOBS.clear()
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        def post(route, vid):
            connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=2)
            connection.request('POST', route, json.dumps({'id': vid}),
                               {'Host': server.LOCAL_HOSTS[0],
                                'Origin': 'chrome-extension://' + server.CHROME_EXTENSION_ID,
                                'Content-Type': 'application/json'})
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result
        try:
            with patch.object(server, 'summarize_video', fake_summary):
                self.assertEqual(post('/summarize', 'aaaaaaaaaaa')[0], 202)
                self.assertTrue(entered.wait(2))
                self.assertEqual(post('/summarize', 'aaaaaaaaaaa')[0], 202)
                self.assertEqual(post('/summarize', 'bbbbbbbbbbb')[0], 202)
                self.assertEqual(post('/job', 'aaaaaaaaaaa')[0], 202)
                # A second entry point (e.g. /ask) shares the same pipeline lock.
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    chat = pool.submit(fake_summary, 'ccccccccccc')
                    release.set()
                    chat.result(timeout=3)
                deadline = time.monotonic() + 3
                while post('/job', 'bbbbbbbbbbb')[0] == 202 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(post('/job', 'bbbbbbbbbbb')[0], 200)
                self.assertEqual(calls.count('aaaaaaaaaaa'), 1)
                self.assertEqual(peak, 1)
                self.assertEqual(post('/job', 'ddddddddddd')[0], 404)
        finally:
            release.set()
            httpd.shutdown()
            httpd.server_close()

    def test_failed_job_is_reported_and_next_job_runs(self):
        def fake(vid):
            if vid == 'eeeeeeeeeee':
                raise RuntimeError('test failure')
            return 200, {'summary': 'next job'}
        with patch.object(server, 'summarize_video', fake):
            for vid in ['eeeeeeeeeee', 'fffffffffff']:
                server.start_job(vid)
            deadline = time.monotonic() + 3
            while server.job_read('fffffffffff')[0] == 202 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(server.job_read('eeeeeeeeeee')[0], 500)
            self.assertEqual(server.job_read('fffffffffff')[0], 200)

    def test_queue_is_bounded(self):
        with patch.dict(server.JOBS, {'queued': {'status': 202, 'payload': {}}}, clear=True), \
             patch.object(server, 'MAX_PENDING_JOBS', 1):
            self.assertEqual(server.start_job('ggggggggggg')[0], 429)

    def test_cached_only_read_never_fetches(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(server, 'TRANSCRIPT_DIR', Path(directory)), \
             patch.object(server, 'fetch_captions', side_effect=AssertionError('download started')):
            self.assertEqual(server.fetch_transcript('hhhhhhhhhhh', cached_only=True)[2], 404)

    def test_model_runner_uses_selected_model_and_empty_directory(self):
        seen = {}
        def fake_run(cmd, **kwargs):
            seen.update(cmd=cmd, **kwargs)
            self.assertEqual(list(Path(kwargs['cwd']).iterdir()), [])
            return subprocess.CompletedProcess(cmd, 0, b'ready', b'')
        with patch.object(claude_text, 'claude_bin', return_value=sys.executable), \
             patch.object(claude_text.processes, 'run', side_effect=fake_run):
            self.assertEqual(claude_text.run('check', model='sonnet', timeout=60), 'ready')
        self.assertEqual(seen['cmd'][seen['cmd'].index('--model') + 1], 'sonnet')
        self.assertEqual(seen['timeout'], 60)
        self.assertFalse(Path(seen['cwd']).exists())


if __name__ == '__main__':
    unittest.main()
