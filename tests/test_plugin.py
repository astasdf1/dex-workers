from __future__ import annotations
import importlib.util, json, os, stat, subprocess, sys, tarfile, tempfile, textwrap, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/dex_workers.py"

def load_module():
    spec=importlib.util.spec_from_file_location("dex_workers_under_test",CLI)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


class DexWorkersTest(unittest.TestCase):
    def test_review_role_routing_and_five_percent_boundary(self):
        module = load_module(); probes = {"codex":{"enabled":True}, "agy":{"enabled":True}}
        usage = {"schema_version":"dex.provider_usage_cache.v3", "claude":{"remaining_percent":90},
                 "openai":{"remaining_percent":99}, "antigravity":{"readiness":"ready"}}
        self.assertEqual(module.choose_for_role("review", "single", "auto", probes, usage)[0], ["agy"])
        usage["openai"]["remaining_percent"] = 4.999
        probes["agy"]["enabled"] = False
        self.assertEqual(module.choose_for_role("review", "single", "auto", probes, usage)[0], [module.CLAUDE_NATIVE])
        usage["openai"]["remaining_percent"] = 5
        self.assertEqual(module.choose_for_role("review", "single", "auto", probes, usage)[0], ["codex"])

    def test_multi_review_includes_all_eligible_and_unknown_agy(self):
        module = load_module(); probes = {"codex":{"enabled":True}, "agy":{"enabled":True}}
        usage = {"schema_version":"dex.provider_usage_cache.v3", "claude":{"remaining_percent":5},
                 "openai":{"remaining_percent":5}, "antigravity":{"readiness":"ready"}}
        selected, reason = module.choose_for_role("review", "multi", "auto", probes, usage)
        self.assertEqual(selected, [module.CLAUDE_NATIVE, "codex", "agy"])
        self.assertEqual(reason, "multi_perspective_all_eligible")
        usage["claude"]["remaining_percent"] = 4.9
        usage["openai"]["remaining_percent"] = 4.9
        self.assertEqual(module.choose_for_role("review", "multi", "auto", probes, usage)[0], ["agy"])
        usage["antigravity"]["remaining_percent"] = 4.9
        self.assertEqual(module.choose_for_role("review", "multi", "auto", probes, usage),
                         ([], "multi_perspective_no_eligible_provider"))

    def test_audit_and_implementation_do_not_auto_select_agy(self):
        module = load_module(); probes = {"codex":{"enabled":True}, "agy":{"enabled":True}}
        usage = {"schema_version":"dex.provider_usage_cache.v3", "claude":{"remaining_percent":80},
                 "openai":{"remaining_percent":70}, "antigravity":{"readiness":"ready"}}
        self.assertNotEqual(module.choose_for_role("audit", "single", "auto", probes, usage)[0], ["agy"])
        self.assertNotEqual(module.choose_for_role("implementation", "single", "auto", probes, usage)[0], ["agy"])
        self.assertEqual(module.choose_for_role("implementation", "single", "agy", probes, usage)[0], ["agy"])

    def test_review_skill_requires_all_three_and_anchored_synthesis(self):
        text = (ROOT / "skills/review/SKILL.md").read_text()
        for token in ("Claude via Task", "Codex", "Antigravity", "below 5%", "supplemental/unconfirmed"):
            self.assertIn(token, text)

    def test_usage_cache_v2_is_accepted(self):
        with tempfile.TemporaryDirectory() as raw:
            home=Path(raw); cache=home/".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v2","openai":{"remaining_percent":70,"windows":{"five_hour":{"remaining_percent":70},"one_week":{"remaining_percent":80}}},"retired_google_provider":{"remaining_percent":20}}))
            module=load_module(); data=module.load_usage(home)
            self.assertEqual(data["schema_version"],"dex.provider_usage_cache.v2"); self.assertEqual(module.remaining("codex",data),70)
            self.assertIsNone(module.remaining("agy", data))

    def test_usage_cache_v3_and_antigravity_never_use_retired_quota(self):
        with tempfile.TemporaryDirectory() as raw:
            home=Path(raw); cache=home/".cache/dex-usage/usage.json"; cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v3","retired_google_provider":{"remaining_percent":99},"antigravity":{"readiness":"ready"}}))
            module=load_module(); data=module.load_usage(home)
            self.assertIsNone(module.remaining("agy",data))
            data["antigravity"].update({"remaining_percent":42,"stale":True,"windows":{"five_hour":{"remaining_percent":42},"one_week":{"remaining_percent":70}}})
            self.assertEqual(module.remaining("agy",data),42)
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
                         {"run", "review", "doctor", "status", "cancel", "delegate", "setup", "setup-project", "wait"})
        delegate = (ROOT / "skills/delegate/SKILL.md").read_text()
        self.assertIn("dex-workers select", delegate)
        self.assertIn("CLAUDE_NATIVE", delegate)
        self.assertIn("Task", delegate)
        self.assertIn("--role <role>", delegate)
        module = load_module(); self.assertEqual(module.VERSION, manifest["version"])
        self.assertIn(f'dex-workers-{manifest["version"]}.tar.gz', (ROOT / "scripts/package.py").read_text())

    def test_setup_defaults_upgrade_backup_idempotent_and_malformed(self):
        setup = ROOT / "scripts/setup.py"
        with tempfile.TemporaryDirectory() as raw:
            home=Path(raw).resolve(); claude=home/".claude"; claude.mkdir()
            config=claude/"CLAUDE.md"
            config.write_text("prefix\n\n## Default Delegation Protocol\n\nRun at most 3 delegated subtasks concurrently.\n\n<!-- USER:PERSISTENT:END -->\n")
            run=lambda *a: subprocess.run([sys.executable,str(setup),"setup-user","--home",str(home),*a],text=True,capture_output=True)
            self.assertEqual(run("--dry-run").returncode,0); self.assertIn("at most 3",config.read_text())
            self.assertEqual(run().returncode,0); current=config.read_text()
            self.assertIn("at most 5 delegated",current); self.assertEqual(current.count("dex-workers:default-delegation BEGIN"),1)
            backups=list((claude/"backups").glob("CLAUDE.md.before-dex-workers.*")); self.assertEqual(len(backups),1)
            self.assertIn("at most 3",backups[0].read_text())
            self.assertEqual(run("--check").returncode,0); self.assertEqual(run().returncode,0); self.assertEqual(config.read_text(),current)
            config.write_text("<!-- dex-workers:default-delegation BEGIN -->\nbroken")
            self.assertEqual(run().returncode,2); self.assertEqual(config.read_text(),"<!-- dex-workers:default-delegation BEGIN -->\nbroken")

    def test_session_start_fresh_existing_idempotent_and_hook_contract(self):
        setup = ROOT / "scripts/setup.py"
        hook = json.loads((ROOT / "hooks/hooks.json").read_text())
        handler = hook["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertTrue(handler["async"]); self.assertEqual(handler["command"], "python3")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/setup.py", handler["args"])
        for initial in (None, "keep this unrelated content\n"):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as raw:
                home = Path(raw).resolve(); config = home / ".claude/CLAUDE.md"
                if initial is not None:
                    config.parent.mkdir(); config.write_text(initial)
                run = lambda: subprocess.run([sys.executable, str(setup), "session-start", "--home", str(home)], text=True, capture_output=True)
                self.assertEqual(run().returncode, 0)
                current = config.read_text(); self.assertIn("at most 5 delegated subtasks", current)
                if initial is not None: self.assertIn(initial.strip(), current)
                backups = list((home / ".claude/backups").glob("CLAUDE.md.before-dex-workers.*"))
                self.assertEqual(len(backups), 1 if initial is not None else 0)
                self.assertEqual(run().returncode, 0); self.assertEqual(config.read_text(), current)
                self.assertEqual(len(list((home / ".claude/backups").glob("*"))), len(backups))

    def test_session_start_opt_out_restore_and_explicit_reenable(self):
        setup = ROOT / "scripts/setup.py"
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw).resolve(); config = home / ".claude/CLAUDE.md"
            call = lambda command: subprocess.run([sys.executable, str(setup), command, "--home", str(home)], text=True, capture_output=True)
            self.assertEqual(call("disable-auto-policy").returncode, 0)
            self.assertEqual(call("session-start").returncode, 0); self.assertFalse(config.exists())
            self.assertTrue((home / ".claude/dex-workers/auto-policy.disabled").is_file())
            self.assertEqual(call("enable-auto-policy").returncode, 0)
            self.assertEqual(call("session-start").returncode, 0); self.assertTrue(config.exists())
            config.write_text("mine\n\n" + config.read_text())
            self.assertEqual(call("restore-user").returncode, 0)
            self.assertEqual(config.read_text(), "mine\n"); self.assertTrue((home / ".claude/dex-workers/auto-policy.disabled").exists())
            self.assertEqual(call("session-start").returncode, 0); self.assertEqual(config.read_text(), "mine\n")

    def test_session_start_malformed_duplicate_locking_concurrency_and_failure(self):
        setup = ROOT / "scripts/setup.py"
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw).resolve(); claude = home / ".claude"; claude.mkdir(); config = claude / "CLAUDE.md"
            malformed = "<!-- dex-workers:default-delegation BEGIN -->\nbroken\n"
            config.write_text(malformed)
            result = subprocess.run([sys.executable, str(setup), "session-start", "--home", str(home)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0); self.assertEqual(config.read_text(), malformed); self.assertIn("skipped", result.stderr)
            duplicate = ("<!-- dex-workers:default-delegation BEGIN -->\n<!-- dex-workers:default-delegation BEGIN -->\n"
                         "<!-- dex-workers:default-delegation END -->\n")
            config.write_text(duplicate)
            result = subprocess.run([sys.executable, str(setup), "session-start", "--home", str(home)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0); self.assertEqual(config.read_text(), duplicate)
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw).resolve(); config = home / ".claude/CLAUDE.md"; config.parent.mkdir(); config.write_text("mine\n")
            processes = [subprocess.Popen([sys.executable, str(setup), "session-start", "--home", str(home)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
            for process in processes: self.assertEqual(process.communicate(timeout=5)[0], "")
            self.assertEqual(config.read_text().count("dex-workers:default-delegation BEGIN"), 1)
            self.assertEqual(len(list((home / ".claude/backups").glob("*"))), 1)
            lock = home / ".claude/dex-workers/auto-policy.lock"; lock.mkdir()
            before = config.read_text(); subprocess.run([sys.executable, str(setup), "session-start", "--home", str(home)], check=True)
            self.assertEqual(config.read_text(), before)
            disable = subprocess.Popen([sys.executable, str(setup), "disable-auto-policy", "--home", str(home)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.05); self.assertIsNone(disable.poll())
            lock.rmdir(); self.assertEqual(disable.communicate(timeout=5)[1], "")
            self.assertTrue((home / ".claude/dex-workers/auto-policy.disabled").exists())
            subprocess.run([sys.executable, str(setup), "session-start", "--home", str(home)], check=True)
            self.assertEqual(config.read_text(), before)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); outside = root / "outside"; outside.mkdir(); home = root / "home"; home.symlink_to(outside, target_is_directory=True)
            result = subprocess.run([sys.executable, str(setup), "session-start", "--home", str(home)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0); self.assertIn("skipped", result.stderr); self.assertEqual(list(outside.iterdir()), [])

    def test_project_harness_fresh_check_idempotent_conflict_and_symlink(self):
        setup=ROOT/"scripts/setup.py"
        with tempfile.TemporaryDirectory() as raw:
            project=Path(raw).resolve()/"project"; project.mkdir(); (project/"CLAUDE.md").write_text("mine")
            run=lambda *a: subprocess.run([sys.executable,str(setup),"setup-project","--target",str(project),*a],text=True,capture_output=True)
            self.assertEqual(run("--dry-run").returncode,0); self.assertFalse((project/".harness").exists())
            self.assertEqual(run().returncode,0); self.assertEqual(run("--check").returncode,0)
            self.assertEqual((project/"CLAUDE.md").read_text(),"mine"); self.assertTrue((project/".harness/verify").stat().st_mode & stat.S_IXUSR)
            self.assertEqual(run().returncode,0)
            (project/".harness/config").write_text("custom")
            before={p.relative_to(project):p.read_bytes() for p in project.rglob("*") if p.is_file()}
            self.assertEqual(run().returncode,2)
            self.assertEqual(before,{p.relative_to(project):p.read_bytes() for p in project.rglob("*") if p.is_file()})
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve(); outside=root/"outside"; outside.mkdir(); project=root/"project"; project.symlink_to(outside, target_is_directory=True)
            result=subprocess.run([sys.executable,str(setup),"setup-project","--target",str(project)],text=True,capture_output=True)
            self.assertEqual(result.returncode,2); self.assertEqual(list(outside.iterdir()),[])
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve(); base=root/"base"; outside=root/"outside"; base.mkdir(); outside.mkdir()
            (base/"link").symlink_to(outside, target_is_directory=True)
            target=base/"link"/"project"
            result=subprocess.run([sys.executable,str(setup),"setup-project","--target",str(target)],text=True,capture_output=True)
            self.assertEqual(result.returncode,2); self.assertEqual(list(outside.iterdir()),[])
            normal=base/"new"/"project"
            result=subprocess.run([sys.executable,str(setup),"setup-project","--target",str(normal)],text=True,capture_output=True)
            self.assertEqual(result.returncode,0, result.stderr); self.assertTrue((normal/".harness/JOURNAL.md").is_file())

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
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v1", "openai":{"remaining_percent":70}, "retired_google_provider":{"remaining_percent":20}}))
            result = self.call(home, "select", "--task", "known-route", path=str(tools)+":/usr/bin:/bin")
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
                                         "retired_google_provider":{"remaining_percent":20}}))
            result = self.call(home, "select", "--task", "known-route", path=str(tools)+":/usr/bin:/bin")
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
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v1", "openai":{"remaining_percent":10}, "retired_google_provider":{"remaining_percent":80}}))
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
            cache.write_text(json.dumps({"schema_version":"dex.provider_usage_cache.v1", "openai":{"remaining_percent":10}, "retired_google_provider":{"remaining_percent":80}}))
            result = self.call(home, "run", "known-route", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(result.stdout); self.assertEqual(data["provider"], "codex"); self.assertIn("10", data["route_reason"])

    def test_unknown_antigravity_quota_is_deterministically_rotated(self):
        module = load_module()
        probes = {
            "codex": {"enabled": True},
            "agy": {"enabled": True},
        }
        usage = {
            "schema_version": "dex.provider_usage_cache.v3",
            "claude": {"remaining_percent": 90},
            "openai": {"remaining_percent": 70},
            "antigravity": {"readiness": "ready"},
        }
        routes = {module.choose_delegation(probes, usage, key)[0] for key in ("a", "known-route")}
        self.assertEqual(routes, {"agy", "CLAUDE_NATIVE"})
        self.assertEqual(module.choose_delegation(probes, usage, "a"),
                         module.choose_delegation(probes, usage, "a"))

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


    # --- 1.7.0: model/effort, resume, structured output, background, agents, images, brief ---

    def test_model_spellings_and_effort_clamping(self):
        module = load_module()
        self.assertEqual(module.parse_model("gpt-5.5"), ("gpt-5.5", None))
        self.assertEqual(module.parse_model("gpt-5.5-high"), ("gpt-5.5", "high"))
        self.assertEqual(module.parse_model("gpt-5.5:xhigh"), ("gpt-5.5", "xhigh"))
        self.assertEqual(module.parse_model("codex/gpt-5.5-high[1m]"), ("gpt-5.5", "high"))
        self.assertEqual(module.parse_model("gpt-5.3-codex-spark"), ("gpt-5.3-codex-spark", None))
        self.assertEqual(module.parse_model(None), (None, None))
        with self.assertRaises(ValueError): module.parse_model("bad model; rm -rf")
        self.assertEqual(module.clamp_effort("codex", "max"), "xhigh")
        self.assertEqual(module.clamp_effort("agy", "xhigh"), "high")
        self.assertEqual(module.clamp_effort("agy", "minimal"), "low")
        self.assertIsNone(module.clamp_effort("codex", None))
        # pinned suffix > --effort > role default
        self.assertEqual(module.resolve_model_effort("codex", "gpt-5.5-low", "high", "audit"), ("gpt-5.5", "low"))
        self.assertEqual(module.resolve_model_effort("codex", "gpt-5.5", "high", "review"), ("gpt-5.5", "high"))
        self.assertEqual(module.resolve_model_effort("codex", None, None, "review"), (None, "low"))
        self.assertEqual(module.resolve_model_effort("codex", None, None, "audit"), (None, "high"))
        self.assertEqual(module.resolve_model_effort("agy", None, None, "implementation"), (None, None))

    def test_codex_argv_carries_model_effort_schema_images_and_resume(self):
        module = load_module(); cwd = Path("/tmp")
        options = module.RunOptions(model="gpt-5.5", effort="high", schema=Path("/s.json"),
                                    images=[Path("/a.png")], last_message=Path("/last.txt"))
        argv, _ = module.command_for("codex", "run", cwd, "task", False, "/bin/codex", options)
        self.assertEqual(argv[:3], ["/bin/codex", "exec", "--json"])
        for flag in (["-m", "gpt-5.5"], ["-c", "model_reasoning_effort=high"], ["--output-schema", "/s.json"],
                     ["-i", "/a.png"], ["-o", "/last.txt"], ["--sandbox", "read-only"], ["-C", "/tmp"]):
            self.assertIn(flag[1], argv); self.assertEqual(argv[argv.index(flag[1]) - 1], flag[0])
        self.assertNotIn("--ephemeral", argv); self.assertEqual(argv[-1], "task")
        argv, _ = module.command_for("codex", "run", cwd, "task", True, "/bin/codex", module.RunOptions(ephemeral=True))
        self.assertIn("--ephemeral", argv); self.assertIn("workspace-write", argv)
        argv, _ = module.command_for("codex", "run", cwd, "again", False, "/bin/codex",
                                     module.RunOptions(resume="01a0-thread", effort="low"))
        self.assertEqual(argv[:4], ["/bin/codex", "exec", "resume", "01a0-thread"])
        self.assertNotIn("--sandbox", argv); self.assertNotIn("-C", argv)
        self.assertIn('sandbox_mode="read-only"', argv); self.assertIn("model_reasoning_effort=low", argv)
        argv, _ = module.command_for("codex", "review", cwd, "focus", False, "/bin/codex",
                                     module.RunOptions(model="gpt-5.5", effort="low"))
        self.assertEqual(argv[1], "review"); self.assertNotIn("-m", argv); self.assertIn('model="gpt-5.5"', argv)
        self.assertTrue(argv[-1].startswith(module.REVIEW_SCOPE))

    def test_agy_argv_respects_probed_capabilities(self):
        module = load_module(); cwd = Path("/tmp")
        full = {"json_output": True, "json_schema": True, "conversation": True, "model": True, "effort": True}
        options = module.RunOptions(model="gemini-3", effort="high", resume="conv-1", schema=Path("/s.json"),
                                    images=[Path("/shot.png")], capabilities=full)
        argv, _ = module.command_for("agy", "run", cwd, "task", False, "/bin/agy", options)
        for flag in (["--output-format", "json"], ["--model", "gemini-3"], ["--effort", "high"],
                     ["--conversation", "conv-1"], ["--json-schema", "/s.json"], ["--mode", "plan"]):
            self.assertEqual(argv[argv.index(flag[0]) + 1], flag[1])
        self.assertIn("/shot.png", argv[-1]); self.assertTrue(argv[-1].startswith("You are in read-only mode"))
        old = {key: False for key in full}
        argv, _ = module.command_for("agy", "run", cwd, "task", True, "/bin/agy",
                                     module.RunOptions(model="x", effort="high", resume="c", schema=Path("/s"), capabilities=old))
        for flag in ("--output-format", "--model", "--effort", "--conversation", "--json-schema"):
            self.assertNotIn(flag, argv)
        self.assertIn("accept-edits", argv)

    def test_codex_json_stream_yields_session_output_and_structured(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir(); capture = home / "args"
            self.tool(tools, "codex", f'''\
                if [ "$1" = login ]; then echo "Logged in"; exit 0; fi
                printf '%s\\n' "$@" > "{capture}"
                last=""; prev=""
                for a in "$@"; do if [ "$prev" = "-o" ]; then last="$a"; fi; prev="$a"; done
                echo '{{"type":"thread.started","thread_id":"thread-abc"}}'
                echo '{{"type":"item.completed","item":{{"id":"i0","type":"agent_message","text":"{{\\"summary\\":\\"ok\\",\\"findings\\":[]}}"}}}}'
                echo '{{"type":"turn.completed","usage":{{"input_tokens":10,"output_tokens":2}}}}'
                [ -n "$last" ] && printf '%s' '{{"summary":"ok","findings":[]}}' > "$last"
            ''')
            done = self.call(home, "run", "check", "--provider", "codex", "--findings", "--model", "gpt-5.5-high",
                             path=str(tools)+":/usr/bin:/bin")
            data = json.loads(done.stdout)
            self.assertEqual(data["status"], "completed", done.stderr)
            self.assertEqual(data["session_id"], "thread-abc")
            self.assertEqual(data["resume_hint"], "--resume thread-abc --provider codex")
            self.assertEqual(data["structured"], {"summary": "ok", "findings": []})
            self.assertEqual(data["usage"]["input_tokens"], 10)
            self.assertEqual(data["model"], "gpt-5.5"); self.assertEqual(data["effort"], "high")
            self.assertEqual(json.loads(data["output"]), {"summary": "ok", "findings": []})
            args = capture.read_text()
            self.assertIn("--output-schema", args); self.assertIn("findings.schema.json", args)
            self.assertIn("model_reasoning_effort=high", args)
            self.assertFalse(list((home / "state").glob("*.last.txt")))
            resumed = self.call(home, "run", "again", "--provider", "codex", "--resume", "thread-abc", "--ephemeral",
                                path=str(tools)+":/usr/bin:/bin")
            data = json.loads(resumed.stdout)
            self.assertEqual(data["resumed_from"], "thread-abc"); self.assertNotIn("resume_hint", data)
            self.assertIn("resume\nthread-abc", capture.read_text())
            bad = self.call(home, "run", "x", "--provider", "codex", "--resume", "../../etc", path=str(tools)+":/usr/bin:/bin")
            self.assertEqual(json.loads(bad.stdout)["error"], "invalid_session_id")
            bad = self.call(home, "run", "x", "--provider", "codex", "--model", "a b", path=str(tools)+":/usr/bin:/bin")
            self.assertEqual(json.loads(bad.stdout)["error"], "invalid_model_name")

    def test_agy_json_output_is_parsed(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir(); capture = home / "args"
            self.tool(tools, "agy", f'''\
                if [ "$1" = models ]; then echo model
                elif [ "$1" = --help ]; then echo "--print --print-timeout --sandbox --output-format --json-schema --conversation --model --effort"
                else printf '%s\\n' "$@" > "{capture}"
                  echo '{{"conversation_id":"conv-9","status":"SUCCESS","response":"looks fine","duration_seconds":1.5,"num_turns":1,"structured_output":{{"summary":"fine","findings":[]}}}}'
                fi
            ''')
            done = self.call(home, "review", "focus", "--provider", "agy", "--findings", "--effort", "max",
                             path=str(tools)+":/usr/bin:/bin")
            data = json.loads(done.stdout)
            self.assertEqual(data["status"], "completed", done.stderr)
            self.assertEqual(data["session_id"], "conv-9"); self.assertEqual(data["output"], "looks fine")
            self.assertEqual(data["structured"]["summary"], "fine"); self.assertEqual(data["effort"], "high")
            self.assertEqual(data["usage"]["num_turns"], 1)
            args = capture.read_text()
            self.assertIn("--output-format\njson", args); self.assertIn("--json-schema", args); self.assertIn("--effort\nhigh", args)

    def test_brief_wraps_prompt_with_workspace_state_and_context(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir(); capture = home / "prompt"
            work = home / "work"; work.mkdir()
            subprocess.run(["git", "init", "-q", str(work)], check=True)
            (work / "a.py").write_text("print(1)\n")
            (home / "notes.md").write_text("remember the edge case\n")
            self.tool(tools, "codex", f'''\
                if [ "$1" = login ]; then echo "Logged in"; exit 0; fi
                for a in "$@"; do :; done; printf '%s' "$a" > "{capture}"; echo done
            ''')
            done = self.call(home, "run", "fix the bug", "--provider", "codex", "--cwd", str(work), "--brief",
                             "--role", "implementation", "--deliverable", "a patch", "--done-when", "tests pass",
                             "--context", str(home / "notes.md"), path=str(tools)+":/usr/bin:/bin")
            data = json.loads(done.stdout); self.assertEqual(data["status"], "completed", done.stderr)
            self.assertTrue(data["brief"])
            prompt = capture.read_text()
            for token in ("# Delegated subtask brief", "Access: read-only", "Role: implementation", "Deliverable: a patch",
                          "Done when: tests pass", "## Task", "fix the bug", "## Workspace state", "?? a.py",
                          "## Context files", "remember the edge case"):
                self.assertIn(token, prompt)
            plain = self.call(home, "run", "fix the bug", "--provider", "codex", "--cwd", str(work), path=str(tools)+":/usr/bin:/bin")
            self.assertFalse(json.loads(plain.stdout)["brief"]); self.assertEqual(capture.read_text(), "fix the bug")
            missing = self.call(home, "run", "x", "--provider", "codex", "--context", str(home / "nope"), path=str(tools)+":/usr/bin:/bin")
            self.assertEqual(json.loads(missing.stdout)["error"], f"context_not_found:{home / 'nope'}")
            module = load_module()
            self.assertIn("Attached images: /img.png", module.build_brief("t", work, "review", False, None, None, [], [Path("/img.png")]))
            self.assertIn(module.DEFAULT_DELIVERABLE["review"], module.build_brief("t", work, "review", False, None, None, [], []))

    def test_images_are_validated_and_forwarded(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir(); capture = home / "args"
            shot = home / "shot.png"; shot.write_bytes(b"\x89PNG")
            self.tool(tools, "codex", f'''\
                if [ "$1" = login ]; then echo "Logged in"; exit 0; fi
                printf '%s\\n' "$@" > "{capture}"; echo ok
            ''')
            done = self.call(home, "run", "look", "--provider", "codex", "--image", str(shot), path=str(tools)+":/usr/bin:/bin")
            data = json.loads(done.stdout); self.assertEqual(data["status"], "completed", done.stderr)
            self.assertEqual(data["images"], [str(shot.resolve())]); self.assertIn(f"-i\n{shot.resolve()}", capture.read_text())
            missing = self.call(home, "run", "look", "--provider", "codex", "--image", str(home / "none.png"), path=str(tools)+":/usr/bin:/bin")
            self.assertEqual(json.loads(missing.stdout)["status"], "error")
            self.assertTrue(json.loads(missing.stdout)["error"].startswith("image_not_found"))

    def test_background_run_wait_result_and_concurrency_limit(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir(); state = home / "state"
            self.tool(tools, "codex", 'if [ "$1" = login ]; then echo "Logged in"; else sleep 0.5; echo bg-ok; fi\n')
            started = self.call(home, "run", "slow", "--provider", "codex", "--background", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(started.stdout)
            self.assertEqual(data["status"], "started", started.stderr); self.assertEqual(started.returncode, 0)
            run_id = data["run_id"]; self.assertTrue(data["result_file"].endswith(f"{run_id}.result.json"))
            early = self.call(home, "result", run_id)
            self.assertIn(json.loads(early.stdout)["status"], {"running", "completed"})
            waited = self.call(home, "wait", run_id, "--timeout", "6")
            data = json.loads(waited.stdout)
            self.assertEqual(data["status"], "completed", waited.stderr)
            inner = data["results"][run_id]
            self.assertEqual(inner["status"], "completed"); self.assertEqual(inner["run_id"], run_id)
            self.assertIn("bg-ok", inner["output"])
            self.assertFalse(list(state.glob("*.result.json"))); self.assertFalse(list(state.glob("*.log")))
            gone = self.call(home, "result", run_id)
            self.assertEqual(json.loads(gone.stdout)["reason"], "run_not_found")
            unknown = self.call(home, "wait", "1-1", "--timeout", "1")
            self.assertEqual(json.loads(unknown.stdout)["not_found"], ["1-1"])
            env_limited = os.environ | {"HOME": str(home), "PATH": str(tools)+":/usr/bin:/bin",
                                        "DEX_WORKERS_STATE_DIR": str(state), "DEX_WORKERS_MAX_ACTIVE": "0"}
            refused = subprocess.run([sys.executable, str(CLI), "--home", str(home), "--probe-timeout", "0.5",
                                      "run", "x", "--provider", "codex", "--background"],
                                     text=True, capture_output=True, env=env_limited, check=False, timeout=8)
            self.assertEqual(json.loads(refused.stdout)["error"], "too_many_active_runs")
            bad = self.call(home, "run", "x", "--result-file", str(home / "elsewhere.result.json"))
            self.assertEqual(json.loads(bad.stdout)["error"], "invalid_result_file")

    def test_background_fallback_is_still_collectable(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            started = self.call(home, "run", "nothing", "--background")
            run_id = json.loads(started.stdout)["run_id"]
            waited = self.call(home, "wait", run_id, "--timeout", "6")
            inner = json.loads(waited.stdout)["results"][run_id]
            self.assertEqual(inner["status"], "CLAUDE_FALLBACK")

    def test_status_reports_finished_runs_and_select_suggests_effort(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); state = home / "state"; state.mkdir()
            (state / "5-5.result.json").write_text(json.dumps({"status": "completed"}))
            data = json.loads(self.call(home, "status").stdout)
            self.assertEqual(data["finished"], ["5-5"]); self.assertEqual(data["max_active"], 5)
            self.assertTrue(data["findings_schema"].endswith("findings.schema.json"))
            for role, effort in (("review", "low"), ("audit", "high"), ("implementation", None)):
                selected = json.loads(self.call(home, "select", "--role", role).stdout)
                self.assertEqual(selected["suggested_effort"], effort)

    def test_agents_schema_and_release_inventory(self):
        agents = {p.stem for p in (ROOT / "agents").glob("*.md")}
        self.assertEqual(agents, {"codex-reviewer", "codex-implementer", "codex-auditor"})
        for path in (ROOT / "agents").glob("*.md"):
            text = path.read_text()
            self.assertTrue(text.startswith("---\nname: " + path.stem + "\n"), path)
            self.assertIn("dex-workers", text); self.assertIn("CLAUDE_FALLBACK", text); self.assertIn("session_id", text)
        schema = json.loads((ROOT / "schemas/findings.schema.json").read_text())
        self.assertEqual(set(schema["properties"]["findings"]["items"]["required"]),
                         {"file", "severity", "category", "summary", "failure_scenario"})
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import release_inventory
            files = set(release_inventory.RELEASE_FILES)
        finally:
            sys.path.pop(0)
        for required in ("schemas/findings.schema.json", "agents/codex-reviewer.md", "skills/wait/SKILL.md"):
            self.assertIn(required, files)
        for skill in ("run", "review", "delegate"):
            text = (ROOT / f"skills/{skill}/SKILL.md").read_text()
            for token in ("--findings", "--resume", "--background"):
                self.assertIn(token, text, skill)
        self.assertIn("--brief", (ROOT / "scripts/setup.py").read_text())


    # --- regressions from the Codex review of 1.7.0 ---

    def test_background_keeps_caller_directory_for_relative_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw).resolve(); tools = home / "tools"; tools.mkdir(); capture = home / "args"
            work = home / "work"; work.mkdir(); (work / "sub").mkdir()
            (work / "s.json").write_text('{"type":"object"}')
            self.tool(tools, "codex", f'''\
                if [ "$1" = login ]; then echo "Logged in"; exit 0; fi
                printf '%s\\n' "$@" > "{capture}"; echo ok
            ''')
            env = os.environ | {"HOME": str(home), "PATH": str(tools)+":/usr/bin:/bin", "DEX_WORKERS_STATE_DIR": str(home / "state")}
            started = subprocess.run([sys.executable, str(CLI), "--home", str(home), "--probe-timeout", "0.5",
                                      "run", "x", "--provider", "codex", "--cwd", "sub", "--schema", "s.json", "--background"],
                                     text=True, capture_output=True, env=env, cwd=work, check=False, timeout=8)
            run_id = json.loads(started.stdout)["run_id"]
            waited = self.call(home, "wait", run_id, "--timeout", "6")
            inner = json.loads(waited.stdout)["results"][run_id]
            self.assertEqual(inner["status"], "completed", inner)
            args = capture.read_text().splitlines()
            self.assertEqual(args[args.index("-C") + 1], str(work / "sub"))
            self.assertEqual(args[args.index("--output-schema") + 1], str(work / "s.json"))

    def test_structured_output_is_redacted_recursively(self):
        module = load_module()
        document = {"summary": "key api_key=abc123secret", "findings": [{"summary": "token sk-ABCDEFGHIJKLMNOP1234"}], "n": 3}
        cleaned = module.redact_value(document)
        self.assertNotIn("abc123secret", json.dumps(cleaned)); self.assertNotIn("sk-ABCDEFGHIJKLMNOP1234", json.dumps(cleaned))
        self.assertEqual(cleaned["n"], 3)
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir()
            self.tool(tools, "codex", '''\
                if [ "$1" = login ]; then echo "Logged in"; exit 0; fi
                echo '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"summary\\":\\"leak sk-ABCDEFGHIJKLMNOP1234 here\\",\\"findings\\":[]}"}}'
            ''')
            done = self.call(home, "run", "x", "--provider", "codex", "--findings", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(done.stdout)
            self.assertEqual(data["status"], "completed", done.stderr)
            self.assertNotIn("sk-ABCDEFGHIJKLMNOP1234", json.dumps(data))
            self.assertIn("[REDACTED]", data["structured"]["summary"])

    def test_worker_failure_surfaces_codex_error_events(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir()
            self.tool(tools, "codex", '''\
                if [ "$1" = login ]; then echo "Logged in"; exit 0; fi
                echo '{"type":"thread.started","thread_id":"t-1"}'
                echo '{"type":"error","message":"The model `gpt-x` does not exist"}'
                echo '{"type":"error","message":"The model `gpt-x` does not exist"}'
                exit 1
            ''')
            done = self.call(home, "run", "x", "--provider", "codex", "--model", "gpt-x", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(done.stdout)
            self.assertEqual(data["status"], "CLAUDE_FALLBACK"); self.assertEqual(data["reason"], "worker_failed")
            self.assertEqual(data["provider_errors"], ["The model `gpt-x` does not exist"]); self.assertEqual(data["session_id"], "t-1")

    def test_empty_worker_output_falls_back(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); tools = home / "tools"; tools.mkdir()
            self.tool(tools, "agy", '''\
                if [ "$1" = models ]; then echo model
                elif [ "$1" = --help ]; then echo "--print --print-timeout --sandbox --output-format"
                else echo '{"conversation_id":"c","status":"SUCCESS","response":""}'; echo "permission auto-denied" >&2; fi
            ''')
            done = self.call(home, "run", "x", "--provider", "agy", path=str(tools)+":/usr/bin:/bin")
            data = json.loads(done.stdout)
            self.assertEqual(data["status"], "CLAUDE_FALLBACK"); self.assertEqual(data["reason"], "empty_output")
            self.assertIn("auto-denied", data["stderr"])

    def test_unusable_result_file_is_reported_and_launching_runs_count(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw); state = home / "state"; state.mkdir()
            (state / "7-7.result.json").write_text("not json"); (state / "7-7.log").write_text("")
            data = json.loads(self.call(home, "result", "7-7").stdout)
            self.assertEqual(data["result"]["error"], "unreadable_result")
            self.assertFalse((state / "7-7.result.json").exists()); self.assertFalse((state / "7-7.log").exists())
            (state / "8-8.result.json").write_bytes(b"[" + b"1," * 2_200_000 + b"1]")
            data = json.loads(self.call(home, "wait", "8-8", "--timeout", "2").stdout)
            self.assertEqual(data["results"]["8-8"]["error"], "result_too_large")
            # A launcher that has not yet recorded its worker still occupies a slot.
            (state / "9-9.log").write_text("")
            status = json.loads(self.call(home, "status").stdout)
            self.assertEqual(status["launching"], ["9-9"])
            env = os.environ | {"HOME": str(home), "PATH": "/usr/bin:/bin", "DEX_WORKERS_STATE_DIR": str(state), "DEX_WORKERS_MAX_ACTIVE": "1"}
            refused = subprocess.run([sys.executable, str(CLI), "--home", str(home), "--probe-timeout", "0.5", "run", "x", "--background"],
                                     text=True, capture_output=True, env=env, check=False, timeout=8)
            self.assertEqual(json.loads(refused.stdout)["error"], "too_many_active_runs")
            self.assertEqual(json.loads(refused.stdout)["active"], 1)


if __name__ == "__main__": unittest.main()
