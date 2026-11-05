from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from infinity_research.executor import ExecutionError, _docker_command, execute_candidate


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform == "linux":
        # Some test environments expose a host /proc with namespace-local PIDs
        # from getpid()/Popen.  Resolve NSpid rather than inspecting an unrelated
        # host process which happens to have the same number.
        namespace = os.readlink("/proc/self/ns/pid")
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                if os.readlink(process / "ns/pid") != namespace:
                    continue
                fields = dict(
                    line.split(":", 1) for line in (process / "status").read_text().splitlines()
                )
                namespace_pid = int(fields.get("NSpid", fields["Pid"]).split()[-1])
                if namespace_pid == pid:
                    return fields["State"].strip()[0] != "Z"
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
    return True


@unittest.skipUnless(os.name == "posix", "local fixtures require POSIX process groups")
class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        self.prepared = self.root / "data"
        (self.prepared / "public").mkdir(parents=True)
        (self.prepared / "public" / "manifest.json").write_text('{"split": "validation"}')
        self.output = self.root / "result" / "output"
        self.config = {"kind": "local", "allow_local": True}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def program(self, source: str) -> None:
        (self.candidate / "main.py").write_text(source)

    def execute(self, *, timeout: float = 5, **kwargs):
        return execute_candidate(
            self.candidate,
            self.prepared,
            self.output,
            self.config,
            timeout,
            seed=17,
            **kwargs,
        )

    def test_runs_real_candidate_with_fixed_arguments_and_seed(self) -> None:
        self.program(
            "import json, os, pathlib, sys\n"
            "out = pathlib.Path(sys.argv[2])\n"
            "out.joinpath('seen.json').write_text(json.dumps({\n"
            " 'manifest': json.load(open(sys.argv[1])),\n"
            " 'seed': os.environ['RESEARCH_SEED'],\n"
            " 'cuda': os.environ['CUDA_VISIBLE_DEVICES'],\n"
            " 'secret': os.environ.get('EXECUTOR_TEST_SECRET')}))\n"
            "print('candidate finished')\n"
        )
        os.environ["EXECUTOR_TEST_SECRET"] = "do-not-pass"
        try:
            result = self.execute()
        finally:
            del os.environ["EXECUTOR_TEST_SECRET"]
        seen = json.loads((self.output / "seen.json").read_text())
        self.assertEqual(seen["manifest"], {"split": "validation"})
        self.assertEqual(seen["seed"], "17")
        self.assertEqual(seen["cuda"], "")
        self.assertIsNone(seen["secret"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["execution_mode"], "local")
        self.assertGreater(result["elapsed_seconds"], 0)
        self.assertIn("candidate finished", Path(result["log_path"]).read_text())
        self.assertFalse(Path(result["log_path"]).is_relative_to(self.output))

    def test_local_mode_requires_explicit_opt_in(self) -> None:
        self.program("pass\n")
        self.config.pop("allow_local")
        with self.assertRaisesRegex(ExecutionError, "allow_local=true"):
            self.execute()

    def test_failure_preserves_status_and_diagnostic_log(self) -> None:
        self.program("import sys\nprint('bad configuration', file=sys.stderr)\nraise SystemExit(7)\n")
        with self.assertRaises(ExecutionError) as caught:
            self.execute()
        error = caught.exception
        self.assertEqual(error.kind, "nonzero_exit")
        self.assertEqual(error.exit_code, 7)
        self.assertGreater(error.elapsed_seconds, 0)
        self.assertIn("bad configuration", Path(error.log_path).read_text())

    def test_stderr_is_drained_but_log_storage_is_bounded(self) -> None:
        self.program("import sys\nsys.stderr.write('x' * 400000)\n")
        self.config["max_log_mb"] = 0.001
        result = self.execute()
        self.assertTrue(result["log_truncated"])
        self.assertLessEqual(Path(result["log_path"]).stat().st_size, int(0.001 * 1024 * 1024))
        self.assertEqual(result["exit_code"], 0)

    def test_timeout_kills_descendants(self) -> None:
        self.program(
            "import pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])\n"
            "pathlib.Path(sys.argv[2], 'child.pid').write_text(str(child.pid))\n"
            "time.sleep(90)\n"
        )
        with self.assertRaises(ExecutionError) as caught:
            self.execute(timeout=0.6)
        self.assertEqual(caught.exception.kind, "timeout")
        pid = int((self.output / "child.pid").read_text())
        deadline = time.monotonic() + 3
        while _running(pid) and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertFalse(_running(pid), "candidate descendant survived the trial timeout")

    def test_normal_exit_also_cleans_up_background_children(self) -> None:
        self.program(
            "import pathlib, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])\n"
            "pathlib.Path(sys.argv[2], 'child.pid').write_text(str(child.pid))\n"
        )
        self.execute()
        pid = int((self.output / "child.pid").read_text())
        self.assertFalse(_running(pid))

    def test_dead_rpc_parent_cannot_leave_a_trial_running(self) -> None:
        self.program(
            "import os, pathlib, sys, time\n"
            "pathlib.Path(sys.argv[2], 'candidate.pid').write_text(str(os.getpid()))\n"
            "time.sleep(90)\n"
        )
        source = (
            "import sys\nfrom pathlib import Path\n"
            "from infinity_research.executor import execute_candidate\n"
            "execute_candidate(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), "
            "{'kind': 'local', 'allow_local': True}, 80, seed=0)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        parent = subprocess.Popen(
            [sys.executable, "-c", source, str(self.candidate), str(self.prepared), str(self.output)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        pid_file = self.output / "candidate.pid"
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and parent.poll() is None and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertTrue(pid_file.exists(), "candidate did not start")
            pid = int(pid_file.read_text())
            os.kill(parent.pid, signal.SIGKILL)
            parent.wait(timeout=3)
            deadline = time.monotonic() + 5
            while _running(pid) and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertFalse(_running(pid), "candidate survived the death of its RPC parent")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)
            if parent.stderr is not None:
                parent.stderr.close()

    def test_heartbeat_failure_stops_the_trial(self) -> None:
        self.program("import time\ntime.sleep(90)\n")
        self.config["heartbeat_interval_seconds"] = 0.05
        calls = 0

        def heartbeat() -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("lease lost")

        with self.assertRaises(ExecutionError) as caught:
            self.execute(heartbeat=heartbeat)
        self.assertEqual(caught.exception.kind, "heartbeat_failed")
        self.assertIn("lease lost", str(caught.exception))

    def test_symlink_output_is_rejected(self) -> None:
        self.program(
            "import pathlib, sys\n"
            "pathlib.Path(sys.argv[2], 'outside').symlink_to('/tmp', target_is_directory=True)\n"
        )
        with self.assertRaises(ExecutionError) as caught:
            self.execute()
        self.assertEqual(caught.exception.kind, "invalid_output")
        self.assertIn("symlink", str(caught.exception))

    def test_large_outputs_abort_a_still_running_candidate(self) -> None:
        self.program(
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[2], 'large.bin').write_bytes(b'x' * 100000)\n"
            "time.sleep(90)\n"
        )
        self.config["max_output_mb"] = 0.01
        started = time.monotonic()
        with self.assertRaises(ExecutionError) as caught:
            self.execute(timeout=10)
        self.assertEqual(caught.exception.kind, "invalid_output")
        self.assertLess(time.monotonic() - started, 5)

    def test_holdout_uses_separate_public_final_manifest(self) -> None:
        (self.prepared / "public_final").mkdir()
        (self.prepared / "public_final" / "manifest.json").write_text('{"split": "holdout"}')
        self.program(
            "import pathlib, sys\n"
            "pathlib.Path(sys.argv[2], 'manifest.json').write_bytes(pathlib.Path(sys.argv[1]).read_bytes())\n"
        )
        self.execute(split="holdout")
        self.assertEqual(json.loads((self.output / "manifest.json").read_text())["split"], "holdout")

    def test_test_split_selects_unlabelled_public_test_for_local_and_docker(self) -> None:
        self.program(
            "import json, pathlib, sys\n"
            "pathlib.Path(sys.argv[2], 'seen.json').write_text(json.dumps({\n"
            " 'argument': sys.argv[1], 'manifest': json.load(open(sys.argv[1]))}))\n"
        )
        # A missing test input must never silently fall back to validation.
        with self.assertRaisesRegex(ExecutionError, "public_test/manifest.json"):
            self.execute(split="test")
        public_test = self.prepared / "public_test"
        public_test.mkdir()
        manifest = {"split": "test", "predict": [{"image_id": "unlabelled-1", "path": "test.png"}]}
        (public_test / "manifest.json").write_text(json.dumps(manifest))
        self.execute(split="test")
        seen = json.loads((self.output / "seen.json").read_text())
        self.assertEqual(seen["argument"], str(public_test / "manifest.json"))
        self.assertEqual(seen["manifest"], manifest)

        command = _docker_command(
            "/usr/bin/docker", "sha256:" + "a" * 64, "test-inference",
            self.candidate, public_test, self.output, {"gpus": 0}, 17,
        )
        mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertEqual(mounts[1], f"type=bind,src={public_test},dst=/input,readonly")
        self.assertNotIn("private", " ".join(mounts))
        self.assertNotIn("public_final", " ".join(mounts))
        self.assertEqual(command[-4:], ["python", "/candidate/main.py", "/input/manifest.json", "/output"])

    def test_input_symlink_and_stale_artifacts_are_rejected(self) -> None:
        self.program("pass\n")
        private = self.prepared / "private"
        private.mkdir()
        (private / "truth.json").write_text("private labels")
        link = self.prepared / "public" / "bad.json"
        link.symlink_to(private / "truth.json")
        with self.assertRaisesRegex(ExecutionError, "symlink"):
            self.execute()
        link.unlink()
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "predictions.json").write_text("old predictions")
        with self.assertRaisesRegex(ExecutionError, "fresh directory"):
            self.execute()

    def test_docker_command_limits_mounts_resources_and_gpu(self) -> None:
        command = _docker_command(
            "/usr/bin/docker", "sha256:" + "a" * 64, "trial-123", self.candidate,
            self.prepared / "public", self.output,
            {"cpus": 3, "memory_mb": 2048, "gpus": 1, "gpu_device": "2"}, 17,
        )
        self.assertIn("--network=none", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn("--pull=never", command)
        mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(mounts[0].endswith("dst=/candidate,readonly"))
        self.assertTrue(mounts[1].endswith("dst=/input,readonly"))
        self.assertNotIn("private", " ".join(mounts))
        self.assertEqual(command[command.index("--gpus") + 1], "device=2")
        self.assertEqual(command[command.index("--memory") + 1], "2048m")
        self.assertEqual(command[-4:], ["python", "/candidate/main.py", "/input/manifest.json", "/output"])

    def test_gpu_requires_a_campaign_assigned_device(self) -> None:
        self.program("pass\n")
        self.config["gpus"] = 1
        with self.assertRaisesRegex(ExecutionError, "explicit gpu_device"):
            self.execute()


if __name__ == "__main__":
    unittest.main()
