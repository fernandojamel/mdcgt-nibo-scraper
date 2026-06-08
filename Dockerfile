# Imagem oficial do Playwright já vem com Node 20 + Chromium + libs
FROM mcr.microsoft.com/playwright:v1.60.0-jammy

WORKDIR /app

# Dependências Node primeiro (cache eficiente em rebuilds)
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

# Python + pdfplumber pros scripts de parsing/sync (consignado e folha).
# Roda como root (antes do USER pwuser) pra instalar no sistema.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && pip3 install --no-cache-dir pdfplumber \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Código (server Node + scripts Python)
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
