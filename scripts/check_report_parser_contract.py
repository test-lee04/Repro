"""모델 호출 없이 제보 해석기의 근거·출처 검증을 검사한다."""

import json
import unittest

from app.agent.report_parser import ReportParser
from app.agent.report_schema import ClarificationAnswer


class StubClient:
    """검증용 응답을 반환하는 모델 대역."""

    def __init__(self, output: dict):
        self.output = output
        self.messages = []

    async def generate(
        self,
        messages: list[dict],
        schema: dict,
    ) -> dict:
        """준비한 응답을 반환하고 요청 메시지를 보관한다.

        Args:
            messages: 해석기가 전달한 메시지.
            schema: 요청한 응답 형식.

        Returns:
            검증용 모델 응답.
        """
        self.messages = messages

        return {
            "content": json.dumps(
                self.output,
                ensure_ascii=False,
            ),
            "usage": {"backend": "검증용"},
        }


class ReportParserContractTests(
    unittest.IsolatedAsyncioTestCase
):
    """실제 모델 성능이 아닌 코드의 검증 규칙을 검사한다."""

    def make_output(
        self,
        source: str,
        evidence: str,
    ) -> dict:
        """근거 검사를 위한 응답을 만든다.

        Args:
            source: 모델이 지정한 출처.
            evidence: 모델이 인용한 근거.

        Returns:
            구조화 응답 딕셔너리.
        """
        return {
            "summary": "저장한 내용이 유지되지 않음",
            "reported_steps": [],
            "inputs": [],
            "actual_behavior": "다시 열면 내용이 사라짐",
            "expected_behavior": "저장한 내용 유지",
            "expectation_source": source,
            "expectation_evidence": evidence,
            "missing_information": [],
        }

    async def test_report_evidence(self):
        """원래 제보에 있는 근거를 허용한다."""
        text = "저장한 내용이 유지돼야 합니다."
        client = StubClient(
            self.make_output("report", text)
        )

        result = await ReportParser(client).parse(
            report=text
        )

        self.assertIsNone(result["source_correction"])

    async def test_answer_evidence_and_history(self):
        """답변을 모델에 전달하고 답변의 근거를 허용한다."""
        text = "다시 열어도 저장한 내용이 유지돼야 합니다."
        client = StubClient(
            self.make_output("user_answer", text)
        )

        result = await ReportParser(client).parse(
            report="저장 기능이 이상합니다.",
            answers=[
                ClarificationAnswer(
                    question="기대 동작은 무엇인가요?",
                    answer=text,
                )
            ],
        )

        payload = json.loads(
            client.messages[1]["content"]
        )

        self.assertEqual(
            payload["answers"][0]["answer"],
            text,
        )
        self.assertIsNone(result["source_correction"])

    async def test_question_is_not_user_evidence(self):
        """질문에만 있는 문장은 사용자 근거로 인정하지 않는다."""
        question = "저장한 내용이 유지돼야 하나요?"
        client = StubClient(
            self.make_output("user_answer", question)
        )

        with self.assertRaises(ValueError):
            await ReportParser(
                client, max_attempts=1
            ).parse(
                report="저장 기능이 이상합니다.",
                answers=[
                    ClarificationAnswer(
                        question=question,
                        answer="아직 확인하지 못했습니다.",
                    )
                ],
            )

    async def test_missing_evidence(self):
        """원문에 없는 근거를 거부한다."""
        client = StubClient(
            self.make_output(
                "public_spec",
                "저장한 내용은 영구 보존됩니다.",
            )
        )

        with self.assertRaises(ValueError):
            await ReportParser(
                client, max_attempts=1
            ).parse(
                report="저장 기능이 이상합니다.",
                public_spec="제목을 입력할 수 있습니다.",
            )

    async def test_unique_source_correction(self):
        """답변에만 있는 인용문의 출처를 보정하고 기록한다."""
        text = "다시 열어도 저장한 내용이 유지돼야 합니다."

        # 모델이 답변의 문장을 인용하면서 출처를 잘못 붙인 경우다.
        client = StubClient(
            self.make_output("report", text)
        )

        result = await ReportParser(client).parse(
            report="저장 기능이 이상합니다.",
            answers=[
                ClarificationAnswer(
                    question="기대 동작은 무엇인가요?",
                    answer=text,
                )
            ],
        )

        self.assertEqual(
            result["spec"].expectation_source.value,
            "user_answer",
        )
        self.assertEqual(
            result["spec"].expectation_evidence,
            text,
        )
        self.assertEqual(
            result["source_correction"]["from"],
            "report",
        )
        self.assertEqual(
            result["evidence_matches"][0]["document_id"],
            "answer:1",
        )

    async def test_ambiguous_correction_rejected(self):
        """다른 출처 여러 종류에 인용문이 있으면 보정을 거부한다."""
        text = "저장한 내용이 유지돼야 합니다."
        client = StubClient(
            self.make_output("report", text)
        )

        with self.assertRaises(ValueError):
            await ReportParser(
                client, max_attempts=1
            ).parse(
                report="저장 기능이 이상합니다.",
                public_spec=text,
                answers=[
                    ClarificationAnswer(
                        question="기대 동작은 무엇인가요?",
                        answer=text,
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)