"""Tests for portparity. Run: python3 -m unittest discover -s tests -v"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "portparity" / "scripts" / "portparity.py"
FAKE = Path(__file__).resolve().parent / "fake_adapter.py"
EXAMPLE = ROOT / "examples" / "shipping-fee"

spec = importlib.util.spec_from_file_location("portparity", SCRIPT)
pp = importlib.util.module_from_spec(spec)
sys.modules["portparity"] = pp
spec.loader.exec_module(pp)


def fake(behavior, *args, mode="process"):
    return {"cmd": [sys.executable, str(FAKE), behavior, *map(str, args)], "mode": mode}


class Workspace:
    """Builds a temporary config + cases and runs the CLI in-process."""

    def __init__(self, test):
        self.dir = Path(tempfile.mkdtemp(prefix="pp-test-"))
        test.addCleanup(shutil.rmtree, self.dir, True)

    def config(self, original, replacement, inputs, **extra):
        lines = []
        for i, value in enumerate(inputs):
            lines.append(json.dumps(value if isinstance(value, dict) and "input" in value
                                    else {"id": f"c{i}", "input": value}))
        (self.dir / "cases.jsonl").write_text("\n".join(lines) + "\n")
        cfg = {"version": 1, "original": original, "replacement": replacement, "cases": ["cases.jsonl"],
               "timeout_seconds": 5, **extra}
        path = self.dir / "portparity.json"
        path.write_text(json.dumps(cfg))
        return path

    def run(self, *args, out="out"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = pp.main(["run", str(self.dir / "portparity.json"), "--out", str(self.dir / out), "--quiet", *args])
        report_path = self.dir / out / "report.json"
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        return code, report, stdout.getvalue() + stderr.getvalue()


class StrictJsonTests(unittest.TestCase):
    def test_rejects_non_interoperable_json(self):
        for text in ("NaN", "[Infinity]", "1e999", '{"a":1,"a":2}', '"\\ud800"', '{"\\udfff":1}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                pp.strict_loads(text)

    def test_depth_limit(self):
        pp.strict_loads("[" * 10 + "]" * 10, max_depth=10)
        with self.assertRaises(ValueError):
            pp.strict_loads("[" * 11 + "]" * 11, max_depth=10)
        with self.assertRaises(ValueError):
            pp.strict_loads("[" * 100000 + "]" * 100000)

    def test_wire_format_is_ascii(self):
        text = pp.wire({"s": "a b cé\U0001F600"})
        self.assertTrue(text.isascii())
        self.assertEqual(pp.strict_loads(text)["s"], "a b cé\U0001F600")


class CompareTests(unittest.TestCase):
    opts = pp.CompareOptions()

    def test_types_are_strict(self):
        self.assertIsNotNone(pp.compare_values(True, 1, self.opts))
        self.assertIsNotNone(pp.compare_values(0, False, self.opts))
        self.assertIsNotNone(pp.compare_values(1, 1.0, self.opts))
        self.assertIsNotNone(pp.compare_values(None, 0, self.opts))
        self.assertIsNone(pp.compare_values(1, 1.0, pp.CompareOptions(int_float_equal=True)))

    def test_float_tolerance(self):
        self.assertIsNotNone(pp.compare_values(0.1 + 0.2, 0.3, self.opts))
        self.assertIsNone(pp.compare_values(0.1 + 0.2, 0.3, pp.CompareOptions(float_abs_tol=1e-9)))
        self.assertIsNone(pp.compare_values(1e10, 1e10 + 1, pp.CompareOptions(float_rel_tol=1e-9)))

    def test_reports_path_of_first_difference(self):
        diff = pp.compare_values({"a": [1, {"b": "x"}]}, {"a": [1, {"b": "y"}]}, self.opts)
        self.assertTrue(diff.startswith("$.a[1].b"), diff)
        self.assertIn("keys differ", pp.compare_values({"a": 1}, {"b": 1}, self.opts))
        self.assertIn("length", pp.compare_values([1], [1, 2], self.opts))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)

    def load(self, **overrides):
        path = self.ws.config(fake("echo"), fake("echo"), [1])
        cfg = json.loads(path.read_text())
        cfg.update(overrides)
        path.write_text(json.dumps(cfg))
        return pp.load_config(path)

    def test_valid_config(self):
        cfg = self.load()
        self.assertEqual(cfg.original.mode, "process")
        self.assertEqual(cfg.original.env["TZ"], "UTC")

    def test_rejects_bad_configs(self):
        bad = [
            {"version": 2},
            {"typo_key": 1},
            {"original": {"cmd": "python3 adapter.py"}},
            {"original": {"cmd": []}},
            {"original": {"cmd": ["x"], "mode": "batch"}},
            {"timeout_seconds": 0},
            {"limits": {"max_output_bytes": 0}},
            {"compare": {"error_fields": ["message"]}},
            {"accepted_differences": [{"case": "c0", "reason": "ok"}]},
            {"accepted_differences": [{"case": "c0", "reason": " ", "approved_by": "me"}]},
            {"negative_controls": [{"cmd": ["x"]}]},
            {"env": {"A=B": "x"}},
            {"cases": []},
        ]
        for override in bad:
            with self.subTest(override=override), self.assertRaises(pp.UsageError):
                self.load(**override)

    def test_rejects_bad_case_files(self):
        path = self.ws.config(fake("echo"), fake("echo"), [{"id": "x", "input": 1}, {"id": "x", "input": 2}])
        cfg = pp.load_config(path)
        with self.assertRaisesRegex(pp.UsageError, "duplicate case id"):
            pp.load_cases(cfg.case_files, cfg.limits, self.ws.dir)
        (self.ws.dir / "cases.jsonl").write_text('{"input": 1, "extra": true}\n')
        with self.assertRaisesRegex(pp.UsageError, "unknown key"):
            pp.load_cases(cfg.case_files, cfg.limits, self.ws.dir)
        (self.ws.dir / "cases.jsonl").write_text("\n\n")
        with self.assertRaisesRegex(pp.UsageError, "no cases"):
            pp.load_cases(cfg.case_files, cfg.limits, self.ws.dir)
        (self.ws.dir / "cases.jsonl").write_text('{"id": "a\\u202eb", "input": 1}\n')
        with self.assertRaisesRegex(pp.UsageError, "control or formatting"):
            pp.load_cases(cfg.case_files, cfg.limits, self.ws.dir)

    def test_cases_without_ids_get_stable_hash_ids(self):
        path = self.ws.config(fake("echo"), fake("echo"), [])
        (self.ws.dir / "cases.jsonl").write_text('{"input": {"b": 1, "a": 2}}\n')
        cfg = pp.load_config(path)
        cases, _ = pp.load_cases(cfg.case_files, cfg.limits, self.ws.dir)
        self.assertEqual(cases[0].id, pp.input_hash({"a": 2, "b": 1}))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)

    def test_pass_both_modes(self):
        for mode in ("process", "stream"):
            with self.subTest(mode=mode):
                self.ws.config(fake("echo", mode=mode), fake("echo", mode=mode), [1, "two", None, [3], {"x": 1.5}])
                code, report, _ = self.ws.run("--force")
                self.assertEqual(code, pp.EXIT_PASS, report)
                self.assertEqual(report["summary"]["match"], 5)
                self.assertEqual(report["result"], "pass")

    def test_mismatch_is_shrunk_to_boundary(self):
        self.ws.config(fake("double"), fake("buggy_ge10", mode="stream"), [3, 37, 9])
        code, report, _ = self.ws.run()
        self.assertEqual(code, pp.EXIT_MISMATCH)
        self.assertEqual(report["summary"]["mismatch"], 1)
        shrunk = report["mismatches"][0]["shrunk"]
        self.assertTrue(shrunk["reduced"])
        self.assertEqual(shrunk["input"], 10)
        regressions = (self.ws.dir / "out" / "regressions.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(regressions[0])["input"], 10)

    def test_same_failure_is_shrunk_once(self):
        self.ws.config(fake("double", mode="stream"), fake("buggy_ge10", mode="stream"), [40, 50, 60])
        _, report, _ = self.ws.run()
        self.assertEqual(sum(1 for m in report["mismatches"] if m["shrunk"].get("same_failure_as")), 2)
        self.assertEqual(len((self.ws.dir / "out" / "regressions.jsonl").read_text().splitlines()), 1)

    def test_shrinking_keeps_the_same_failure(self):
        # Removing the key would produce a different error; the shrinker must not drift to it.
        value = {"n": 37}
        calls = []

        def still_fails(v):
            calls.append(v)
            return isinstance(v, dict) and isinstance(v.get("n"), int) and v["n"] >= 10

        result, _ = pp.shrink(value, still_fails, 500, lambda: False)
        self.assertEqual(result, {"n": 10})

    def test_error_types_compared_messages_ignored(self):
        self.ws.config(fake("error_a"), fake("error_a"), [1, 2])
        self.assertEqual(self.ws.run("--force")[0], pp.EXIT_PASS)
        self.ws.config(fake("error_a"), fake("error_b"), [1])
        code, report, _ = self.ws.run("--force")
        self.assertEqual(code, pp.EXIT_MISMATCH)
        self.assertIn("error.type", report["mismatches"][0]["diff"])

    def test_int_and_float_are_different_by_default(self):
        self.ws.config(fake("echo"), fake("as_float"), [1])
        self.assertEqual(self.ws.run()[0], pp.EXIT_MISMATCH)

    def test_replacement_failures_are_mismatches(self):
        cases = {
            "crash": "closed stdout without a response",
            "garbage": "not valid JSON",
            "extra_line": "extra output after the response",
            "wrong_id": "id does not match",
            "nan": "non-standard JSON constant",
            "big": "larger than",
            "exit_after": "exit code 2",
        }
        for behavior, expected in cases.items():
            with self.subTest(behavior=behavior):
                self.ws.config(fake("echo"), fake(behavior), [1], limits={"max_output_bytes": 100_000})
                code, report, _ = self.ws.run("--force", "--shrink-budget", "0")
                self.assertEqual(code, pp.EXIT_MISMATCH)
                self.assertIn(expected, report["mismatches"][0]["diff"])

    def test_original_failure_is_inconclusive_not_pass(self):
        self.ws.config(fake("crash"), fake("echo"), [1])
        code, report, _ = self.ws.run()
        self.assertEqual(code, pp.EXIT_INCONCLUSIVE)
        self.assertEqual(report["summary"]["inconclusive"], 1)

    def test_timeout_kills_process_group(self):
        pid_file = self.ws.dir / "child.pid"
        self.ws.config(fake("echo"), fake("spawn_sleeper", pid_file), [1], timeout_seconds=1)
        started = time.monotonic()
        code, report, _ = self.ws.run("--shrink-budget", "0")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(code, pp.EXIT_MISMATCH)
        self.assertIn("timeout", report["mismatches"][0]["diff"])
        child = int(pid_file.read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_adapter_that_never_reads_stdin_cannot_block_the_runner(self):
        self.ws.config(fake("echo"), fake("no_read"), [{"input": "x" * 900_000}], timeout_seconds=1)
        started = time.monotonic()
        code, _, _ = self.ws.run("--shrink-budget", "0")
        self.assertEqual(code, pp.EXIT_MISMATCH)
        self.assertLess(time.monotonic() - started, 10)

    def test_stderr_is_drained_and_capped(self):
        self.ws.config(fake("echo"), fake("stderr_flood"), [1], limits={"max_stderr_bytes": 1000})
        self.assertEqual(self.ws.run()[0], pp.EXIT_PASS)

    def test_stream_mode_restarts_after_crash(self):
        self.ws.config(fake("echo", mode="stream"), fake("crash_on", 2, mode="stream"), [1, 2, 3, 4])
        code, report, _ = self.ws.run("--shrink-budget", "0")
        self.assertEqual(code, pp.EXIT_MISMATCH)
        self.assertEqual(report["summary"]["match"], 3)
        self.assertIn("boom", report["mismatches"][0]["replacement"]["stderr_tail"])

    def test_nondeterminism_is_detected(self):
        self.ws.config(fake("random"), fake("random"), [1])
        code, report, _ = self.ws.run()
        self.assertEqual(code, pp.EXIT_INCONCLUSIVE)
        self.assertIn("unstable", report["inconclusive"][0]["diff"])
        self.ws.config(fake("echo"), fake("stateful", mode="stream"), [1])
        code, report, _ = self.ws.run("--force", "--shrink-budget", "0")
        self.assertEqual(code, pp.EXIT_MISMATCH)
        self.assertIn("unstable", report["mismatches"][0]["diff"])

    def test_negative_controls(self):
        self.ws.config(fake("double"), fake("double"), [3, 12],
                       negative_controls=[{"name": "bug", **fake("buggy_ge10")}])
        code, report, _ = self.ws.run()
        self.assertEqual(code, pp.EXIT_PASS)
        self.assertTrue(report["negative_controls"][0]["rejected"])
        self.ws.config(fake("double"), fake("double"), [3, 4],
                       negative_controls=[{"name": "bug", **fake("buggy_ge10")}])
        code, report, _ = self.ws.run("--force")
        self.assertEqual(code, pp.EXIT_INEFFECTIVE)
        self.assertFalse(report["negative_controls"][0]["rejected"])

    def test_missing_negative_control_warns(self):
        self.ws.config(fake("echo"), fake("echo"), [1])
        _, report, _ = self.ws.run()
        self.assertTrue(any("negative_controls" in w for w in report["warnings"]))

    def test_accepted_differences(self):
        acc = {"case": "c0", "reason": "owner wants half-up", "approved_by": "owner"}
        self.ws.config(fake("double"), fake("buggy_ge10"), [11], accepted_differences=[acc])
        code, report, _ = self.ws.run()
        self.assertEqual(code, pp.EXIT_PASS)
        self.assertEqual(report["summary"]["accepted"], 1)
        pinned = {**acc, "replacement": {"status": "ok", "output": 999}}
        self.ws.config(fake("double"), fake("buggy_ge10"), [11], accepted_differences=[pinned])
        self.assertEqual(self.ws.run("--force")[0], pp.EXIT_MISMATCH)
        self.ws.config(fake("double"), fake("double"), [11], accepted_differences=[acc])
        code, report, _ = self.ws.run("--force")
        self.assertEqual(code, pp.EXIT_PASS)
        self.assertEqual(report["stale_accepted_differences"][0]["case"], "c0")

    def test_time_budget_reports_not_run(self):
        self.ws.config(fake("slow", 0.3), fake("echo"), list(range(20)), determinism_runs=1)
        code, report, _ = self.ws.run("--max-seconds", "0.5")
        self.assertEqual(code, pp.EXIT_INCONCLUSIVE)
        self.assertGreater(report["summary"]["not_run"], 0)

    def test_missing_command_is_usage_error(self):
        self.ws.config({"cmd": ["definitely-not-a-command-xyz"]}, fake("echo"), [1])
        code, _, output = self.ws.run()
        self.assertEqual(code, pp.EXIT_USAGE)
        self.assertIn("command not found", output)

    def test_environment_is_scrubbed(self):
        os.environ["PORTPARITY_TEST_SECRET"] = "s3cret"
        self.addCleanup(os.environ.pop, "PORTPARITY_TEST_SECRET", None)
        keys = ["PORTPARITY_TEST_SECRET", "TZ", "EXTRA"]
        self.ws.config(fake("env"), fake("env"), [keys], env={"EXTRA": "1"})
        code, _, _ = self.ws.run()
        self.assertEqual(code, pp.EXIT_PASS)
        obs = json.loads((self.ws.dir / "out" / "observations.jsonl").read_text())
        self.assertEqual(obs["original"]["output"], {"PORTPARITY_TEST_SECRET": None, "TZ": "UTC", "EXTRA": "1"})

    def test_untrusted_output_is_escaped_in_reports(self):
        self.ws.config(fake("echo"), fake("hostile"), [1])
        self.ws.run()
        md = (self.ws.dir / "out" / "report.md").read_text()
        self.assertNotIn("\x1b", md)
        self.assertNotIn("‮", md)
        self.assertIn("\\u202e", md)
        self.assertIn("````", md)  # fence longer than the embedded ```

    def test_case_filter_and_fail_fast(self):
        self.ws.config(fake("double"), fake("buggy_ge10"), [1, 20, 30, 2])
        code, report, _ = self.ws.run("--case", "c3")
        self.assertEqual((code, report["summary"]["total"]), (pp.EXIT_PASS, 1))
        self.assertTrue(any("subset" in w for w in report["warnings"]))
        code, report, _ = self.ws.run("--force", "--fail-fast", "--shrink-budget", "0")
        self.assertEqual(report["summary"]["mismatch"], 1)
        self.assertEqual(report["summary"]["not_run"], 2)


class OutputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)
        self.ws.config(fake("echo"), fake("echo"), [1])

    def test_reports_are_private(self):
        self.ws.run()
        out = self.ws.dir / "out"
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for name in ("report.json", "report.md", "observations.jsonl", "regressions.jsonl"):
            self.assertEqual(stat.S_IMODE((out / name).stat().st_mode), 0o600, name)

    def test_refuses_to_clobber(self):
        (self.ws.dir / "other").mkdir()
        (self.ws.dir / "other" / "important.txt").write_text("keep")
        self.assertEqual(self.ws.run(out="other")[0], pp.EXIT_USAGE)
        self.assertEqual(self.ws.run("--force", out="other")[0], pp.EXIT_USAGE)
        self.assertEqual((self.ws.dir / "other" / "important.txt").read_text(), "keep")
        self.assertEqual(self.ws.run()[0], pp.EXIT_PASS)
        self.assertEqual(self.ws.run()[0], pp.EXIT_USAGE)
        self.assertEqual(self.ws.run("--force")[0], pp.EXIT_PASS)

    def test_refuses_symlinked_output(self):
        target = self.ws.dir / "target"
        target.mkdir()
        (self.ws.dir / "link").symlink_to(target)
        self.assertEqual(self.ws.run(out="link")[0], pp.EXIT_USAGE)
        self.assertEqual(list(target.iterdir()), [])


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)

    def gen(self, spec, *args, name="cases.jsonl"):
        path = self.ws.dir / "spec.json"
        path.write_text(json.dumps(spec))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = pp.main(["generate", str(path), "--out", str(self.ws.dir / name), *args])
        return code, self.ws.dir / name

    def test_deterministic_and_within_bounds(self):
        spec = {"seed": 5, "count": 200, "schema": {
            "type": "object", "required": ["n", "s"],
            "properties": {"n": {"type": "integer", "minimum": -3, "maximum": 1000},
                           "s": {"type": "string", "minLength": 1, "maxLength": 4},
                           "f": {"type": "number", "minimum": 0, "maximum": 1},
                           "e": {"enum": ["x", "y"]},
                           "l": {"type": "array", "items": {"type": "boolean"}, "maxItems": 3},
                           "o": {"type": ["null", "integer"], "minimum": 7}}}}
        code, path = self.gen(spec)
        self.assertEqual(code, 0)
        first = path.read_text()
        code, path2 = self.gen(spec, name="again.jsonl")
        self.assertEqual(first, path2.read_text())
        self.assertTrue(first.isascii())
        rows = [json.loads(line) for line in first.splitlines()]
        self.assertEqual(len(rows), 200)
        self.assertEqual(len({r["id"] for r in rows}), 200)
        for row in rows:
            v = row["input"]
            self.assertTrue(-3 <= v["n"] <= 1000 and type(v["n"]) is int)
            self.assertTrue(1 <= len(v["s"]) <= 4)
            if "f" in v:
                self.assertTrue(0 <= v["f"] <= 1)
            if "o" in v and v["o"] is not None:
                self.assertGreaterEqual(v["o"], 7)
        self.assertTrue(any(r["input"]["n"] in (-3, 1000) for r in rows), "edge values expected")

    def test_include_and_errors(self):
        code, path = self.gen({"count": 3, "include": [{"special": True}], "schema": {"type": "boolean"}})
        self.assertEqual(code, 0)
        self.assertEqual(len(path.read_text().splitlines()), 3)  # include + only two booleans exist
        self.assertEqual(self.gen({"schema": {"type": "string", "pattern": "a+"}}, name="x.jsonl")[0], 2)
        self.assertEqual(self.gen({"schema": {"type": "integer"}}, name="cases.jsonl")[0], 2)  # exists
        self.assertEqual(self.gen({"schema": {"type": "integer", "minimum": 5, "maximum": 1}}, name="y.jsonl")[0], 2)


class ValidateTests(unittest.TestCase):
    def test_validate(self):
        ws = Workspace(self)
        path = ws.config(fake("echo"), {"cmd": ["no-such-command-xyz"]}, [1])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pp.main(["validate", str(path)]), pp.EXIT_USAGE)
        self.assertIn("command not found", err.getvalue())
        ws.config(fake("echo"), fake("echo"), [1])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pp.main(["validate", str(path)]), pp.EXIT_PASS)


TEMPLATES = ROOT / "skills" / "portparity" / "templates"
PORTABLE_INPUTS = [0, -7, 2**31, "", "héllo   \U0001F600", None, True, [1, [2, {"k": "v"}]],
                   {"nested": {"a": [None, False]}}]


class TemplateTests(unittest.TestCase):
    """The shipped adapter templates must speak the protocol and agree with each other."""

    def check(self, original, replacement):
        ws = Workspace(self)
        ws.config(original, replacement, PORTABLE_INPUTS)
        code, report, _ = ws.run()
        self.assertEqual(code, pp.EXIT_PASS, report and report["mismatches"][:1])
        self.assertEqual(report["summary"]["match"], len(PORTABLE_INPUTS))

    def test_python_template(self):
        py = {"cmd": [sys.executable, str(TEMPLATES / "adapter.py")], "mode": "stream"}
        self.check(fake("echo"), py)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_node_template(self):
        node = {"cmd": ["node", str(TEMPLATES / "adapter.mjs")], "mode": "stream"}
        self.check({"cmd": [sys.executable, str(TEMPLATES / "adapter.py")]}, node)

    @unittest.skipUnless(shutil.which("dotnet"), "needs dotnet")
    def test_dotnet_template(self):
        build = Path(tempfile.mkdtemp(prefix="pp-dotnet-"))
        self.addCleanup(shutil.rmtree, build, True)
        shutil.copytree(TEMPLATES / "dotnet", build / "src")
        subprocess.run(["dotnet", "build", str(build / "src"), "-c", "Release", "-o", str(build / "out"),
                        "--nologo", "-v", "q"], check=True, capture_output=True)
        dotnet = {"cmd": ["dotnet", str(build / "out" / "Adapter.dll")], "mode": "stream"}
        self.check({"cmd": [sys.executable, str(TEMPLATES / "adapter.py")], "mode": "stream"}, dotnet)

    def test_template_config_is_valid_shape(self):
        cfg = json.loads((TEMPLATES / "portparity.json").read_text())
        self.assertEqual(set(cfg) - pp.CONFIG_KEYS, set())


@unittest.skipUnless(shutil.which("dotnet") and shutil.which("node"), "needs dotnet and node")
class ExampleTests(unittest.TestCase):
    """End-to-end: legacy C# oracle, correct Python port, broken JavaScript port."""

    @classmethod
    def setUpClass(cls):
        subprocess.run(["dotnet", "build", "original", "-c", "Release", "-o", "original/out", "--nologo", "-v", "q"],
                       cwd=EXAMPLE, check=True, capture_output=True)

    def run_example(self, config):
        out = Path(tempfile.mkdtemp(prefix="pp-example-")) / "out"
        self.addCleanup(shutil.rmtree, out.parent, True)
        result = subprocess.run([sys.executable, str(SCRIPT), "run", config, "--out", str(out), "--quiet"],
                                cwd=EXAMPLE, capture_output=True, text=True, timeout=300)
        return result.returncode, json.loads((out / "report.json").read_text()), out

    def test_correct_port_passes_and_control_is_rejected(self):
        code, report, _ = self.run_example("portparity.json")
        self.assertEqual(code, 0, report["mismatches"][:2])
        self.assertTrue(all(c["rejected"] for c in report["negative_controls"]))

    def test_broken_port_fails_with_minimal_regressions(self):
        code, report, out = self.run_example("portparity-broken.json")
        self.assertEqual(code, 1)
        regressions = [json.loads(line)["input"] for line in (out / "regressions.jsonl").read_text().splitlines()]
        self.assertIn({"express": False, "weight_grams": 2, "zone": "A"}, regressions)


if __name__ == "__main__":
    unittest.main()
