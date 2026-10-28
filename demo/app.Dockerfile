FROM python:3.12.7-slim
# optional corporate/proxy root CA for pip (demo/extra-ca/*.pem, gitignored)
COPY extra-ca/ /tmp/extra-ca/
RUN if ls /tmp/extra-ca/*.pem >/dev/null 2>&1; then cat /etc/ssl/certs/ca-certificates.crt /tmp/extra-ca/*.pem > /tmp/bundle.pem; export PIP_CERT=/tmp/bundle.pem; fi \
 && pip install --no-cache-dir "psycopg[binary]==3.2.3"
WORKDIR /app
