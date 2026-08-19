# Imagem oficial do Playwright já vem com Node 20 + Chromium + libs
FROM mcr.microsoft.com/playwright:v1.60.0-jammy

WORKDIR /app

# Dependências Node primeiro (cache eficiente em rebuilds)
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

# Python pros scripts de parsing/sync. pdfplumber (PDFs) + openpyxl (Excel
# do rateio INSS/IRRF). Roda como root (antes do USER pwuser).
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && pip3 install --no-cache-dir pdfplumber openpyxl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Código (server Node + scripts Python)
# ARG invalida o cache de COPYs sempre que o valor muda — garantia de que
# mudancas em src/ ou scripts/ realmente entrem no container, mesmo se o
# EasyPanel tiver cache estranho do Docker.
ARG CACHE_BUST=2026-08-18-inss-irrf-vencimento
RUN echo "build cache key: $CACHE_BUST"
COPY src/ ./src/
COPY scripts/ ./scripts/

ENV NODE_ENV=production
ENV PORT=3000
# O sync roda no MESMO container -> fala com o server Node em localhost.
ENV SCRAPER_URL=http://localhost:3000

EXPOSE 3000

# Roda como usuário não-root (a imagem do Playwright já cria o user `pwuser`)
USER pwuser

CMD ["node", "src/server.js"]
