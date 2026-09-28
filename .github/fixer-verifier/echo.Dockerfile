FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY src/requirements.txt src/requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY src/ ./
CMD ["python", "-m", "pytest", "-q", "-x", "--deselect", "tests/test_clipper_phase0.py::test_detect_prereqs_faster_whisper_absent"]

