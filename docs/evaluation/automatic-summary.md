# Repro 평가 결과

평가 방식: 추가 답변 없는 실행
평가 상태: completed / 실제 시도: 14 / 계획: 14
자료 버전: r8-1-v1
자료 식별값: 5953d37085a45786d2a8a68c56c5e9cec02b1a47baab0c8d5b59a5803e28adff
앱 코드 식별값: 52ba8739045e3e18c7e18be2ff621a6cb05c56cf86ef6801cb8894b8d885ed25

오류·보류도 시도 수에 포함합니다. 이 결과는 아래 평가 자료에 한정됩니다.

| 지표 | 실측 결과 |
| --- | --- |
| 정답 판정 일치 | 10/14 (71.43%) |
| 버그 환경 탐지 | 4/6 (66.67%) |
| 정상 환경 미재현 확인 | 4/6 (66.67%) |
| 정상 환경 오탐 | 0/6 (0.0%) |
| 버그·정상 판정 완료 | 8/12 (66.67%) |
| 정보 부족 질문 요청 | 2/2 (100.0%) |
| 회귀 테스트 생성 | 8/12 (66.67%) |
| 수정 검증 완료 | 4/4 (100.0%) |

평균 전체 소요 시간: 72.25초 / 중앙값: 70.04초
실제 모델 호출: 102회 / 호출 오류: 0회
응답에서 확인한 모델: qwen3:8b

입력 토큰: 217795 / 확인된 소계: 217795 / 사용량 제공 호출: 102/102
출력 토큰: 17218 / 확인된 소계: 17218 / 사용량 제공 호출: 102/102

## 개별 결과

| 사례 | 환경 | 회차 | 정답 | 실제 판정 | 전체 시간 | 모델 호출 |
| --- | --- | --- | --- | --- | --- | --- |
| note | buggy | 1 | reproduced | reproduced | 122.67 | 11 |
| note | fixed | 1 | not_reproduced | not_reproduced | 104.78 | 11 |
| search | buggy | 1 | reproduced | reproduced | 79.95 | 9 |
| search | fixed | 1 | not_reproduced | not_reproduced | 72.13 | 9 |
| validation | buggy | 1 | reproduced | needs_information | 62.64 | 5 |
| validation | fixed | 1 | not_reproduced | needs_information | 57.96 | 5 |
| unclear | buggy | 1 | needs_information | needs_information | 13.84 | 1 |
| note | buggy | 2 | reproduced | reproduced | 113.1 | 11 |
| note | fixed | 2 | not_reproduced | not_reproduced | 104.77 | 11 |
| search | buggy | 2 | reproduced | reproduced | 77.93 | 9 |
| search | fixed | 2 | not_reproduced | not_reproduced | 67.95 | 9 |
| validation | buggy | 2 | reproduced | needs_information | 63.16 | 5 |
| validation | fixed | 2 | not_reproduced | needs_information | 57.47 | 5 |
| unclear | buggy | 2 | needs_information | needs_information | 13.13 | 1 |

## 해석 범위

유한한 로컬 검사 자료의 결과이며 임의의 모든 웹사이트에 대한 성공률이 아닙니다.
수정 검증은 선택 실행입니다. 실행하지 않은 경우 미측정으로 표시합니다.
전체 시간에는 브라우저 준비와 정리를 포함하고 선택한 재실행 검증 시간은 별도로 기록합니다.
수동 작업이나 다른 실행 방식과의 비교 측정은 아직 수행하지 않았습니다. 시간 절감률은 주장하지 않습니다.
실패 원인과 전체 실행 기록은 evaluation.json과 각 artifact_dir에서 확인합니다.
