import { chromium } from 'playwright';
import { sleep } from '../util/retry.js';
import { logger } from '../util/logger.js';
import {
  PASSPORT_BASE,
  APP_BASE,
  USER_AGENT,
} from './config.js';

/**
 * Faz login no Nibo via Playwright (passport.nibo.com.br), captura o Bearer
 * token interceptando uma chamada autenticada após o login, e retorna uma
 * sessão pronta pra uso pela camada api.js.
 *
 * Por que Playwright e não axios direto? Porque o login do passport.nibo usa:
 *  - Anti-forgery token em campo hidden (__RequestVerificationToken)
 *  - Cadeia de 5+ redirects 302 com cookies que precisam ser preservados
 *  - OAuth Code flow que termina num exchange code → cookie de sessão
 * Replicar tudo isso em axios daria muito mais código e quebraria à primeira
 * mudança do Nibo. Playwright simula o navegador inteiro.
 *
 * Após o login a gente captura o JWT que o SPA usa internamente e passa
 * pra camada de API usar com axios puro — bem mais rápido que continuar
 * navegando pela UI.
 *
 * 2FA: desabilitado na conta Nibo do scraper (decisao jun/2026). Se a conta
 * voltar a exigir 2FA (Nibo forcar ou usuario reativar), o login cai em
 * /MFA/* e damos erro claro pedindo pra desativar.
 */
export async function loginAndCaptureSession({ email, password }) {
  const browser = await chromium.launch({
    headless: (process.env.HEADLESS ?? 'true') === 'true',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });

  const navTimeout = Number(process.env.NAV_TIMEOUT_MS ?? 60000);
  const waitTimeout = Number(process.env.WAIT_TIMEOUT_MS ?? 15000);

  const context = await browser.newContext({
    userAgent: USER_AGENT,
    locale: 'pt-BR',
    timezoneId: 'America/Sao_Paulo',
    viewport: { width: 1280, height: 800 },
  });

  // Captura passiva do Bearer token: toda requisição saindo do browser pra
  // api-contador/api-empresa que tiver Authorization, a gente intercepta o valor.
  let bearerToken = null;
  context.on('request', (req) => {
    const url = req.url();
    if (
      !bearerToken &&
      (url.includes('api-contador.nibo.com.br') || url.includes('api-empresa.nibo.com.br'))
    ) {
      const auth = req.headers()['authorization'];
      if (auth?.startsWith('Bearer ')) {
        bearerToken = auth.slice('Bearer '.length);
      }
    }
  });

  const page = await context.newPage();
  page.setDefaultTimeout(waitTimeout);
  page.setDefaultNavigationTimeout(navTimeout);

  try {
    // ------------------------------------------------------------------
    // 1. Bater na app raiz dispara cascata de redirects pra tela de login.
    //    O Nibo usa OAuth Code flow: empresa → /Auth/Callback → passport.
    // ------------------------------------------------------------------
    logger.info('navegando pra login');
    await page.goto(APP_BASE, { waitUntil: 'domcontentloaded' });

    // Espera chegar na tela de login do passport (URL contém /Account/Login)
    await page.waitForURL(/passport\.nibo\.com\.br\/Account\/Login/i, {
      timeout: navTimeout,
    });

    // ------------------------------------------------------------------
    // 2. Login Nibo é "identifier-first": primeiro o email, aí aparece senha.
    //    A página tem um campo Username e um Password — submetemos os dois
    //    em uma requisição só, igual o SPA faz quando ShowPasswordField=true.
    // ------------------------------------------------------------------
    logger.info('preenchendo credenciais');
    await page.locator('input[name="Username"], input[type="email"]').fill(email);

    // Se a senha ainda não está visível (modo identifier-first ativo), clica
    // no botão "Próximo"/"Continuar" antes de preencher senha.
    const passwordVisible = await page
      .locator('input[name="Password"]')
      .isVisible()
      .catch(() => false);

    if (!passwordVisible) {
      logger.debug('senha não visível, clicando próximo');
      await page.locator('button[type="submit"], input[type="submit"]').first().click();
      await page.locator('input[name="Password"]').waitFor({ state: 'visible' });
    }

    await page.locator('input[name="Password"]').fill(password);

    // Submete login. Promise.all garante que esperamos a navegação iniciar.
    await Promise.all([
      page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
      page.locator('button[type="submit"], input[type="submit"]').first().click(),
    ]);

    // ------------------------------------------------------------------
    // 3. Guard: se o Nibo redirecionou pra alguma tela de MFA, alguem reativou
    //    o 2FA na conta. O scraper foi simplificado em jun/2026 pra rodar sem
    //    2FA — desative no Nibo (Perfil → Seguranca → Verificacao em 2 etapas).
    // ------------------------------------------------------------------
    if (page.url().toLowerCase().includes('/mfa/')) {
      throw new Error(
        `Nibo redirecionou pra MFA (${page.url()}), mas o scraper roda sem 2FA. ` +
        `Desative o 2FA na conta Nibo do scraper (Perfil > Seguranca > Verificacao em 2 etapas).`
      );
    }

    // ------------------------------------------------------------------
    // 4. Esperar cair no app (empresa.nibo.com.br/Organization).
    //    Se ainda estiver em passport, algo deu errado (código inválido,
    //    senha errada, etc.) — joga erro com a URL pra debugar.
    // ------------------------------------------------------------------
    await page.waitForURL(/empresa\.nibo\.com\.br\/(Organization|Document)/i, {
      timeout: navTimeout,
    });

    logger.info({ url: page.url() }, 'login completo, na app');

    // ------------------------------------------------------------------
    // 5. Esperar o SPA do Nibo carregar e fazer suas chamadas API iniciais.
    //    Quando carrega, ele bate em /organizations/context, /accountants/.../features,
    //    etc. — todas com Authorization: Bearer. Nosso interceptor captura.
    // ------------------------------------------------------------------
    await page.waitForLoadState('networkidle', { timeout: 30_000 }).catch(() => {
      logger.warn('networkidle não atingiu, seguindo mesmo assim');
    });

    // Aguarda ativamente até 15s pelo token aparecer no interceptor
    for (let i = 0; i < 150 && !bearerToken; i++) await sleep(100);

    // Plano B: se ainda não pegou, navega pro selector de organizações
    // (página /Organization) que SEMPRE chama o getOrganizations/context na carga.
    if (!bearerToken) {
      logger.warn('bearer não capturado após networkidle — forçando reload do /Organization');
      try {
        await page.goto('https://empresa.nibo.com.br/Organization', { waitUntil: 'networkidle', timeout: 30_000 });
      } catch {}
      for (let i = 0; i < 100 && !bearerToken; i++) await sleep(100);
    }

    if (!bearerToken) {
      // Diagnóstico antes de morrer
      const url = page.url();
      const cookies = (await context.cookies()).map((c) => c.name).join(',');
      logger.error({ url, cookies }, 'bearer token NUNCA apareceu — SPA não disparou nenhuma chamada autenticada');
      throw new Error('login OK mas não capturei o Bearer token — verifique se houve nova versão do Nibo');
    }

    logger.info({ tokenLen: bearerToken.length }, 'bearer token capturado');

    return {
      browser,
      context,
      page,
      bearerToken,
      capturedAt: Date.now(),
    };
  } catch (err) {
    logger.error({ err: err?.message ?? String(err), url: page.url() }, 'falha no login');
    await browser.close().catch(() => {});
    throw err;
  }
}

export async function closeSession(session) {
  if (!session) return;
  try {
    await session.browser.close();
  } catch (err) {
    logger.warn({ err: err.message }, 'erro fechando browser');
  }
}
