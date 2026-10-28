FROM apache/airflow:2.10.5-python3.12
COPY extra-ca/ /tmp/extra-ca/
# dbt lives in its own venv so its pins cannot disturb Airflow's constraints
RUN python -m venv /home/airflow/dbt-venv \
 && if ls /tmp/extra-ca/*.pem >/dev/null 2>&1; then cat /etc/ssl/certs/ca-certificates.crt /tmp/extra-ca/*.pem > /tmp/bundle.pem; export PIP_CERT=/tmp/bundle.pem; fi \
 && /home/airflow/dbt-venv/bin/pip install --no-cache-dir "dbt-core==1.8.*" "dbt-postgres==1.8.*"
COPY airflow/entrypoint.sh /entrypoint-demo.sh
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/entrypoint-demo.sh"]
