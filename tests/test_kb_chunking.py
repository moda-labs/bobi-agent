"""Unit tests for the KB text chunking algorithm."""

from bobi.kb.store import _chunk_text


class TestChunkText:
    def test_empty_string(self):
        assert _chunk_text("") == []

    def test_whitespace_only(self):
        assert _chunk_text("   \n\n\t  ") == []

    def test_short_text_single_chunk(self):
        text = "This is a short paragraph."
        chunks = _chunk_text(text)
        assert chunks == ["This is a short paragraph."]

    def test_two_paragraphs(self):
        text = "First paragraph.\n\nSecond paragraph."
        chunks = _chunk_text(text, min_chars=0)
        assert chunks == ["First paragraph.", "Second paragraph."]

    def test_multiple_blank_lines_between_paragraphs(self):
        text = "First.\n\n\n\nSecond.\n\n\n\n\nThird."
        chunks = _chunk_text(text, min_chars=0)
        assert chunks == ["First.", "Second.", "Third."]

    def test_long_paragraph_splits_on_sentences(self):
        sentences = [f"Sentence number {i} is here." for i in range(50)]
        text = " ".join(sentences)
        chunks = _chunk_text(text, max_chars=200)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 200 + 50  # some tolerance for sentence boundary

    def test_tiny_paragraphs_merged(self):
        text = "A.\n\nB.\n\nC."
        chunks = _chunk_text(text, min_chars=100)
        assert len(chunks) < 3
        assert "A." in chunks[0]
        assert "B." in chunks[0] or "B." in chunks[-1]

    def test_single_very_long_sentence(self):
        text = "word " * 1000
        chunks = _chunk_text(text, max_chars=200)
        assert len(chunks) >= 1
        assert all(c.strip() for c in chunks)

    def test_preserves_content(self):
        text = "First paragraph with details.\n\nSecond paragraph with more info."
        chunks = _chunk_text(text)
        combined = " ".join(chunks)
        assert "First paragraph" in combined
        assert "Second paragraph" in combined

    def test_strips_whitespace(self):
        text = "  Hello world.  \n\n  Goodbye world.  "
        chunks = _chunk_text(text)
        for chunk in chunks:
            assert chunk == chunk.strip()

    def test_mixed_sizes(self):
        short = "Short."
        long = " ".join([f"Sentence {i}." for i in range(40)])
        text = f"{short}\n\n{long}\n\nAnother short."
        chunks = _chunk_text(text, max_chars=300, min_chars=50)
        assert len(chunks) >= 2

    def test_respects_max_chars(self):
        text = "\n\n".join([f"Paragraph {i}. " + "x" * 150 for i in range(10)])
        chunks = _chunk_text(text, max_chars=300)
        for chunk in chunks:
            assert len(chunk) <= 300 + 100  # tolerance for sentence rounding

    def test_no_empty_chunks(self):
        text = "A.\n\n\n\nB.\n\n\n\n\n\nC.\n\n"
        chunks = _chunk_text(text)
        for chunk in chunks:
            assert chunk.strip()
