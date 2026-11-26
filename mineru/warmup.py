"""Run MinerU OCR once at build time.

PaddleOCR downloads its weights to ~/.paddleocr on first use, so this run
stores them in the image. It also fails the build if MinerU cannot process
a scanned PDF.

Usage: python warmup.py <scanned.pdf>
"""

import sys
import time

from magic_pdf.config.enums import SupportedPdfParseMethod
from magic_pdf.data.data_reader_writer import FileBasedDataWriter
from magic_pdf.data.dataset import PymuDocDataset
from magic_pdf.model.doc_analyze_by_custom_model import doc_analyze

MIN_CHARS = 200


def main(pdf_path):
    start = time.time()
    with open(pdf_path, "rb") as f:
        dataset = PymuDocDataset(f.read())
    if dataset.classify() != SupportedPdfParseMethod.OCR:
        sys.exit(f"{pdf_path} is not classified as scanned, so OCR was not tested")

    result = dataset.apply(doc_analyze, ocr=True).pipe_ocr_mode(
        FileBasedDataWriter("/tmp/mineru-warmup-images")
    )
    markdown = result.get_markdown("images")
    print(f"MinerU OCR extracted {len(markdown)} characters in {time.time() - start:.0f}s")
    print(markdown[:300])
    if len(markdown.strip()) < MIN_CHARS:
        sys.exit("MinerU OCR returned almost no text")


if __name__ == "__main__":
    main(sys.argv[1])
