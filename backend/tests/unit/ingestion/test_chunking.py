from __future__ import annotations

from policy_api.ingestion.chunking import chunk_blocks
from policy_api.ingestion.types import ParsedBlock


def block(text: str, location: str = "paragraph:1") -> ParsedBlock:
    return ParsedBlock(text=text, page=2, heading_path=("报销制度",), location=location, block_type="paragraph")


def test_chunker_keeps_small_structural_blocks_and_source_metadata() -> None:
    chunks = chunk_blocks([block("第一条 申请人不得事后补签。"), block("第二条 审批后报销。", "paragraph:2")], max_chars=100)
    assert [chunk.text for chunk in chunks] == ["第一条 申请人不得事后补签。", "第二条 审批后报销。"]
    assert chunks[0].page == 2 and chunks[0].heading_path == ("报销制度",)
    assert chunks[1].location == "paragraph:2"


def test_chunker_splits_long_blocks_at_sentence_boundaries_without_losing_policy_facts() -> None:
    text = "第3.2条 差旅费不得超过500元。2026年8月1日起执行。超额部分不予报销。"
    chunks = chunk_blocks([block(text)], max_chars=24)
    reconstructed = "".join(chunk.text for chunk in chunks)
    assert reconstructed == text
    assert "500元" in reconstructed and "2026年8月1日" in reconstructed
    assert "第3.2条" in reconstructed and "不得" in reconstructed and "不予" in reconstructed
    assert all(chunk.text for chunk in chunks)


def test_chunker_is_deterministic_and_does_not_duplicate_full_text() -> None:
    blocks = [block("第一句。第二句。第三句。")]
    first = chunk_blocks(blocks, max_chars=8)
    second = chunk_blocks(blocks, max_chars=8)
    assert first == second
    assert [chunk.chunk_key for chunk in first] == [chunk.chunk_key for chunk in second]
    assert "".join(chunk.text for chunk in first) == blocks[0].text
