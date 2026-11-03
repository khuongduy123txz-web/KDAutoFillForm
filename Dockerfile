FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    KD_CLOUD=1 \
    KD_HOST=0.0.0.0 \
    PORT=10000

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps --only-shell chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser \
    && chmod -R a+rX /ms-playwright

COPY --chown=appuser:appuser google_form_tool.py google_fast.py form_scanning.py work_queue.py microsoft_forms.py html_forms.py spss_export.py run_scheduling.py teaching_tools.py ./
USER appuser
EXPOSE 10000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "google_form_tool.py", "--web", "--no-open"]
