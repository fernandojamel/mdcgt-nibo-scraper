/**
 * scripts/login-manual.js
 *
 * Abre Chromium VISIVEL pra voce logar manualmente no Nibo (incluindo a
 * verificacao 2FA por SMS, se o Nibo pedir). Quando detecta que voce
 * chegou no app (empresa.nibo.com.br/Organization), salva os cookies +
 * localStorage + indexedDB em base64.
 *
 * Por que: o scraper na VPS, ao rodar headless, sempre eh visto como
 * "primeiro acesso" pelo Nibo, que exige 2FA por SMS. Pra fugir disso,
 * a gente faz UM login manual aqui no PC do user (Nibo confia no
 * dispositivo apos esse login) e exporta esses cookies pra VPS.
 *
 * Uso (rodar dentro de nibo-scraper/):
 *   node --env-file=.env scripts/login-manual.js
 *
 * Output:
 *   1. Browser abre, voce loga normalmente
 *   2. Quando chegar no app do Nibo (qualquer pagina), pressione ENTER no terminal
 *   3. Script salva nibo-session.json local + imprime o conteudo em base64
 *   4. Voce copia o base64 e cola no EasyPanel:
 *      Ambiente > NIBO_SESSION_B64 = <conteudo>
 *      > Salvar > Implantar (pra container pegar a env nova)
 *
 * O cookie de "confiar neste dispositivo" do Nibo dura ~30 dias.
 * Quando expirar, o scraper joga erro claro e voce roda este script
 * de novo.
 */
import { chromium } from 'playwright';
import { writeFileSync, readFileSync } from 'fs';
import { createInterface } from 'readline';

const APP_BASE = 'https://empresa.nibo.com.br/';
const OUTFILE = 'nibo-session.json';

function waitEnter() {
  return new Promise((resolve) => {
    const rl = createInterface({ input: process.stdin, output: process.stdout });
    rl.question(
      '\n>>> Quando voce chegar no app do Nibo (pode marcar "confiar neste dispositivo" se aparecer), aperte ENTER aqui no terminal. <<<\n',
      () => {
        rl.close();
        resolve();
      }
    );
  });
}

async function main() {
  console.log('Abrindo Chromium visivel...');
  const browser = await chromium.launch({
    headless: false,
    args: ['--no-sandbox'],
  });
  const context = await browser.newContext({
    locale: 'pt-BR',
    timezoneId: 'America/Sao_Paulo',
    viewport: { width: 1280, height: 800 },
  });
  const page = await context.newPage();
  await page.goto(APP_BASE);

  console.log('\n=== INSTRUCOES ===');
  console.log('1. Faca login no Nibo (email + senha)');
  console.log('2. Se pedir codigo via SMS, complete o 2FA');
  console.log('3. Se aparecer "confiar neste dispositivo", MARQUE');
  console.log('4. Espere chegar em empresa.nibo.com.br/...');
  console.log('5. Volte aqui e aperte ENTER\n');

  await waitEnter();

  // Salva estado completo (cookies + localStorage)
  const state = await context.storageState();
  const json = JSON.stringify(state);
  writeFileSync(OUTFILE, json);
  console.log(`\nSalvo em ${OUTFILE} (${json.length} bytes).`);

  const b64 = Buffer.from(json, 'utf-8').toString('base64');
  console.log(`\n=== COPIE O BASE64 ABAIXO ===\n`);
  console.log(b64);
  console.log(`\n=== FIM ===`);
  console.log(`\nAgora no EasyPanel: Ambiente > NIBO_SESSION_B64 = (cole o base64 acima) > Salvar > Implantar.\n`);
  console.log(`Cookies validos por ~30 dias. Se o scraper falhar com 'sessao expirou', roda este script de novo.\n`);

  await browser.close();
}

main().catch((err) => {
  console.error('ERRO:', err);
  process.exit(1);
});
