FROM ubuntu:22.04
LABEL org.opencontainers.image.source="https://github.com/listyque/NAS-Game-Switcher" \
    org.opencontainers.image.title="NAS Game Switcher Server" \
    org.opencontainers.image.version="0.1.0-preview.1"
ENV DEBIAN_FRONTEND=noninteractive \
    TACTIC_INSTALL_DIR=/opt/tactic/tactic \
    TACTIC_DATA_DIR=/opt/tactic/tactic_data \
    TACTIC_SITE_DIR=/opt/tactic/projects \
    TACTIC_CONFIG_PATH=/opt/tactic/tactic_data/config/tactic-conf.xml \
    PYTHONPATH=/opt/tactic/tactic/3rd_party/python3/site-packages:/opt/tactic/tactic/3rd_party/common/site-packages:/opt/tactic/tactic_data/plugins:/opt/tactic \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    apache2 ca-certificates curl gosu postgresql-client python3 python3-pip tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN curl --fail --location --retry 3 --output /tmp/tactic.tar.gz https://codeload.github.com/Southpaw-TACTIC/TACTIC/tar.gz/17c78e0dfb20a6fef16d70df6aa7b0c9bf20a596 \
    && echo 'eaea278dfa76b81273d8face02723bbae4230cc0b6bf1121aab214624ca6920c  /tmp/tactic.tar.gz' | sha256sum -c - \
    && mkdir -p /opt/tactic/tactic && tar -xzf /tmp/tactic.tar.gz -C /opt/tactic/tactic --strip-components=1 \
    && rm /tmp/tactic.tar.gz \
    && cp -a /opt/tactic/tactic/src/install/data /opt/tactic/tacticenv
COPY requirements.txt /opt/ngs/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /opt/ngs/requirements.txt \
    && python3 -c "import tacticenv, psycopg2; from cherrypy._cpreqbody import Part; from Cryptodome.Signature import pkcs1_15; assert hasattr(Part, 'get_path')" \
    && useradd --uid 10001 --create-home --shell /bin/bash tactic \
    && a2enmod proxy proxy_http slotmem_shm proxy_balancer lbmethod_byrequests \
    && printf 'Listen 8081\n' > /etc/apache2/ports.conf \
    && printf '\nexport APACHE_RUN_USER=tactic\nexport APACHE_RUN_GROUP=tactic\n' >> /etc/apache2/envvars
# Match the database major version; Ubuntu 22.04's default pg_dump is version 14.
RUN install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt jammy-pgdg main\n' > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
    && /usr/lib/postgresql/16/bin/pg_dump --version && rm -rf /var/lib/apt/lists/*
COPY apache.conf /etc/apache2/sites-available/000-default.conf
COPY container/ /opt/ngs/
COPY plugin/ /opt/ngs/plugin/
RUN python3 /opt/ngs/patch_tactic.py && chmod 0755 /opt/ngs/entrypoint.sh && apache2ctl -t
EXPOSE 8081
ENTRYPOINT ["/opt/ngs/entrypoint.sh"]
