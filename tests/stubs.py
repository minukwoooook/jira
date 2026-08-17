"""리포지토리 함수를 기록용 스텁으로 바꿔치기한다.

DB가 없으므로 "무엇이 저장됐는가"는 검증할 수 없다. "무엇을 어떤 순서로
어떤 인자로 호출했는가"까지가 사외 검증의 한계다 (spec §11.3).
"""
from dataclasses import dataclass, field
from typing import Any


class Sentinel:
    """conn 자리에 넣는 더미. 실수로 SQL을 실행하면 AttributeError로 터진다."""

    def __repr__(self) -> str:
        return "<no-db>"


CONN = Sentinel()


@dataclass
class Recorder:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    returns: dict[str, Any] = field(default_factory=dict)

    def stub(self, name: str):
        """monkeypatch.setattr(module, name, recorder.stub(name)) 로 쓴다."""
        def _fn(*args, **kwargs):
            self.calls.append((name, {"args": args[1:], "kwargs": kwargs}))
            value = self.returns.get(name)
            return value(*args, **kwargs) if callable(value) else value
        return _fn

    def patch(self, monkeypatch, module, *names) -> None:
        for name in names:
            monkeypatch.setattr(module, name, self.stub(name))

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def args_of(self, name: str) -> list[dict]:
        return [payload for n, payload in self.calls if n == name]

    def first(self, name: str) -> dict:
        return self.args_of(name)[0]

    def order_of(self, *names: str) -> list[int]:
        """지정한 이름들이 처음 등록된 위치. 순서 검증에 쓴다."""
        seen = self.names()
        return [seen.index(n) for n in names]
