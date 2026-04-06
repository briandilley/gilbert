"""Text chunking — split extracted text into overlapping chunks for embedding."""

import re

from gilbert.interfaces.knowledge import DocumentChunk

# Sentence boundary pattern
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# Page marker pattern from PDF extraction
_PAGE_RE = re.compile(r"\[Page (\d+)\]")


def chunk_text(
    text: str,
    document_id: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> list[DocumentChunk]:
    """Split extracted text into overlapping chunks for embedding.

    Strategy:
    1. Split on paragraph boundaries (double newlines)
    2. Accumulate paragraphs into chunks up to chunk_size
    3. Overlap of chunk_overlap characters between adjacent chunks
    4. Sub-split long paragraphs on sentence boundaries
    5. Track page numbers from PDF markers
    """
    if not text.strip():
        return []

    paragraphs = text.split("\n\n")
    chunks: list[DocumentChunk] = []
    current_text = ""
    current_offset = 0
    chunk_index = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If adding this paragraph exceeds chunk_size, emit current chunk
        if current_text and len(current_text) + len(para) + 2 > chunk_size:
            chunks.append(_make_chunk(
                document_id, chunk_index, current_text, current_offset, text,
            ))
            chunk_index += 1

            # Start new chunk with overlap from the end of the previous
            overlap_start = max(0, len(current_text) - chunk_overlap)
            overlap_text = current_text[overlap_start:]
            current_offset = current_offset + overlap_start
            current_text = overlap_text

        # If a single paragraph exceeds chunk_size, split on sentences
        if len(para) > chunk_size:
            sentences = _SENTENCE_RE.split(para)
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                if current_text and len(current_text) + len(sentence) + 1 > chunk_size:
                    chunks.append(_make_chunk(
                        document_id, chunk_index, current_text, current_offset, text,
                    ))
                    chunk_index += 1
                    overlap_start = max(0, len(current_text) - chunk_overlap)
                    overlap_text = current_text[overlap_start:]
                    current_offset = current_offset + overlap_start
                    current_text = overlap_text

                if current_text:
                    current_text += " " + sentence
                else:
                    current_text = sentence
        else:
            if current_text:
                current_text += "\n\n" + para
            else:
                current_text = para

    # Emit final chunk
    if current_text.strip():
        chunks.append(_make_chunk(
            document_id, chunk_index, current_text, current_offset, text,
        ))

    return chunks


def _make_chunk(
    document_id: str,
    chunk_index: int,
    text: str,
    start_offset: int,
    full_text: str,
) -> DocumentChunk:
    """Create a DocumentChunk with page number detection."""
    # Find the most recent page marker before this chunk's position
    page_number = _detect_page_number(full_text, start_offset)

    return DocumentChunk(
        document_id=document_id,
        chunk_index=chunk_index,
        text=text.strip(),
        start_offset=start_offset,
        end_offset=start_offset + len(text),
        page_number=page_number,
    )


def _detect_page_number(full_text: str, offset: int) -> int | None:
    """Find the most recent page marker before the given offset."""
    text_before = full_text[:offset]
    matches = list(_PAGE_RE.finditer(text_before))
    if matches:
        return int(matches[-1].group(1))
    # Check if there's a page marker anywhere (i.e., this is a PDF)
    if _PAGE_RE.search(full_text):
        return 1  # Before first marker = page 1
    return None
