import { chromium } from 'playwright';
import { logger } from '../util/logger.js';

// Engenharia reversa do app.harmo.me (2026-08-23). SPA (React) + API JSON em
// gowalski.ms.harmo.me -- bem mais simples que Meu Mania: o botao "Exportar"
// da UI so gera um XLSX pra download humano, mas a MESMA lista que alimenta a
// tela de "Caixa de entrada" ja vem em JSON estruturado (sem precisar
// parsear planilha nenhuma). Ver [[reference_harmo_scraper]].
//
// Login (DOM, sem 2FA): input[type=email] + input[type=password] + botao
// "Acessar". Auth por Bearer JWT (Auth0) anexado pelo JS da pagina nas
// chamadas fetch/XHR -- mas `page.request` (contexto autenticado do
// Playwright) já reproduz esse token sem precisar extrai-lo manualmente.
//
// `establishment_name` vem como "TIJ | Mania de Churrasco | P. TIJUCA" /
// "MET | Mania de Churrasco | P. METROP. RJ" -- mesmo prefixo usado pelo
// parser do Excel (harmo_excel_parser.dart).

const BASE = (process.env.HARMO_BASE || 'https://app.harmo.me').replace(/\/$/, '');
const API_BASE = process.env.HARMO_API_BASE || 'https://gowalski.ms.harmo.me';
// "1908" = id do grupo (todos os locais) na conta da Mania de Churrasco.
const GROUP_ID = process.env.HARMO_GROUP_ID || '1908';

function fmtDataHora(d) {
  const p2 = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())} `
    + `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}`;
}

/**
 * Loga no Harmo e busca TODAS as avaliações (Google + iFood + TripAdvisor)
 * no intervalo [dateFrom, dateTo] (Date em UTC, mas a API trata como
 * horário local do usuário -- ver nota no sync_harmo_avaliacoes.py sobre
 * conversão de fuso). Devolve o array bruto de reviews da API.
 */
export async function buscarAvaliacoes({ dateFrom, dateTo }) {
  const email = process.env.HARMO_USER;
  const password = process.env.HARMO_PASS;
  if (!email || !password || email.startsWith('COLE_')) {
    throw new Error('HARMO_USER/HARMO_PASS ausentes no ambiente');
  }
  if (!dateFrom || !dateTo) throw new Error('dateFrom/dateTo obrigatórios');

  const navTimeout = Number(process.env.NAV_TIMEOUT_MS ?? 60000);
  const browser = await chromium.launch({
    headless: (process.env.HEADLESS ?? 'true') === 'true',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const ctx = await browser.newContext({
    locale: 'pt-BR',
    viewport: { width: 1440, height: 900 },
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(Number(process.env.WAIT_TIMEOUT_MS ?? 20000));
  page.setDefaultNavigationTimeout(navTimeout);

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: navTimeout })
      .catch((e) => logger.warn({ err: e.message }, 'harmo: goto warn'));
    await page.waitForTimeout(1500);

    const emailInput = page.locator('input[type="email"], input[placeholder*="mail" i]').first();
    const passInput = page.locator('input[type="password"]').first();
    await emailInput.waitFor({ state: 'visible', timeout: 30000 });
    await emailInput.fill(email);
    await passInput.fill(password);
    await page.waitForTimeout(300);
    await page.getByRole('button', { name: /acessar/i }).click({ timeout: 15000 });
    await page.waitForTimeout(3000);
    const aindaLogin = await passInput.isVisible().catch(() => false);
    if (aindaLogin) {
      // Diagnóstico: screenshot + texto da página no momento da falha (pode
      // ser bloqueio anti-robô específico do IP do servidor, captcha, ou
      // mensagem de erro visível que não aparece testando local).
      let screenshotBase64;
      let pageText;
      let pageUrl;
      try {
        const buf = await page.screenshot({ timeout: 8000 });
        screenshotBase64 = buf.toString('base64');
      } catch (e) {
        logger.warn({ err: e.message }, 'harmo: screenshot de diagnostico falhou');
      }
      try {
        pageText = (await page.locator('body').innerText({ timeout: 5000 })).slice(0, 1000);
      } catch { /* ignore */ }
      pageUrl = page.url();
      const err = new Error('login falhou (senha ainda visível) — confira HARMO_USER/PASS');
      err.diagnostico = { screenshotBase64, pageText, pageUrl };
      throw err;
    }
    logger.info('harmo: login OK');

    const de = fmtDataHora(new Date(Date.UTC(
      dateFrom.getUTCFullYear(), dateFrom.getUTCMonth(), dateFrom.getUTCDate(), 0, 0, 0,
    )));
    const ate = fmtDataHora(new Date(Date.UTC(
      dateTo.getUTCFullYear(), dateTo.getUTCMonth(), dateTo.getUTCDate(), 23, 59, 59,
    )));

    // 1) pega o total pra saber o pageSize necessário (evita paginação).
    const countUrl = `${API_BASE}/v1/reviews/count/${GROUP_ID}`
      + `?pageNumber=1&pageSize=10&dateFrom=${encodeURIComponent(de)}&dateTo=${encodeURIComponent(ate)}`
      + '&source=Google&source=IFoodApi&source=TripAdvisor&countTerms=160&parseType=group';
    const countResp = await page.request.get(countUrl);
    if (!countResp.ok()) {
      throw new Error(`count falhou: HTTP ${countResp.status()}`);
    }
    const { total } = await countResp.json();
    logger.info({ total, de, ate }, 'harmo: total de avaliacoes no periodo');
    if (!total) return [];

    // 2) busca tudo numa página só (a propria UI usa pageSize=999999 na
    // chamada de contagem inicial -- confirmado que a API aceita).
    const listUrl = `${API_BASE}/v1/reviews/${GROUP_ID}`
      + `?pageNumber=1&pageSize=${Math.max(total, 10)}`
      + `&dateFrom=${encodeURIComponent(de)}&dateTo=${encodeURIComponent(ate)}`
      + '&source=Google&source=IFoodApi&source=TripAdvisor&countTerms=160&parseType=group';
    const listResp = await page.request.get(listUrl);
    if (!listResp.ok()) {
      throw new Error(`lista falhou: HTTP ${listResp.status()}`);
    }
    const { reviews } = await listResp.json();
    logger.info({ count: reviews.length }, 'harmo: avaliacoes baixadas');
    return reviews;
  } finally {
    await browser.close().catch(() => {});
  }
}
