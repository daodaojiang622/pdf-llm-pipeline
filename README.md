# PDF Processor Application

This application allows users to upload PDF documents, extract text using OCR, and generate summaries using an LLM (Google Gemini by default, or xAI Grok).

## Features

- PDF document upload
- Text extraction using OCR (Magic_pdf / MinerU)
- Text summarization using Google Gemini or xAI Grok, chosen with `LLM_PROVIDER`
- Simple web interface for uploading and viewing results
- Asynchronous processing with a message queue system
- Amazon S3 storage for documents, extracted text, and summaries
- Docker support for easy deployment
- DynamoDB for storing metadata and processing status
- Amazon SQS for message queuing
- AWS Lambda for serverless processing
- HMAC-signed webhook notifications with automatic retries

## Project Structure

```plaintext
PDF-PROCESSOR
├── README.md                # Project documentation
├── Dockerfile             # Dockerfile for containerization
├── requirements.txt          # Python dependencies
├── deploy.sh                 # Deployment script
├── lambda_handler.py          # AWS Lambda function handler
├── template.yml             # AWS SAM template for Lambda deployment
├── update_templates.py       # Script to update HTML templates
├── update_static_to_s3.py    # Script to update static files to S3
├── mineru/                   # MinerU config and build-time model download
├── pdf-processor/
   ├── __init__.py
   ├── app.py                 # Main application file
   ├── config.py              # Configuration settings
   ├── static/
   │   ├── css/
   │   │   └── style.css      # CSS styles for the web interface
   │   └── js/
   │       └── main.js        # JavaScript for the web interface
   ├── templates/
   │   ├── index.html         # HTML template for the upload page
   │   └── results.html       # HTML template for displaying results
   ├── services/
   │   ├── __init__.py
   │   ├── ocr_service.py     # OCR service for text extraction
   │   ├── summary_service.py # Summary service for text summarization
   │   ├── storage_service.py # Storage service for file handling
   │   ├── message_service.py # Message service for queue handling
   │   └── webhook_dispatcher.py # Signed webhook delivery
   ├── detectron2/           # Detectron2 models and configurations

```

## Installation

1. Install AWS SAM CLI and configure your AWS credentials:
```
pip install aws-sam-cli
aws configure
```

2. Execute the deployment script to set up the AWS resources:
```
chmod +x deploy.sh
./deploy.sh
```

## Text extraction

MinerU (`magic-pdf` 1.2.2) extracts text, and runs OCR on scanned pages. If MinerU fails, PyMuPDF reads the embedded text layer instead. A job fails when neither returns usable text.

The Docker build downloads the models into `/opt/mineru`, because Lambda can only write to `/tmp`. Model revisions are pinned in [mineru/download_models.py](mineru/download_models.py), and the configuration is in [mineru/magic-pdf.json](mineru/magic-pdf.json). The build also runs OCR on a scanned sample, and fails if no text comes back.

Formula recognition is slow on CPU, so it is off by default. Set `MINERU_FORMULA_ENABLE=true` to turn it on. Table recognition is disabled, and its models are not in the image.

Outside the image, MinerU has no configuration, so local runs use the PyMuPDF fallback.

## Webhooks

The application can send a `POST` request when a document finishes processing.

Set `WEBHOOK_SECRET` to enable delivery. Choose the destination in one of these ways:

- Lambda API: send an `X-Webhook-Url` header with `POST /upload`.
- Flask app: include a `webhookUrl` form field with the upload.
- Fallback: set `DEFAULT_WEBHOOK_URL`.

URLs must use HTTPS and resolve to a public address. Set `WEBHOOK_ALLOW_PRIVATE_URLS=true` only for local development.

### Payload

```json
{
  "eventId": "2f1c7f0e-7c55-5a26-a7f6-0b6f4f9d6f10",
  "eventType": "document.summarized",
  "documentId": "b7c9f8f2-1f3e-4f55-9f7a-2a8c6f1f0d11",
  "completedAt": "2026-01-15T12:30:45.123Z",
  "summaryUrl": "https://example-bucket.s3.amazonaws.com/summary.txt?X-Amz-Signature=..."
}
```

`eventType` is `document.summarized` or `document.failed`. Failed events contain `error` instead of `summaryUrl`. `summaryUrl` is a presigned S3 link that expires after one hour, and it is omitted for local files.

### Headers and verification

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-Webhook-Signature` | `t=<unix_seconds>,v1=<hex_hmac_sha256>` |
| `X-Idempotency-Key` | Same value as `eventId` |
| `X-Webhook-Event` | Same value as `eventType` |

The signature is an HMAC-SHA256 of `<t>.<raw_request_body>`. Verify the raw bytes before parsing the JSON, and reject timestamps more than 300 seconds from the current time:

```python
from services.webhook_dispatcher import verify_webhook_signature

is_valid = verify_webhook_signature(raw_body, signature_header, webhook_secret)
```

Use `X-Idempotency-Key` to ignore duplicates. It stays the same across retries of a document and event type.

### Retry policy

Each request times out after 5 seconds. Delivery makes up to 5 attempts (1 initial attempt and 4 retries), waiting a random time between 0 and `1000 * 2^attempt` ms between attempts.

Network errors, timeouts, and HTTP `408`, `429`, and `5xx` are retried. Other non-2xx responses, including redirects, fail immediately. Delivery failures are logged and never change the saved job or summary.

## Tests

```
python3 -m unittest discover -s tests
```
