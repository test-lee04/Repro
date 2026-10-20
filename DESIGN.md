# Repro 코드 설계 v1

상태: 설계. 현재 실행 가능한 것은 장바구니 데모와 수동 기준 테스트뿐이다.
참고: 제공된 all_total_vue_student/doc/README.md 및 all_total_vue_student.zip의 server.py, agent/graph.py, agent/deps.py, agent/retry.py, agent/session_store.py, requirements.txt를 읽어 구조를 검토했다. 샘플을 실행하거나 코드를 복사하지 않았다.

## 1. 주제와 목표

버그 제보를 브라우저에서 재현하고, 증거와 실행 검증된 회귀 테스트를 제공하는 QA 보조 Agent.
사용자는 개발자·QA. 제보의 불완전성 때문에 재현 순서 탐색, 증거 정리, 테스트 작성을 따로 수행해야 하는 문제가 있다.
성공은 자동 코드 수정이 아니라, 수정 담당자에게 재현 가능한 증거와 테스트를 인계하는 것이다.

## 2. 정량 목표: 실측 전 가설

초기 목표이며 결과를 본 뒤 기준을 바꾸지 않는다. 최종 평가 전 평가 세트와 함께 고정한다.

|지표|목표|산식/해석|
|---|---|---|
|유효 회귀 테스트|알려진 버그 6개 중 4개 이상|버그 버전 관련 assertion 실패 AND 수정 버전 통과. 사례별 3회 중 2회 이상 성공해야 사례 성공|
|단발 생성 대비 개선|6개 중 성공 사례 1개 이상 증가|동일 사례·모델·초기 정보·최대 예산. 1/6은 약 16.7%p. 보장 수치가 아님|
|정상 동작 오탐|정상 사례 2개에서 0건|재현 확인이라고 잘못 보고한 실행 수 / 전체 정상 실행 수도 별도 공개|
|반복 안정성|각 성공 사례에서 3회 중 2회 이상 동일 성공|미세한 타이밍 차이에 의한 우연한 성공을 구분|
|자원 상한|실행 1회 120초, 도구 행동 20회, LLM 8회, 재계획 2회|초기 운영 상한. 모든 대기·검증을 포함. 제한 초과도 실패/중단으로 보고|

관찰 기반 Agent가 단발 생성보다 느리거나 비쌀 수 있다. 속도 개선을 무조건 목표로 잡지 않는다. 시간은 성공 사례 중앙값과 전체 완료·중단 시간 모두 공개한다. 사람의 작업 시간 감소는 별도 사용자 실험을 해야 주장한다.

## 3. 참고 프로젝트에서 적용/변경할 점

|참고 README의 구조|우리 설계|
|---|---|
|서버 lifespan의 의존성 생성|모델·저장소·브라우저 서비스 수명 관리|
|SupervisorDeps 주입|Planner, BrowserTools, ReplayRunner, RunStore 주입|
|fake/실모델 교체|개발용 결정론 모드와 실모델 모드 분리. fake 결과는 AI 성능에서 제외|
|v1/v2 전환|one_shot / agent 실행 모드로 같은 시나리오 비교|
|SSE 진행 이벤트|실행 단계·도구 결과 요약·증거 상태 표시. 내부 추론 원문은 표시하지 않음|
|재계획·검증·재시도|탐색 재계획과 일시적 도구 오류 재시도를 별도 관리|
|SQLite 세션|대화 전체 대신 실행, 단계 이벤트, 산출물, 지표 저장|
|다중 MCP·RAG·멀티 Agent|첫 MVP 필수 아님. 브라우저 도구 인터페이스부터 구현|

브라우저 프로세스는 서비스 수명 동안 관리하되, 매 실행은 새 context로 격리한다. 공유 페이지나 쿠폰 상태를 재사용하지 않는다. 초기 버전은 단일 worker·동시 실행 1개로 제한한다.

### 실제 샘플 코드 확인 후 반영하는 차이

- graph.py의 validate_node는 길이와 절차 번호를 검사하고 최대 3회 후 통과 처리한다. 우리 서비스에서는 최대 시도 도달을 검증 성공으로 바꾸지 않는다. 미검증/실패로 종료한다.
- replan_check_node는 런북 존재 여부 또는 라운드 상한으로 충분성을 결정한다. 우리 서비스의 탐색 예산 소진은 재현 성공이나 버그 없음의 증거가 아니다.
- retry.py는 asyncio.TimeoutError를 재시도한다. 브라우저의 클릭은 시간 초과 전에 이미 적용됐을 수 있으므로 무조건 반복하면 수량 증가나 쿠폰 토글이 중복될 수 있다. 조회는 제한 재시도하고, 상태 변경은 재관찰 후 적용 여부를 확인하거나 초기화 후 실행한다.
- research_node의 고정 sleep 및 의도적 저품질 초안은 학습용 패턴이다. 성능 평가에 인위적 지연·실패를 넣지 않는다. 오류 주입 실험은 별도 표시한다.
- 샘플 /chat/stream은 GET 연결에서 실행을 시작한다. 우리 서비스는 POST로 실행 생성, GET SSE로 저장 이벤트 조회를 분리해 재접속으로 중복 실행되는 일을 방지한다.
- session_store.py는 메시지 이력을 저장한다. 우리 저장소는 실행 상태와 단조 증가 event_id를 저장해 중단·재접속·평가 원시 결과를 구분한다.
- 공유 의존성 초기화와 개별 실행 격리를 구분하고 try/finally로 자원 정리를 보장한다.
- 참고 코드의 단가·모델명은 실측 기준으로 복사하지 않는다. 평가 실행 시 실제 설정과 사용량을 기록하고 확인된 단가로만 비용을 산정한다.

## 4. 계층과 예정 디렉토리

```text
app/
  main.py                 HTTP·lifespan·의존성 조립 (현재 존재)
  schemas.py              입력·출력·판정 enum
  api/runs.py             실행 생성·조회·추가 답변·이벤트
  agent/
    state.py              구조화 상태
    graph.py              노드·조건 분기
    nodes.py              parse/observe/plan/act/assess/export
    deps.py               서비스 인터페이스 주입
    planner.py            모델 응답 → 허용된 ActionPlan 검증
  browser/
    tools.py              관찰·클릭·입력·증거 수집
    session.py            context 생성·초기화·정리
  testing/
    compiler.py           ActionPlan → 고정 Playwright 코드 템플릿
    replay.py             동일 테스트 버그/수정 버전 실행
    verdict.py            assertion 실패·인프라 오류 분류
  storage.py              SQLite 실행/이벤트·산출물 메타데이터
  static/
    cart.html             실험 대상 (현재 존재)
    index.html            제보·진행·결과 UI
eval/
  public_cases.json       제보·공개 요구사항·초기 조건
  private_oracles.json    정답·버그/수정 환경 매핑. Agent 입력 금지
  run_eval.py             두 모드·반복·원시 결과 저장
  summarize.py            분모 포함 비교표
tests/
  cart_regression.py      수동 기준 테스트 (현재 존재)
  test_verdicts.py        인프라 오류가 재현 성공으로 분류되지 않는지
  test_compiler.py        동작 로그 기반 코드가 같은 행동을 보존하는지
```

## 5. Agent 흐름

parse → clarify 또는 observe → plan → act → assess

- clarify: 기대 동작·필수 입력 부족이면 waiting_input. 브라우저 context는 닫고 명세만 저장한다. 답변 후 초기 상태에서 다시 시작.
- observe: 사용자에게 보이는 요소의 role/name/값, 현재 URL, 텍스트를 수집. 소스코드·버전 토글·정답 데이터는 제공하지 않는다.
- plan: 관찰에 존재하는 요소 ID로 짧은 행동 계획 작성.
- act: 검증된 도구 호출 실행. 행동마다 사전/사후 관찰과 실제 실행 기록 저장.
- assess: 공개 요구사항을 바탕으로 실제/기대값 비교. 기대값은 관찰된 잘못된 값에서 역으로 만들지 않는다.
- 불충분하면 예산 내 observe/plan으로 복귀. 예산 소진이면 not_reproduced.
- reproduced이면 실제 성공 실행 기록 → compiler → replay → report.
- 테스트 보정은 locator·대기·행동 누락에 한정. 정상 기대값을 버그 출력에 맞춰 바꾸는 보정 금지.

전체 실행 상태: queued/running/waiting_input/completed/failed/cancelled.
재현 판정: pending/reproduced/not_reproduced/needs_information/environment_error.
테스트 판정: not_generated/generated/replay_failed/bug_detected/validated/environment_error.
completed는 작업이 끝났다는 뜻이지 버그가 재현됐다는 뜻이 아니다.

## 6. 데이터 계약

RunRequest: report, target_id, mode(one_shot/agent).
target_id는 등록된 demo_cart만 허용. 임의 URL 대신 서버 설정으로 목적지 결정.

BugSpec: summary, reported_steps, input_values, expected_behavior, expected_source, missing_information.
expected_source는 사용자 답변 또는 공개 서비스 명세의 문장/식별자. 기대값 없는 상태는 검증 완료로 승격 불가.

Observation: observation_id, visible_url, elements[{id, role, name, value}], visible_text, screenshot_artifact_id.
Action: kind(click/fill/press), observation_id, element_id, value.
ActionResult: action, success, error_type, before_observation_id, after_observation_id, elapsed_ms.
AssertionSpec: target, comparison, expected_value, expected_source.
AgentState: run_id, BugSpec, observations, action_results, assertions, reproduction_status, test_status, counters, deadline, errors.
RunResult: 두 판정, 요약, 실제 실행 순서, 기대/실제, 증거 ID, 테스트 ID, replay 결과, metrics, limitations.

중요: 관찰 이후 요소 ID가 바뀌면 재관찰한다. 생성된 locator 문자열을 무조건 실행하지 않는다.

## 7. 인터페이스

Planner.parse(report, public_spec) → BugSpec
Planner.next_actions(spec, observation, history, remaining_budget) → ActionPlan
BrowserTools.observe(session) → Observation
BrowserTools.execute(session, action) → ActionResult
TestCompiler.compile(successful_actions, assertions) → TestArtifact
ReplayRunner.run(test_artifact, configured_target) → ReplayResult
RunStore.append_event(run_id, event) / save_state(state) / get_result(run_id)

모델은 자유 Python 코드를 실행하지 않는다. 구조화된 행동을 만들고 검증된 템플릿으로 테스트를 생성한다. 이를 통해 실행 로그와 생성 코드의 추적 관계를 유지한다.

## 8. API 설계 (아직 미구현)

|API|입출력|
|---|---|
|POST /api/runs|RunRequest → 202 {run_id,status}|
|GET /api/runs/{id}|상태·요약·현재 지표|
|POST /api/runs/{id}/answers|대기 중인 질문 답변. 다른 상태는 409|
|POST /api/runs/{id}/cancel|중단 요청. 도구 타임아웃/반복 경계에서 반영|
|GET /api/runs/{id}/events|번호가 있는 SSE 이벤트. 재접속 시 이전 번호 이후 전송|
|GET /api/runs/{id}/result|종료된 결과. 미완료는 409|
|GET /api/runs/{id}/artifacts/{artifact_id}|저장소에 등록된 산출물만 조회|

POST 요청이 실행을 만들고 SSE GET은 관찰만 한다. 재접속이 새 실행을 만들면 안 된다. 중단·타임아웃 시 browser context와 자식 실행을 정리한다. 서버 재시작으로 중단된 running 실행은 environment_error로 표시하고 자동 재개를 보장하지 않는다.

## 9. 회귀 검증과 평가 격리

Agent는 버그 버전만 탐색한다. 검증 도구가 같은 테스트를 별도로 수정 버전에서 실행하며, 그 결과를 탐색 계획으로 돌려주지 않는다. 수정 환경은 평가용 fixture이며 실제 고객 서비스에서는 개발자의 수정 배포 후 동일 테스트를 재사용한다.

valid = bug_run.failed_assertion_is_relevant AND fixed_run.passed.
syntax_error, missing_element, navigation_error, timeout을 자동으로 버그 발견으로 간주하지 않는다. 관련성은 assertion ID와 공개 기대 조건을 대조하고 애매하면 사람 검토 대상으로 남긴다.

기준 방식에도 동일한 공개 명세·초기 관찰을 제공한다. 기준 방식은 계획 1회 후 실제 행동 로그로 코드 생성·같은 검증을 수행한다. Agent만 실행 피드백 기반 재계획을 허용한다. 양쪽 최대 예산은 동일하되 실제 사용량을 공개한다. 비교는 전체 적응형 흐름의 효과이며 재계획 하나의 순수 인과 효과라고 주장하지 않는다.

개발 6개/최종 12개는 제보 수다. 같은 결함을 바꿔 표현한 질문은 독립 결함 수로 세지 않는다. 결함 유형별·제보별 결과를 모두 보고한다. 최종 12개: 알려진 버그 6, 정보 부족 2, 정상 2, 환경/지원범위 2. 사례당 3회, 방식당 36회.

## 10. 구현 순서

1. 실행 환경·브라우저 설치 확인. 수동 기준 테스트가 버그에서 관련 assertion 실패, 정상에서 통과하는지 확인.
2. 도구·Observation·Action·상태 계약 구현. 가짜 Planner로 한 흐름 연결.
3. 실제 모델 파서·Planner 연결, 관찰/재계획 예산 적용.
4. 행동 로그 기반 테스트 compiler·replay·판정 구현.
5. Vue 실행 화면·API·SQLite 이벤트 연결. MVP에서는 FastAPI가 화면까지 제공해 별도 프록시 생략.
6. 추가 질문·취소·timeout·환경 오류 검증.
7. 고정 평가·비교표·README·5분 시연.

Day 1: 1~2, Day 2: 3~5, Day 3 오전: 6~7. Day 3 13:00 제출. 일정이 부족하면 추가 시나리오보다 하나의 전체 흐름을 우선한다.

## 11. 결정이 필요한 실행 설정

실모델 backend와 계정, 모델 식별자, 호출 예산을 구현 시 설정한다. 키는 환경변수로 받고 문서·로그·저장소에 기록하지 않는다. 이 설계는 특정 모델이나 유료 호출 실행을 전제로 성능을 보장하지 않는다.

## 공식 기술 참고

- https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/graph-api.mdx
- https://playwright.dev/python/docs/test-assertions
