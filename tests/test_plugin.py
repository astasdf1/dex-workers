from __future__ import annotations
import importlib.util, json, os, stat, subprocess, sys, tarfile, tempfile, textwrap, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/dex_workers.py"

def load_module():
    spec=importlib.util.spec_from_file_location("dex_workers_under_test",CLI)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


class DexWorkersTest(unittest.TestCase):
    def test_usage_cache_v2_is_accepted(self):
        with tempfile.TemporaryDirectory() as raw:
            home=Path(raw); cache=home/".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v2","openai":{"remaining_percent":70,"windows":{"five_hour":{"remaining_percent":70},"one_week":{"remaining_percent":80}}},"gemini":{"remaining_percent":20}}))
            module=load_module(); data=module.load_usage(home)
            self.assertEqual(data["schema_version"],"dex.provider_usage_cache.v2"); self.assertEqual(module.remaining("codex",data),70)
            self.assertIsNone(module.remaining("agy", data))

    def test_usage_cache_v3_and_antigravity_never_use_gemini_quota(self):
        with tempfile.TemporaryDirectory() as raw:
            home=Path(raw); cache=home/".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v3","gemini":{"remaining_percent":99},"antigravity":{"readiness":"ready"}}))
            module=load_module(); data=module.load_usage(home)
            self.assertIsNone(module.remaining("agy",data))
    def call(self, home: Path, *args: str, path: str = "/usr/bin:/bin"):
        env = os.environ | {"HOME": str(home), "PATH": path, "DEX_WORKERS_STATE_DIR": str(home / "state")}
        return subprocess.run([sys.executable, str(CLI), "--home", str(home), "--probe-timeout", "0.5", *args],
                              text=True, capture_output=True, env=env, check=False, timeout=8)

    def tool(self, directory: Path, name: str, body: str):
        path = directory / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def test_manifest_and_skills(self):
        manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(manifest["name"], "dex-workers")
        self.assertEqual(market["plugins"][0]["name"], "dex-workers")
        self.assertEqual({p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md")},
                         {"run", "review", "doctor", "status", "cancel", "delegate"})
        delegate = (ROOT / "skills/delegate/SKILL.md").read_text()
        self.assertIn("dex-workers select", delegate)
        self.assertIn("CLAUDE_NATIVE", delegate)
        self.assertIn("Task", delegate)

    def test_select_returns_claude_native_without_ready_provider(self):
        with tempfile.TemporaryDirectory() as raw:
            result = self.call(Path(raw), "select")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["schema_version"], "dex.worker_selection.v1")
            self.assertEqual(data["selection"], "CLAUDE_NATIVE")

    def test_select_uses_ready_provider_and_quota_without_launching(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir(); launched = home / "launched"
            self.tool(tools, "codex", f'''\
                if [ "$1" = login ]; then echo "Logged in"; exit 0; fi
                touch "{launched}"
            ''')
            self.tool(tools, "agy", '''
                if [ "$1" = models ]; then echo model
                elif [ "$1" = --help ]; then echo "--print --print-timeout --sandbox"
                else exit 9; fi
            ''')
            cache = home / ".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v1", "openai":{"remaining_percent":70}, "gemini":{"remaining_percent":20}}))
            result = self.call(home, "select", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(result.stdout)
            self.assertEqual(data["selection"], "codex")
            self.assertIn("70", data["route_reason"])
            self.assertFalse(launched.exists())

    def test_select_compares_native_claude_quota_with_external_workers(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir()
            self.tool(tools, "codex", 'if [ "$1" = login ]; then echo "Logged in"; else exit 9; fi\n')
            self.tool(tools, "agy", '''
                if [ "$1" = models ]; then echo model
                elif [ "$1" = --help ]; then echo "--print --print-timeout --sandbox"
                else exit 9; fi
            ''')
            cache = home / ".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v1",
                                         "claude":{"remaining_percent":90},
                                         "openai":{"remaining_percent":70},
                                         "gemini":{"remaining_percent":20}}))
            result = self.call(home, "select", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(result.stdout)
            self.assertEqual(data["selection"], "CLAUDE_NATIVE")
            self.assertIn("90", data["route_reason"])

    def test_select_prefers_claude_native_when_external_quota_is_exhausted(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir()
            self.tool(tools, "codex", 'if [ "$1" = login ]; then echo "Logged in"; fi\n')
            cache = home / ".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v1", "openai":{"remaining_percent":0}}))
            result = self.call(home, "select", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(result.stdout)
            self.assertEqual(data["selection"], "CLAUDE_NATIVE")
            self.assertEqual(data["route_reason"], "all_ready_providers_quota_exhausted")

    def test_no_provider_returns_local_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            result = self.call(Path(raw), "run", "inspect")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "CLAUDE_FALLBACK")

    def test_codex_readonly_and_write_opt_in(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir(); capture = home / "args"
            self.tool(tools, "codex", f'''\
                if [ "$1" = login ]; then echo "Logged in"; exit 0; fi
                printf '%s\\n' "$@" > "{capture}"
                echo worker-ok
            ''')
            readonly = self.call(home, "run", "inspect", "--provider", "codex", path=str(tools)+":/usr/bin:/bin")
            self.assertEqual(json.loads(readonly.stdout)["status"], "completed", readonly.stderr)
            self.assertIn("read-only", capture.read_text())
            writable = self.call(home, "run", "change", "--provider", "codex", "--write", path=str(tools)+":/usr/bin:/bin")
            self.assertEqual(json.loads(writable.stdout)["write_enabled"], True)
            self.assertIn("workspace-write", capture.read_text())

    def test_agy_is_readonly_by_default_and_routing_is_advisory(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir(); capture = home / "args"
            self.tool(tools, "codex", 'if [ "$1" = login ]; then echo "Logged in"; else echo codex; fi\n')
            self.tool(tools, "agy", f'''\
                if [ "$1" = models ]; then echo model
                elif [ "$1" = --help ]; then echo "--output-format --print --print-timeout --sandbox"
                else printf '%s\\n' "$@" > "{capture}"; echo agy; fi
            ''')
            cache = home / ".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v1", "openai":{"remaining_percent":10}, "gemini":{"remaining_percent":80}}))
            result = self.call(home, "run", "inspect", "--provider", "agy", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(result.stdout)
            self.assertEqual(data["provider"], "agy")
            self.assertFalse(data["write_enabled"])
            self.assertIn("plan", capture.read_text())
            self.assertEqual(data["route_reason"], "explicit")

    def test_cache_routes_to_higher_ready_provider(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir()
            self.tool(tools, "codex", 'if [ "$1" = login ]; then echo "Logged in"; else echo codex; fi\n')
            self.tool(tools, "agy", '''
                if [ "$1" = models ]; then echo model
                elif [ "$1" = --help ]; then echo "--output-format --print --print-timeout --sandbox"
                else echo agy; fi
            ''')
            cache = home / ".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v1", "openai":{"remaining_percent":10}, "gemini":{"remaining_percent":80}}))
            result = self.call(home, "run", "inspect", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(result.stdout); self.assertEqual(data["provider"], "codex"); self.assertIn("10", data["route_reason"])

    def test_unsupported_agy_and_unsafe_cache_are_ignored(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir()
            self.tool(tools, "agy", 'echo "--print-timeout --sandbox"\n')
            cache = home / ".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            outside = home / "outside"; outside.write_text('{"schema_version":"dex.provider_usage_cache.v1"}')
            cache.symlink_to(outside)
            result = self.call(home, "status", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(result.stdout)
            self.assertEqual(data["providers"]["agy"]["reason"], "unsupported_cli")
            self.assertEqual(data["dex_usage_cache"], "missing_or_invalid")

    def test_timeout_falls_back_and_cancel_is_scoped(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir()
            self.tool(tools, "codex", 'if [ "$1" = login ]; then echo "Logged in"; else sleep 2; fi\n')
            timed = self.call(home, "run", "slow", "--provider", "codex", "--timeout", "0.1", path=str(tools)+":/usr/bin:/bin")
            self.assertEqual(json.loads(timed.stdout)["reason"], "timeout")
            missing = self.call(home, "cancel", "not-a-run")
            self.assertEqual(json.loads(missing.stdout)["status"], "CLAUDE_FALLBACK")

    def test_active_run_is_persisted_and_cancellable(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir()
            self.tool(tools, "codex", 'if [ "$1" = login ]; then echo "Logged in"; else sleep 10; fi\n')
            env = os.environ | {"HOME": str(home), "PATH": str(tools)+":/usr/bin:/bin", "DEX_WORKERS_STATE_DIR": str(home / "state")}
            worker = subprocess.Popen([sys.executable, str(CLI), "--home", str(home), "run", "slow", "--provider", "codex", "--timeout", "20"],
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            record = next((home / "state").glob("*.json"), None)
            for _ in range(30):
                record = next((home / "state").glob("*.json"), None)
                if record: break
                time.sleep(0.05)
            self.assertIsNotNone(record)
            persisted = json.loads(record.read_text())
            self.assertTrue(persisted["process_identity"])
            run_id = persisted["run_id"]
            cancelled = self.call(home, "cancel", run_id, path=str(tools)+":/usr/bin:/bin")
            self.assertEqual(json.loads(cancelled.stdout)["status"], "cancelled")
            stdout, stderr = worker.communicate(timeout=5)
            self.assertEqual(json.loads(stdout)["status"], "CLAUDE_FALLBACK", stderr)

    def test_stale_state_cannot_cancel_a_reused_pid(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); state = home / "state"; state.mkdir()
            run_id = "123-456"
            # The current test process is a real process-group leader only in
            # some runners, so the identity mismatch is the deterministic
            # ownership guard under test.
            (state / f"{run_id}.json").write_text(json.dumps({
                "run_id": run_id, "pid": os.getpid(), "process_identity": "stale-token"
            }))
            result = self.call(home, "cancel", run_id)
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "CLAUDE_FALLBACK")
            self.assertEqual(data["reason"], "stale_or_unowned_run")
            self.assertFalse((state / f"{run_id}.json").exists())

    def test_packaging_uses_exact_safe_inventory(self):
        with tempfile.TemporaryDirectory() as raw:
            archive = Path(raw) / "dex-workers.tar.gz"
            packed = subprocess.run([sys.executable, str(ROOT / "scripts/package.py"), "--out", str(archive)],
                                    text=True, capture_output=True, check=False)
            self.assertEqual(packed.returncode, 0, packed.stderr)
            with tarfile.open(archive) as bundle:
                names = {member.name for member in bundle.getmembers()}
                self.assertIn("dex-workers/scripts/dex_workers.py", names)
                self.assertNotIn("dex-workers/tests/test_plugin.py", names)
                self.assertTrue(all(member.isfile() and not member.issym() for member in bundle.getmembers()))


if __name__ == "__main__": unittest.main()
