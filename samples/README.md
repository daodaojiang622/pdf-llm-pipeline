# Test PDFs

These files exercise the extraction and summary steps.

| File | Source | Expected result |
| --- | --- | --- |
| `irs-w9/fw9.pdf` | IRS Form W-9, https://www.irs.gov/pub/irs-pdf/fw9.pdf (6 pages, US government work) | Text extraction of a form layout, then `document.summarized` |
| `generated/scanned-fw9-p1-3.pdf` | Pages 1–3 of the W-9 as rotated grayscale images, with no text layer | Plain text extraction finds nothing. Only OCR can read it. |
| `generated/encrypted.pdf` | Page 1 of the W-9, password `test-password` | `document.failed` |
| `generated/not-a-pdf.pdf` | Plain text renamed to `.pdf` | `document.failed` |
| `generated/empty.pdf` | Zero bytes | `document.failed` |
| `arxiv-attention/attention-is-all-you-need.pdf` | https://arxiv.org/pdf/1706.03762 (15 pages). Not committed. | Text extraction with formulas and tables, then `document.summarized` |

The arXiv paper is not in the repository, because its arXiv license does not allow redistribution. Download it with:

```bash
mkdir -p samples/arxiv-attention
curl -fL -o samples/arxiv-attention/attention-is-all-you-need.pdf https://arxiv.org/pdf/1706.03762
```
