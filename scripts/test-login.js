/**
 * scripts/test-login.js
 *
 * Testa rapidamente se as credenciais NIBO_EMAIL + NIBO_PASSWORD conseguem
 * logar SEM precisar passar por 2FA. Use pra validar um usuario novo antes
 * de colocar em producao.
 *
 * Uso (dentro de nibo-scraper/):
 *   node --env-file=.env scripts/test-login.js
 *
 * Roda HEADFUL (browser visivel) pra voce ver se cai em /mfa/ ou nao.
 */
import { loginAndCaptureSession, closeSession } from '../src/nibo/auth.js';

// Forca browser visivel pra debug
process.env.HEADLESS = 'false';
// Desliga sessao persistida pra esse teste — queremos ver o login real
delete process.env.NIBO_SESSION_B64;

console.log('Testando login com:');
console.log('  email:', process.env.NIBO_EMAIL);
console.log('  senha:', process.env.NIBO_PASSWORD ? '***' + process.env.NIBO_PASSWORD.slice(-2) : '(VAZIA!)');

const t0 = Date.now();
try {
  const session = await loginAndCaptureSession({
    email: process.env.NIBO_EMAIL,
    password: process.env.NIBO_PASSWORD,
  });
  const ms = Date.now() - t0;
  console.log(`\n✅ LOGIN OK em ${ms}ms — sem 2FA. Token JWT capturado (${session.bearerToken.length} chars).`);
  console.log('Esse usuario pode ser usado no scraper.');
  await closeSession(session);
} catch (err) {
  const ms = Date.now() - t0;
  console.error(`\n❌ LOGIN FALHOU em ${ms}ms:`);
  console.error(err.message);
  process.exit(1);
}
