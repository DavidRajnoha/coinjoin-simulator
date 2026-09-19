import pytest

from manager.engine.joinmarket_engine import JoinmarketEngine


class RecordingNode:
    def __init__(self, height: int) -> None:
        self.height = height
        self.mined = 0

    def mine_block(self, count: int = 1) -> None:
        self.mined += count
        self.height += count

    def get_block_count(self) -> int:
        return self.height


def harness(node: RecordingNode | None) -> JoinmarketEngine:
    engine = object.__new__(JoinmarketEngine)
    engine.node = node
    return engine


def test_the_baseline_is_taken_after_the_warm_up_blocks() -> None:
    node = RecordingNode(height=120)
    engine = harness(node)

    baseline = engine.start_block_clock()

    assert node.mined == JoinmarketEngine.WARMUP_BLOCKS
    assert baseline == node.get_block_count()
    assert node.get_block_count() - baseline == 0


def test_an_unreachable_node_falls_back_to_a_zero_baseline() -> None:
    node = RecordingNode(height=120)
    node.get_block_count = lambda: (_ for _ in ()).throw(ConnectionError("rpc down"))  # type: ignore[method-assign]
    engine = harness(node)

    assert engine.start_block_clock() == 0
    assert node.mined == JoinmarketEngine.WARMUP_BLOCKS


def test_a_missing_node_is_refused() -> None:
    with pytest.raises(RuntimeError):
        harness(None).start_block_clock()
