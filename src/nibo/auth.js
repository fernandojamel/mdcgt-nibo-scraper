import { chromium } from 'playwright';
import { generateTotp } from '../util/totp.js';
import { sleep } from '../util/retry.js';
import { logger } from '../util/logger.js';
import {
  PASSPORT_BASE,
  APP_BASE,
  USER_AGENT,
} from './config.js';

/**
 * Faz login no Nibo via Playwright (passport.nibo.com.br + MFA TOTP),
 * captura o Bearer token interceptando uma chamada autenticada após o login,
 * e retorna uma sessão pronta pra uso pela camada api.js.
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
 */
export async function loginAndCaptureSession({ email, password, totpSecret }) {
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
    // 3. Tela de escolha de método MFA (passport/mfa/choosefa).
    //    Tem 2 rádios: "Mensagem no celular" e "Aplicativo de autenticação".
    //    Sempre escolhemos o aplicativo (type=4).
    // ------------------------------------------------------------------
    if (page.url().includes('/mfa/choosefa')) {
      logger.info('selecionando aplicativo autenticador');
      // Marca o rádio de aplicativo autenticador. Os rádios têm value="4" e value="1" (SMS).
      const appRadio = page.locator('input[type="radio"][value="4"]');
      if (await appRadio.count() > 0) {
        await appRadio.check();
      } else {
        // Fallback: label contém "Aplicativo"
        await page.locator('label:has-text("Aplicativo")').click();
      }
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
        page.locator('button[type="submit"], input[type="submit"]').first().click(),
      ]);
    }

    // ------------------------------------------------------------------
    // 4. Tela do código TOTP. Geramos o código aqui e submetemos.
    //    O Nibo aceita "trustThisDevice=true" — vamos marcar pra reduzir
    //    a frequência com que precisamos passar pelo MFA (cookie ~30 dias).
    // ------------------------------------------------------------------
    if (page.url().includes('/MFA/VerifyCode')) {
      // Espera o JS da página rodar e renderizar as caixinhas (algumas
      // implementações geram o form em runtime).
      await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});

      // Retry: TOTP pode ser rejeitado quando geramos no final da janela
      // de 30s e o Nibo recebe ja no proximo slot (drift de relogio + latencia).
      // Estrategia: ate 3 tentativas, esperando ~32s entre cada (= novo slot).
      const MAX_TOTP_TRIES = 3;
      let lastErr = null;
      for (let attempt = 1; attempt <= MAX_TOTP_TRIES; attempt++) {
        // Sincroniza com o inicio do proximo slot TOTP pra ter ~30s inteiros
        // de validade. Sem isso, codigo gerado no final do slot fica obsoleto
        // antes do Nibo processar.
        const msIntoSlot = Date.now() % 30_000;
        const msToNextSlot = 30_000 - msIntoSlot;
        // Se faltam menos de 5s no slot atual, espera o proximo (evita gerar
        // codigo que vai expirar antes do submit chegar)
        if (msToNextSlot < 5_000) {
          logger.info({ msToNextSlot }, `aguardando proximo slot TOTP (${msToNextSlot}ms)`);
          await page.waitForTimeout(msToNextSlot + 500);
        }

        const code = generateTotp(totpSecret);
        logger.info({ attempt, code, slotMs: Date.now() % 30_000 },
          `tentativa ${attempt}/${MAX_TOTP_TRIES} de TOTP`);

        // Procura input visivel pra digitar. Robustos: a tela pode ser 1
        // campo unico ou 6 caixinhas com auto-avanco.
        const candidatos = [
          'input:visible[maxlength="1"]',
          'input:visible[inputmode="numeric"]',
          'input:visible[autocomplete="one-time-code"]',
          'input[type="text"]:visible:not([name="Username"])',
          'input[type="tel"]:visible',
          'input[name="Code"]:visible',
          'input[name="code"]:visible',
        ];
        let firstInput = null;
        for (const sel of candidatos) {
          const loc = page.locator(sel).first();
          if ((await loc.count()) > 0) {
            firstInput = loc;
            break;
          }
        }

        if (!firstInput) {
          await page.screenshot({ path: '/tmp/totp-fail.png', fullPage: true }).catch(() => {});
          throw new Error('nenhum input visível encontrado na tela de TOTP');
        }

        // Limpa qualquer valor anterior (retries) selecionando tudo e digitando.
        await firstInput.click({ delay: 50 });
        await page.keyboard.press('Control+A').catch(() => {});
        await page.keyboard.press('Delete').catch(() => {});
        await page.keyboard.type(code, { delay: 80 });
        await page.keyboard.press('Tab');
        await page.waitForTimeout(300);

        if (attempt === 1) {
          // So marca "confiar neste dispositivo" na 1a tentativa
          const trustCheckbox = page.locator('input[name="trustThisDevice"][type="checkbox"]');
          if ((await trustCheckbox.count()) > 0) {
            await trustCheckbox.check().catch(() => {});
          }
        }

        const codeFieldValue = await page.evaluate(() => {
          const inp = document.querySelector('input[name="Code"], input[name="code"]');
          return inp ? inp.value : '(nao encontrado)';
        });
        logger.debug({ codeFieldValue, expected: code, attempt }, 'valor do campo Code');

        // Submit
        try {
          await Promise.all([
            page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 30_000 }),
            page.locator('button[type="submit"], input[type="submit"]').first().click(),
          ]);
        } catch (navErr) {
          await dumpDiagnostico(page, codeFieldValue, `click-no-nav-${attempt}`);
          lastErr = new Error(`TOTP submit não navegou: ${navErr.message}`);
          continue;
        }

        // Se SAIU de /MFA/*, login bem sucedido.
        if (!page.url().includes('/MFA/') && !page.url().includes('/Account/Login')) {
          logger.info({ attempt }, 'TOTP aceito');
          break;
        }

        // Rejeitado. Pega erros visiveis pra log e prepara proxima tentativa.
        const visibleErrors = await page.evaluate(() => {
          const cand = document.querySelectorAll(
            '.alert, .error, .validation-summary-errors, [role="alert"], .text-danger, .help-block, .field-validation-error'
          );
          return [...cand].map((e) => e.textContent.trim()).filter(Boolean);
        });
        lastErr = new Error(
          `TOTP rejeitado pelo Nibo (tentativa ${attempt}). URL: ${page.url()}. ` +
          `Erros: ${visibleErrors.join(' | ') || '(nenhum)'}`
        );
        logger.warn({ attempt, url: page.url(), visibleErrors }, 'TOTP rejeitado, vou tentar de novo');

        if (attempt < MAX_TOTP_TRIES) {
          // Espera ~32s pra cair em NOVO slot TOTP. Se nao esperar, o proximo
          // generateTotp() vai retornar o MESMO codigo que acabou de ser
          // rejeitado.
          logger.info('aguardando 32s pro proximo slot TOTP');
          await page.waitForTimeout(32_000);
          // A pagina pode ter mudado entre tentativas (alerta vermelho, etc)
          // mas a URL ainda eh /MFA/VerifyCode, entao basta tentar de novo.
        }
      }

      // Se saiu do loop ainda em /MFA, exauriu tentativas.
      if (page.url().includes('/MFA/') || page.url().includes('/Account/Login')) {
        await dumpDiagnostico(page, '(varias tentativas)', 'rejected-final');
        throw new Error(
          `TOTP rejeitado pelo Nibo após ${MAX_TOTP_TRIES} tentativas. ` +
          `URL após submit: ${page.url()}. ` +
          `Último erro: ${lastErr?.message || '(desconhecido)'}. ` +
          `Print: c:/Users/ferna/Downloads/totp-fail.png`
        );
      }
    }

    // ------------------------------------------------------------------
    // 5. Esperar cair no app (empresa.nibo.com.br/Organization).
    //    Se ainda estiver em passport, algo deu errado (código inválido,
    //    senha errada, etc.) — joga erro com a URL pra debugar.
    // ------------------------------------------------------------------
    await page.waitForURL(/empresa\.nibo\.com\.br\/(Organization|Document)/i, {
      timeout: navTimeout,
    });

    logger.info({ url: page.url() }, 'login completo, na app');

    // ------------------------------------------------------------------
    // 6. Esperar o SPA do Nibo carregar e fazer suas chamadas API iniciais.
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

async function dumpDiagnostico(page, codeFieldValue, tag) {
  try {
    await page.screenshot({
      path: 'c:/Users/ferna/Downloads/totp-fail.png',
      fullPage: true,
    });
  } catch {}
  try {
    const url = page.url();
    const title = await page.title();
    const bodyText = await page.evaluate(() => document.body?.innerText?.slice(0, 1500));
    logger.error({ tag, url, title, codeFieldValue, bodyText }, 'diagnóstico TOTP');
  } catch {}
}

export async function closeSession(session) {
  if (!session) return;
  try {
    await session.browser.close();
  } catch (err) {
    logger.warn({ err: err.message }, 'erro fechando browser');
  }
}
