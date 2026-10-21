"""정상 기대 동작과 정보 부족 질문의 모순을 검사한다."""

import json
import unittest

from pydantic import ValidationError

from app.agent.report_parser import ReportParser
from app.agent.report_schema import BugSpec

REPORT = '등록 버튼을 눌렀지만 결과가 없습니다. 처리 결과에는 접수 완료라는 문구가 표시돼야 합니다.'


def known_data():
    """정상 기대 동작과 근거가 제공된 공통 제보를 만든다."""
    return {'summary': '처리 결과 미표시', 'reported_steps': ['등록 버튼을 누릅니다.'], 'inputs': [],
            'actual_behavior': '결과가 없습니다.', 'expected_behavior': '처리 결과에는 접수 완료라는 문구가 표시돼야 합니다.',
            'expectation_source': 'report', 'expectation_evidence': REPORT, 'missing_information': []}


class Client:
    """준비한 해석 응답을 순서대로 반환한다."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    async def generate(self, messages, schema):
        """재시도 시 수정한 전체 응답을 반환한다."""
        self.calls += 1
        return {'content': json.dumps(self.outputs.pop(0), ensure_ascii=False),
                'usage': {'backend': 'fixture', 'model': 'fixture', 'input_tokens': 10, 'output_tokens': 5}}


class ReportConsistencyTests(unittest.IsolatedAsyncioTestCase):
    """알고 있는 기대 동작을 누락으로 표시하는 응답을 강제로 승인하지 않는다."""

    async def test_known_expectation_cannot_be_missing(self):
        """확정된 기대 동작과 같은 필드의 정보 부족 표시는 함께 허용하지 않는다."""
        data = known_data()
        data['missing_information'] = [{'field': 'expected_behavior', 'question': '기대 문구가 실제로 표시되나요?'}]
        with self.assertRaises(ValidationError):
            BugSpec.model_validate(data)

    async def test_unknown_keeps_question(self):
        """기대 동작이 불명확하면 기존 확인 질문을 그대로 유지한다."""
        data = known_data()
        data.update(expected_behavior=None, expectation_source='unknown', expectation_evidence=None,
                    missing_information=[{'field': 'expected_behavior', 'question': '정상 기대 동작은 무엇인가요?'}])
        spec = BugSpec.model_validate(data)
        self.assertTrue(spec.needs_clarification)
        self.assertIsNone(spec.expected_behavior)

    async def test_other_missing_fields_preserved(self):
        """기대 동작을 알아도 필요한 입력값이 없으면 질문을 유지한다."""
        data = known_data()
        data['missing_information'] = [{'field': 'inputs', 'question': '필수 입력값을 알려주세요.'}]
        spec = BugSpec.model_validate(data)
        self.assertTrue(spec.needs_clarification)
        self.assertEqual(spec.missing_information[0].field, 'inputs')

    async def test_retry_validates_whole_response(self):
        """모순 응답을 거부한 뒤 수정한 전체 응답을 다시 검증한다."""
        invalid = known_data()
        invalid['missing_information'] = [{'field': 'expected_behavior', 'question': '실제로 정상 동작하나요?'}]
        client = Client([invalid, known_data()])
        result = await ReportParser(client).parse(REPORT)
        self.assertEqual(client.calls, 2)
        self.assertEqual(result['usage']['model_calls'], 2)
        self.assertFalse(result['spec'].needs_clarification)
        self.assertEqual(result['spec'].expected_behavior, known_data()['expected_behavior'])

    async def test_repeated_conflict_not_silently_fixed(self):
        """모순이 계속되면 질문을 코드에서 지워 성공한 응답으로 바꾸지 않는다."""
        invalid = known_data()
        invalid['missing_information'] = [{'field': 'expected_behavior', 'question': '실제로 정상 동작하나요?'}]
        client = Client([invalid, invalid])
        with self.assertRaises(ValueError):
            await ReportParser(client).parse(REPORT)
        self.assertEqual(client.calls, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
