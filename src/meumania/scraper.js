import fs from 'fs';
import path from 'path';
import { chromium } from 'playwright';
import { logger } from '../util/logger.js';

// Engenharia reversa em reference_meumania_scraper (jun/2026). meumania.com é
// AngularJS + ASP.NET. Fluxo: login (DOM) -> abrir Documentos dispara
// POST ashx/colab/social.ashx (ListDocuments) que devolve TODOS os documentos
// -> filtra a categoria "PEX - Folhetim de Resultados" -> baixa os PDFs mais
// recentes via blob.ashx (cookie da sessão).

const BASE = (process.env.MEUMANIA_BASE || 'https://meumania.com').replace(/\/$/, '');
const USER_SEL = 'input[type="text"][placeholder="Usuário"]';
// ⚠️ placeholder "Senha" casa 7 campos (modais escondidos); use o ng-model.
const PASS_SEL = 'input[ng-model="USERSESSION.USER_PASSWORD"]';

/**
 * Loga no Meu Mania, acha os folhetins do PEX e baixa os `quantidade` mais
 * recentes (precisamos de ~3-4 pra média móvel do Farol no parser). Grava os
 * PDFs em `destDir` com prefixo "Folhetim_" e devolve metadados.
 */
export async function baixarFolhetins({ quantidade = 4, destDir }) {
  const email = process.env.MEUMANIA_USER;
  const password = process.env.MEUMANIA_PASS;
  if (!email || !password || email.startsWith('COLE_')) {
    throw new Error('MEUMANIA_USER/MEUMANIA_PASS ausentes no ambiente');
  }
  if (!destDir) throw new Error('destDir obrigatório');

  const navTimeout = Number(process.env.NAV_TIMEOUT_MS ?? 60000);
  const browser = await chromium.launch({
    headless: (process.env.HEADLESS ?? 'true') === 'true',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const ctx = await browser.newContext({
    locale: 'pt-BR',
    timezoneId: 'America/Sao_Paulo',
    viewport: { width: 1366, height: 900 },
    acceptDownloads: true,
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(Number(process.env.WAIT_TIMEOUT_MS ?? 20000));
  page.setDefaultNavigationTimeout(navTimeout);

  try {
    // 1) login
    await page.goto(`${BASE}/`, { waitUntil: 'commit', timeout: navTimeout })
      .catch((e) => logger.warn({ err: e.message }, 'meumania: goto warn'));
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
    await page.locator(USER_SEL).first().waitFor({ state: 'visible', timeout: 30000 });
    await page.locator(USER_SEL).first().fill(email);
    await page.locator(PASS_SEL).first().fill(password);
    await page.waitForTimeout(300);
    await page.getByRole('button', { name: 'Entrar' }).click({ timeout: 15000 });
    await page.waitForURL(/#\/index\/main/i, { timeout: navTimeout }).catch(() => {});
    await page.waitForTimeout(3000);
    const aindaLogin = await page.locator(PASS_SEL).first().isVisible().catch(() => false);
    if (aindaLogin) {
      throw new Error('login falhou (senha ainda visível) — confira MEUMANIA_USER/PASS');
    }
    logger.info('meumania: login OK');

    // 2) abrir Documentos e capturar a resposta do ListDocuments
    const respPromise = page.waitForResponse(
      (r) => /colab\/social\.ashx/i.test(r.url()) && r.request().method() === 'POST',
      { timeout: navTimeout },
    );
    await page.getByText('Documentos', { exact: true }).first().click({ timeout: 15000 });
    const resp = await respPromise;
    const data = await resp.json();
    const docs = JSON.parse(data?.DOCUMENTS || '[]');
    logger.info({ total: docs.length }, 'meumania: documentos listados');

    // 3) filtra folhetins .pdf, ordena por data desc. NÃO depender só da
    // categoria (VIDEO_CATEGORY_NAME) -- o folhetim de julho/2026 foi
    // publicado com categoria NULL (inconsistência do próprio Meu Mania),
    // o que fazia o filtro anterior (só por categoria) perder o documento
    // silenciosamente. O lead ("Folhetim de Resultados | <Mês> <Ano>") é
    // mais confiável.
    const folhetins = docs
      .filter((d) =>
        /^Folhetim de Resultados/i.test(d.SOCIAL_LEAD || '') &&
        (d.SOCIAL_FILE_TYPE || '').toLowerCase() === '.pdf' &&
        d.SOCIAL_FILE_NAME)
      .sort((a, b) => String(b.SOCIAL_POST_DT).localeCompare(String(a.SOCIAL_POST_DT)));
    if (folhetins.length === 0) {
      throw new Error('nenhum folhetim na categoria "PEX - Folhetim de Resultados"');
    }

    // 4) baixa os N mais recentes via blob.ashx (usa o cookie da sessão)
    const escolhidos = folhetins.slice(0, quantidade);
    const items = [];
    let i = 0;
    for (const d of escolhidos) {
      const url = `${BASE}/ashx/blob.ashx?mode=v&url=social/${encodeURIComponent(d.SOCIAL_FILE_NAME)}`;
      const r = await page.request.get(url, { timeout: navTimeout });
      if (!r.ok()) {
        logger.warn({ status: r.status(), lead: d.SOCIAL_LEAD }, 'meumania: download falhou');
        continue;
      }
      const buf = await r.body();
      if (buf.slice(0, 5).toString('latin1') !== '%PDF-') {
        logger.warn({ lead: d.SOCIAL_LEAD }, 'meumania: resposta não é PDF');
        continue;
      }
      const nome = `Folhetim_${String(i).padStart(2, '0')}.pdf`;
      const fpath = path.join(destDir, nome);
      fs.writeFileSync(fpath, buf);
      items.push({
        path: fpath,
        lead: d.SOCIAL_LEAD,
        originalName: d.SOCIAL_FILE_ORIGINAL,
        postDate: d.SOCIAL_POST_DT,
        sizeBytes: buf.length,
      });
      i += 1;
    }
    if (items.length === 0) throw new Error('baixou 0 folhetins válidos');
    logger.info({ count: items.length, leads: items.map((x) => x.lead) },
      'meumania: folhetins baixados');
    return items;
  } finally {
    await browser.close().catch(() => {});
  }
}
