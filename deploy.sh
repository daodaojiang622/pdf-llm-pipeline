#!/bin/bash
# Build and deploy the PDF Processor application to AWS.
#
# Usage: AWS_PROFILE=my-profile ./deploy.sh
#
# Settings come from the environment, then from .env for anything not already
# set. Secrets are never printed.

set -e  # Exit on error

cd "$(dirname "$0")"

# Load .env without overriding variables that are already set
if [ -f .env ]; then
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        if [ -z "${!key}" ]; then
            value="${value%\"}"; value="${value#\"}"
            value="${value%\'}"; value="${value#\'}"
            export "$key=$value"
        fi
    done < .env
fi

# Configuration
STACK_NAME=${STACK_NAME:-pdf-processor}
REGION=${AWS_REGION:-us-west-2}
DEPLOY_BUCKET=${DEPLOY_BUCKET:-}  # Optional: bucket for SAM artifacts. SAM manages one when empty.
STATIC_BUCKET=${STATIC_BUCKET:-}  # Optional: bucket for static assets. Skipped when empty.
CLOUDFRONT_DOMAIN=${CLOUDFRONT_DOMAIN:-}  # CloudFront domain (if any)
LLM_PROVIDER=${LLM_PROVIDER:-gemini}  # gemini or grok
GEMINI_API_KEY=${GEMINI_API_KEY:-}  # Gemini API Key
GEMINI_MODEL=${GEMINI_MODEL:-gemini-flash-lite-latest}  # Gemini model ID
GROK_API_KEY=${GROK_API_KEY:-}  # Grok API Key
GROK_API_URL=${GROK_API_URL:-https://api.x.ai/v1/chat/completions}  # Grok API URL
SECRET_KEY=${SECRET_KEY:-}  # Application secret key. Generated when empty.
WEBHOOK_SECRET=${WEBHOOK_SECRET:-}  # Shared secret for signing webhooks
DEFAULT_WEBHOOK_URL=${DEFAULT_WEBHOOK_URL:-}  # Fallback webhook URL

# Validate requirements
if ! command -v sam &> /dev/null; then
    echo "AWS SAM CLI is not installed. Please install it first."
    echo "See: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"
    exit 1
fi

# The image build needs BuildKit. SAM's legacy builder fails with Docker's containerd image store.
if ! sam build --help 2>/dev/null | grep -q -- "--use-buildkit"; then
    echo "This SAM CLI is too old to build with BuildKit. Upgrade it, for example:"
    echo "  pip install --upgrade aws-sam-cli"
    exit 1
fi

if ! command -v aws &> /dev/null; then
    echo "AWS CLI is not installed. Please install it first."
    echo "See: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    exit 1
fi

# Access keys in the environment take precedence over AWS_PROFILE
if [ -n "$AWS_PROFILE" ] && [ -n "$AWS_ACCESS_KEY_ID" ]; then
    echo "Warning: AWS_ACCESS_KEY_ID is set, so it is used instead of profile $AWS_PROFILE."
fi

# Show where this will deploy and ask before creating anything
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text) || {
    echo "Could not get AWS credentials. Sign in first, or set AWS_PROFILE."
    exit 1
}
echo "Stack:    $STACK_NAME"
echo "Region:   $REGION"
echo "Identity: $CALLER_ARN"
read -p "Deploy with these settings? (y/n): " confirm
[ "$confirm" = "y" ] || { echo "Cancelled."; exit 1; }

# Ask for the selected LLM provider's API key if not provided
if [ "$LLM_PROVIDER" = "gemini" ] && [ -z "$GEMINI_API_KEY" ]; then
    read -sp "Enter your Gemini API Key (input hidden): " GEMINI_API_KEY
    echo
elif [ "$LLM_PROVIDER" = "grok" ] && [ -z "$GROK_API_KEY" ]; then
    read -sp "Enter your Grok API Key (input hidden): " GROK_API_KEY
    echo
fi

# Generate the application secret key if not provided
if [ -z "$SECRET_KEY" ]; then
    SECRET_KEY=$(openssl rand -hex 32)
    echo "Generated a new SECRET_KEY for this deployment."
fi

echo "Building and deploying $STACK_NAME to region $REGION..."

# Build the SAM application
echo "Building the application..."
sam build --use-buildkit

# Static assets and HTML templates are only needed when serving the web UI from S3
if [ -n "$STATIC_BUCKET" ]; then
    echo "Uploading static assets to S3..."
    STATIC_BUCKET=$STATIC_BUCKET CLOUDFRONT_DOMAIN=$CLOUDFRONT_DOMAIN python upload_static_to_s3.py

    echo "Would you like to update HTML templates with API Gateway URLs? (y/n)"
    read update_templates

    if [ "$update_templates" = "y" ]; then
        # Get the API Gateway URL from a previous deployment if available
        api_url=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --region $REGION --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text 2>/dev/null || echo "")

        if [ -z "$api_url" ]; then
            read -p "Enter the API Gateway URL (without trailing slash): " api_url
        else
            echo "Found API Gateway URL from previous deployment: $api_url"
            read -p "Use this URL? (y/n): " use_existing_url
            if [ "$use_existing_url" != "y" ]; then
                read -p "Enter the API Gateway URL (without trailing slash): " api_url
            fi
        fi

        static_url=""
        if [ -n "$CLOUDFRONT_DOMAIN" ]; then
            static_url="https://$CLOUDFRONT_DOMAIN"
        else
            static_url="https://$STATIC_BUCKET.s3.amazonaws.com"
        fi

        echo "Updating templates with API URL: $api_url"
        echo "Updating templates with static URL: $static_url"
        python update_templates.py --api-url "$api_url" --static-url "$static_url"
    fi
fi

# Deploy the SAM application
echo "Deploying the application..."
PARAMETER_OVERRIDES=("LlmProvider=$LLM_PROVIDER" "GeminiModel=$GEMINI_MODEL" "GrokApiUrl=$GROK_API_URL" "SecretKey=$SECRET_KEY")
if [ -n "$GEMINI_API_KEY" ]; then
    PARAMETER_OVERRIDES+=("GeminiApiKey=$GEMINI_API_KEY")
fi
if [ -n "$GROK_API_KEY" ]; then
    PARAMETER_OVERRIDES+=("GrokApiKey=$GROK_API_KEY")
fi
if [ -n "$WEBHOOK_SECRET" ]; then
    PARAMETER_OVERRIDES+=("WebhookSecret=$WEBHOOK_SECRET")
fi
if [ -n "$DEFAULT_WEBHOOK_URL" ]; then
    PARAMETER_OVERRIDES+=("DefaultWebhookUrl=$DEFAULT_WEBHOOK_URL")
fi

if [ -n "$DEPLOY_BUCKET" ]; then
    ARTIFACT_ARGS=(--s3-bucket "$DEPLOY_BUCKET")
else
    ARTIFACT_ARGS=(--resolve-s3)
fi

sam deploy \
    --stack-name "$STACK_NAME" \
    "${ARTIFACT_ARGS[@]}" \
    --resolve-image-repos \
    --region "$REGION" \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides "${PARAMETER_OVERRIDES[@]}"

echo "Deployment complete!"

# Display stack outputs
echo "Stack outputs:"
aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" --query "Stacks[0].Outputs" --output table

echo "You can now access your application at the API Gateway URL shown above."
