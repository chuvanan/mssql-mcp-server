# Debian/glibc, not Alpine: mssql-python ships manylinux (glibc) native
# bindings and musl is not a supported target.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# No FreeTDS or unixODBC: mssql-python bundles its own ODBC/DDBC layer.
# Its native libraries still need libstdc++ and the Kerberos runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libstdc++6 \
    libkrb5-3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

# Fail at build time rather than on the first connection attempt.
RUN python -c "import mssql_python, mssql_mcp_server"

CMD ["python", "-m", "mssql_mcp_server"]
