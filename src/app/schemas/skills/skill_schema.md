# Skill 작성 스키마

이 문서는 `app/workflow/skills/**/*.md`에 저장하는 Skill 문서의 작성 계약이다.
완성된 예시는 `app/workflow/skills/modeling/predictive_modeling.md`를 참고한다.
기계가 사용하는 정규화 스키마는 같은 디렉터리의 `skill_schema.json`에 정의되어 있다.

## 필수 작성 규칙

1. 파일은 UTF-8 Markdown으로 작성한다.
2. frontmatter에는 `name`과 `description`만 작성한다.
3. 아래 7개 섹션을 이름과 순서를 바꾸지 않고 모두 작성한다.
4. 일반 항목은 `- `로 시작하는 목록으로 작성한다.
5. 이전 Skill이 없으면 `typical_previous_skill`에 `- 없음`을 작성한다.
6. Tool 실행 방식은 `항상 실행` 또는 `조건부 실행`만 사용한다.
7. Tool 이름은 한 Skill 안에서 중복될 수 없다.
8. `Contract source`, `Workflow generation rules`, `argument_rationale`, `skipped_tools`는 Skill 문서에 작성하지 않는다.

## 작성 양식

```markdown
---
name: snake_case_skill_name
description: 이 Skill이 해결하는 문제와 구성 판단을 한 문장으로 설명한다.
---

# 사람이 읽을 Skill 제목

## capabilities

- 이 Skill이 수행할 수 있는 일

## limitations

- 이 Skill이 수행하지 못하는 일 또는 적용 범위

## when_to_use

- 이 Skill을 선택해야 하는 조건

## not_for

- 이 Skill을 선택하면 안 되는 조건

## typical_previous_skill

- `previous_skill_name`

## typical_next_skills

- `next_skill_name`

## Tool list

| Tool | 실행 방식 | 실행 조건 | 역할 |
|---|---|---|---|
| `tool_name` | 항상 실행 | Tool을 실행할 수 있는 조건 | Tool이 담당하는 역할 |
| `optional_tool_name` | 조건부 실행 | 이 Tool이 필요한 경우 | Tool이 담당하는 역할 |

```'

## condition_tool 작성 규칙

- `condition_tool`은 해당 Tool의 실행 여부를 판단하는 근거 Tool이다.
- 항상 실행 Tool의 `condition_tool`은 `없음`으로 작성한다.
- 조건부 실행 Tool도 사용자 의도나 확정 입력만으로 선택 여부를 판단할 수 있으면 `없음`으로 작성한다.
- 같은 Skill의 선행 Tool 결과를 보고 판단해야 하면 앞선 Tool 이름을 작성한다. 예: `compute_statistics`
- 이전 Skill의 Tool 결과를 보고 판단해야 하면 qualified name을 작성한다. 예: `data_quality_check.compute_statistics`
- 내부 `condition_tool`은 반드시 같은 Skill의 Tool list에서 더 위에 작성된 Tool이어야 한다.
- 외부 `condition_tool`은 현재 Skill 또는 `typical_previous_skill`에 있는 Skill을 참조해야 한다.
'
