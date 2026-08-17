import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from jira_dashboard.db.repository import history as history_repo
from jira_dashboard.jira.models import MAX_VAL_STR_BYTES, SENTINEL, ChangelogItem
from jira_dashboard.jira.parser import truncate

log = logging.getLogger(__name__)

STATUS_CATEGORY_FIELD = "status_category"


@dataclass(frozen=True)
class Interval:
    field_id: str
    valid_from: datetime
    valid_to: datetime
    val_str: str | None
    val_id: str | None


def build_intervals(
    created_at: datetime,
    current_values: Mapping[str, tuple[str | None, str | None]],
    changes: Sequence[ChangelogItem],
    tracked_field_ids: set[str],
) -> list[Interval]:
    """이슈 하나 안에서 완결된다. spec §5.3의 경계 조건을 전부 여기서 처리한다.

    정렬은 여기서만 결정한다 (Correction 1) — DC 10.3 참고문서는 changelog 도착
    순서에 대해 침묵하고, Cloud는 한때 expand=changelog를 내림차순으로 바꿨으며,
    Atlassian 자체 지원 가이드도 도착 순서가 아니라 created 타임스탬프로 정렬하라고
    한다. 그래서 parse_changelog는 의도적으로 정렬하지 않고, 여기 sorted(...)가
    유일한 권위다.
    """
    by_field: dict[str, list[ChangelogItem]] = defaultdict(list)
    for c in changes:
        if c.field_id and c.field_id in tracked_field_ids:
            by_field[c.field_id].append(c)

    fields = sorted((set(tracked_field_ids) & set(current_values)) | set(by_field))
    out: list[Interval] = []

    for field_id in fields:
        current_str, current_id = current_values.get(field_id, (None, None))
        items = sorted(by_field.get(field_id, []),
                       key=lambda c: (c.changed_at, c.item_seq))

        if not items:
            out.append(Interval(field_id, created_at, SENTINEL,
                                truncate(current_str, MAX_VAL_STR_BYTES), current_id))
            continue

        # (시각, 값, 값id) 경계 목록. 첫 구간의 값은 첫 변경의 from_str이다.
        boundaries: list[tuple[datetime, str | None, str | None]] = [
            (created_at, items[0].from_str, items[0].from_id)
        ]
        for item in items:
            stamp = max(item.changed_at, created_at)   # 생성보다 이른 변경은 clamp
            if boundaries[-1][0] == stamp:
                boundaries[-1] = (stamp, item.to_str, item.to_id)  # 길이 0 구간 제거
            else:
                boundaries.append((stamp, item.to_str, item.to_id))

        # 이력 종점 != 현재값이면 이력이 유실된 것이다. 현재값을 신뢰한다.
        if boundaries[-1][1] != current_str:
            log.warning(
                "history endpoint mismatch on %s: history=%r current=%r",
                field_id, boundaries[-1][1], current_str,
            )
            boundaries[-1] = (boundaries[-1][0], current_str, current_id)

        for idx, (start, val_str, val_id) in enumerate(boundaries):
            end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else SENTINEL
            if start < end:
                out.append(Interval(field_id, start, end,
                                    truncate(val_str, MAX_VAL_STR_BYTES), val_id))
    return out


def merge_categories(
    status_intervals: Sequence[Interval],
    category_of: Mapping[str, str],
) -> list[Interval]:
    """상태명 구간 → 카테고리 구간. 연속한 같은 카테고리는 하나로 합친다.

    이게 없으면 인스턴스마다 상태명이 달라 과거 시점 교차 분석이 불가능하다 (spec §6.6).
    """
    ordered = sorted(
        (i for i in status_intervals if i.field_id == "status"),
        key=lambda i: i.valid_from,
    )
    merged: list[Interval] = []
    for interval in ordered:
        category = category_of.get(interval.val_str or "", "undefined")
        if merged and merged[-1].val_str == category:
            merged[-1] = Interval(
                STATUS_CATEGORY_FIELD, merged[-1].valid_from,
                interval.valid_to, category, None,
            )
        else:
            merged.append(Interval(
                STATUS_CATEGORY_FIELD, interval.valid_from,
                interval.valid_to, category, None,
            ))
    return merged


def _tracked_fields(conn, instance_id: int) -> dict[str, int]:
    """is_dimension='Y'인 필드만 추적한다. summary 이력까지 담으면 테이블만 부푼다."""
    return history_repo.dimension_field_pks(conn, instance_id)


def derive_history(conn, instance_id: int, issue_ids: list[int],
                   *, category_of: Mapping[str, str] | None = None,
                   batch: int = 1000) -> int:
    """변경된 이슈만 DELETE 후 재생성. 이슈 단위로 닫혀 있어 중간 커밋이 안전하다.

    category_of가 없으면 DB에 이미 적재된 이슈에서 상태명→카테고리 대응을 뽑는다
    (Correction 3). 그 대응은 "현재 어떤 이슈가 쓰고 있는 상태"만 담으므로, 워크플로우
    에서 이미 빠진 상태는 여기 없다 — Task 10의 러너는 /rest/api/2/status에서 뽑은,
    인스턴스에 정의된 모든 상태를 담은 맵을 넘겨 이 문제를 피한다.
    """
    if not issue_ids:
        return 0
    field_pks = _tracked_fields(conn, instance_id)
    tracked = set(field_pks)
    if category_of is None:
        category_of = history_repo.status_category_map(conn, instance_id)
    written = 0

    for start in range(0, len(issue_ids), batch):
        chunk = issue_ids[start:start + batch]
        states = history_repo.load_issue_states(conn, chunk)
        changes = history_repo.load_changes(conn, chunk)
        for issue_id in chunk:
            state = states.get(issue_id)
            if state is None:
                continue
            intervals = build_intervals(
                state["created_at"], state["current_values"],
                changes.get(issue_id, []), tracked,
            )
            intervals = intervals + merge_categories(intervals, category_of)
            rows = [
                {"issue_id": issue_id, "field_pk": field_pks[i.field_id],
                 "valid_from": i.valid_from, "valid_to": i.valid_to,
                 "val_str": i.val_str, "val_id": i.val_id}
                for i in intervals if i.field_id in field_pks
            ]
            history_repo.replace_history(conn, issue_id, rows)
            written += len(rows)
        conn.commit()
    return written


def update_first_done_at(conn, issue_ids: list[int], *, batch: int = 1000) -> int:
    """status_category 구간에서 'done'인 첫 구간의 valid_from. 재오픈 이슈는 MIN 유지."""
    if not issue_ids:
        return 0
    total = 0
    for start in range(0, len(issue_ids), batch):
        total += history_repo.update_first_done_at(conn, issue_ids[start:start + batch])
        conn.commit()
    return total
