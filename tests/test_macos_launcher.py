import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "启动审核界面.command"
SWIFT_LAUNCHER = ROOT / "macos" / "ImageFactoryLauncher.swift"


class MacOSLauncherCommandTests(unittest.TestCase):
    def test_backup_launcher_opens_empty_workbench_without_an_active_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            command = project / COMMAND.name
            shutil.copy2(COMMAND, command)
            python = project / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            arguments_log = project / "arguments.txt"
            python.write_text(
                "#!/bin/sh\n"
                'if [ "${1:-}" = "-c" ]; then\n'
                '  exec "$REAL_PYTHON" "$@"\n'
                "fi\n"
                'printf "%s\\n" "$@" > "$ARGUMENTS_LOG"\n',
                encoding="utf-8",
            )
            python.chmod(0o755)
            environment = os.environ.copy()
            environment["REAL_PYTHON"] = sys.executable
            environment["ARGUMENTS_LOG"] = str(arguments_log)

            result = subprocess.run(
                [str(command)], cwd=project, env=environment,
                capture_output=True, text=True, timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = arguments_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("human-ui", arguments)
            self.assertNotIn("--run", arguments)

    def test_backup_launcher_skips_an_occupied_default_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            command = project / COMMAND.name
            shutil.copy2(COMMAND, command)
            python = project / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            arguments_log = project / "arguments.txt"
            python.write_text(
                "#!/bin/sh\n"
                'if [ "${1:-}" = "-c" ]; then\n'
                '  exec "$REAL_PYTHON" "$@"\n'
                "fi\n"
                'printf "%s\\n" "$@" > "$ARGUMENTS_LOG"\n',
                encoding="utf-8",
            )
            python.chmod(0o755)
            pointer = project / "var" / "live" / "active-v1-run.json"
            pointer.parent.mkdir(parents=True)
            pointer.write_text('{"run_id":"TOPSTAR-DEMO-001"}', encoding="utf-8")
            environment = os.environ.copy()
            environment["REAL_PYTHON"] = sys.executable
            environment["ARGUMENTS_LOG"] = str(arguments_log)

            occupied = None
            for port in range(8790, 8891):
                candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    candidate.bind(("127.0.0.1", port))
                except OSError:
                    candidate.close()
                    continue
                candidate.listen()
                occupied = candidate
                break
            self.assertIsNotNone(occupied, "no test port available in launcher range")
            try:
                result = subprocess.run(
                    [str(command)],
                    cwd=project,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            finally:
                occupied.close()

            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = arguments_log.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("--run", arguments)
            port_index = arguments.index("--port")
            chosen_port = int(arguments[port_index + 1])
            self.assertGreaterEqual(chosen_port, 8790)
            self.assertLessEqual(chosen_port, 8890)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", chosen_port))
                except OSError as exc:
                    self.fail(f"launcher selected an occupied port: {chosen_port}: {exc}")


class MacOSLauncherIntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.swift = SWIFT_LAUNCHER.read_text(encoding="utf-8")

    def swift_block(self, signature):
        start = self.swift.index(signature)
        opening = self.swift.index("{", start)
        depth = 0
        for position in range(opening, len(self.swift)):
            if self.swift[position] == "{":
                depth += 1
            elif self.swift[position] == "}":
                depth -= 1
                if depth == 0:
                    return self.swift[opening:position + 1]
        self.fail(f"unclosed Swift block for {signature}")

    def test_intake_requires_an_approved_profile_and_matching_product_scope(self):
        gate = self.swift_block("private static func hasAuthorizedIntakeConfiguration")

        for field in ("intake_grant", "intake_profile", "approved", "authorization_id", "actor_id",
                      "product_record_id", "allowed_product_ids"):
            with self.subTest(field=field):
                self.assertIn(f'"{field}"', gate)
        self.assertIn("allowedProductIDs.contains(productRecordID)", gate)

    def test_human_ui_opens_independently_and_intake_never_starts_a_runner(self):
        opening = self.swift_block("func openWorkbench()")
        intake = self.swift_block("private func startIntakeLoopIfAuthorized")

        self.assertIn('"human-ui"', opening)
        self.assertIn("startIntakeLoopIfAuthorized(root: root)", opening)
        self.assertLess(opening.index("try child.run()"),
                        opening.index("startIntakeLoopIfAuthorized(root: root)"))
        for argument in ('"intake-loop"', '"--interval"', '"--max-cycles"'):
            with self.subTest(argument=argument):
                self.assertIn(argument, intake)
        self.assertNotIn('"runner-loop"', self.swift)
        self.assertNotIn('"runtime-dispatch-next"', self.swift)
        self.assertNotIn("hasActiveRun", opening)
        self.assertIn("目前暂无任务", self.swift)

    def test_stopping_or_exiting_app_stops_both_independent_children(self):
        stop = self.swift_block("func stopService()")
        terminate = self.swift_block("private func terminateOwnedProcesses()")
        ui_termination = self.swift_block("private func childDidTerminate")

        self.assertIn("stopIntakeLoop()", stop)
        self.assertIn("process.terminate()", stop)
        self.assertIn("intakeProcess", terminate)
        self.assertIn("if let process, process.isRunning", terminate)
        self.assertIn("if intakeProcess?.isRunning == true", terminate)
        self.assertIn("terminateOwnedProcesses()", self.swift)
        self.assertIn("stopIntakeLoop()", ui_termination)

    def test_intake_failure_has_separate_status_and_does_not_fail_review_ui(self):
        intake_termination = self.swift_block("private func intakeChildDidTerminate")

        self.assertIn("intakeMessage", self.swift)
        self.assertIn("launcher.intakeMessage", self.swift)
        self.assertIn("表单接单", intake_termination)
        self.assertNotIn("phase = .failed", intake_termination)


if __name__ == "__main__":
    unittest.main()
