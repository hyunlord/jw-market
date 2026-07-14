ARG BASE_IMAGE=asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/mnc/doc-parser-preprocessor:1.3.8.1-gpu-libpath-memfix-fv
FROM ${BASE_IMAGE}

ENV OCR_TESSERACT_PSM=4

COPY src/preprocessor.py /app/src/preprocessor.py
