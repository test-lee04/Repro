# 평가 실행 안내

실제 평가 자료는 `fixtures.py`에 정의한 문서 저장, 검색, 가입 검증 및 정보 부족 제보다. 자료별 버그·정상 환경을 준비하되, Agent에는 제보와 URL만 전달하고 정답과 환경 이름은 전달하지 않는다.

## 실행

```sh
python -m eval.run_eval --case all --repeats 2 --verify-tests
```

Ollama와 `.env`에 설정한 모델, Playwright Chromium이 필요하다. 모델 없이 평가 도구 자체를 확인하려면 다음 명령을 사용한다.

```sh
python -m scripts.check_evaluation
python -m scripts.check_confirmed_evaluation
```

실행 결과는 `artifacts/evaluation-.../`의 `evaluation.json`, `results.csv`, `summary.json`, `summary.md`에 저장된다. 실행 오류와 보류도 분모에 포함한다. 선택한 회귀 테스트 재실행 검증 시간은 Agent 실행 시간과 구분한다.

## 확인 답변 반영 후 평가

```sh
python -m scripts.check_verification_planner --interactive --report '평가할 원래 제보'
python -m eval.run_eval --case validation --confirmed-plan artifacts/verification-plan-실제식별자 --repeats 2 --verify-tests
```

두 번째 명령의 경로는 첫 번째 명령으로 생성한 실제 폴더로 바꾼다. 평가 자료의 원래 제보와 실제 사용자 답변이 일치해야 한다. 다른 제보의 답변이나 질문 대기 기록을 재사용할 수 없다. `--case all`과 확인 기록 하나를 함께 사용하지 않는다. 자동 평가와 답변 후 평가는 서로 다른 방식으로 보고하며 합산하지 않는다.

## 주요 파일

- `fixtures.py`: 로컬 대상 사이트와 평가 정답. 앱의 사례별 실행 규칙으로 사용하지 않는다.
- `run_eval.py`: 실제 실행과 선택한 동일 테스트 재실행 검증.
- `metered_client.py`: 실제 호출·토큰 사용량 계측.
- `summarize.py`: 분모와 미측정을 구분한 결과 집계.
- `confirmation.py`: 저장된 실제 확인 답변과 원래 제보 대조.
- `reference_rules/`와 `check_cart_assessment.py`: 초기 장바구니 사례의 평가용 참조 규칙. 일반 Agent 경로와 분리한다.

제출 수치는 [반복 자동 평가](../docs/evaluation/automatic-summary.md)와 [확인 답변 후 반복 평가](../docs/evaluation/confirmed-summary.md)를 참고한다. 이전 단일 실행은 개발 기록으로 보존한다. 성능 수치는 준비한 로컬 사례에 한정하며 수동 방식 대비 시간 절감률은 측정하지 않았다.
