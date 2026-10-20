# Repro

> Skeleton v0.1 — 구조와 인터페이스 설계 단계입니다. Agent 기능·성능 평가는 미구현입니다.

자연어 버그 제보를 브라우저에서 재현하고, 증거와 회귀 테스트를 제공하여 개발자의 수정을 돕는 QA 보조 Agent 프로젝트입니다. 소스코드 자동 수정은 범위에 포함하지 않습니다.

## Pain point

불완전한 버그 제보로 인해 개발자·QA는 재현 조건을 찾고, 증거를 수집하고, 회귀 테스트를 따로 작성해야 합니다. 이를 관찰 기반 실행 흐름으로 연결하는 것이 목표입니다.

## 현재 구현 상태

- 장바구니 데모의 버그/정상 버전과 초기화 기능.
- FastAPI 웹 서버 및 상태 확인 경로.
- 정상 기대값을 검증하는 수동 작성 기준 테스트.
- Agent·브라우저·테스트 생성·저장소 인터페이스 및 실행 API 골격.
- `/` 안내 화면과 `/docs` API 문서. 미구현 실행 API는 HTTP 501 반환.
- 아직 미구현: 자연어 해석, Agent 탐색·재계획, 테스트 자동 생성, 결과 화면, 실측 평가.
- 기준 테스트는 평가용이며 Agent에 정답이나 테스트 코드를 제공하지 않습니다.

## 실행

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python -m uvicorn app.main:app --reload
```

브라우저에서 `http://127.0.0.1:8000/demo?version=buggy` 또는 `http://127.0.0.1:8000/demo?version=fixed`에 접속합니다.

## 기준 테스트

서버 실행 후:

```sh
python tests/cart_regression.py --version buggy
python tests/cart_regression.py --version fixed
```

버그 버전에서는 수량 변경 후 27000원이어야 한다는 assertion이 실패하고 정상 버전에서는 통과해야 합니다. 접속 실패나 브라우저 설치 실패는 버그 재현으로 계산하지 않습니다.

## 평가 및 일정

[프로젝트 계획](PROJECT_PLAN.md)을 참고하세요. 버그 재현 여부와 테스트 유효성을 분리하고, 단발 생성 기준 방식과 관찰·재계획 Agent를 동일 사례로 비교할 예정입니다. 측정 성과는 아직 없습니다.

## 코드 설계

[코드 설계 v1](DESIGN.md)에 정량 목표, 참고 샘플과의 차이, 예정 모듈, Agent 상태 흐름, 데이터 계약, API, 평가 격리 및 개발 순서를 정리했습니다. 이 문서의 예정 API와 모듈은 아직 구현되지 않았습니다.

## 디렉토리

```text
app/
  main.py          서버·화면·API 연결
  schemas.py       요청 형식
  api/             실행 API 골격
  agent/           상태·의존성·계획·노드·그래프 골격
  browser/         브라우저 관찰·조작·격리 인터페이스
  testing/         테스트 생성·재실행 인터페이스와 판정 상태
  storage.py       실행 저장소 인터페이스
  static/          스켈레톤 안내 화면·장바구니 데모
eval/              빈 평가 데이터·평가 실행 골격
tests/             수동 기준 회귀 테스트
artifacts/         실행 증거·테스트 산출물 (Git 제외)
```

Protocol은 구현체가 지켜야 할 인터페이스입니다. NotImplementedError와 HTTP 501은 미구현 상태를 명시합니다. `.env.example`의 실행 제한은 설정 예시이며 아직 적용되지 않습니다.
