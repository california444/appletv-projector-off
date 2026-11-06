# --- Build-Stufe: Wheels bauen ------------------------------------------------
# pyatv hängt von miniaudio ab. Dafür gibt es auf PyPI keine aarch64-Wheels,
# es muss also mit C/C++-Compiler aus den Quellen gebaut werden.
FROM python:3.12-slim AS build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN pip wheel --no-cache-dir --wheel-dir /wheels pyatv

# --- Laufzeit-Stufe: ohne Compiler -------------------------------------------
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends adb \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels pyatv \
 && rm -rf /wheels

# pyatv-Credentials (~/.pyatv.conf) und ADB-Schlüssel (~/.android) landen im Volume
ENV HOME=/data \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY atv_projector.py .

# Herkunft des Builds bewusst ganz am Ende: APP_REVISION ändert sich mit
# jedem Commit und würde weiter oben die Layer davor jedes Mal invalidieren.
ARG APP_REVISION=""
ENV APP_REVISION=${APP_REVISION}

CMD ["python", "/app/atv_projector.py"]
