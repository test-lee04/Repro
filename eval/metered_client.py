"""평가용 모델 연결에서 실제 호출 횟수와 제공된 사용량을 기록한다."""

import asyncio
from time import perf_counter

from app.testing.replay import write_json


class MeteredClient:
    """동일 응답을 그대로 전달하며 호출 기록만 추가하는 모델 연결 대리자."""

    def __init__(self, client, path):
        """원래 연결 객체와 호출 이력 저장 경로를 받는다."""
        self.client = client
        self.path = path
        self.calls = []

    async def generate(self, messages, schema):
        """성공, 실패와 취소를 모두 한 번의 실제 호출로 기록한다."""
        row = {'call_number': len(self.calls) + 1, 'usage': {}, 'error': None}
        self.calls.append(row)
        started = perf_counter()
        try:
            response = await self.client.generate(messages=messages, schema=schema)
            row['usage'] = response.get('usage') or {}
            return response
        except asyncio.CancelledError:
            row['error'] = {'type': 'CancelledError', 'message': '모델 호출이 취소됐습니다.'}
            raise
        except Exception as error:
            row['error'] = {'type': type(error).__name__, 'message': str(error)}
            raise
        finally:
            row['elapsed_seconds'] = round(perf_counter() - started, 3)
            write_json(self.path, {'calls': self.calls})

    def summary(self):
        """누락된 토큰을 0으로 바꾸지 않고 실제 확인한 사용량만 합산한다."""
        result = {'model_calls': len(self.calls), 'model_errors': sum(item['error'] is not None for item in self.calls)}
        for key in ['input_tokens', 'output_tokens']:
            values = [item['usage'].get(key) for item in self.calls]
            known = [value for value in values if type(value) is int and value >= 0]
            result[key] = sum(known) if len(known) == len(values) else None
            result[key + '_known_subtotal'] = sum(known)
            result[key + '_known_calls'] = len(known)
        result['models'] = sorted({str(item['usage']['model']) for item in self.calls if item['usage'].get('model')})
        result['backends'] = sorted({str(item['usage']['backend']) for item in self.calls if item['usage'].get('backend')})
        return result
