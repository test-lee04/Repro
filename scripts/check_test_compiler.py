"""회귀 테스트 생성 규칙과 동일한 테스트의 버그·정상 재실행을 검사한다."""

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.testing.compiler import RecordedTestCompiler, UnsupportedCompilation


def fixture_run():
    """생성기 검사에만 쓰는 실행 기록이며 Agent의 처리 분기가 아니다."""
    from scripts.check_reproduction_assessment import make_run

    run = make_run()
    run.update(assessment_status="completed", reproduction_status="reproduced",
               assessment={"status": "reproduced"})
    history = []
    for kind, element_id, tag, name, value in [
        ("fill", "observed-input-123", "input", "문서 제목", "주간 계획"),
        ("click", "observed-button-456", "button", "저장", None),
        ("reload", "page-temporary-789", "page", "현재 페이지", None),
    ]:
        history.append({
            "success": True,
            "action": {"kind": kind, "element_id": element_id, "value": value},
            "before_elements": [{"element_id": element_id, "dom_id": "", "tag": tag,
                                 "name": name, "label": name if tag == "input" else "",
                                 "actions": [kind]}],
        })
    run["execution"]["history"] = history
    run["checks"][0]["observation"]["elements"][0].update(tag="input", supports_value=True)
    return run


class CompilerTests(unittest.TestCase):
    def test_extract_actions(self):
        """실제로 실행한 입력, 클릭과 새로고침을 순서대로 추출한다."""
        artifact = RecordedTestCompiler().compile_run(fixture_run())
        self.assertEqual([item["kind"] for item in artifact["actions"]], ["fill", "click", "reload"])

    def test_normal_expectation(self):
        """버그의 빈 실제 값이 아닌 정상 기대값을 테스트에 넣는다."""
        artifact = RecordedTestCompiler().compile_run(fixture_run())
        self.assertEqual(artifact["assertions"][0]["condition"]["expected_value"], "주간 계획")

    def test_temporary_ids_not_locators(self):
        """관찰의 임시 식별자를 재실행 위치로 사용하지 않는다."""
        text = json.dumps(RecordedTestCompiler().compile_run(fixture_run()))
        for value in ["observed-input-123", "observed-button-456", "page-temporary-789"]:
            self.assertNotIn(value, text)

    def test_reload_has_no_locator(self):
        artifact = RecordedTestCompiler().compile_run(fixture_run())
        self.assertIsNone(artifact["actions"][-1]["locator"])

    def test_failed_action_rejected(self):
        run = fixture_run()
        run["execution"]["history"][0]["success"] = False
        with self.assertRaises(UnsupportedCompilation):
            RecordedTestCompiler().compile_run(run)

    def test_incomplete_verdict_rejected(self):
        run = fixture_run()
        run["reproduction_status"] = "inconclusive"
        with self.assertRaises(UnsupportedCompilation):
            RecordedTestCompiler().compile_run(run)

    def test_missing_action_target_rejected(self):
        run = fixture_run()
        run["execution"]["history"][0]["before_elements"] = []
        with self.assertRaises(UnsupportedCompilation):
            RecordedTestCompiler().compile_run(run)

    def test_checkpoint_rejected(self):
        run = fixture_run()
        run["checks"][0]["checkpoint"]["review"].update(
            status="inconclusive", coverage=[], checkpoint_action_numbers=[]
        )
        with self.assertRaises(UnsupportedCompilation):
            RecordedTestCompiler().compile_run(run)

    def test_incompatible_assertion_rejected(self):
        run = fixture_run()
        run["checks"][0]["observation"]["elements"][0]["tag"] = "p"
        with self.assertRaises(UnsupportedCompilation):
            RecordedTestCompiler().compile_run(run)

    def test_anonymous_target_rejected(self):
        run = fixture_run()
        target = run["execution"]["history"][0]["before_elements"][0]
        target.update(name="", label="", dom_id="")
        with self.assertRaises(UnsupportedCompilation):
            RecordedTestCompiler().compile_run(run)

    def test_unknown_action_rejected(self):
        run = fixture_run()
        run["execution"]["history"][0]["action"]["kind"] = "evaluate"
        with self.assertRaises(UnsupportedCompilation):
            RecordedTestCompiler().compile_run(run)

    def test_export(self):
        compiler = RecordedTestCompiler()
        with tempfile.TemporaryDirectory() as folder:
            result = compiler.export(compiler.compile_run(fixture_run()), Path(folder))
            self.assertEqual(result["test_status"], "generated")
            self.assertTrue((Path(folder) / "case.json").exists())
            source = (Path(folder) / "test_regression.py").read_text()
            compile(source, "test_regression.py", "exec")

    def test_data_not_python(self):
        compiler = RecordedTestCompiler()
        artifact = compiler.compile_run(fixture_run())
        artifact["actions"][0]["value"] = "__import__('os').system('example')"
        with tempfile.TemporaryDirectory() as folder:
            compiler.export(artifact, Path(folder))
            self.assertNotIn("system('example')", (Path(folder) / "test_regression.py").read_text())
            data = json.loads((Path(folder) / "case.json").read_text())
            self.assertEqual(data["actions"][0]["value"], artifact["actions"][0]["value"])

    def test_code_is_fixed(self):
        compiler = RecordedTestCompiler()
        artifact = compiler.compile_run(fixture_run())
        with tempfile.TemporaryDirectory() as folder:
            one, two = Path(folder) / "one", Path(folder) / "two"
            compiler.export(artifact, one)
            artifact["actions"][0]["value"] = "다른 입력값"
            compiler.export(artifact, two)
            self.assertEqual((one / "test_regression.py").read_bytes(),
                             (two / "test_regression.py").read_bytes())

    def test_empty_test_rejected(self):
        with self.assertRaises(UnsupportedCompilation):
            RecordedTestCompiler().compile([], [])


def generate(args):
    """저장된 실제 실행 기록을 테스트 자료와 실행 코드로 변환한다."""
    compiler = RecordedTestCompiler()
    source = Path(args.run_dir) / "workflow_result.json"
    run = json.loads(source.read_text(encoding="utf-8"))
    artifact = compiler.compile_run(run)
    artifact["source_run"] = str(source)
    output = Path(args.output_dir) if args.output_dir else Path(args.run_dir) / "regression"
    result = compiler.export(artifact, output)
    (output / "generation_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("생성 상태:", result["test_status"])
    print("행동 수:", result["action_count"])
    print("검증 조건 수:", result["assertion_count"])
    print("테스트 자료:", result["case_path"])
    print("실행 코드:", result["test_path"])
    print("생성 완료이며 재실행 검증 완료를 의미하지 않습니다.")


def replay_demo(args):
    """동일한 생성 파일을 두 데모 중 하나에서 AI 호출 없이 실행한다."""
    from scripts.check_reproduction_workflow import start_demo

    folder = Path(args.case_dir)
    script = folder / "test_regression.py"
    if not script.is_file() or not (folder / "case.json").is_file():
        raise ValueError("생성한 case.json과 test_regression.py가 필요합니다.")
    server, url = start_demo(args.demo)
    try:
        completed = subprocess.run(
            [sys.executable, "-B", str(script.resolve()), "--url", url],
            capture_output=True, text=True, timeout=75,
        )
        print(completed.stdout)
        if completed.stderr:
            print(completed.stderr)
        expected_code = 1 if args.demo == "buggy" else 0
        assert completed.returncode == expected_code, (
            f"데모의 예상 종료 코드 {expected_code}와 다릅니다: {completed.returncode}"
        )
        print("PASS: 동일한 생성 테스트의 데모 재실행 결과")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--run-dir")
    group.add_argument("--case-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--demo", choices=["buggy", "fixed"])
    args = parser.parse_args()
    if args.run_dir:
        generate(args)
    elif args.case_dir:
        if not args.demo:
            parser.error("검사할 --demo 버전을 선택해야 합니다.")
        replay_demo(args)
    elif args.demo or args.output_dir:
        parser.error("--run-dir 또는 --case-dir을 먼저 지정해야 합니다.")
    else:
        unittest.main(argv=["check_test_compiler"], verbosity=2)
